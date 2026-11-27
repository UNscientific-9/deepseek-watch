#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docwatch.toolbox —— 给被唤醒的 agent 用的只读调查工具箱（CONTRACT §5）。

    python3 -m docwatch.toolbox status
    python3 -m docwatch.toolbox pages --changed
    python3 -m docwatch.toolbox brief 17
    python3 -m docwatch.toolbox signals 17 --json
    python3 -m docwatch.toolbox text https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
    python3 -m docwatch.toolbox strings ./assets/main.9ab31f02.js --grep 价格
    python3 -m docwatch.toolbox diff https://api-docs.deepseek.com/zh-cn/ --n 3
    python3 -m docwatch.toolbox asset https://api-docs.deepseek.com/zh-cn/assets/js/main.9ab31f02.js
    python3 -m docwatch.toolbox search deepseek-v4
    python3 -m docwatch.toolbox history --days 7
    python3 -m docwatch.toolbox fetch https://api-docs.deepseek.com/zh-cn/ --out page.html

约定：
- **只读**数据目录（`--data-dir`，默认 /opt/docwatch/data）。
- **写只允许写作业自己的 `out/`**：`fetch --out`、`text --out` 的文件一律落在
  `<job>/out/` 下，绝对路径也只允许 out/ 里面，防止 agent 手滑改到别处。
- 数据不全是常态（第一次部署、作业还没落盘、旧库没有快照表）：一律给**人话提示**，
  绝不 traceback。`--json` 时输出结构化结果，方便脚本消费。
- 仅标准库 + 项目内 `collect.asset`（都不引第三方依赖）。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python docwatch/toolbox.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docwatch.collect.asset import extract_rsc_text, html_to_text, js_strings

DEFAULT_DATA_DIR = os.environ.get("DOCWATCH_DATA_DIR", "/opt/docwatch/data")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
LEVEL_LABELS = {0: "噪音", 1: "轻微", 2: "值得看", 3: "重要"}
# 工具箱只允许访问官方站点，防止被页面里的提示注入引去访问内网/元数据地址。
TOOLBOX_ALLOWED_HOSTS = (
    "api-docs.deepseek.com",
    "www.deepseek.com",
    "deepseek.com",
    "fe-static.deepseek.com",
)
TOOLBOX_MAX_BYTES = 8 * 1024 * 1024
TOOLBOX_MAX_COMPRESSED = 4 * 1024 * 1024


class _ToolboxNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_TOOLBOX_OPENER = urllib.request.build_opener(_ToolboxNoRedirect)


def _validate_toolbox_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError("工具箱只允许 https URL")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in TOOLBOX_ALLOWED_HOSTS):
        raise ValueError(f"工具箱只允许官方站点，拒绝：{host or '(空)'}")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("工具箱不允许账号密码或非 443 端口")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("工具箱 URL 含控制字符")
    for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(str(sockaddr[0])).is_global:
            raise ValueError(f"工具箱拒绝解析到非公网地址：{sockaddr[0]}")



# --------------------------------------------------------------------------- #
# 输出小工具
# --------------------------------------------------------------------------- #

def dump_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def print_table(headers, rows, max_width=60) -> None:
    """人读表格：列宽按**显示宽度**算（中文占 2 格），过长截断成 …。"""
    if not rows:
        print("（没有数据）")
        return
    cells = [[_fit(str(r[i] if i < len(r) else ""), max_width) for i in range(len(headers))]
             for r in rows]
    widths = [_disp_width(headers[i]) for i in range(len(headers))]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], min(_disp_width(c), max_width))
    print("  ".join(_pad(headers[i], widths[i]) for i in range(len(headers))))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in cells:
        print("  ".join(_pad(row[i], widths[i]) for i in range(len(headers))))


def _disp_width(text: str) -> int:
    w = 0
    for ch in text or "":
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                or 0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF):
            w += 2
        else:
            w += 1
    return w


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _fit(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ")
    if _disp_width(text) <= width:
        return text
    out, w = [], 0
    for ch in text:
        cw = _disp_width(ch)
        if w + cw > max(1, width - 1):
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def human_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def short_hash(h: str, n: int = 10) -> str:
    h = str(h or "")
    return h[:n] + "…" if len(h) > n else h


# --------------------------------------------------------------------------- #
# 作业目录 / 数据目录
# --------------------------------------------------------------------------- #

class Store:
    """数据目录的统一入口：作业目录、BRIEF、信号、快照库（容错读出）。"""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).expanduser()
        self.jobs_dir = self.data_dir / "jobs"

    # ---- 作业 ----
    def job_dir(self, job_id) -> Path | None:
        if job_id in (None, "", "-"):
            latest = self.latest_job()
            return self.jobs_dir / str(latest) if latest else None
        p = Path(str(job_id)).expanduser()
        if p.is_dir() and (p / "manifest.json").exists():
            return p
        cand = self.jobs_dir / str(job_id)
        return cand if cand.is_dir() else None

    def job_ids(self) -> list:
        if not self.jobs_dir.is_dir():
            return []
        ids = [d.name for d in self.jobs_dir.iterdir()
               if d.is_dir() and (d / "manifest.json").exists()]
        return sorted(ids, key=_job_sort_key, reverse=True)

    def latest_job(self):
        ids = self.job_ids()
        return ids[0] if ids else None

    def manifest(self, job_id) -> dict:
        d = self.job_dir(job_id)
        if not d:
            return {}
        data = _read_json(d / "manifest.json", {})
        return data if isinstance(data, dict) else {}

    def verdict(self, job_id) -> dict:
        d = self.job_dir(job_id)
        if not d:
            return {}
        data = _read_json(d / "verdict.json", {})
        return data if isinstance(data, dict) else {}

    def brief(self, job_id) -> str:
        d = self.job_dir(job_id)
        if not d:
            return ""
        for name in ("BRIEF.md", "brief.md"):
            p = d / name
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        return ""

    # ---- 快照库（SQLite，容错）----
    def db_candidates(self) -> list:
        found = []
        for name in ("docs.db", "docwatch.db", "snapshots.db", "jobs.db"):
            p = self.data_dir / name
            if p.is_file():
                found.append(p)
        if self.data_dir.is_dir():
            for p in sorted(self.data_dir.glob("*.db")):
                if p not in found:
                    found.append(p)
        return found

    def db_tables(self) -> list:
        """返回 [(db_path, table, [columns])]，任何错误都跳过。"""
        out = []
        for db in self.db_candidates():
            for table, cols in _db_schema(db):
                out.append((db, table, cols))
        return out

    def page_rows(self) -> list:
        """监控页清单：优先 pages 表，其次历史作业里的信号。"""
        rows = []
        for db, table, cols in self.db_tables():
            url_col = _pick(cols, "url", "page", "link", "page_url")
            if not url_col:
                continue
            sel = [url_col]
            for name in ("title", "last_changed_at", "updated_at", "content_hash"):
                c = _pick(cols, name)
                if c:
                    sel.append(c)
            sql = f'SELECT {", ".join(_q(c) for c in sel)} FROM {_q(table)}'
            for rec in _db_query(db, sql):
                row = dict(zip(sel, rec))
                rows.append({
                    "url": row.get(url_col),
                    "title": row.get(_pick(cols, "title") or "", ""),
                    "last_changed_at": row.get(_pick(cols, "last_changed_at") or "", ""),
                    "updated_at": row.get(_pick(cols, "updated_at") or "", ""),
                })
            if rows:
                return rows
        # 退化：从作业信号里攒
        seen = {}
        for jid in self.job_ids():
            for sig in (self.manifest(jid).get("signals") or []):
                if isinstance(sig, dict) and sig.get("page"):
                    seen.setdefault(sig["page"], {"url": sig["page"], "title": "",
                                                  "last_changed_at": "", "updated_at": ""})
                    seen[sig["page"]]["last_changed_at"] = \
                        self.manifest(jid).get("created_at", "")
        return list(seen.values())

    def snapshot_versions(self, url: str, limit: int = 5, min_versions: int = 1) -> list:
        """某个 URL 的历史内容版本（新→旧）：[{time, content, hash}]。

        优先挑「历史表」（名字里带 snapshot/history/version，或是同一个 url 有多行的表）；
        `pages` 这种只存当前内容的表排在最后，只在别无选择时用。
        """
        cands = []
        for db, table, cols in self.db_tables():
            url_col = _pick(cols, "url", "page", "link", "page_url")
            body_col = _pick(cols, "body", "content", "text", "snapshot", "raw")
            if not (url_col and body_col):
                continue
            tn = table.lower()
            score = 0
            if any(k in tn for k in ("snapshot", "history", "version", "rev", "archive",
                                     "change")):
                score += 10
            if "page" in tn:
                score -= 5
            cands.append((score, db, table, cols, url_col, body_col))
        cands.sort(key=lambda c: -c[0])

        fallback = []
        for _score, db, table, cols, url_col, body_col in cands:
            time_col = _pick(cols, "updated_at", "ts", "fetched_at", "created_at", "time")
            hash_col = _pick(cols, "content_hash", "hash", "sha256")
            sel = [body_col] + ([time_col] if time_col else []) + ([hash_col] if hash_col else [])
            order = f" ORDER BY {_q(time_col)} DESC" if time_col else ""
            sql = (f'SELECT {", ".join(_q(c) for c in sel)} FROM {_q(table)} '
                   f'WHERE {_q(url_col)} = ?{order} LIMIT ?')
            rows = []
            for rec in _db_query(db, sql, (url, max(limit, min_versions))) or []:
                rows.append({"content": rec[0] or "",
                             "time": rec[1] if time_col else "",
                             "hash": rec[2] if hash_col else "",
                             "source": f"{db.name}:{table}"})
            rows = _dedupe_versions(rows)
            if len(rows) >= min_versions:
                return rows[:limit]
            if rows and not fallback:
                fallback = rows
        if fallback:
            return fallback[:limit]

        # 再退一步：作业里 pages/<slug>.before/.after
        out = []
        for jid in self.job_ids()[:40]:
            for sub, suffix in (("pages", ".after.txt"), ("pages", ".before.txt"),
                                ("assets", ".after.txt"), ("assets", ".before.txt")):
                d = self.jobs_dir / jid / sub
                if not d.is_dir():
                    continue
                for f in d.glob("*" + suffix):
                    p = _read_text(f)
                    if url and (url in p or _slugish(url).startswith(f.name.split(".")[0][:12])):
                        out.append({"content": p, "time": jid, "hash": "",
                                    "source": f"job {jid}/{sub}/{f.name}"})
        return _dedupe_versions(out)[:limit]

    def change_rows(self) -> list:
        rows = []
        for db, table, cols in self.db_tables():
            if not (_pick(cols, "level") and _pick(cols, "headline", "title", "summary")):
                continue
            sql = f'SELECT * FROM {_q(table)}'
            cur_cols = cols
            for rec in _db_query(db, sql):
                row = dict(zip(cur_cols, rec))
                rows.append({
                    "time": row.get(_pick(cur_cols, "ts", "created_at", "time") or "", ""),
                    "level": row.get("level"),
                    "headline": row.get(_pick(cur_cols, "headline", "title", "summary") or "", ""),
                    "url": row.get(_pick(cur_cols, "url", "page") or "", ""),
                    "source": f"{db.name}:{table}",
                })
            if rows:
                return rows
        for jid in self.job_ids():
            v = self.verdict(jid)
            m = self.manifest(jid)
            rows.append({
                "time": m.get("created_at", ""),
                "level": v.get("level"),
                "headline": v.get("headline") or _first_signal_detail(m),
                "url": _first_signal_url(m),
                "source": f"job {jid}",
            })
        return rows

    def status_info(self) -> dict:
        info = {"data_dir": str(self.data_dir), "exists": self.data_dir.is_dir()}
        jobs = self.job_ids()
        info["jobs"] = len(jobs)
        info["latest_job"] = jobs[0] if jobs else None
        latest_manifest = self.manifest(jobs[0]) if jobs else {}
        info["latest_at"] = latest_manifest.get("created_at", "")
        info["mode"] = latest_manifest.get("mode", "")
        info["first_at"] = ""
        if jobs:
            first = self.manifest(jobs[-1])
            info["first_at"] = first.get("created_at", "")
        # meta 表（如果旧库有）
        for db, table, cols in self.db_tables():
            if table == "meta":
                for k, v in _db_query(db, "SELECT key, value FROM meta") or []:
                    info.setdefault(f"meta_{k}", v)
        # 今日触发次数
        today = time.strftime("%Y-%m-%d")
        info["today_jobs"] = sum(
            1 for jid in jobs if str(self.manifest(jid).get("created_at", "")).startswith(today))
        levels = {}
        for jid in jobs[:200]:
            lv = self.verdict(jid).get("level")
            if lv is not None:
                levels[int(lv)] = levels.get(int(lv), 0) + 1
        info["levels"] = {str(k): v for k, v in sorted(levels.items())}
        info["dbs"] = [{"file": p.name, "bytes": p.stat().st_size}
                       for p in self.db_candidates()]
        info["pages_known"] = len(self.page_rows())
        return info


def _job_sort_key(job_id: str):
    m = re.match(r"^(\d+)$", str(job_id))
    if m:
        return (1, int(m.group(1)))
    return (0, str(job_id))


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _pick(cols, *names):
    """在列名里按优先级找（大小写/后缀不敏感）。"""
    low = {str(c).lower(): c for c in cols}
    for n in names:
        if n in low:
            return low[n]
    for n in names:
        for lc, c in low.items():
            if n in lc:
                return c
    return None


def _q(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _db_connect(path: Path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = None
        return conn
    except Exception:
        return None


def _db_schema(path: Path) -> list:
    conn = _db_connect(path)
    if conn is None:
        return []
    out = []
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info({_q(t)})')]
            except Exception:
                continue
            out.append((t, cols))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _db_query(path: Path, sql: str, params=()):
    conn = _db_connect(path)
    if conn is None:
        return None
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _first_signal_url(manifest: dict) -> str:
    for s in manifest.get("signals") or []:
        if isinstance(s, dict):
            if s.get("page") or s.get("asset"):
                return s.get("page") or s.get("asset")
    return ""


def _first_signal_detail(manifest: dict) -> str:
    for s in manifest.get("signals") or []:
        if isinstance(s, dict) and s.get("detail"):
            return s["detail"]
    return ""


def _slugish(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (url or "").lower()).strip("-")


# --------------------------------------------------------------------------- #
# 文本抽取：HTML / RSC / JSON / JS
# --------------------------------------------------------------------------- #
# html_to_text / extract_rsc_text / js_strings 统一从 collect.asset 引入（见文件顶部），
# 工具箱不再维护第二套 HTML/RSC/JS 解析。下面只留 JSON 查看用的小工具。


def _clean_lines(s: str) -> str:
    lines = []
    for line in s.splitlines():
        line = re.sub(r"[ \t\u00a0\u3000]+", " ", line).strip()
        if line and line not in lines[-3:]:
            lines.append(line)
    return "\n".join(lines)


def _walk_strings(node, out, depth=0):
    if depth > 12 or len(out) > 600:
        return
    if isinstance(node, str):
        if _looks_like_prose(node):
            out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_strings(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_strings(v, out, depth + 1)


def _looks_like_prose(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 8 or len(s) > 2000:
        return False
    if s.startswith(("$", "{", "[", "http://", "https://", "/", "#")):
        return False
    if re.search(r"[\u4e00-\u9fff]", s):
        return True
    if s.count(" ") >= 3 and re.search(r"[A-Za-z]{3,}", s):
        return True
    return False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_fetch(url: str, timeout: float = 20.0, method: str = "GET",
               headers: dict | None = None) -> dict:
    """抓 URL（浏览器 UA、gzip）。只允许官方站点，并限制响应大小。"""
    try:
        _validate_toolbox_url(url)
    except Exception as e:
        return {"ok": False, "status": 0, "body": b"", "headers": {}, "url": url,
                "bytes": 0, "error": f"{type(e).__name__}: {e}"}
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method=method)
    try:
        with _TOOLBOX_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(TOOLBOX_MAX_COMPRESSED + 1)
            if len(raw) > TOOLBOX_MAX_COMPRESSED:
                raise ValueError("响应超过 4MB 上限")
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
                body = obj.decompress(raw, TOOLBOX_MAX_BYTES + 1)
                if obj.unconsumed_tail or len(body) > TOOLBOX_MAX_BYTES:
                    raise ValueError("解压后正文超过 8MB 上限")
            elif enc == "deflate":
                try:
                    obj = zlib.decompressobj()
                    body = obj.decompress(raw, TOOLBOX_MAX_BYTES + 1)
                except zlib.error:
                    obj = zlib.decompressobj(-zlib.MAX_WBITS)
                    body = obj.decompress(raw, TOOLBOX_MAX_BYTES + 1)
                if obj.unconsumed_tail or len(body) > TOOLBOX_MAX_BYTES:
                    raise ValueError("解压后正文超过 8MB 上限")
            else:
                body = raw
            if len(body) > TOOLBOX_MAX_BYTES:
                raise ValueError("正文超过 8MB 上限")
            return {"ok": True, "status": resp.status, "body": body,
                    "headers": dict(resp.headers), "url": resp.geturl(),
                    "bytes": len(body)}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(TOOLBOX_MAX_COMPRESSED + 1)
        except Exception:
            pass
        return {"ok": False, "status": e.code, "body": body,
                "headers": dict(e.headers or {}), "url": url, "bytes": len(body),
                "error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        return {"ok": False, "status": 0, "body": b"", "headers": {}, "url": url,
                "bytes": 0, "error": f"{type(e).__name__}: {e}"}


def decode_bytes(body: bytes, headers: dict | None = None) -> str:
    ctype = ((headers or {}).get("Content-Type") or "").lower()
    for cand in re.findall(r"charset=([\w-]+)", ctype):
        try:
            return body.decode(cand, errors="replace")
        except Exception:
            pass
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 写权限：只允许作业自己的 out/
# --------------------------------------------------------------------------- #

def resolve_job(args) -> Path | None:
    """作业目录：--job → 环境变量 → 当前目录（含 manifest.json）。"""
    if getattr(args, "job", None):
        p = Path(str(args.job)).expanduser()
        if p.is_dir():
            return p
        return None
    env = os.environ.get("DOCWATCH_JOB_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    cwd = Path.cwd()
    if (cwd / "manifest.json").exists() or (cwd / "BRIEF.md").exists():
        return cwd
    return None


def resolve_out_path(args, preferred_name: str) -> Path:
    """把 --out 解析成一个允许写的路径：必须落在 <job>/out/ 下。"""
    job = resolve_job(args)
    if args.out in (None, ""):
        if not job:
            raise SystemExit("toolbox: 要保存文件但不在作业目录里（没有 --job，当前目录也不是作业目录），"
                             "请用 --out 指定 <作业目录>/out/ 下的文件名")
        return Path(job) / "out" / preferred_name
    p = Path(args.out).expanduser()
    if not job:
        raise SystemExit("toolbox: 没有作业目录上下文，不能写文件（写操作只允许写作业的 out/）")
    out_dir = (Path(job) / "out").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if p.is_absolute():
        target = p
    elif p.parent == Path("."):
        target = out_dir / p.name
    else:
        target = out_dir / p
    target = target.resolve()
    try:
        target.relative_to(out_dir)
    except ValueError:
        raise SystemExit(f"toolbox: 只允许写作业的 out/ 目录（{out_dir}），拒绝写 {target}")
    return target


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #

def cmd_status(args, store: Store) -> int:
    info = store.status_info()
    if args.json:
        dump_json(info)
        return 0
    if not info["exists"]:
        print(f"数据目录还不存在：{info['data_dir']}")
        print("提示：监控还没部署或还没跑过第一轮。可以先用 `--data-dir <临时目录>` 试命令。")
        return 0
    if not info["jobs"] and not info["dbs"]:
        print(f"数据目录是空的：{info['data_dir']}")
        print("提示：还没有作业（jobs/）也没有快照库（*.db）。等第一轮巡检或 --init 之后再看。")
        return 0
    print(f"数据目录：{info['data_dir']}")
    print(f"作业数：{info['jobs']}    已知页面：{info['pages_known']}")
    if info["jobs"]:
        print(f"最近一次作业：#{info['latest_job']}  {info['latest_at']}  档位 {info['mode'] or '?'}")
        print(f"今日触发：{info['today_jobs']} 次")
    if info.get("first_at"):
        print(f"最早一次作业：{info['first_at']}")
    if info["levels"]:
        parts = [f"{LEVEL_LABELS.get(int(k), k)}×{v}" for k, v in info["levels"].items()]
        print("历史级别分布：" + "  ".join(parts))
    if info["dbs"]:
        db_txt = "  ".join(f"{d['file']} {human_bytes(d['bytes'])}" for d in info["dbs"])
        print(f"快照库：{db_txt}")
    for k, v in info.items():
        if k.startswith("meta_"):
            v = str(v)
            shown = v if len(v) <= 100 else v[:100] + f"…（共 {len(v)} 字符）"
            print(f"{k[5:]}：{shown}")
    return 0


def cmd_pages(args, store: Store) -> int:
    rows = store.page_rows()
    if not rows:
        msg = "没有页面数据（快照库没有 pages 表，也没有历史作业信号）"
        if args.json:
            dump_json({"pages": [], "note": msg})
        else:
            print(msg)
            jobs = store.job_ids()
            print("提示：用 `toolbox signals <作业ID>` 看某次作业的信号，"
                  f"或用 `toolbox history` 看历次触发。已有作业：{jobs[:5] or '（无）'}")
        return 0
    if args.changed:
        changed = {}
        for jid in store.job_ids()[:200]:
            for sig in store.manifest(jid).get("signals") or []:
                if isinstance(sig, dict) and sig.get("page"):
                    changed.setdefault(sig["page"], jid)
        rows = [r for r in rows if r["url"] in changed]
        for r in rows:
            r["last_changed_at"] = r.get("last_changed_at") or changed.get(r["url"], "")
    if args.json:
        dump_json({"pages": rows, "total": len(rows)})
        return 0
    print(f"共 {len(rows)} 页" + ("（只显示有变化的）" if args.changed else ""))
    print_table(["页面", "最近变化", "标题"],
                [(r["url"], r.get("last_changed_at") or r.get("updated_at") or "-",
                  r.get("title") or "") for r in rows])
    return 0


def cmd_brief(args, store: Store) -> int:
    text = store.brief(args.job_id)
    if not text:
        jobs = store.job_ids()
        if args.json:
            dump_json({"job": args.job_id, "found": False, "jobs": jobs[:10]})
            return 0
        print(f"没有找到作业 {args.job_id or '(自动最新的)'} 的 BRIEF.md")
        print(f"可用作业：{jobs[:10] if jobs else '（还没有作业）'}")
        print("提示：作业目录里应该有 BRIEF.md / manifest.json / pages/ / diff/。")
        return 0
    if args.head:
        text = "\n".join(text.splitlines()[:args.head])
    if args.json:
        dump_json({"job": args.job_id, "found": True, "brief": text})
    else:
        print(text)
    return 0


def cmd_signals(args, store: Store) -> int:
    manifest = store.manifest(args.job_id)
    if not manifest:
        jobs = store.job_ids()
        if args.json:
            dump_json({"job": args.job_id, "signals": [], "jobs": jobs[:10]})
            return 0
        print(f"没有找到作业 {args.job_id or '(自动最新的)'} 的 manifest.json")
        print(f"可用作业：{jobs[:10] if jobs else '（还没有作业）'}")
        return 0
    signals = manifest.get("signals") or []
    if args.kind:
        signals = [s for s in signals if isinstance(s, dict) and s.get("kind") == args.kind]
    if args.json:
        dump_json({"job": manifest.get("id", args.job_id), "created_at": manifest.get("created_at"),
                   "mode": manifest.get("mode"), "pages_total": manifest.get("pages_total"),
                   "pages_changed": manifest.get("pages_changed"),
                   "signals": signals, "total": len(signals)})
        return 0
    print(f"作业 #{manifest.get('id', args.job_id)}  {manifest.get('created_at', '')}  "
          f"档位 {manifest.get('mode', '?')}  信号 {len(signals)} 条")
    if not signals:
        print("这次作业没有信号明细（manifest.signals 为空）")
        return 0
    print_table(["#", "kind", "对象", "明细", "before → after"],
                [(i, s.get("kind", ""), s.get("page") or s.get("asset") or "",
                  s.get("detail", ""),
                  (f"{_fit(str(s.get('before') or ''), 40)} → {_fit(str(s.get('after') or ''), 40)}"
                   if (s.get("before") or s.get("after")) else ""))
                 for i, s in enumerate(signals, 1) if isinstance(s, dict)],
                max_width=44)
    return 0


def cmd_history(args, store: Store) -> int:
    rows = store.change_rows()
    cutoff = time.time() - max(1, args.days) * 86400
    kept = []
    for r in rows:
        ts = _parse_ts(r.get("time"))
        if ts is None or ts >= cutoff:
            kept.append(r)
    if args.json:
        dump_json({"days": args.days, "rows": kept, "total": len(kept)})
        return 0
    if not kept:
        print(f"最近 {args.days} 天没有触发记录。")
        print("提示：`toolbox status` 看监控是否在跑；`toolbox pages` 看页面清单。")
        return 0
    print(f"最近 {args.days} 天共 {len(kept)} 条")
    print_table(["时间", "级别", "一句话", "对象"],
                [(r.get("time", ""), _level_txt(r.get("level")), r.get("headline", ""),
                  r.get("url", "")) for r in kept])
    return 0


def _level_txt(level) -> str:
    try:
        return f"{int(level)}·{LEVEL_LABELS.get(int(level), '?')}"
    except (TypeError, ValueError):
        return "-"


def _parse_ts(text):
    if not text:
        return None
    s = str(text).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s[:len(time.strftime(fmt))] if "%z" not in fmt else s, fmt))
        except Exception:
            continue
    try:
        return float(s)
    except Exception:
        return None


def cmd_fetch(args, store: Store) -> int:
    res = http_fetch(args.url, timeout=args.timeout)
    if not res["ok"] and not res["body"]:
        msg = res.get("error", "抓取失败")
        if args.json:
            dump_json({"url": args.url, "ok": False, "error": msg})
        else:
            print(f"抓取失败：{msg}")
            print("提示：确认 URL 正确、能联网；也可以 `toolbox text <本地文件>` 先看已有证据。")
        return 1
    text = decode_bytes(res["body"], res["headers"])
    sha = hashlib.sha256(res["body"]).hexdigest()
    if args.stdout:
        sys.stdout.write(text)
        return 0
    target = resolve_out_path(args, _safe_name(args.url) + ".html")
    target.write_bytes(res["body"])
    if args.json:
        dump_json({"url": res["url"], "status": res["status"], "bytes": res["bytes"],
                   "sha256": sha, "saved": str(target), "content_type":
                   res["headers"].get("Content-Type", "")})
        return 0
    print(f"HTTP {res['status']}  {human_bytes(res['bytes'])}  sha256 {short_hash(sha, 16)}")
    print(f"已保存：{target}")
    print(f"内容类型：{res['headers'].get('Content-Type', '?')}")
    preview = html_to_text(text) if "<html" in text[:2000].lower() else text
    preview = _fit(preview.replace("\n", " ⏎ "), args.preview)
    print(f"开头：{preview}")
    return 0


def _safe_name(url: str) -> str:
    p = urllib.parse.urlparse(url)
    base = (p.path.rstrip("/").split("/")[-1] or p.netloc or "page")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base)[:60]
    return f"{base or 'page'}"


def cmd_text(args, store: Store) -> int:
    src = args.source
    kind = "file"
    if re.match(r"^https?://", src):
        kind = "url"
        res = http_fetch(src, timeout=args.timeout)
        if not res["ok"] and not res["body"]:
            msg = res.get("error", "抓取失败")
            if args.json:
                dump_json({"source": src, "ok": False, "error": msg})
            else:
                print(f"抓取失败：{msg}")
                print("提示：检查 URL 或改用本地证据文件（pages/*.after.txt）。")
            return 1
        raw = decode_bytes(res["body"], res["headers"])
        status = res["status"]
    else:
        p = Path(src).expanduser()
        if not p.is_file():
            msg = f"文件不存在：{src}"
            if args.json:
                dump_json({"source": src, "ok": False, "error": msg})
            else:
                print(msg)
                print("提示：证据文件在作业目录的 pages/ assets/ diff/ 下，"
                      "先 `ls` 或 `toolbox brief <作业ID>` 看看有哪些。")
            return 1
        if p.stat().st_size > 20 * 1024 * 1024:
            print(f"文件太大（{human_bytes(p.stat().st_size)}），只读前 20MB")
        raw = _read_text(p)[:20 * 1024 * 1024]
        status = 0

    lowered = raw[:4000].lower()
    if "<html" in lowered or "<!doctype" in lowered or "<body" in lowered or "__next_f" in raw:
        text = html_to_text(raw)
        mode = "html/rsc"
    elif raw.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(raw)
            picked = []
            _walk_strings(data, picked)
            text = _clean_lines("\n".join(picked)) or raw[:args.max_chars]
            mode = "json"
        except Exception:
            text = _clean_lines(raw)
            mode = "text"
    else:
        text = _clean_lines(raw)
        mode = "text"

    if args.grep:
        text = _grep_lines(text, args.grep)
    if args.max_chars and len(text) > args.max_chars:
        text = text[:args.max_chars] + f"\n…（已截断，共 {len(text)} 字）"

    if args.json:
        payload = {"source": src, "kind": kind, "status": status, "mode": mode,
                   "chars": len(text), "text": text}
        if args.out:
            target = resolve_out_path(args, _safe_name(src) + ".txt")
            target.write_text(text, encoding="utf-8")
            payload["saved"] = str(target)
        dump_json(payload)
        return 0
    if args.out:
        target = resolve_out_path(args, _safe_name(src) + ".txt")
        target.write_text(text, encoding="utf-8")
        print(f"已保存：{target}（{len(text)} 字）")
    print(f"—— {src}（{kind}/{mode}，{len(text)} 字）——")
    print(text)
    return 0


def _compile_re(pattern: str):
    """用户输入的正则：编译失败就把整个串当字面量（不 traceback）。"""
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return re.compile(re.escape(pattern), re.I)


def _grep_lines(text: str, pattern: str, context: int = 0) -> str:
    rx = _compile_re(pattern)
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines):
        if rx.search(line):
            hits.extend(lines[max(0, i - context):i + context + 1])
    return "\n".join(_dedupe_keep(hits))


def _dedupe_keep(items):
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _dedupe_versions(rows: list) -> list:
    """同一份内容（hash 或正文相同）只留最新一条，避免「版本数」被重复行撑起来。"""
    seen, out = set(), []
    for r in rows or []:
        key = (r.get("hash") or "").strip() or (r.get("content") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def cmd_strings(args, store: Store) -> int:
    p = Path(args.file).expanduser()
    if not p.is_file():
        msg = f"文件不存在：{args.file}"
        if args.json:
            dump_json({"file": args.file, "ok": False, "error": msg})
        else:
            print(msg)
            print("提示：压缩 JS 在作业目录的 assets/ 下（例：assets/main.9ab31f02.js.after.txt）。")
        return 1
    js = _read_text(p)
    strings = js_strings(js, min_len=args.min)
    if args.grep:
        rx = _compile_re(args.grep)
        strings = [s for s in strings if rx.search(s)]
    total = len(strings)
    shown = strings[:args.limit]
    if args.json:
        dump_json({"file": str(p), "total_strings": total, "returned": len(shown),
                   "grep": args.grep, "strings": shown})
        return 0
    print(f"{p.name}：抽到 {total} 条可读字符串"
          + (f"（--grep {args.grep!r} 过滤后）" if args.grep else "")
          + f"，显示前 {len(shown)} 条")
    for s in shown:
        print("  " + _fit(s, args.width))
    if total == 0:
        print("提示：文件可能不是 JS，或字符串太短；试试 --min 4 或 --grep <关键词>。")
    return 0


def cmd_asset(args, store: Store) -> int:
    url = args.url
    info = {"url": url, "name": _safe_name(url), "found": False}
    m = re.search(r"([0-9a-f]{8,})", Path(urllib.parse.urlparse(url).path).name or "")
    info["hash_in_name"] = m.group(1) if m else ""

    # 1) 作业里有没有这个资源
    job_hits = []
    for jid in store.job_ids()[:60]:
        d = store.jobs_dir / jid / "assets"
        if d.is_dir():
            for f in d.iterdir():
                if f.name.startswith(info["name"]) or (m and m.group(1) in f.name):
                    job_hits.append({"job": jid, "file": str(f),
                                     "bytes": f.stat().st_size})
    info["job_files"] = job_hits[:10]
    # 2) 历史版本
    versions = store.snapshot_versions(url, limit=args.limit)
    info["versions"] = [{"time": v.get("time"), "bytes": len(v.get("content") or ""),
                         "hash": short_hash(v.get("hash", ""), 16),
                         "source": v.get("source")} for v in versions]
    # 3) 需要时真去抓一次
    if args.remote:
        res = http_fetch(url, timeout=args.timeout, method="HEAD")
        if not res["ok"] or not res.get("headers"):
            res = http_fetch(url, timeout=args.timeout)
        if res["body"]:
            info["remote"] = {"status": res["status"], "bytes": res["bytes"],
                              "sha256": hashlib.sha256(res["body"]).hexdigest(),
                              "content_type": res["headers"].get("Content-Type", "")}
        else:
            info["remote"] = {"status": res.get("status"), "error": res.get("error")}

    if args.json:
        dump_json(info)
        return 0
    print(f"资源：{url}")
    print(f"文件名：{info['name']}   文件名里的哈希：{info['hash_in_name'] or '（无）'}")
    remote = info.get("remote")
    if remote:
        if remote.get("bytes") is not None:
            print(f"线上：HTTP {remote.get('status')} {human_bytes(remote['bytes'])} "
                  f"sha256 {short_hash(remote.get('sha256', ''), 16)} "
                  f"{remote.get('content_type', '')}")
        else:
            print(f"线上抓取失败：{remote.get('error')}")
    if info["job_files"]:
        print("作业里的本地副本：")
        print_table(["作业", "文件", "大小"],
                    [(j["job"], Path(j["file"]).name, human_bytes(j["bytes"]))
                     for j in info["job_files"]])
    if info["versions"]:
        print("历史版本（新→旧）：")
        print_table(["时间", "大小", "哈希", "来源"],
                    [(v["time"] or "-", v["bytes"], v["hash"] or "-", v["source"])
                     for v in info["versions"]])
    if not info["job_files"] and not info["versions"] and not info.get("remote"):
        print("本地没有这个资源的记录（快照库里没有它的历史版本）。")
        print("提示：加 `--remote` 真去抓一次；或者用 `toolbox search <关键词>` 在历史快照里找。")
    return 0


def cmd_diff(args, store: Store) -> int:
    target = args.target
    n = max(0, args.n)

    # 1) 作业里现成的 diff 文件最省事
    job = store.job_dir(args.job)
    if job:
        ddir = job / "diff"
        if ddir.is_dir():
            files = list(ddir.glob("*.diff"))
            for f in files:
                if not target or target in f.name or _slugish(target) in f.name:
                    text = _read_text(f)
                    if args.json:
                        dump_json({"target": target, "source": str(f), "diff": text})
                    else:
                        print(f"—— {f.name} ——")
                        print(text)
                    return 0
            if files and not target:
                f = files[0]
                text = _read_text(f)
                if args.json:
                    dump_json({"target": str(f), "source": str(f), "diff": text})
                else:
                    print(f"—— {f.name}（作业里第一个 diff）——")
                    print(text)
                return 0

    # 2) 用历史版本现算
    versions = store.snapshot_versions(target, limit=max(2, args.n + 1), min_versions=2)
    if len(versions) >= 2:
        new, old = versions[0], versions[1]
        diff = "".join(difflib.unified_diff(
            (old["content"] or "").splitlines(True), (new["content"] or "").splitlines(True),
            fromfile=f"{target} @ {old.get('time') or '上一版'}",
            tofile=f"{target} @ {new.get('time') or '当前版'}", n=n))
        if args.json:
            dump_json({"target": target, "versions": len(versions), "diff": diff})
        else:
            print(diff or "（两版内容一致）")
        return 0

    if args.json:
        dump_json({"target": target, "found": False,
                   "note": "没有 diff 文件，也没有 ≥2 个历史版本"})
        return 0
    print(f"找不到 {target} 的差异：作业里没有对应 diff 文件，快照库里也没有 ≥2 个历史版本。")
    print("提示：`toolbox signals <作业ID>` 看本轮信号；"
          "`toolbox history --days 7` 看最近触发；`toolbox pages --changed` 看哪些页变过。")
    return 0


def cmd_search(args, store: Store) -> int:
    kw = args.keyword
    if not kw:
        print("请给一个关键词，例如：toolbox search deepseek-v4")
        return 1
    results = []
    rx = _compile_re(kw)

    # 1) 快照库
    for db, table, cols in store.db_tables():
        body_col = _pick(cols, "body", "content", "text", "snapshot", "raw")
        url_col = _pick(cols, "url", "page", "link")
        if not body_col:
            continue
        sel = [body_col] + ([url_col] if url_col else [])
        for rec in _db_query(db, f'SELECT {", ".join(_q(c) for c in sel)} FROM {_q(table)}') or []:
            body = rec[0] or ""
            if rx.search(str(body)):
                where = rec[1] if url_col else f"{db.name}:{table}"
                lines = _grep_lines(str(body), kw, context=0).splitlines()[:args.limit]
                results.append({"where": str(where), "source": f"{db.name}:{table}",
                                "hits": lines})

    # 2) 作业证据文件（BRIEF / diff / pages / assets）
    for jid in store.job_ids()[:80]:
        jd = store.jobs_dir / jid
        candidates = [jd / "BRIEF.md"]
        for sub in ("diff", "pages", "assets"):
            d = jd / sub
            if d.is_dir():
                candidates.extend(sorted(d.iterdir())[:40])
        for f in candidates:
            if not f.is_file() or f.stat().st_size > 4 * 1024 * 1024:
                continue
            text = _read_text(f)
            if rx.search(text):
                hits = _grep_lines(text, kw, context=0).splitlines()[:args.limit]
                try:
                    rel = f.relative_to(jd).as_posix()
                except ValueError:
                    rel = f.name
                results.append({"where": f"job {jid}/{rel}", "source": "jobs",
                                "hits": hits})

    if args.json:
        dump_json({"keyword": kw, "results": results[:args.limit_files],
                   "total_sources": len(results)})
        return 0
    if not results:
        print(f"没有在历史快照里搜到 {kw!r}。")
        print("提示：关键词换短一点（如 deepseek-v4 而不是整句）；"
              "或 `toolbox strings <JS文件> --grep <关键词>` 在压缩 JS 里找。")
        return 0
    print(f"在 {len(results)} 处搜到 {kw!r}：")
    for r in results[:args.limit_files]:
        print(f"\n【{r['where']}】")
        for line in r["hits"]:
            print("  " + _fit(line, 200))
    if len(results) > args.limit_files:
        print(f"\n…另有 {len(results) - args.limit_files} 处未显示（--limit-files 调大）")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=None,
                        help=f"数据目录（默认 {DEFAULT_DATA_DIR}，可用环境变量 DOCWATCH_DATA_DIR）")
    common.add_argument("--json", action="store_true", help="机器可读输出")
    common.add_argument("--job", default=None, help="作业目录或作业 id（默认从环境/当前目录推断）")

    ap = argparse.ArgumentParser(
        prog="python3 -m docwatch.toolbox",
        description="docwatch 只读调查工具箱（给被唤醒的 agent 用）")
    ap.add_argument("--data-dir", default=None, help="数据目录（也支持写在子命令后面）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", parents=[common], help="监控天数、上次检查、档位、今日触发次数")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pages", parents=[common], help="监控页面清单与最近变化时间")
    p.add_argument("--changed", action="store_true", help="只看有变化记录的页面")
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("brief", parents=[common], help="打印作业的 BRIEF.md")
    p.add_argument("job_id", nargs="?", default=None, help="作业 id（默认最近一次）")
    p.add_argument("--head", type=int, default=None, help="只看前 N 行")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("signals", parents=[common], help="作业的信号表格")
    p.add_argument("job_id", nargs="?", default=None, help="作业 id（默认最近一次）")
    p.add_argument("--kind", default=None, help="只看某种信号，如 page_text / asset_url")
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("fetch", parents=[common], help="抓 URL 存到作业 out/")
    p.add_argument("url")
    p.add_argument("--out", default=None, help="文件名，必须落在 <job>/out/ 下")
    p.add_argument("--stdout", action="store_true", help="直接打印到终端，不落盘")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--preview", type=int, default=200, help="打印开头多少字")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("text", parents=[common], help="HTML/JSON → 纯文本（含 Next.js RSC）")
    p.add_argument("source", help="URL 或本地文件")
    p.add_argument("--grep", default=None, help="只保留匹配的行")
    p.add_argument("--max-chars", type=int, default=20000, help="最多输出多少字（默认 2 万）")
    p.add_argument("--out", default=None, help="把结果存到 <job>/out/")
    p.add_argument("--timeout", type=float, default=20.0)
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("strings", parents=[common], help="从压缩 JS 抽可读字符串")
    p.add_argument("file")
    p.add_argument("--grep", default=None, help="只留匹配的字符串")
    p.add_argument("--min", type=int, default=6, help="最短长度（默认 6）")
    p.add_argument("--limit", type=int, default=60, help="最多显示多少条（默认 60）")
    p.add_argument("--width", type=int, default=160)
    p.set_defaults(func=cmd_strings)

    p = sub.add_parser("asset", parents=[common], help="资源元信息：大小/哈希/历史版本")
    p.add_argument("url")
    p.add_argument("--remote", action="store_true", help="真去线上抓一次算大小和 sha256")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_asset)

    p = sub.add_parser("diff", parents=[common], help="与上一版/上上版的差异")
    p.add_argument("target", nargs="?", default=None, help="页面 URL / 资源 URL / 文件名片段")
    p.add_argument("--n", type=int, default=2, help="上下文行数（默认 2）")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("search", parents=[common], help="在所有历史快照里搜关键词")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=5, help="每个来源最多显示几行")
    p.add_argument("--limit-files", type=int, default=20, help="最多显示多少个来源")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("history", parents=[common], help="历史触发与判定记录")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_history)

    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    data_dir = args.data_dir or (os.environ.get("DOCWATCH_DATA_DIR") or DEFAULT_DATA_DIR)
    store = Store(data_dir)
    try:
        return args.func(args, store) or 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # 绝不 traceback 给 agent 看
        if os.environ.get("DOCWATCH_TOOLBOX_DEBUG"):
            raise
        print(f"toolbox: {type(e).__name__}: {e}")
        print("提示：这是工具自身的问题，不是数据的问题。换个命令（比如 `status`）或加 --json 再试。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
