#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docwatch —— 官方站点「正文变化」监控的旧版正文提取器

本文件保留作为 docwatch v2 的兼容抽取与历史快照工具；v2 的主流程见 `docwatch/run.py`。

抓取范围：
  - https://api-docs.deepseek.com/  全站（官方 sitemap 里的页面 × 中英两版）
  - https://www.deepseek.com/       首页等营销页

判级（详见 classify()）：
  3 级 = 价格 / 计费 / 限流 / 弃用 / 新模型 / 错误码 / 更新日志新增  → 立刻发信
  2 级 = 正文段落有实质改动（参数、示例、能力说明）              → 合并成一封发
  1 级 = 只剩措辞级微调                                          → 只入库，日报里提
  0 级 = 时间戳 / 导航 / 页脚 / 版权                              → 归一化阶段就丢掉

用法：
  python3 watch_docs.py --init            首次建立基线（不发任何通知）
  python3 watch_docs.py                   正常巡检（systemd timer 每小时触发）
  python3 watch_docs.py --dry --limit 8   只跑前 8 页，不写库、不发信，打印结果
  python3 watch_docs.py --only <url>      只跑指定页面（调试用）
  python3 watch_docs.py --report          打印当前状态概览

只依赖 Python 标准库，不需要 pip 安装任何东西。
"""

from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import html as html_mod
import http.client
import json
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------- 配置

UA = "dsh-docwatch/0.1 (+personal monitoring)"
TIMEOUT = 20                    # 单次请求超时（秒）
RETRIES = 2                     # 失败重试次数
TYPE_DELAY = 0.0                # 每页之间额外间隔（并发抓取后不需要了）
MAX_DIFF_LINES = 24             # 一封邮件里单页最多列几条差异
QUIET_START_H = 23              # 夜间静默开始（北京时间）
QUIET_END_H = 7                 # 静默结束：到点把攒下的变化合并成一条发
MAX_QUEUE_MAIL = 40             # 一封邮件最多列多少条变化
KEEP_CHANGES_DAYS = 90          # 变更记录保留天数
FAIL_ALERT_AFTER = 3            # 同一页连续失败几次开始报
FAIL_ALERT_COOLDOWN_H = 24
PAGE_LIST_TTL_H = 24            # 页面清单多久重新拉一次 sitemap

DOCS_ROOT = "https://api-docs.deepseek.com"
DOCS_SITEMAP = f"{DOCS_ROOT}/sitemap.xml"
SITE_ROOT = "https://www.deepseek.com"

DOCS_PREFIX = "/zh-cn"          # 中文前缀；带这个的页面才算「中文正文」
ZH_ONLY = True                  # 只监控中文版（英文页不抓，省一半流量和时间）

BEIJING = timezone(timedelta(hours=8))

# 3 级判定的关键词：命中即「会影响开发/账单/可用性」
HIGH_WORDS = (
    "价格", "计费", "收费", "免费", "调价", "涨价", "降价",
    "元/百万", "元 / 百万", "每百万", "元每百万",
    "限流", "rpm", "tpm", "并发", "qps",
    "弃用", "废弃", "deprecated", "不再支持", "停止服务", "即将下线", "迁移",
    "新模型", "模型版本", "版本号", "发布", "上线", "推出",
    "错误码", "429", "500", "上下文长度", "最大输出", "上限",
)

# 归一化阶段直接丢掉的行（页脚 / 时间戳 / 导航噪音）
NOISE_PATTERNS = (
    re.compile(r"^最后更新(于)?\s*[:：]?\s*\d"),
    re.compile(r"^last updated", re.I),
    re.compile(r"^(©|\(c\)|copyright)", re.I),
    re.compile(r"^(版权所有|版权)", re.I),
    re.compile(r"^(上一页|下一页|编辑此页|edit this page|previous|next)\s*$", re.I),
    re.compile(r"^(本页目录|on this page|目录)\s*$", re.I),
    re.compile(r"^(搜索|search|登录|log ?in|sign ?up|注册)\s*$", re.I),
    # 文档站自身的 UI 按钮 / 提示条，稳定但无意义
    re.compile(r"^复制 ?(markdown|代码|链接)?"),
    re.compile(r"^打开 ?(markdown|代码)"),
    re.compile(r"^(跳到主要内容|skip to main content|回到顶部)$", re.I),
    re.compile(r"^(本页总览|table of contents)$", re.I),
    re.compile(r"\|\s*deepseek api docs\s*$", re.I),
)

# 营销站的页面清单（sitemap 没有 / 抓不稳的，手工列）
SITE_PAGES = (
    [(f"{SITE_ROOT}/zh/", "深度求索官网首页（中文）")]
    if ZH_ONLY
    else [(f"{SITE_ROOT}/", "深度求索官网首页"),
          (f"{SITE_ROOT}/zh/", "深度求索官网首页（中文）")]
)

# 重点盯的四个板块：除了 sitemap，还会顺着这几页里的链接补齐「子页」，
# 防止 sitemap 更新滞后导致漏页。
SEED_PAGES = (
    "/zh-cn/guides/vision",
    "/zh-cn/api/create-chat-completion",
    "/zh-cn/news/news261017",
    "/zh-cn/updates",
)

MAX_LINK_PAGES = 400   # 链接补齐时每页最多收多少个站内链接
WORKERS = 6            # 并发抓取数（同一站点别开太大，礼貌优先）


# ---------------------------------------------------------------- 小工具

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime | None:
    try:
        return datetime.strptime(text or "", "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def bj(dt: datetime) -> str:
    return dt.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")


def log(msg: str) -> None:
    print(f"[{bj(now_utc())}] {msg}", flush=True)


def load_env(path: Path) -> None:
    """把 env 文件里的 KEY=VALUE 灌进 os.environ（已存在的不覆盖）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------- 抓取

_CONNS = threading.local()


def _conn_for(host: str) -> http.client.HTTPSConnection:
    """每线程复用同一主机的 TLS 连接 —— 116 页只握几次手，省时也省流量。"""
    conns: dict[str, http.client.HTTPSConnection] = getattr(_CONNS, "pool", None)
    if conns is None:
        conns = {}
        _CONNS.pool = conns
    conn = conns.get(host)
    if conn is None:
        conn = http.client.HTTPSConnection(
            host, timeout=TIMEOUT, context=ssl.create_default_context()
        )
        conns[host] = conn
    return conn


def _drop_conn(host: str) -> None:
    conns: dict[str, http.client.HTTPSConnection] = getattr(_CONNS, "pool", {}) or {}
    conn = conns.pop(host, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def http_get(url: str, etag: str = "", last_modified: str = "",
             _hops: int = 0) -> dict:
    """条件请求。返回 {status, html, etag, last_modified}；304 时 html 为空。

    http.client 不像 urllib 那样自动跟随重定向，这里自己跟（最多 3 跳）——
    文档站的地址常常会 302 到带斜杠的版本。
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    conn = _conn_for(host)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        resp_etag = resp.getheader("ETag") or ""
        resp_lm = resp.getheader("Last-Modified") or ""
        encoding = (resp.getheader("Content-Encoding") or "").lower()
    except Exception:  # noqa: BLE001
        # 复用的连接可能已被服务端关掉：丢掉它，交给上层重试时重新握手
        _drop_conn(host)
        raise

    if status == 304:
        return {"status": 304, "html": "", "etag": etag, "last_modified": last_modified}
    if status in (301, 302, 303, 307, 308):
        location = resp.getheader("Location") or ""
        if location and _hops < 3:
            target = urllib.parse.urljoin(url, location)
            return http_get(target, etag, last_modified, _hops + 1)
        raise RuntimeError(f"HTTP {status} 重定向无法跟随（Location={location[:80]}）")
    if status != 200:
        raise RuntimeError(f"HTTP {status} {raw.decode('utf-8', 'replace')[:200]}".strip())
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    return {
        "status": 200,
        "html": raw.decode("utf-8", "replace"),
        "etag": resp_etag,
        "last_modified": resp_lm,
    }


def fetch_with_retry(url: str, etag: str, last_modified: str) -> dict:
    last_err: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            return http_get(url, etag, last_modified)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < RETRIES:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{type(last_err).__name__}: {last_err}")


# ---------------------------------------------------------------- 正文抽取

_BLOCK_RE = re.compile(
    r"(?is)</?(p|div|br|li|tr|h[1-6]|section|article|pre|blockquote|td|th|dt|dd|table|ul|ol)[^>]*>"
)
_DROP_RE = re.compile(
    r"(?is)<(script|style|nav|header|footer|aside|svg|noscript|iframe|form)[^>]*>.*?</\1>"
)
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_ARTICLE_RE = re.compile(r"(?is)<article[^>]*>(.*?)</article>")
_MAIN_RE = re.compile(r"(?is)<main[^>]*>(.*?)</main>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_H1_RE = re.compile(r"(?is)<h1[^>]*>(.*?)</h1>")


def _plain_text(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment)
    text = html_mod.unescape(text)
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    return re.sub(r"\s+", " ", text).strip()


def _lines_from(fragment: str) -> list[str]:
    """把一段 HTML 变成「干净的正文段落列表」。"""
    text = _COMMENT_RE.sub(" ", fragment)
    text = _DROP_RE.sub(" ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")

    lines: list[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
        if len(line) < 2:
            continue
        if any(p.search(line) for p in NOISE_PATTERNS):
            continue
        if not re.search(r"[\w\u4e00-\u9fff]", line):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines


def extract_page(html: str) -> tuple[str, list[str]]:
    """返回 (标题, 正文段落列表)。

    页面上可能有好几个 <article>（卡片、相关阅读），所以把 <article> / <main> / 整页
    都试一遍，取正文最长的那个 —— 比「抓第一个 article」稳得多。
    """
    title = ""
    m = _TITLE_RE.search(html)
    if m:
        title = _plain_text(m.group(1))
    if not title:
        m = _H1_RE.search(html)
        title = _plain_text(m.group(1)) if m else ""
    title = re.sub(r"\s*[|｜]\s*DeepSeek.*$", "", title).strip()

    best: list[str] = []
    best_len = 0
    for frag in [m.group(1) for m in _ARTICLE_RE.finditer(html)] + \
                [m.group(1) for m in _MAIN_RE.finditer(html)] + [html]:
        lines = _lines_from(frag)
        size = sum(len(x) for x in lines)
        if size > best_len:
            best, best_len = lines, size
    return title, best


def content_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 比对

def diff_lines(old: list[str], new: list[str]) -> list[dict]:
    """段落级差异，返回 [{kind, before, after}]。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        before, after = old[i1:i2], new[j1:j2]
        if tag == "replace":
            out.append({"kind": "change", "before": before, "after": after})
        elif tag == "delete":
            out.append({"kind": "del", "before": before, "after": []})
        else:
            out.append({"kind": "add", "before": [], "after": after})
    return out


def classify(url: str, diffs: list[dict], is_new_page: bool = False) -> tuple[int, str]:
    """把差异判成 1/2/3 级，返回 (级别, 人话原因)。"""
    if is_new_page:
        return 3, "站点新增了一个页面"

    after_text = " ".join(
        line for d in diffs for line in (d["after"] or [])
    )
    before_text = " ".join(
        line for d in diffs for line in (d["before"] or [])
    )
    joined = f"{after_text} {before_text}"
    lowered = joined.lower()

    hits = [w for w in HIGH_WORDS if w in lowered]
    if "/updates" in url and any(d["kind"] in ("add", "change") for d in diffs):
        return 3, "更新日志有新条目"
    if hits:
        shown = "、".join(dict.fromkeys(hits))[:40]
        return 3, f"改到了关键内容（{shown}）"

    added_chars = sum(len(x) for d in diffs for x in (d["after"] or []))
    removed_chars = sum(len(x) for d in diffs for x in (d["before"] or []))
    if added_chars + removed_chars >= 30:
        return 2, "正文有实质改动"
    return 1, "只有细微措辞变化"


# ---------------------------------------------------------------- 模型解读
#
# 优先级：
#   1) 服务器本机的 llm 网关（127.0.0.1:3425）里的路由组 example-route
#      —— 组内顺序 example-provider/hy3（免费）→ relay/deepseek/example-model-d（opencode 备用），
#         谁先能答就谁答，失败自动换下一个，这层由 llm 自己完成，本脚本不重复实现。
#   2) 本机跑（没有 llm）时退回 opencode 网关，用独立的 LLM_API_KEY。
# **绝不使用** DeepSeek 官方 key。

LLM_DEFAULT_BASE = "https://llm.example.com/v1"
LLM_DEFAULT_MODEL = "example-model-d"
LLM_BASE = "http://127.0.0.1:3425/v1"
LLM_MODEL = "example-route"
LLM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")   # 网关前面有 Cloudflare，挡陌生 UA
LLM_TIMEOUT = 40
LLM_TIMEOUT = 20
LLM_MAX_TOKENS = 16384      # hy3 这类模型长思考很常见（实测能推理上千 token），上限给足才拿得到正文
LLM_MAX_DIFF_CHARS = 1500   # 判读只需要看改动性质，喂太长反而让它推理到没边
LLM_PROMPT = (
    "你在帮一个用 DeepSeek API 的开发者盯官方文档。下面是某页面本次的变化"
    "（- 开头是旧内容，+ 开头是新内容）。\n"
    "页面：{url}\n\n{diff}\n\n"
    "只输出 JSON，不要任何多余文字：\n"
    '{{"summary": "30字内说清改了什么", "impact": "30字内说清对使用者影响，没有就写无"}}\n'
    "只描述事实和影响，不要判断级别（级别由规则决定）。"
)


def _llm_key() -> str:
    """opencode 网关的独立 key；不读 DEEPSEEK_API_KEY。"""
    for name in ("LLM_API_KEY", "OPENCODE_API_KEY", "OPENCODE_ZEN_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _llm_targets() -> list[dict]:
    """按优先级给出可用的解读入口。"""
    targets: list[dict] = []
    explicit = os.getenv("LLM_BASE_URL", "").strip()
    if explicit:
        targets.append({
            "via": "自定义", "base": explicit,
            "model": os.getenv("LLM_MODEL", "").strip() or LLM_DEFAULT_MODEL,
            "key": _llm_key(), "session": False, "timeout": LLM_TIMEOUT,
        })
    targets.append({
        "via": "llm", "base": LLM_BASE,
        "model": os.getenv("DOCWATCH_LLM_MODEL", "").strip() or LLM_MODEL,
        "key": "", "session": False, "timeout": LLM_TIMEOUT,
    })
    key = _llm_key()
    if key:
        targets.append({
            "via": "opencode", "base": LLM_DEFAULT_BASE, "model": LLM_DEFAULT_MODEL,
            "key": key, "session": True, "timeout": LLM_TIMEOUT,
        })
    return targets


def llm_interpret(item: dict) -> dict | None:
    """让模型写「改了什么 / 有什么影响」；不改级别。所有入口都失败时返回 None（规则结果照旧生效）。"""
    diff_text = "\n".join(render_diff_lines(item["diffs"]))[:LLM_MAX_DIFF_CHARS]
    prompt = LLM_PROMPT.format(url=item["url"], diff=diff_text)
    errors: list[str] = []

    for target in _llm_targets():
        if not target["key"] and target["via"] == "opencode":
            continue
        for attempt in range(2):
            payload = {
                "model": target["model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": LLM_MAX_TOKENS if attempt == 0 else LLM_MAX_TOKENS * 2,
                "temperature": 0,
            }
            if target["via"] == "llm":
                payload["reasoning_effort"] = "low"   # 判读不需要深度推理，压一压延迟和消耗
            headers = {
                "Content-Type": "application/json",
                "User-Agent": LLM_UA,
                "Accept": "application/json",
            }
            if target["key"]:
                headers["Authorization"] = f"Bearer {target['key']}"
                headers["x-opencode-session"] = str(uuid.uuid4())
            req = urllib.request.Request(
                target["base"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=target["timeout"]) as resp:
                    served = resp.headers.get("X-LLM-Model") or ""
                    body = json.loads(resp.read().decode("utf-8", "replace"))
                content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                if not content.strip():
                    log(f"[llm] {target['via']} 空回复（可能 token 被推理占满），加预算重试")
                    continue  # 推理吃光 token，加预算再来
                parsed = _extract_json(content)
                if not parsed:
                    log(f"[llm] {target['via']} 返回的不是 JSON：{content[:80]!r}")
                    continue
                summary = str(parsed.get("summary") or "").strip()
                impact = str(parsed.get("impact") or "").strip()
                if not summary and not impact:
                    log(f"[llm] {target['via']} JSON 里没有 summary/impact，跳过")
                    continue
                cost = ((body.get("usage") or {}).get("opencode") or {}).get("list_cost")
                label = served or body.get("model") or target["via"]
                return {
                    "summary": summary[:120],
                    "impact": impact[:120],
                    "cost_usd": cost,
                    "via": f"{target['via']} · {label}",
                }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{target['via']}: {type(exc).__name__}: {str(exc)[:80]}")
                break   # 这个入口不行就换下一个
    if errors:
        log("[llm] 解读没成功，按规则继续：" + " | ".join(errors[:3]))
    return None


def _extract_json(text: str) -> dict | None:
    """模型偶尔会包一层 ```json，这里抠出第一个 JSON 对象。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def apply_llm(items: list[dict]) -> list[dict]:
    """规则先定级；模型只写「改了什么 / 有什么影响」，**绝不改 level、绝不拦信**。

    v1 旧入口（watch_docs.py）已降级为证据层，但仍可能被手工运行；
    这里必须与 v2 的 CONTRACT §3.3 一致：级别与发送权只属于规则。
    """
    out: list[dict] = []
    for it in items:
        if it["level"] < 2:
            out.append(it)
            continue
        ai = llm_interpret(it)
        if not ai:
            out.append(it)
            continue
        out.append({**it, "ai": ai})
    return out


# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url            TEXT PRIMARY KEY,
    title          TEXT,
    etag           TEXT,
    last_modified  TEXT,
    content_hash   TEXT,
    body           TEXT,
    tier           TEXT,
    first_seen     TEXT,
    updated_at     TEXT,
    last_changed_at TEXT,
    fail_streak    INTEGER DEFAULT 0,
    last_error     TEXT
);
CREATE TABLE IF NOT EXISTS changes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    url     TEXT,
    ts      TEXT,
    level   INTEGER,
    reason  TEXT,
    summary TEXT,
    title   TEXT,
    diff_text TEXT,
    ai_summary TEXT,
    ai_impact  TEXT,
    ai_via     TEXT,
    notified   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 老库升级：补齐后来加的列，缺哪列补哪列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(changes)")}
    for name, ddl in (
        ("title", "ALTER TABLE changes ADD COLUMN title TEXT"),
        ("diff_text", "ALTER TABLE changes ADD COLUMN diff_text TEXT"),
        ("ai_summary", "ALTER TABLE changes ADD COLUMN ai_summary TEXT"),
        ("ai_impact", "ALTER TABLE changes ADD COLUMN ai_impact TEXT"),
        ("ai_via", "ALTER TABLE changes ADD COLUMN ai_via TEXT"),
        ("notified", "ALTER TABLE changes ADD COLUMN notified INTEGER DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(ddl)
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- 页面清单

def parse_sitemap(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def canon(url: str) -> str:
    """统一 URL：去锚点 / 查询串；文档站统一补末尾斜杠。

    文档站的规范地址是带斜杠的（不带斜杠会 302 跳一次），补上以后
    抓取少一跳、也更容易命中 304。
    """
    url = url.split("#")[0].split("?")[0].strip()
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.hostname == "api-docs.deepseek.com" and not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def discover_pages(conn: sqlite3.Connection, force: bool = False) -> list[dict]:
    """返回 [{url, tier}]。清单缓存 PAGE_LIST_TTL_H 小时。"""
    cached = meta_get(conn, "page_list")
    cached_at = meta_get(conn, "page_list_at")
    if cached and cached_at and not force:
        try:
            age_h = (now_utc() - datetime.strptime(cached_at, "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if age_h < PAGE_LIST_TTL_H:
                return json.loads(cached)
        except Exception:  # noqa: BLE001
            pass

    pages: list[dict] = []

    # 1) 官方文档站：sitemap 给的是英文路径，中文镜像同结构
    try:
        sm = fetch_with_retry(DOCS_SITEMAP, "", "")
        for url in parse_sitemap(sm["html"]):
            if not url.startswith(DOCS_ROOT):
                continue
            path = url[len(DOCS_ROOT):].split("#")[0]
            if not ZH_ONLY:
                pages.append({"url": url, "tier": "en"})
            pages.append({"url": f"{DOCS_ROOT}{DOCS_PREFIX}{path}", "tier": "zh"})
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] sitemap 抓取失败，沿用缓存清单：{exc}")

    # 2) 指定页面的站内子页：抓这几页里的链接，防止 sitemap 漏页
    known = {canon(p["url"]) for p in pages}
    for seed in SEED_PAGES:
        url = canon(f"{DOCS_ROOT}{seed}")
        for link in _links_in(url):
            if link in known:
                continue
            known.add(link)
            pages.append({"url": link, "tier": "link"})

    # 3) 营销站固定页
    for url, _label in SITE_PAGES:
        pages.append({"url": url, "tier": "site"})

    # 去重（保留中文优先顺序）
    seen: set[str] = set()
    uniq: list[dict] = []
    for item in pages:
        url = canon(item["url"])
        if url in seen:
            continue
        seen.add(url)
        uniq.append({"url": url, "tier": item["tier"]})

    meta_set(conn, "page_list", json.dumps(uniq, ensure_ascii=False))
    meta_set(conn, "page_list_at", iso(now_utc()))
    return uniq


_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css",
             ".js", ".zip", ".md", ".json", ".xml", ".txt", ".pdf")


def _links_in(page_url: str) -> list[str]:
    """抓一页里的站内链接（用于补齐 sitemap 可能漏掉的子页）。失败返回空。"""
    try:
        resp = fetch_with_retry(page_url, "", "")
    except Exception:  # noqa: BLE001
        return []
    if resp["status"] != 200 or not resp["html"]:
        return []
    out: list[str] = []
    for href in re.findall(r'href="([^"]+)"', resp["html"]):
        if href.startswith(("mailto:", "javascript:", "#")):
            continue
        link = urllib.parse.urljoin(page_url, href).split("#")[0]
        if not link.startswith(DOCS_ROOT):
            continue
        link_path = urllib.parse.urlsplit(link).path
        if ZH_ONLY and not link_path.startswith(DOCS_PREFIX):
            continue          # 只要中文版，英文页的链接一律不收
        if not link_path or link_path.lower().endswith(_SKIP_EXT):
            continue
        out.append(canon(link))
    return list(dict.fromkeys(out))[:MAX_LINK_PAGES]


# ---------------------------------------------------------------- 推送

def notify_email(subject: str, body: str) -> bool:
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not user or not password:
        log("[notify] 没有 SMTP_USER / SMTP_PASS，跳过发信")
        return False
    host = os.getenv("SMTP_HOST", "").strip() or "smtp.qq.com"
    to = os.getenv("MAIL_TO", "").strip() or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(host, 465, timeout=TIMEOUT,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        log(f"[notify] 已发送：{subject}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[notify] 发送失败：{type(exc).__name__}: {exc}")
        return False


def render_diff_lines(diffs: list[dict]) -> list[str]:
    """把差异渲染成 +/- 文本行，超长截断。"""
    out: list[str] = []
    for d in diffs:
        for line in d["before"]:
            out.append(f"  - {line[:160]}")
        for line in d["after"]:
            out.append(f"  + {line[:160]}")
        if len(out) >= MAX_DIFF_LINES:
            out.append("  …（更多差异已省略）")
            return out
    return out


def drop_sitewide_chrome(items: list[dict], threshold: int = 5) -> list[dict]:
    """同一句话在多页同时改 → 那是站点级横幅/菜单/模板，不是正文，丢掉。

    否则官方换个公告条，114 页会同时「变化」，一封信里全是同一个噪音。
    """
    counts: dict[str, int] = {}
    for it in items:
        seen: set[str] = set()
        for d in it["diffs"]:
            for line in list(d["before"]) + list(d["after"]):
                if line not in seen:
                    seen.add(line)
                    counts[line] = counts.get(line, 0) + 1
    chrome = {line for line, n in counts.items() if n >= threshold}
    if not chrome:
        return items

    kept: list[dict] = []
    for it in items:
        new_diffs: list[dict] = []
        for d in it["diffs"]:
            before = [x for x in d["before"] if x not in chrome]
            after = [x for x in d["after"] if x not in chrome]
            if before or after:
                new_diffs.append({"kind": d["kind"], "before": before, "after": after})
        if not new_diffs:
            continue
        level, reason = classify(it["url"], new_diffs)
        kept.append({**it, "diffs": new_diffs, "level": level, "reason": reason})
    dropped = len(items) - len(kept)
    if dropped:
        log(f"[chrome] 过滤站点级模板改动 {len(chrome)} 行，丢弃 {dropped} 页的噪音")
    return kept


def is_quiet(now: datetime) -> bool:
    """夜间静默：23:00–07:00（北京时间）攒着不发，早上 7 点合并成一条。"""
    hour = now.astimezone(BEIJING).hour
    return hour >= QUIET_START_H or hour < QUIET_END_H


def build_email_rows(rows: list, total_pages: int, failed: int,
                     now: datetime) -> tuple[str, str]:
    """把「待发的变更记录」渲染成一封邮件。rows 来自 changes 表。"""
    rows = list(rows)
    top = max((int(r["level"]) for r in rows), default=0)
    subject = (
        f"DeepSeek 文档有重要更新（{len(rows)} 页）" if top >= 3
        else f"DeepSeek 文档更新（{len(rows)} 页）"
    )

    held = [r for r in rows
            if (parse_iso(r["ts"]) or now).astimezone(BEIJING).date()
            < now.astimezone(BEIJING).date()]
    lines = [f"发送时间：{bj(now)}（北京时间）",
             f"本次检查 {total_pages} 页，共 {len(rows)} 条变化"
             + (f"（其中 {len(held)} 条是夜间攒下的）" if held else "")
             + (f"，{failed} 页抓取失败" if failed else "") + "。",
             ""]

    # 版式：每页先列「变化内容」（原文逐字差异），下面再跟「AI 解读」
    for r in rows[:MAX_QUEUE_MAIL]:
        title = r["title"] or r["url"]
        lines.append("=" * 46)
        lines.append(f"【{r['level']} 级】{title}")
        lines.append(f"  页面：{r['url']}")
        if r["ts"]:
            lines.append(f"  发现：{bj(parse_iso(r['ts']) or now)}")
        lines.append(f"  判定：{r['reason']}")
        lines.append("")
        lines.append("  ── 变化内容 ──")
        diff_lines = [x for x in (r["diff_text"] or "").split("\n") if x.strip()]
        lines += diff_lines[:MAX_DIFF_LINES] or ["  （无逐行差异）"]
        if len(diff_lines) > MAX_DIFF_LINES:
            lines.append("  …（更多差异已省略）")
        lines.append("")
        ai_summary = r["ai_summary"] or ""
        ai_impact = r["ai_impact"] or ""
        ai_via = (r["ai_via"] if "ai_via" in r.keys() else "") or ""
        if ai_summary or ai_impact:
            lines.append(f"  ── AI 解读{'（' + ai_via + '）' if ai_via else ''} ──")
            if ai_summary:
                lines.append(f"  改了什么：{ai_summary}")
            if ai_impact and ai_impact not in ("无", "没有"):
                lines.append(f"  有什么影响：{ai_impact}")
        else:
            lines.append("  ── AI 解读 ──")
            lines.append("  （模型这次没给出解读，以上面原文差异为准）")
        lines.append("")
    if len(rows) > MAX_QUEUE_MAIL:
        lines.append(f"……另有 {len(rows) - MAX_QUEUE_MAIL} 条变化，见本地状态库。")
        lines.append("")

    lines.append("—— docwatch：正文级比对，已过滤时间戳/导航/页脚/站点横幅")
    return subject, "\n".join(lines)


def record_changes(conn: sqlite3.Connection, changed: list[dict], started: datetime) -> None:
    """把本轮变化写进队列。2 级以上等发信，1 级只留档（notified=1）。"""
    for it in changed:
        ai = it.get("ai") or {}
        conn.execute(
            "INSERT INTO changes(url, ts, level, reason, summary, title, diff_text, "
            "ai_summary, ai_impact, ai_via, notified) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (it["url"], iso(started), it["level"], it["reason"], it.get("title", ""),
             it.get("title", ""), "\n".join(render_diff_lines(it["diffs"])),
             ai.get("summary", ""), ai.get("impact", ""), ai.get("via", ""),
             0 if it["level"] >= 2 else 1),
        )


# ---------------------------------------------------------------- 主流程

def load_credentials(args: argparse.Namespace) -> Path | None:
    """按顺序找 env 文件并灌进环境变量，返回命中的路径。"""
    base = Path(args.data_dir).expanduser()
    here = Path(__file__).resolve().parent
    candidates = [Path(args.env_file).expanduser()] if args.env_file else []
    candidates += [base / "env", here / "env", here.parent / ".env", Path.cwd() / ".env"]
    for cand in candidates:
        if cand.exists():
            load_env(cand)
            log(f"凭据来自 {cand}")
            return cand
    return None


def run(args: argparse.Namespace) -> int:
    base = Path(args.data_dir).expanduser()
    load_credentials(args)
    conn = open_db(base / "docs.db")
    started = now_utc()

    pages = discover_pages(conn, force=args.refresh_pages)
    if args.only:
        want = canon(args.only)
        pages = [p for p in pages if p["url"] == want]
    if args.limit:
        pages = pages[: args.limit]
    if not pages:
        log("[warn] 页面清单为空，退出")
        return 1
    log(f"开始巡检：{len(pages)} 页，模式={'dry' if args.dry else ('init' if args.init else 'run')}")

    changed: list[dict] = []
    failed = 0
    unchanged = 0
    not_modified = 0

    # ---- 先并发抓取（只发 HTTP，不碰数据库），再在主线程按顺序比对、写库
    workers = max(1, int(getattr(args, "workers", WORKERS) or WORKERS))
    prefetch: dict[str, dict] = {}
    fetch_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for page in pages:
            rowp = conn.execute(
                "SELECT etag, last_modified FROM pages WHERE url = ?", (page["url"],)
            ).fetchone()
            futures[pool.submit(
                fetch_with_retry, page["url"],
                (rowp["etag"] if rowp else "") or "",
                (rowp["last_modified"] if rowp else "") or "",
            )] = page["url"]
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                prefetch[url] = {"ok": True, "resp": fut.result()}
            except Exception as exc:  # noqa: BLE001
                prefetch[url] = {"ok": False, "err": exc}
    log(f"抓取完成：{len(pages)} 页 / {workers} 并发 / {time.monotonic() - fetch_started:.1f}s")

    for idx, page in enumerate(pages, 1):
        url = page["url"]
        row = conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        etag = row["etag"] if row else ""
        lm = row["last_modified"] if row else ""
        old_body = (row["body"] or "").split("\n") if row and row["body"] else []

        pre = prefetch.get(url) or {"ok": False, "err": RuntimeError("未抓取")}
        exc = None if pre["ok"] else pre["err"]
        if exc is not None:
            msg = str(exc)
            if "HTTP 404" in msg or "HTTP 410" in msg:
                # 页面不存在（多半是英文页没有中文镜像）——不算故障，别让它污染失败计数
                log(f"  [{idx}/{len(pages)}] 跳过（页面不存在） {url}")
                if not args.dry:
                    conn.execute(
                        "INSERT INTO pages(url, tier, fail_streak, last_error, updated_at) "
                        "VALUES(?, 'missing', 0, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                        "tier = 'missing', fail_streak = 0, last_error = excluded.last_error, "
                        "updated_at = excluded.updated_at",
                        (url, msg[:200], iso(started)),
                    )
                time.sleep(TYPE_DELAY)
                continue
            failed += 1
            streak = (row["fail_streak"] if row else 0) + 1
            log(f"  [{idx}/{len(pages)}] 失败 {url} :: {exc}")
            if not args.dry:
                conn.execute(
                    "INSERT INTO pages(url, fail_streak, last_error, updated_at) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                    "fail_streak = ?, last_error = ?, updated_at = ?",
                    (url, streak, str(exc)[:200], iso(started),
                     streak, str(exc)[:200], iso(started)),
                )
            time.sleep(TYPE_DELAY)
            continue

        resp = pre["resp"]
        if resp["status"] == 304:
            not_modified += 1
            if not args.dry:
                conn.execute(
                    "UPDATE pages SET updated_at = ?, fail_streak = 0, last_error = '' WHERE url = ?",
                    (iso(started), url),
                )
            time.sleep(TYPE_DELAY)
            continue

        title, new_body = extract_page(resp["html"])
        if not new_body:
            log(f"  [{idx}/{len(pages)}] 正文为空，跳过 {url}")
            time.sleep(TYPE_DELAY)
            continue

        new_hash = content_hash(new_body)
        old_hash = row["content_hash"] if row else ""
        is_new_page = row is None

        if old_hash and new_hash == old_hash:
            unchanged += 1
            if not args.dry:
                conn.execute(
                    "UPDATE pages SET etag = ?, last_modified = ?, title = ?, updated_at = ?, "
                    "fail_streak = 0, last_error = '' WHERE url = ?",
                    (resp["etag"], resp["last_modified"], title, iso(started), url),
                )
            time.sleep(TYPE_DELAY)
            continue

        diffs = diff_lines(old_body, new_body) if old_hash else []
        if not is_new_page and not diffs:
            unchanged += 1
            time.sleep(TYPE_DELAY)
            continue

        level, reason = classify(url, diffs, is_new_page=is_new_page)
        if args.init:
            log(f"  [{idx}/{len(pages)}] 建立基线 {url}")
        else:
            log(f"  [{idx}/{len(pages)}] {level} 级变化 {url} :: {reason}")
            changed.append({
                "url": url, "title": title, "level": level,
                "reason": reason, "diffs": diffs,
            })

        if not args.dry:
            conn.execute(
                "INSERT INTO pages(url, title, etag, last_modified, content_hash, body, tier, "
                "first_seen, updated_at, last_changed_at, fail_streak, last_error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '') "
                "ON CONFLICT(url) DO UPDATE SET title = excluded.title, etag = excluded.etag, "
                "last_modified = excluded.last_modified, content_hash = excluded.content_hash, "
                "body = excluded.body, updated_at = excluded.updated_at, "
                "last_changed_at = CASE WHEN excluded.content_hash != pages.content_hash "
                "THEN excluded.updated_at ELSE pages.last_changed_at END, "
                "fail_streak = 0, last_error = ''",
                (url, title, resp["etag"], resp["last_modified"], new_hash,
                 "\n".join(new_body), page["tier"], iso(started), iso(started), iso(started)),
            )
        time.sleep(TYPE_DELAY)

    # ---- 汇总
    if changed:
        changed = drop_sitewide_chrome(changed)
        if not getattr(args, "no_llm", False):
            changed = apply_llm(changed)      # llm 路由组优先（hy3 免费 → opencode 备用），不碰 DeepSeek 官方 key
            spent = sum(float((it.get("ai") or {}).get("cost_usd") or 0) for it in changed)
            if spent:
                prev = float(meta_get(conn, "llm_cost_usd") or 0)
                meta_set(conn, "llm_cost_usd", f"{prev + spent:.6f}")
                log(f"[llm] 本次解读花费 ${spent:.6f}（累计 ${prev + spent:.6f}）")
        if not args.dry:
            record_changes(conn, changed, started)
    total = conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"]
    log(f"完成：检查 {len(pages)} 页，未变 {unchanged}，304 命中 {not_modified}，"
        f"变化 {len(changed)}，失败 {failed}；库内共 {total} 页")

    if args.dry:
        for it in changed:
            print(f"\n[{it['level']} 级] {it['title']}  {it['url']}\n  原因：{it['reason']}")
            for line in render_diff_lines(it["diffs"])[:12]:
                print(line)
        return 0

    # ---- 失败告警（同一页连续失败，且 24h 内没报过）
    last_fail_alert = meta_get(conn, "fail_alert_at")
    recent = False
    if last_fail_alert:
        try:
            recent = (started - datetime.strptime(last_fail_alert, "%Y-%m-%dT%H:%M:%SZ")
                      .replace(tzinfo=timezone.utc)) < timedelta(hours=FAIL_ALERT_COOLDOWN_H)
        except Exception:  # noqa: BLE001
            recent = False
    if failed and not recent:
        dying = conn.execute(
            "SELECT url, fail_streak, last_error FROM pages WHERE fail_streak >= ? "
            "ORDER BY fail_streak DESC LIMIT 10", (FAIL_ALERT_AFTER,)
        ).fetchall()
        if dying:
            body = "\n".join(f"- {r['url']}（连续 {r['fail_streak']} 次）：{r['last_error']}"
                             for r in dying)
            if notify_email("docwatch 抓取连续失败", body + "\n\n—— 页面可能改版或站点异常"):
                meta_set(conn, "fail_alert_at", iso(started))

    # ---- 变化通知：夜间静默时段先攒着，早上 7 点合并成一条发
    if args.init:
        log("[init] 已建立基线，不发送通知")
    elif args.dry:
        pass
    else:
        queued = conn.execute(
            "SELECT * FROM changes WHERE notified = 0 AND level >= 2 ORDER BY id"
        ).fetchall()
        if queued and is_quiet(started):
            log(f"[quiet] 夜间静默（{QUIET_START_H}:00–{QUIET_END_H}:00），"
                f"{len(queued)} 条变化攒着，早上 {QUIET_END_H}:00 合并发一条")
        elif queued:
            subject, body = build_email_rows(queued, len(pages), failed, started)
            if getattr(args, "preview", False):
                print("\n===== 邮件预览（不会真的发出）=====")
                print(f"主题：{subject}\n")
                print(body)
            elif notify_email(subject, body):
                conn.executemany(
                    "UPDATE changes SET notified = 1 WHERE id = ?",
                    [(r["id"],) for r in queued],
                )

    meta_set(conn, "last_run", iso(started))
    meta_set(conn, "last_run_pages", str(len(pages)))
    conn.execute("DELETE FROM changes WHERE ts < ?",
                 (iso(started - timedelta(days=KEEP_CHANGES_DAYS)),))
    conn.commit()
    conn.close()
    return 0


def report(args: argparse.Namespace) -> int:
    base = Path(args.data_dir).expanduser()
    conn = open_db(base / "docs.db")
    total = conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"]
    last_run = meta_get(conn, "last_run") or "（从未运行）"
    llm_cost = meta_get(conn, "llm_cost_usd") or "0"
    print(f"库内页面：{total}")
    print(f"最后巡检：{last_run}")
    print(f"模型解读累计花费：${float(llm_cost):.6f}")
    rows = conn.execute(
        "SELECT ts, level, reason, url FROM changes ORDER BY id DESC LIMIT 15"
    ).fetchall()
    print(f"\n最近 {len(rows)} 条变更记录：")
    for r in rows:
        print(f"  [{r['level']} 级] {r['ts']} {r['reason']} :: {r['url']}")
    fail = conn.execute(
        "SELECT url, fail_streak, last_error FROM pages WHERE fail_streak > 0 "
        "ORDER BY fail_streak DESC LIMIT 10"
    ).fetchall()
    if fail:
        print("\n抓取异常页面：")
        for r in fail:
            print(f"  x{r['fail_streak']} {r['url']} :: {r['last_error']}")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DeepSeek 官方站点正文变化监控")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"))
    ap.add_argument("--env-file", default="")
    ap.add_argument("--init", action="store_true", help="建立基线，不发通知")
    ap.add_argument("--dry", action="store_true", help="不写库、不发信，只打印")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 页（调试）")
    ap.add_argument("--only", default="", help="只跑指定 URL")
    ap.add_argument("--refresh-pages", action="store_true", help="强制重新拉 sitemap")
    ap.add_argument("--workers", type=int, default=WORKERS, help="并发抓取数（默认 6）")
    ap.add_argument("--test-notify", action="store_true", help="只测推送通道（会真发一封）")
    ap.add_argument("--no-llm", action="store_true",
                    help="不调用模型解读（默认会用 opencode 网关的独立 key 解读；加这个就纯规则）")
    ap.add_argument("--preview", action="store_true", help="把该发的邮件打印出来，不真发")
    ap.add_argument("--report", action="store_true", help="打印状态概览")
    args = ap.parse_args()
    if args.test_notify:
        load_credentials(args)
        ok = notify_email(
            "docwatch 测试",
            f"这是一条测试消息，说明 docwatch 的推送通道可用。\n\n时间：{bj(now_utc())}（北京时间）",
        )
        print("[test-notify] " + ("成功，去邮箱看看" if ok else "失败，看上面的报错"))
        return 0 if ok else 1
    if args.report:
        return report(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
