"""docwatch 告警邮件排版（HTML + 纯文本，仅标准库）。

设计：级别色标 + 一句话结论 + 影响前置，一屏看懂「重不重要 / 变了什么 / 怎么办」；
每页一张卡（卡头带原文链接），证据收进灰框（diff ≤8 行，删浅红增浅绿，全角 －＋），
建议单独一行，页脚「依据 / 判读 / 判读模型 / 调档提示」。HTML 全 table + inline style、640px 单列、
无外链无图片；纯文本版结构对齐，供微信「QQ邮箱提醒」窄预览。缺字段、空 signals、
纯噪音都能渲染，不抛异常。
"""

# ---------------------------------------------------------------- 用法
#  render(verdict, job) -> {"subject", "text", "html"}
#    verdict：CONTRACT §3.3 的判读结论（agent 产出）。
#    job    ：CONTRACT §3.2 的作业包（含 signals / pages_total / created_at / mode）。
#
#  多页多卡：Verdict 是「一次唤醒一个结论」，要一页一张卡时在 verdict 里加可选字段
#    "cards": [{"level":3,"page":"https://…","tag":"模型 & 价格","headline":"…",
#               "what_changed":"…","bullets":["…"],"diff":["－旧","＋新"],
#               "action":"…"}, …]
#  也可用 "findings" / "groups" 作同义键。没给 cards 时，自动按 job["signals"].page
#  分组，一页一卡；字段缺什么补什么，不要求 agent 严格遵守。

from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from pathlib import Path

from docwatch import rules

# ---------------------------------------------------------------- 常量

LEVELS = {
    3: ("重要", "#c0392b"),
    2: ("值得看", "#c77b1a"),
    1: ("轻微", "#2b6cb0"),
    0: ("噪音", "#8a8f98"),
}

SUBJECT_PREFIX = "[DeepSeek 更新]"
SUBJECT_MAX = 60          # 汉字上限：微信通知栏只显示这么多
MAX_DIFF_LINES = 8        # 原文差异最多 8 行，其余折叠成「另有 N 行未显示」
MAX_BULLETS = 5
MAX_CARDS = 8             # 非噪音卡片上限，其余合并成「其余 N 页」摘要
PAGE_WIDTH = 640
BEIJING = dt.timezone(dt.timedelta(hours=8))

BULLET_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"
TEXT_RULE = "─" * 24

# 正文字体：不引外链字体，只用系统字体栈（QQ 邮箱 / 微信内建浏览器都能落）
FONT = "-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Courier New',monospace"

COLOR_TEXT = "#1f2328"
COLOR_STRONG = "#111827"
COLOR_MUTED = "#6b7280"
COLOR_FAINT = "#8a8f98"
COLOR_DEL_BG = "#fdecea"
COLOR_DEL_FG = "#8a2b1f"
COLOR_ADD_BG = "#e8f6ee"
COLOR_ADD_FG = "#186b38"

DEFAULT_HEADLINE = {
    3: "检测到重要变化，请尽快确认",
    2: "文档内容有更新，建议看一眼",
    1: "本次检查没有需要关注的变化",
    0: "本次检查完成，没有变化",
}
DEFAULT_EVIDENCE = "页面 HTML 与前端资源自动比对"
DEFAULT_NOTE = "重新构建，正文无变化"
EMPTY_NOTE = "本次检查未发现任何变化。"

# 页脚最后一行：业务用户能自己动手的「调档」方式（不写命令、不写环境变量）
SOOTHING_HINT = "觉得太吵？跟 DSH 说一句「调安静点」就行。"

# 模型技术 id → 业务用户看得懂的名字；没收录的型号原样显示
MODEL_LABELS = {
    "example-model-a": "Example Model",
    "example-model-b": "Example Model B",
    "example-model-c": "Example Model C",
    "example-model-d": "Example Model D",
    "example-model-e": "Example Model E",
    "example-model-f": "Example Model F",
    "example-route": "Example Route",
    "example-route-v2": "Example Route V2",
}

# 只看路径判断页面属于哪一块，避免把域名里的 api-docs 当「接口文档」
SECTION_HINTS = (
    ("pricing", "模型 & 价格"),
    ("assets", "前端资源"),
    ("quick_start", "快速上手"),
    ("quick-start", "快速上手"),
    ("rate_limit", "限流与配额"),
    ("rate-limit", "限流与配额"),
    ("news", "新闻动态"),
    ("faq", "常见问题"),
    ("guide", "使用指南"),
    ("tutorial", "使用指南"),
    ("reasoner", "推理模型"),
    ("context", "上下文与缓存"),
    ("about", "关于 DeepSeek"),
    ("term", "条款与协议"),
    ("sitemap", "站点地图"),
    ("api", "接口文档"),
    ("/zh/", "站点首页"),
)

# 资源类信号只说「前端重建了」，不占正文要点（正文要点留给真实内容变化）
ASSET_KINDS = {"asset_url", "asset_content"}

# 表情符号范围（保留 → ↗ · － ＋ 这些排版符号）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]"
)

_DIFF_MARKS = {"－": "－", "-": "－", "−": "－", "―": "－", "＋": "＋", "+": "＋"}


# ---------------------------------------------------------------- 基础工具


def _s(value, limit: int = 4000) -> str:
    """把任意 JSON 值揉成干净字符串（不抛异常）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "是" if value else ""
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, (list, tuple)):
        text = "、".join(_s(item, 200) for item in value)
    elif isinstance(value, dict):
        text = "、".join(_s(item, 200) for item in value.values())
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit and len(text) > limit:
        text = text[:limit]
    return text


def _clip(text, limit: int) -> str:
    """按字符数截断，留一个省略号。"""
    text = _s(text, max(limit * 4, 400))
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _one_line(text) -> str:
    return re.sub(r"\s+", " ", _s(text)).strip()


def _lines(value) -> list:
    """把 str / list 统一拆成非空文本行（diff 用）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_lines(item))
        return out
    return [line.strip() for line in _s(value).split("\n") if line.strip()]


def _esc(value) -> str:
    return html.escape(_s(value), quote=True)


def _strip_emoji(text) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", _s(text))).strip()


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _s(value).strip().lower() in ("1", "true", "yes", "y", "on", "是", "噪音", "noise")


def _norm_level(value, default: int = 1) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number in LEVELS else default


def _as_int(value) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(number, 0)


def _sentences(text) -> list:
    """切成短句，给「要点」用；切不出就整段。"""
    text = _s(text)
    if not text:
        return []
    parts = re.split(r"[。！？!?\n]+", text)
    return [part.strip(" ：:；;,、\t") for part in parts if part.strip(" ：:；;,、\t")]


def _split_first(text) -> tuple:
    """挑出第一句加粗（≤90 字才算「首句」），剩下原样返回。"""
    text = _s(text)
    match = re.search(r"[。！？!?]", text)
    if match and match.end() <= 90:
        return text[: match.end()], text[match.end():].strip()
    return "", text


def _safe_url(value) -> str:
    text = _s(value).strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


def _page_parts(url: str) -> tuple:
    """页面 URL → (中文区块名, slug)。例：…/quick_start/pricing/ → (模型 & 价格, pricing)"""
    if not url:
        return "检查结果", ""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        path = ""
    low = path.lower()
    segments = [seg for seg in path.split("/") if seg]
    slug = segments[-1] if segments else ""
    slug = re.sub(r"\.(html?|php|aspx?|jsp)$", "", slug, flags=re.I)
    slug = re.sub(r"\.([0-9a-f]{6,})\.(js|css)$", r".\2", slug, flags=re.I)
    label = ""
    for key, name in SECTION_HINTS:
        if key in low:
            label = name
            break
    if not label:
        label = "文档页面" if slug else "站点首页"
    return label, slug


def _parse_iso(value):
    if isinstance(value, dt.datetime):
        return value
    text = _s(value)
    if not text:
        return None
    text = text.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _fmt_time(value) -> str:
    moment = _parse_iso(value)
    if not moment:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.astimezone(BEIJING).strftime("%m-%d %H:%M")


def _fmt_elapsed(value) -> str:
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return ""
    if millis <= 0:
        return ""
    seconds = millis / 1000.0
    if seconds < 1:
        return f"{int(round(millis))} 毫秒"
    if seconds < 60:
        return f"{int(round(seconds))} 秒"
    minutes = int(seconds // 60)
    rest = int(round(seconds - minutes * 60))
    return f"{minutes} 分 {rest} 秒" if rest else f"{minutes} 分"


def _judge_text(verdict: dict, pages_total: int) -> str:
    """页脚「判读」行：只说业务用户看得懂的事（模型名单独一行）。"""
    model = _s(verdict.get("model")).strip()
    fallback = _truthy(verdict.get("degraded")) or not model or model == "规则兜底"
    head = "规则兜底" if fallback else "自动解读"
    elapsed = _fmt_elapsed(verdict.get("elapsed_ms"))
    if elapsed:
        head = f"{head} {elapsed}"
    parts = [head]
    if pages_total:
        parts.append(f"本次检查 {pages_total} 页")
    return " · ".join(parts)


def _model_text(verdict: dict) -> str:
    """页脚「判读模型」行：显示实际模型；降级时明确写「规则兜底」。"""
    if not isinstance(verdict, dict):
        return "规则兜底"
    model = _s(verdict.get("model")).strip()
    if _truthy(verdict.get("degraded")):
        return "规则兜底（模型未给出解读）"
    if not model or model == "规则兜底":
        return "规则兜底"
    if model.startswith(("group/", "example-route")):
        return MODEL_LABELS.get(model, model)
    if " / " in model:
        return " / ".join(MODEL_LABELS.get(part, part) for part in model.split(" / "))
    label = MODEL_LABELS.get(model)
    return f"{label}（{model}）" if label else model


# ---------------------------------------------------------------- 差异


def _dedupe_diff(pairs) -> list:
    seen = set()
    out = []
    for mark, text in pairs:
        text = _clip(text, 200)
        if not text:
            continue
        key = (mark, text)
        if key in seen:
            continue
        seen.add(key)
        out.append((mark, text))
    return out


def _diff_from_signals(signals) -> list:
    """信号里的 before/after → 差异行。

    新旧行数相同就一行对一行交替（－旧 ＋新 挨着，价格改动最好读）；
    行数不同就先把删除列完再列新增。完全相同的行不入 diff。
    """
    pairs = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        before = _lines(signal.get("before"))
        after = _lines(signal.get("after"))
        if before and after and len(before) == len(after):
            for old, new in zip(before, after):
                if old == new:
                    continue
                pairs.append(("－", old))
                pairs.append(("＋", new))
        else:
            pairs.extend(("－", line) for line in before)
            pairs.extend(("＋", line) for line in after)
    return _dedupe_diff(pairs)


def _normalize_diff(raw) -> list:
    """外部传入的 diff 也接住：list[str] / list[(mark, text)] / 「－x ＋y」混排。"""
    if not raw:
        return []
    if isinstance(raw, str):
        items = [line for line in raw.replace("\r", "").split("\n") if line.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    pairs = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            mark, text = item
            pairs.append((_DIFF_MARKS.get(_s(mark).strip()[:1], "－"), _s(text)))
        elif isinstance(item, dict):
            pairs.append(("－", _s(item.get("before") or item.get("old"))))
            pairs.append(("＋", _s(item.get("after") or item.get("new"))))
        else:
            text = _s(item).lstrip()
            if not text:
                continue
            first = text[0]
            if first in _DIFF_MARKS:
                pairs.append((_DIFF_MARKS[first], text[1:].strip()))
            elif first == " ":
                pairs.append(("＋", text.strip()))
            else:
                pairs.append(("＋", text))
    return _dedupe_diff(pairs)


# ---------------------------------------------------------------- 卡片


def _bullets_from(src: dict) -> list:
    raw = None
    for key in ("bullets", "changes", "points", "items", "details"):
        value = src.get(key)
        if value:
            raw = value
            break
    out = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                item = item.get("text") or item.get("detail") or item.get("title") or item.get("value")
            text = _clip(_one_line(item), 160)
            if text:
                out.append(text)
    elif raw:
        out = [_clip(text, 160) for text in _sentences(raw)]
    return out


def _make_card(src, default_level: int, noise: bool) -> dict:
    src = src if isinstance(src, dict) else {}
    level = default_level if src.get("level") is None else _norm_level(src.get("level"), default_level)
    card_noise = noise or _truthy(src.get("noise")) or level == 0
    if card_noise:
        level = 0

    url = _safe_url(_s(src.get("page") or src.get("url") or src.get("link")))
    label = _page_parts(url)[0]
    custom_label = _one_line(src.get("tag") or src.get("section") or src.get("area"))
    if custom_label:
        label = _clip(custom_label, 20)

    signals = src.get("signals")
    signals = [sig for sig in signals if isinstance(sig, dict)] if isinstance(signals, list) else [src]
    signal_kinds = {_s(sig.get("kind")).strip().lower() for sig in signals}
    is_asset = bool(src.get("is_asset")) or (
        bool(signal_kinds) and signal_kinds <= ASSET_KINDS)
    is_error = bool(src.get("is_error")) or signal_kinds == {"fetch_error"}
    if is_asset and not custom_label:
        label = "前端资源"
    # label/URL 兜底也在这里定死，后面所有地方只读 card["is_asset"]。
    is_asset = bool(is_asset or label == "前端资源" or "/assets/" in url)

    bullets = _bullets_from(src)
    if not bullets:
        page_details, asset_details = [], []
        for signal in signals:
            detail = _clip(_one_line(signal.get("detail")), 160)
            if not detail:
                continue
            kind = _s(signal.get("kind")).strip().lower()
            (asset_details if kind in ASSET_KINDS else page_details).append(detail)
        bullets = page_details or asset_details
        if not bullets:
            bullets = [_clip(text, 160) for text in _sentences(src.get("what_changed"))]

    diff = _normalize_diff(src.get("diff"))
    if not diff:
        diff = _diff_from_signals(signals)

    return {
        "level": level,
        "noise": card_noise,
        "is_asset": is_asset,
        "is_error": is_error,
        "url": url,
        "label": label,
        "bullets": [text for text in bullets if text][:MAX_BULLETS],
        "diff": diff,
        "action": _clip(_one_line(src.get("action") or src.get("advice")), 300),
        "headline": _clip(_strip_emoji(_one_line(src.get("headline"))), 120),
        # 夜间合并 / 多卡时，模型对整次变化的「改了什么 / 影响」要跟着卡片走，不能丢在 verdict 顶层。
        "what_changed": _clip(_one_line(src.get("what_changed")), 800),
        "why_matters": _clip(_one_line(src.get("why_matters")), 400),
        "note": _clip(_one_line(src.get("note")), 240),
    }


def _is_asset_card(card: dict) -> bool:
    return bool(card.get("is_asset"))


def _is_blank_card(card: dict) -> bool:
    return not (card.get("url") or card.get("headline") or card.get("bullets")
                or card.get("diff") or card.get("action") or card.get("what_changed")
                or card.get("why_matters") or card.get("note"))


def is_rebuild_group(signals: list) -> bool:
    """整组信号是否只是「前端重建」：正文没变，只有资源文件名变了。

    attach_cards 与 _cards_from_signals 共用本实现：
    page_html 必须全部由采集层明确标了 refs_only，
    普通 page_html（没这个字段）不当噪音，否则会把「HTML 里真的改了正文」的旧数据吞掉；
    没有 page_html 的纯资源组只认 action=rename。
    """
    kinds = {_s(s.get("kind")).strip().lower() for s in signals if isinstance(s, dict)}
    if not kinds or not kinds <= {"page_html", "asset_url"}:
        return False
    page_html = [s for s in signals
                 if isinstance(s, dict) and _s(s.get("kind")).strip().lower() == "page_html"]
    asset_urls = [s for s in signals
                  if isinstance(s, dict) and _s(s.get("kind")).strip().lower() == "asset_url"]
    if any(s.get("ref_kind") in ("img", "other") for s in asset_urls):
        return False
    if page_html:
        # 正文没变时，同页 JS/CSS 无论改名还是增删，页面本身都不算内容变化。
        return all(s.get("refs_only") for s in page_html)
    return bool(asset_urls) and all(s.get("action") == "rename" for s in asset_urls)


def rebuild_note(signals: list) -> str:
    has_page = any(isinstance(s, dict) and _s(s.get("kind")).strip().lower() == "page_html"
                   for s in signals)
    actions = {s.get("action") for s in signals
               if isinstance(s, dict) and _s(s.get("kind")).strip().lower() == "asset_url"}
    if actions & {"added", "removed"}:
        return ("前端资源有增删（站点重新构建），正文无改动" if has_page
                else "前端资源有增删（站点重新构建）")
    if has_page:
        return "差异只在资源文件名上（站点重新构建），正文无改动"
    return "前端资源文件名变了（站点重新构建），正文无改动"


def _friendly_detail(sig: dict) -> str:
    """把采集层的机器口吻翻成人话（只动展示，不改证据）。"""
    kind = _s(sig.get("kind"))
    detail = _s(sig.get("detail")).strip()
    if kind == "page_text":
        m = re.search(r"\+(\d+)\s*行\s*/\s*-(\d+)\s*行[（(]共\s*\d+\s*行，(\d+)\s*字", detail)
        if m:
            added, removed, chars = int(m.group(1)), int(m.group(2)), m.group(3)
            if added and removed:
                return f"正文改了 {added + removed} 行（{chars} 字）"
            if added:
                return f"正文新增 {added} 行（{chars} 字）"
            if removed:
                return f"正文删除 {removed} 行（{chars} 字）"
            return f"正文改了 {chars} 字"
        m = re.search(r"(\d+)\s*字", detail)
        return f"正文改了 {m.group(1)} 字" if m else (detail or "正文有改动")
    if kind == "asset_content":
        return "前端资源内容变了"
    if kind == "asset_url":
        ref_kind = sig.get("ref_kind")
        if ref_kind in ("img", "other"):
            name = _s(sig.get("asset")).rsplit("/", 1)[-1]
            name = re.sub(r"\.[0-9a-f]{6,}(?=\.)", "", name, flags=re.I)
            if ref_kind == "img":
                return f"页面图片有变化（{name}）" if name else "页面图片有变化"
            return f"页面引用的资源有变化（{name}）" if name else "页面引用的资源有变化"
        return detail or "前端资源地址变了"
    if kind == "new_page":
        return "多了个新页面"
    if kind == "gone_page":
        return "有个页面不见了"
    if kind == "fetch_error":
        return "抓取失败"
    return detail


def _diff_from_evidence(ev, key: str) -> list:
    """从 <evidence>/diff/<slug>.diff 里抽出 －/＋ 行；没有就返回空。"""
    if not key or ev is None:
        return []
    ev = Path(ev)
    if not ev.is_dir():
        return []
    try:
        from docwatch.collect.signals import slug_for
        slug = slug_for(key)
    except Exception:  # noqa: BLE001
        slug = ""
    # 只认当前页面自己的 diff。以前「目录里只有一个 .diff 就用它」的回退
    # 会把 A 页的正文 diff 挂到 B 页（新增/下线/抓取失败）的卡片下，宁缺勿错。
    candidates = [ev / "diff" / f"{slug}.diff"] if slug else []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = []
        for raw in text.splitlines():
            if raw.startswith("+++") or raw.startswith("---") or raw.startswith("@@"):
                continue
            if raw.startswith(("+", "-")):
                content = raw[1:].strip()
                if len(content) > 190:
                    content = content[:189].rstrip() + "…"
                lines.append(("＋" if raw.startswith("+") else "－") + content)
        if lines:
            return lines[:40]
    return []


def _annotate_note(verdict: dict) -> None:
    """把「模型没给解读 / 复核没过」这类状态如实附在判读补充里。"""
    notes = []
    if verdict.get("degraded"):
        notes.append("模型这次没给出解读，上面是规则兜底的描述")
    if verdict.get("needs_human"):
        notes.append("AI 复核没通过，结论仅供参考")
    if notes:
        old = _s(verdict.get("model_note")).strip()
        if old:
            notes.append(old)
        verdict["model_note"] = "；".join(notes)


def _summary_evidence(verdict: dict, signals: list) -> list:
    """页脚「依据」只放一句人话摘要，模型给的路径级依据不铺给用户看。

    页数只算「正文/内容真变了」的页面；纯前端重建的几十页单独报一句，
    免得用户以为有几十页内容都改了。
    """
    cards = [c for c in (verdict.get("cards") or []) if isinstance(c, dict)]
    page_cards = [c for c in cards if c.get("page") and not c.get("is_asset")]
    changed_pages = {s.get("page") for s in signals
                     if s.get("kind") in ("page_text", "page_html", "new_page", "gone_page")
                     and s.get("page")}
    real = [c for c in page_cards
            if not c.get("noise") and _s(c.get("page")) in changed_pages]
    rebuild = [c for c in page_cards if c.get("noise")]
    out = [f"{len(real)} 页有变化" if real else "没有内容变化"]
    if rebuild:
        out.append(f"另有 {len(rebuild)} 页只是前端重建")
    # 资源改名属于重建噪音，不算「有变化」；但页脚要如实说明是「仅重命名」，
    # 否则会和正文的「资源文件名变了」自相矛盾。
    assets = {s.get("asset") for s in signals
              if s.get("asset") and (
                  s.get("kind") == "asset_content"
                  or (s.get("kind") == "asset_url" and (
                      s.get("action") != "rename"
                      or s.get("ref_kind") in ("img", "other"))))}
    renamed = {s.get("asset") for s in signals
               if s.get("asset") and s.get("kind") == "asset_url"
               and s.get("action") == "rename"
               and s.get("ref_kind") not in ("img", "other")}
    if assets:
        out.append(f"前端资源 {len(assets)} 个有变化")
    elif renamed:
        out.append(f"前端资源 {len(renamed)} 个仅重命名（重建）")
    else:
        out.append("前端资源无变化")
    review = verdict.get("review") or {}
    if review:
        passed = review.get("pass")
        if passed is True:
            out.append("AI 复核通过")
        elif passed is False:
            out.append("AI 复核未通过，仅供参考")
        else:
            out.append("AI 复核未完成")
    return out


def _signal_cards(job: dict, evidence_dir=None, *, include_signals: bool = False,
                  rich: bool = True) -> list:
    """按页面/资源分组生成「原始卡片」（attach_cards 与 _cards_from_signals 共用）。

    rich=True：给正常变化邮件用的完整卡片（逐组级别、人话要点、证据包 diff）。
    rich=False：保留旧 fallback 行为（沿用调用方的全局级别，要点/diff 由 _make_card
    从信号明细生成），供健康告警、预览等没有 attach_cards 合成的场景使用。
    """
    job = job if isinstance(job, dict) else {}
    signals = [s for s in (job.get("signals") or []) if isinstance(s, dict)]
    if not signals:
        return []
    groups: dict[str, list] = {}
    for sig in signals:
        key = _s(sig.get("page")) or _s(sig.get("asset"))
        groups.setdefault(key, []).append(sig)
    ev = evidence_dir if evidence_dir is not None else job.get("evidence_dir")
    ev = Path(ev) if _s(ev).strip() else None
    cards = []
    for key, sigs in groups.items():
        is_asset = all(_s(s.get("kind")) in ASSET_KINDS for s in sigs)
        is_error = all(_s(s.get("kind")) == "fetch_error" for s in sigs)
        if is_rebuild_group(sigs):
            card = {}
            if rich:
                card["level"] = int(rules.decide({"signals": sigs})["level"])
            card.update({
                "page": key,
                "is_asset": is_asset,
                "is_error": is_error,
                "noise": True,
                "note": rebuild_note(sigs),
            })
            if include_signals:
                card["signals"] = sigs
            cards.append(card)
            continue
        if not rich:
            card = {"page": key, "is_asset": is_asset, "is_error": is_error}
            if include_signals:
                card["signals"] = sigs
            cards.append(card)
            continue
        kinds = {_s(s.get("kind")) for s in sigs}
        bullets = []
        for sig in sigs:
            if _s(sig.get("kind")) == "page_html" and (kinds - {"page_html"}):
                continue                      # 正文/资源另有信号时，HTML 哈希不提
            if (_s(sig.get("kind")) == "asset_url" and sig.get("action") == "rename"
                    and sig.get("ref_kind") not in ("img", "other")):
                continue                      # JS/CSS 改名是重建噪音，别混进正文卡
            text = _friendly_detail(sig)
            if text and text not in bullets:
                bullets.append(text)
        card = {
            "level": int(rules.decide({"signals": sigs})["level"]),
            "page": key,
            "is_asset": is_asset,
            "is_error": is_error,
            "bullets": bullets[:4],
            "diff": _diff_from_evidence(ev, key),
        }
        if include_signals:
            card["signals"] = sigs
        cards.append(card)
    if rich:
        cards.sort(key=lambda c: (-c["level"], c["page"]))
    return cards


def attach_cards(verdict: dict, job: dict, evidence_dir=None) -> dict:
    """按页面分组生成卡片，diff 取证据包里的真实文本差异（唯一实现）。

    为什么不让模型给：模型只讲人话，行级差异是我们手上最硬的证据，
    而且 page_html 信号里的 before/after 是哈希，直接渲染会变成给用户看哈希。

    纯重建的页面/资源（正文一个字没动，只是 HTML 里的资源引用换了）不单独出卡：
    站点每次发版会有几十页同时这样，逐条渲染会把真正的变化淹没。它们被标成噪音，
    由排版层合并成一行「其余 N 页…」。
    """
    verdict = verdict if isinstance(verdict, dict) else {}
    signals = [s for s in ((job or {}).get("signals") or []) if isinstance(s, dict)]
    if not signals:
        return verdict
    verdict["cards"] = _signal_cards(job, evidence_dir)
    verdict["evidence"] = _summary_evidence(verdict, signals)
    _annotate_note(verdict)
    return verdict


def _cards_from_signals(job: dict, level: int, noise: bool) -> list:
    """没有预置 cards 时（补发/告警/预览），沿用旧 fallback 行为补卡。"""
    return [_make_card(card, level, noise)
            for card in _signal_cards(job, include_signals=True, rich=False)]


def _overflow_summary_card(cards: list) -> dict:
    """非噪音卡片超过上限时，用一张摘要卡兜住剩余页面/资源。"""
    pages = [card for card in cards if not _is_asset_card(card)]
    assets = [card for card in cards if _is_asset_card(card)]
    label_parts = []
    if pages:
        label_parts.append(f"{len(pages)} 页")
    if assets:
        label_parts.append(f"{len(assets)} 个资源")
    bullets = []
    if pages:
        first = _page_parts(pages[0]["url"])[1] or pages[0].get("label") or ""
        last = _page_parts(pages[-1]["url"])[1] or pages[-1].get("label") or ""
        if first and last:
            bullets.append(f"{first} … {last}" if first != last else first)
    if assets:
        names = [_page_parts(card["url"])[1] or card.get("label") or "" for card in assets[:3]]
        names = [name for name in names if name]
        if names:
            bullets.append("资源：" + "、".join(names) + ("等" if len(assets) > 3 else ""))
    if not bullets:
        bullets.append("完整清单见证据包")
    return {
        "level": 1,
        "noise": False,
        "url": "",
        "label": "其余 " + " + ".join(label_parts),
        "bullets": bullets,
        "diff": [],
        "action": "",
        "headline": "",
        "what_changed": "",
        "why_matters": "",
        "note": "",
        "page_count": len(pages),
    }


def _cards(verdict: dict, job: dict, level: int, noise: bool) -> list:
    raw = None
    for key in ("cards", "findings", "groups"):
        value = verdict.get(key)
        if isinstance(value, list) and value:
            raw = value
            break
    cards = []
    if raw:
        for item in raw:
            if isinstance(item, dict):
                cards.append(_make_card(item, level, noise))
    if not cards:
        cards = _cards_from_signals(job, level, noise)
    if not cards:
        cards = [_make_card({}, level, noise)]
    cards.sort(key=lambda card: (-card["level"], card["url"]))
    # label 重复时补短 slug，避免一堆卡片全叫「文档页面」（P2-51）。
    label_counts: dict[str, int] = {}
    for card in cards:
        label_counts[card["label"]] = label_counts.get(card["label"], 0) + 1
    for card in cards:
        if label_counts.get(card["label"], 0) > 1 and card.get("url"):
            slug = _page_parts(card["url"])[1]
            if slug:
                card["label"] = f"{card['label']} · {slug}"
    real, noise_cards = [], []
    for card in cards:
        if card["noise"]:
            # 噪音卡只留一行灰字，不展示 diff / 要点；有证据才说「重新构建」。
            # 完全空白的占位卡保持全空，交给 _build 丢掉，别让它变成「1 页重建」。
            has_evidence = bool(card["bullets"] or card["diff"])
            empty = _is_blank_card(card)
            card["note"] = (card["note"] or card["headline"]
                            or (DEFAULT_NOTE if has_evidence
                                else ("" if empty else EMPTY_NOTE)))
            card["bullets"] = []
            card["diff"] = []
            card["action"] = ""
            noise_cards.append(card)
        else:
            real.append(card)
    # 站点重建时几十页会同时「变」；超过一张就合并成一行，别让噪音淹没真正的变化。
    # 夜间合并信会把多轮的卡片拼在一起，同一页可能重复出现，先按 URL 去重再计数。
    noise_cards = _dedupe_cards(noise_cards)
    if len(noise_cards) > 1:
        real.append(_noise_summary_card(noise_cards))
    elif noise_cards:
        real.extend(noise_cards)
    # 非噪音卡片上限 8 张：全站页脚/导航变化可能一次几十页，
    # 150 张卡 HTML 实测 148 KB，会被 Gmail 约 102 KB 截断（P2-51）。
    head = [card for card in real if not card["noise"]]
    tail = [card for card in real if card["noise"]]
    if len(head) > MAX_CARDS:
        head = head[:MAX_CARDS] + [_overflow_summary_card(head[MAX_CARDS:])]
    return head + tail


def _dedupe_cards(cards: list) -> list:
    """同名页面/资源只留一张（夜间多轮合并时常见）。"""
    seen, out = set(), []
    for card in cards:
        key = card.get("url") or card.get("label")
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


def _noise_summary_card(cards: list) -> dict:
    """把多张噪音卡合并成一张：页数、资源数 + 一句说明。"""
    pages = [card for card in cards if not _is_asset_card(card)]
    assets = [card for card in cards if _is_asset_card(card)]
    label_parts = []
    if pages:
        label_parts.append(f"{len(pages)} 页")
    if assets:
        label_parts.append(f"{len(assets)} 个资源")

    refs_only = sum(1 for card in pages if "资源文件名" in card["note"])
    add_remove = sum(1 for card in pages if "有增删" in card["note"])
    other = len(pages) - refs_only - add_remove
    if pages and not other and not add_remove:
        note = f"{len(pages)} 页的 HTML 只是资源文件名变了（站点重新构建）"
    elif pages:
        note = f"{len(pages)} 页 HTML 有变化但正文没变"
        details = []
        if refs_only:
            details.append(f"{refs_only} 页只是资源文件名变了")
        if add_remove:
            details.append(f"{add_remove} 页资源有增删")
        if details:
            note += "（其中 " + "、".join(details) + "）"
    else:
        note = "前端资源文件名变了（站点重新构建）"
    if assets and pages:
        names = [_page_parts(card["url"])[1] or "前端资源" for card in assets[:3]]
        suffix = "（" + "、".join(names) + ("等" if len(assets) > 3 else "") + "）"
        note += f"，另有 {len(assets)} 个前端资源换了文件名{suffix}"
    note += "；正文没有改动，为保持可读性不逐条列出。"

    return {
        "level": 0,
        "noise": True,
        "url": "",
        "label": "其余 " + " + ".join(label_parts),
        "bullets": [],
        "diff": [],
        "action": "",
        "headline": "",
        "note": note,
        "page_count": len(pages),
    }


# ---------------------------------------------------------------- 上下文


def _evidence_text(verdict: dict, job: dict) -> str:
    raw = verdict.get("evidence")
    items = []
    if isinstance(raw, (list, tuple)):
        items = [_clip(_one_line(item), 70) for item in raw]
    elif raw:
        items = [_clip(text, 70) for text in _sentences(raw)]
    items = [text for text in items if text]
    if not items:
        signals = job.get("signals")
        if isinstance(signals, list):
            for signal in signals[:3]:
                if isinstance(signal, dict):
                    detail = _clip(_one_line(signal.get("detail")), 60)
                    if detail:
                        items.append(detail)
    return _clip(" · ".join(items[:3]), 150) if items else DEFAULT_EVIDENCE


def _meta_text(time_text: str, pages_total: int, pages_changed: int,
               pages_noise: int = 0) -> str:
    parts = []
    if time_text:
        parts.append(time_text)
    if pages_total:
        parts.append(f"检查 {pages_total} 页")
    if pages_changed:
        parts.append(f"{pages_changed} 页有变化")
    if pages_noise:
        parts.append(f"{'另有 ' if pages_changed else ''}{pages_noise} 页仅前端重建")
    return " · ".join(parts)


def _subject(label: str, headline: str, pages_changed: int, pages_noise: int = 0) -> str:
    if pages_changed:
        tail = f"（{pages_changed} 页）"
    elif pages_noise:
        tail = f"（{pages_noise} 页重建）"
    else:
        tail = ""
    room = SUBJECT_MAX - len(SUBJECT_PREFIX) - 1 - len(label) - 3 - len(tail)
    head = _clip(headline, max(room, 10))
    subject = f"{SUBJECT_PREFIX} {label} · {head}{tail}"
    return _clip(subject, SUBJECT_MAX + 2)


# ---------------------------------------------------------------- HTML


def _divider() -> str:
    return (
        '<tr><td style="padding:0 20px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td style="border-bottom:1px solid #e8eaed;font-size:15px;line-height:1.2;">&nbsp;</td></tr>'
        '</table></td></tr>'
    )


def _banner(ctx: dict) -> str:
    meta = (
        f'<p style="margin:8px 0 0;font-size:15px;color:{COLOR_MUTED};line-height:1.6;">'
        f'{_esc(ctx["meta"])}</p>'
        if ctx["meta"] else ""
    )
    return (
        '<tr><td style="padding:18px 20px 14px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{ctx["color"]}" style="background:{ctx["color"]};color:#ffffff;font-size:15px;'
        'font-weight:700;padding:4px 12px;border-radius:5px;letter-spacing:1px;">'
        f'{_esc(ctx["label"])}</td></tr></table>'
        f'<p style="margin:12px 0 0;font-size:21px;font-weight:700;color:{COLOR_STRONG};line-height:1.5;">'
        f'{_esc(ctx["headline"])}</p>'
        f'{meta}</td></tr>'
    )


def _impact_html(why: str, color: str) -> str:
    head, rest = _split_first(why)
    body = ""
    if head:
        body += f'<strong style="color:{COLOR_STRONG};">{_esc(head)}</strong>'
    if rest:
        body += (" " if body else "") + _esc(rest)
    return (
        '<tr><td style="padding:16px 20px 16px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="border-left:3px solid {color};padding:2px 0 2px 12px;">'
        f'<p style="margin:0;font-size:15px;font-weight:700;color:{COLOR_MUTED};letter-spacing:1px;">有什么影响</p>'
        f'<p style="margin:6px 0 0;font-size:16px;color:{COLOR_TEXT};line-height:1.7;">{body}</p>'
        '</td></tr></table></td></tr>'
    )


def _diff_html(diff: list) -> str:
    shown = diff[:MAX_DIFF_LINES]
    rest = len(diff) - len(shown)
    rows = []
    for mark, text in shown:
        if mark == "－":
            bg, fg = COLOR_DEL_BG, COLOR_DEL_FG
        else:
            bg, fg = COLOR_ADD_BG, COLOR_ADD_FG
        rows.append(
            f'<tr><td bgcolor="{bg}" style="background:{bg};color:{fg};font-size:15px;line-height:1.6;'
            f'padding:5px 10px;border-radius:4px;font-family:{MONO};">{mark} {_esc(text)}</td></tr>'
        )
    if rest > 0:
        rows.append(
            f'<tr><td style="font-size:15px;color:{COLOR_FAINT};padding:6px 10px 2px;">另有 {rest} 行未显示</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-top:10px;border:1px solid #e6e8eb;border-radius:8px;background:#fcfcfd;">'
        f'<tr><td style="padding:9px 12px 6px;font-size:15px;font-weight:700;color:{COLOR_MUTED};">原文差异</td></tr>'
        '<tr><td style="padding:0 10px 10px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="border-spacing:0 3px;">'
        + "".join(rows) +
        '</table></td></tr></table>'
    )


def _page_link(card: dict) -> str:
    """卡片右上角的「打开页面」；没有 URL 时不渲染。"""
    if not card["url"]:
        return ""
    return (
        f'<a href="{_esc(card["url"])}" target="_blank" '
        f'style="color:{COLOR_FAINT};font-size:15px;text-decoration:none;white-space:nowrap;">打开页面</a>'
    )


def _noise_card_html(card: dict) -> str:
    name = card["label"]
    link = _page_link(card)
    return (
        '<tr><td style="padding:14px 20px;border-top:1px solid #e8eaed;background:#fcfcfd;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-size:15px;font-weight:700;color:{COLOR_FAINT};">噪音 · {_esc(name)}</td>'
        f'<td align="right">{link}</td>'
        '</tr></table>'
        f'<p style="margin:5px 0 0;font-size:15px;color:{COLOR_FAINT};line-height:1.6;">'
        f'{_esc(card["note"] or DEFAULT_NOTE)}</p>'
        '</td></tr>'
    )


def _card_html(card: dict, ctx: dict) -> str:
    if card["noise"]:
        return _noise_card_html(card)

    mark = ""
    if ctx["multi"]:
        name, color = LEVELS[card["level"]]
        mark = f'<span style="color:{color};font-size:15px;font-weight:700;">{_esc(name)}</span> '
    link = _page_link(card)

    body = []
    if card["headline"]:
        body.append(
            f'<p style="margin:10px 0 0;font-size:16px;font-weight:700;color:{COLOR_STRONG};'
            f'line-height:1.6;">{_esc(card["headline"])}</p>'
        )
    if card["what_changed"]:
        body.append(
            f'<p style="margin:8px 0 0;font-size:15px;color:{COLOR_TEXT};line-height:1.65;">'
            f'<strong style="color:{COLOR_STRONG};">改动：</strong>{_esc(card["what_changed"])}</p>'
        )
    for index, bullet in enumerate(card["bullets"]):
        bullet_mark = BULLET_MARKS[index] if index < len(BULLET_MARKS) else "·"
        body.append(
            f'<p style="margin:{10 if index == 0 and not card["headline"] else 6}px 0 0;'
            f'font-size:15px;color:{COLOR_TEXT};line-height:1.65;">'
            f'{bullet_mark} {_esc(bullet)}</p>'
        )
    if card["why_matters"]:
        body.append(
            f'<p style="margin:8px 0 0;font-size:15px;color:{COLOR_TEXT};line-height:1.65;">'
            f'<strong style="color:{COLOR_STRONG};">影响：</strong>{_esc(card["why_matters"])}</p>'
        )
    if card["diff"]:
        body.append(_diff_html(card["diff"]))
    if card["action"]:
        body.append(
            f'<p style="margin:12px 0 0;font-size:15px;color:{COLOR_TEXT};line-height:1.65;">'
            f'<strong style="color:{COLOR_STRONG};">建议：</strong>{_esc(card["action"])}</p>'
        )
    if not body:
        body.append(
            f'<p style="margin:10px 0 0;font-size:15px;color:{COLOR_FAINT};line-height:1.65;">{EMPTY_NOTE}</p>'
        )

    return (
        '<tr><td style="padding:16px 20px 18px;border-top:1px solid #e8eaed;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-size:16px;font-weight:700;color:{COLOR_STRONG};">{mark}{_esc(card["label"])}</td>'
        f'<td align="right">{link}</td>'
        '</tr></table>' + "".join(body) + '</td></tr>'
    )


def _action_html(action: str) -> str:
    return (
        '<tr><td style="padding:16px 20px 18px;border-top:1px solid #e8eaed;">'
        f'<p style="margin:0;font-size:15px;font-weight:700;color:{COLOR_MUTED};letter-spacing:1px;">建议</p>'
        f'<p style="margin:6px 0 0;font-size:16px;color:{COLOR_TEXT};line-height:1.7;">{_esc(action)}</p>'
        '</td></tr>'
    )


def _model_note_html(note: str) -> str:
    """模型自己的补充看法：建议下方一行灰字，有就显示、没有就不占位。"""
    return (
        '<tr><td style="padding:0 20px 16px;">'
        f'<p style="margin:0;font-size:15px;color:{COLOR_FAINT};line-height:1.6;">判读补充：{_esc(note)}</p>'
        '</td></tr>'
    )


def _footer_html(ctx: dict) -> str:
    lines = [
        f"依据：{ctx['evidence_text']}",
        f"判读：{ctx['judge_text']}",
        f"判读模型：{ctx['model_text']}",
        SOOTHING_HINT,
    ]
    paras = "".join(
        f'<p style="margin:{0 if index == 0 else 4}px 0 0;font-size:15px;color:{COLOR_FAINT};'
        f'line-height:1.6;">{_esc(line)}</p>'
        for index, line in enumerate(lines)
    )
    return (
        '<tr><td style="padding:14px 20px 18px;border-top:1px solid #e8eaed;background:#fbfbfc;'
        f'border-radius:0 0 12px 12px;">{paras}</td></tr>'
    )


def _email_document(subject: str, body: str, *, viewport: bool = True) -> str:
    viewport_meta = (
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        if viewport else ""
    )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        + viewport_meta
        + f'<title>{_esc(subject)}</title></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f4f5f7;"><tr><td align="center" style="padding:16px 8px;">'
        f'<table role="presentation" width="{PAGE_WIDTH}" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:640px;background:#ffffff;border:1px solid #e2e5e9;border-radius:12px;'
        f'font-family:{FONT};font-size:15px;color:{COLOR_TEXT};line-height:1.65;text-align:left;">'
        + body
        + '</table></td></tr></table></body></html>'
    )


def _render_html(cards: list, ctx: dict) -> str:
    rows = [_banner(ctx)]
    if ctx["why"] or cards:
        rows.append(_divider())
    if ctx["why"]:
        rows.append(_impact_html(ctx["why"], ctx["color"]))
    rows.extend(_card_html(card, ctx) for card in cards)
    if ctx["action"]:
        rows.append(_action_html(ctx["action"]))
    if ctx["model_note"]:
        rows.append(_model_note_html(ctx["model_note"]))
    rows.append(_footer_html(ctx))
    return _email_document(ctx["subject"], "".join(rows))


# ---------------------------------------------------------------- 纯文本


def _text_card_title(card: dict, multi: bool) -> str:
    name = card["label"]
    if card["noise"]:
        return f"【噪音 · {name}】"
    if multi:
        return f"【{LEVELS[card['level']][0]}】{name}"
    return f"【{name}】"


def _render_text(cards: list, ctx: dict) -> str:
    out = [f"[{ctx['label']}] {ctx['headline']}"]
    if ctx["meta"]:
        out.append(ctx["meta"])
    if ctx["why"]:
        out.extend(["", "有什么影响", ctx["why"]])

    for card in cards:
        out.append("")
        out.append(_text_card_title(card, ctx["multi"]))
        if card["noise"]:
            out.append(f"  {card['note'] or DEFAULT_NOTE}")
            if card["url"]:
                out.append(f"  原文：{card['url']}")
            continue
        if card["headline"]:
            out.append(f"  {card['headline']}")
        if card["what_changed"]:
            out.append(f"  改动：{card['what_changed']}")
        for index, bullet in enumerate(card["bullets"]):
            bullet_mark = BULLET_MARKS[index] if index < len(BULLET_MARKS) else "·"
            out.append(f"  {bullet_mark} {bullet}")
        if card["why_matters"]:
            out.append(f"  影响：{card['why_matters']}")
        shown = card["diff"][:MAX_DIFF_LINES]
        rest = len(card["diff"]) - len(shown)
        if shown:
            out.append("  原文差异：")
            out.extend(f"    {mark} {text}" for mark, text in shown)
            if rest > 0:
                out.append(f"    另有 {rest} 行未显示")
        if card["action"]:
            out.append(f"  建议：{card['action']}")
        # 与 HTML 的 body 判定保持一致：headline / what_changed / why_matters 都算内容，
        # 否则有标题的卡片仍会误印「本次检查未发现任何变化。」（P2-29）
        if not (card["headline"] or card["what_changed"] or card["bullets"]
                or card["diff"] or card["why_matters"] or card["action"]):
            out.append(f"  {EMPTY_NOTE}")
        if card["url"]:
            out.append(f"  原文：{card['url']}")

    if ctx["action"]:
        out.extend(["", "建议", ctx["action"]])
    if ctx["model_note"]:
        out.extend(["", f"判读补充：{ctx['model_note']}"])

    out.extend([
        "",
        TEXT_RULE,
        f"依据：{ctx['evidence_text']}",
        f"判读：{ctx['judge_text']}",
        f"判读模型：{ctx['model_text']}",
        SOOTHING_HINT,
    ])
    return "\n".join(out)


# ---------------------------------------------------------------- 入口


def _build(verdict, job) -> dict:
    verdict = verdict if isinstance(verdict, dict) else {}
    job = job if isinstance(job, dict) else {}

    # 缺 level 时：有材料按「轻微」处理（别把真实变化埋掉），没材料就是「噪音」
    signals = job.get("signals")
    explicit_cards = bool(verdict.get("cards") or verdict.get("findings") or verdict.get("groups"))
    has_material = bool(
        explicit_cards
        or _s(verdict.get("headline")) or _s(verdict.get("what_changed"))
        or _s(verdict.get("why_matters")) or _s(verdict.get("action"))
        or (isinstance(signals, list) and signals)
    )
    level = _norm_level(verdict.get("level"), 1 if has_material else 0)
    noise = _truthy(verdict.get("noise"))
    if noise:
        level = 0

    cards = _cards(verdict, job, level, noise)
    label, color = LEVELS[level]

    headline = _clip(_strip_emoji(_one_line(verdict.get("headline"))), 120) or DEFAULT_HEADLINE[level]
    why = _clip(_one_line(verdict.get("why_matters")), 600)
    action = _clip(_one_line(verdict.get("action")), 300)
    model_note = _clip(_one_line(verdict.get("model_note")), 200)
    pages_total = _as_int(job.get("pages_total"))
    pages_changed = _as_int(job.get("pages_changed"))
    signal_list = [sig for sig in signals if isinstance(sig, dict)] if isinstance(signals, list) else []
    has_input = explicit_cards or bool(signal_list)
    # 健康告警：全是抓取失败，标题已经写明「N 个地址」，不要再拿「N 页」误导。
    all_failed = bool(signal_list) and all(
        _s(sig.get("kind")).strip().lower() == "fetch_error" for sig in signal_list)

    # 什么都没有（缺字段/空数据）：不硬凑一张空卡片，标题一句话就够
    if len(cards) == 1 and _is_blank_card(cards[0]):
        cards = []

    meaningful = [card for card in cards
                  if not card["noise"] and (card["bullets"] or card["diff"] or card["action"]
                                            or card["headline"] or card["what_changed"]
                                            or card["why_matters"])]
    pages_noise = 0
    for card in cards:
        if not card["noise"]:
            continue
        if card.get("page_count") is not None:
            pages_noise += _as_int(card.get("page_count"))
        elif not _is_asset_card(card):
            pages_noise += 1
    # 有真实材料时，页数只算正文/内容真的变了的页面；纯重建页单独报。
    # 没有材料（只有 headline/页数）时保留调用方给的页数。
    if has_input:
        pages_changed = (0 if all_failed
                         else sum((_as_int(card.get("page_count")) if "page_count" in card else 1)
                                  for card in meaningful
                                  if not _is_asset_card(card) and not card.get("is_error")))

    # 单卡：建议放进卡里（和线框图一致）
    if len(cards) == 1 and not cards[0]["noise"] and not cards[0]["action"] and action:
        cards[0]["action"] = action
        action = ""
    # 每张卡都已经给了建议 → 全局建议不再重复展示（免得「无需操作」后又来一条）
    non_noise_cards = [card for card in cards if not card["noise"]]
    if action and non_noise_cards and all(card["action"] for card in non_noise_cards):
        action = ""

    ctx = {
        "level": level,
        "label": label,
        "color": color,
        "headline": headline,
        "why": why,
        "action": action,
        "model_note": model_note,
        "multi": len(cards) > 1,
        "meta": _meta_text(_fmt_time(job.get("created_at")), pages_total,
                           pages_changed, pages_noise),
        "evidence_text": _evidence_text(verdict, job),
        "judge_text": _judge_text(verdict, pages_total),
        "model_text": _model_text(verdict),
    }
    ctx["subject"] = _subject(label, headline, pages_changed, pages_noise)
    return {
        "subject": ctx["subject"],
        "text": _render_text(cards, ctx),
        "html": _render_html(cards, ctx),
    }


def _fallback(verdict) -> dict:
    """排版出错时的兜底：仍然给一封能看的信，不让发信链路崩。"""
    level = _norm_level((verdict or {}).get("level") if isinstance(verdict, dict) else None)
    label, color = LEVELS[level]
    subject = _clip(f"{SUBJECT_PREFIX} {label} · 有新的变化，请查看正文", SUBJECT_MAX)
    note = "本次检查发现变化，但邮件排版出错，请直接查看监控记录或证据包。"
    model_text = _model_text(verdict)
    text = "\n".join([f"[{label}] 有新的变化", "", note, "", f"判读模型：{model_text}"])
    page = _email_document(
        subject,
        '<tr><td style="padding:18px 20px;">'
        f'<p style="margin:0;font-size:21px;font-weight:700;color:{COLOR_STRONG};">{_esc(label)}</p>'
        f'<p style="margin:10px 0 0;font-size:16px;color:{COLOR_TEXT};">{_esc(note)}</p>'
        f'<p style="margin:6px 0 0;font-size:15px;color:{COLOR_MUTED};">判读模型：{_esc(model_text)}</p>'
        '</td></tr>',
        viewport=False,
    )
    return {"subject": subject, "text": text, "html": page}


def render_with_status(verdict: dict, job: dict) -> tuple[dict, bool]:
    """渲染邮件并告诉调用方有没有走兜底：返回 (out, fallback_used)。

    发信端删队列前必须确认 `fallback_used is False`：兜底信虽然能发，但它丢掉了
    这一封的真实解读，不能当作「已处理」把队列文件删掉。
    """
    try:
        return _build(verdict, job), False
    except Exception:  # noqa: BLE001 —— 排版失败也不能阻断发信
        return _fallback(verdict), True


def render(verdict: dict, job: dict) -> dict:
    """把一次唤醒的判读结论渲染成邮件：{"subject": str, "text": str, "html": str}。

    verdict 见 CONTRACT §3.3，job 见 §3.2；任何字段缺失都走默认值，不抛异常。
    """
    return render_with_status(verdict, job)[0]


__all__ = ["attach_cards", "render", "render_with_status"]
