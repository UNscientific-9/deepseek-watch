"""采集 docwatch 与自建 webwatch 的记录，整理成网站事件。

这个模块只读数据，不调用任何模型，也不修改 docwatch。
生产环境由 ``watchsite.push`` 每 5 分钟调用一次。
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

from . import state as _push_state

SOURCE_LABELS = {
    "docs": "官方文档",
    "chat": "Chat 网页",
    "platform": "Platform 控制台",
    "harness": "Harness",
    "fingerprint": "模型指纹",
}
LEVEL_LABELS = {0: "噪音", 1: "轻微", 2: "值得看", 3: "重要"}
WEB_SOURCES = ("chat", "platform", "harness", "fingerprint")
ALL_SOURCES = ("docs",) + WEB_SOURCES
BEIJING = dt.timezone(dt.timedelta(hours=8))
DOCS_HOME = "https://api-docs.deepseek.com/zh-cn/"
# 推送器每 5 分钟跑一次；v2_jobs 会长期累积，查询必须有上限。
# 500 条约等于 10 天（48 轮/天），足够覆盖 push-state 丢失后的回放窗口，
# 同时避免把几十 MB 的 job_json 全部读进 256M 内存的推送单元。
DOCS_JOB_QUERY_LIMIT = 500

# 事件目录增量索引（P1-05 / B-6）：每轮只读取、归一化新增或变更的文件；
# 已发送且未变化的文件只保留 id/source/changed_ts 三个轻量字段用于状态展示。
# 索引放在推送状态目录里，和 push-state.json 一起随部署保留。
MONITOR_INDEX_FILE = "monitor-index.json"
MONITOR_INDEX_VERSION = 1
# 单轮完整归一化的原始字节预算。事件目录最多 800 条，正常每条约几 KB，
# 32 MB 足够一轮处理完；超过时按文件记 collection_errors 并留到下一轮，
# 绝不静默丢弃事件。可通过环境变量覆盖，方便测试和小内存环境。
DEFAULT_MAX_NORMALIZE_BYTES = 32 * 1024 * 1024

# docwatch 在服务器上是 /opt/docwatch-v2，本地仓库是当前目录。
# 生产推送器的 WorkingDirectory 是 /opt/deepseek-watch，光靠 cwd 找不到 docwatch；
# 先把 DOCWATCH_ROOT 放进 sys.path，再尝试复用它的规则与分包函数。
_DOCWATCH_ROOT_ENV = os.getenv("DOCWATCH_ROOT", "").strip()
if _DOCWATCH_ROOT_ENV and Path(_DOCWATCH_ROOT_ENV).is_dir():
    if _DOCWATCH_ROOT_ENV not in sys.path:
        sys.path.insert(0, _DOCWATCH_ROOT_ENV)

try:  # pragma: no cover - 生产环境一定存在
    from docwatch import rules as _docwatch_rules
except Exception:  # noqa: BLE001
    _docwatch_rules = None

try:  # pragma: no cover - 生产环境一定存在
    from docwatch.run import _fallback_verdict as _docwatch_fallback
    from docwatch.run import attach_cards as _docwatch_attach_cards
except Exception:  # noqa: BLE001
    _docwatch_fallback = None
    _docwatch_attach_cards = None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_from_epoch(value: int) -> str:
    """epoch 秒 → 站内统一的时间戳文本；状态行里多处复用。"""
    return _iso(dt.datetime.fromtimestamp(value, tz=dt.timezone.utc))


def _parse_time(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    # 归档站的时间可能带 9 位纳秒，datetime.fromisoformat 在部分版本里不接受。
    text = re.sub(r"(\.\d{6})\d+(?=[+-Z])", r"\1", text)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _epoch(value: object, fallback: int = 0) -> int:
    parsed = _parse_time(value)
    return int(parsed.timestamp()) if parsed else int(fallback)


def _clamp_level(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(3, number))


def _int_or(value: object, fallback: int = 0) -> int:
    """把外部状态里的数字安全转换；坏值回退到默认值而不是让整轮崩溃。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: object, limit: int = 4000) -> str:
    text = str(value or "").strip()
    return text[:limit]


# 单条 diff 上限：D1 单语句 100KB 的安全余量（服务端 ingest.js 用同一个上限）。
MAX_DIFF_CHARS = 20_000


def _text_diff(value: object) -> str:
    """把 diff 列表拼成逐行文本，避免 str(list) 把推送内容变成 Python repr。"""
    if isinstance(value, (list, tuple)):
        lines: list[str] = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                mark = str(item[0] or "").strip()
                text = str(item[1] or "").strip()
                if text:
                    lines.append(f"{mark}{text}" if mark else text)
            else:
                text = str(item or "").strip()
                if text:
                    lines.append(text)
        return "\n".join(lines)
    return str(value or "").strip()


def _slugify(value: object, fallback: str = "event") -> str:
    # 第一处正则已把任意非字母数字串压成单个 "-"，连续横线不可能出现。
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (text or fallback)[:100].strip("-") or fallback


def _diff_limited(value: object, limit: int = MAX_DIFF_CHARS) -> str:
    """把 diff 正文截到 limit 字符，末尾注明原文大小（含备注也不超 limit）。"""
    text = _text_diff(value)
    if len(text) <= limit:
        return text
    total_kb = len(text.encode("utf-8")) / 1024
    marker = f"\n…（已截断，原文 {total_kb:.1f} KB）"
    return (text[: max(1, limit - len(marker))] + marker)[:limit]


def _split_changes(changes: list[dict]) -> tuple[list[dict], list[dict]]:
    """把变化拆成「目录（进 payload）」与「明细（进 changes_detail）」。

    主 payload 必须远低于 ingest 的 60,000 字符上限，所以目录里只保留
    计数 / 标记字段；diff 与 note 正文随 changes_detail 同一次请求推送。
    旧字段 diff / note 仍保留键位（置空），保证旧渲染器回滚时不会 KeyError。
    """
    directory: list[dict] = []
    detail: list[dict] = []
    for change in changes:
        diff = str(change.get("diff") or "")
        bullets = [str(item) for item in _as_list(change.get("bullets"))]
        base = {
            "page": str(change.get("page") or ""),
            "level": _clamp_level(change.get("level")),
            "is_asset": bool(change.get("is_asset")),
            "is_error": bool(change.get("is_error")),
            "noise": bool(change.get("noise")),
            "has_diff": bool(diff),
            "diff_bytes": len(diff.encode("utf-8")),
            "bullets": bullets,
        }
        directory.append({**base, "diff": "", "note": ""})
        detail.append({**base, "diff": diff, "note": str(change.get("note") or "")})
    return directory, detail


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


_HTTP_URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


def _http_url(value: object) -> str:
    text = str(value or "").strip()
    # 与服务端 httpUrl() 对齐：只认完整 http(s) 地址且不含任何空白字符。
    # 只查前缀会把 "https://a b" 放过去，服务端拒收后该事件每轮重试。
    return text if _HTTP_URL_RE.match(text) else ""


def _evidence_list(value: object, limit: int = 20) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(_text(item, 800))
        elif isinstance(item, dict):
            text = item.get("detail") or item.get("text") or item.get("summary")
            if text:
                out.append(_text(text, 800))
        if len(out) >= limit:
            break
    return out


def _payload_size(payload: dict) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 10**9


def _fit_analysis(out: dict, limit: int) -> dict:
    """analysis 有独立降级顺序：先砍尾部 sections/段落，保住 tldr_points 和 overview。

    旧实现把 analysis 当普通键，超限时按体积整个 pop 掉，读者看到的就是「有变化但无解读」；
    而 search_text 还在用裁剪前的 analysis，搜索能搜到页面看不到的内容。
    """
    analysis = out.get("analysis")
    if not isinstance(analysis, dict):
        return out
    analysis = copy.deepcopy(analysis)
    out["analysis"] = analysis
    if _payload_size(out) <= limit:
        return out
    sections = analysis.get("sections")
    if isinstance(sections, list):
        while sections and _payload_size(out) > limit:
            if len(sections) > 1:
                sections.pop()
            else:
                paragraphs = sections[0].get("paragraphs") if isinstance(sections[0], dict) else None
                if isinstance(paragraphs, list) and len(paragraphs) > 1:
                    paragraphs.pop()
                else:
                    sections.clear()
            out["analysis"] = analysis
    if _payload_size(out) <= limit:
        return out
    # sections 全裁掉仍超限：保留 tldr_points/overview/action，把长文本截短。
    analysis.pop("sections", None)
    for key in ("overview", "tldr", "headline"):
        value = analysis.get(key)
        if isinstance(value, str) and len(value) > 500:
            analysis[key] = value[:500]
    out["analysis"] = analysis
    return out


def _fit_release_note(out: dict, limit: int) -> dict:
    """DSH 完整更新日志不能整块丢掉：先丢重复字段，再按尾部截断并注明。

    ``notes`` 与 ``what_changed`` 是兼容 / 搜索用的重复内容；详情页和 RSS 只看
    ``release_note.blocks``。超限时必须先删这些重复项，不能反过来按整包体积
    把真实条目全部删光。
    """
    note = out.get("release_note")
    if not isinstance(note, dict):
        return out
    blocks = note.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return out
    out.pop("notes", None)
    what_changed = out.get("what_changed")
    if isinstance(what_changed, str) and len(what_changed) > 5000:
        out["what_changed"] = what_changed[:5000]
    note = copy.deepcopy(note)
    out["release_note"] = note
    blocks = note.get("blocks")
    truncated = False
    while blocks and _payload_size(out) > limit:
        blocks.pop()
        truncated = True
    if truncated:
        blocks.append({"type": "paragraph", "text": "…（更新日志过长，已截断）"})
    return out


def _fit_payload(payload: dict, limit: int = 60_000) -> dict:
    """把 payload 压到 ingest 的上限内；只裁剪展示字段，不动事件本身。"""
    out = dict(payload)
    # 序列化是这里唯一的贵操作：每轮复用上一次算出的体积，不重复 dumps。
    size = _payload_size(out)
    if size <= limit:
        return out
    out = _fit_analysis(out, limit)
    out = _fit_release_note(out, limit)
    size = _payload_size(out)
    if size <= limit:
        return out
    # 先裁最占地方的列表：changes / models / brief / evidence。
    for key in ("changes", "models", "brief", "evidence"):
        value = out.get(key)
        if not isinstance(value, list):
            continue
        while value and size > limit:
            value = value[: max(1, len(value) // 2)] if len(value) > 1 else []
            out[key] = value
            size = _payload_size(out)
        if size <= limit:
            return out
    # 再清空 diff / 长备注。
    changes = out.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict):
                change["diff"] = ""
                change["note"] = _text(change.get("note"), 300)
        size = _payload_size(out)
        if size <= limit:
            return out
    for key in ("model_note", "brief", "models"):
        out.pop(key, None)
    size = _payload_size(out)
    if size <= limit:
        return out
    # 最后兜底：逐级截短字符串，仍超限就按体积从大到小丢掉非核心键。
    for key, value in list(out.items()):
        if isinstance(value, str) and len(value) > 500:
            out[key] = value[:500]
    # 键只会被删除、体积只会变小，所以「每轮重新找最大键」等价于按首次体积降序删；
    # 每次删除后仍用真实序列化体积判断，不引入估算误差。
    order = sorted(
        (key for key in out if key not in ("analysis", "job_id", "release_note")),
        key=lambda item: len(json.dumps(out[item], ensure_ascii=False)),
        reverse=True,
    )
    for key in order:
        if _payload_size(out) <= limit:
            break
        out.pop(key, None)
    return out


def _search_text(*parts: object) -> str:
    """拼接搜索索引：每段截断后合并，总长仍受服务端 20,000 字符上限约束。

    详情页正文（what_changed / changes_detail bullets / changes.preview）也要能
    被搜到，否则用户复制页面里的文案来搜会是 0 结果（P2-50 / S-A8）。
    """
    chunks: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        chunks.append(text[:5000])
    return " ".join(chunks)[:20000]


def _change_search_text(payload: dict, changes_detail: object) -> str:
    """把 payload.changes.preview 与 changes_detail 的 bullets 拼成搜索补充文本。"""
    parts: list[str] = []
    previews = [
        _text(change.get("preview"), 500)
        for change in _as_list(payload.get("changes"))
        if isinstance(change, dict)
    ]
    previews = [text for text in previews if text]
    if previews:
        parts.append(" ".join(previews))
    bullets: list[str] = []
    for change in _as_list(changes_detail):
        if not isinstance(change, dict):
            continue
        for bullet in _as_list(change.get("bullets")):
            text = _text(bullet, 500)
            if text:
                bullets.append(text)
    if bullets:
        parts.append(" ".join(bullets))
    return " ".join(parts)


def _level_label(level: int) -> str:
    return LEVEL_LABELS.get(level, "未知")


def _minimal_cards(job: dict) -> list[dict]:
    """没有 docwatch.run 时的简版分组，保证采集器不会因为导入失败而停摆。"""
    groups: dict[str, list[dict]] = {}
    for signal in _as_list(job.get("signals")):
        if not isinstance(signal, dict):
            continue
        key = str(signal.get("page") or signal.get("asset") or "")
        groups.setdefault(key, []).append(signal)
    cards: list[dict] = []
    for page, signals in groups.items():
        bullets: list[str] = []
        for signal in signals:
            detail = _text(signal.get("detail"), 500)
            if detail and detail not in bullets:
                bullets.append(detail)
        cards.append(
            {
                "page": page,
                "level": _clamp_level(_guess_level(signals)),
                "is_asset": all(s.get("kind") in ("asset_url", "asset_content") for s in signals),
                "is_error": all(s.get("kind") == "fetch_error" for s in signals),
                "noise": False,
                "bullets": bullets[:4],
                "diff": "",
            }
        )
    cards.sort(key=lambda c: (-int(c.get("level") or 0), str(c.get("page") or "")))
    return cards


def _guess_level(signals: Iterable[dict]) -> int:
    best = 0
    for signal in signals:
        kind = str(signal.get("kind") or "")
        if kind == "new_page":
            best = max(best, 3)
        elif kind in ("gone_page", "fetch_error", "page_text"):
            best = max(best, 2)
        elif kind in ("asset_url", "asset_content", "page_html"):
            best = max(best, 1)
    return best


def _minimal_verdict(level: int, job: dict, why: str) -> dict:
    signals = [s for s in _as_list(job.get("signals")) if isinstance(s, dict)]
    details: list[str] = []
    for signal in signals[:8]:
        detail = _text(signal.get("detail"), 500)
        if detail and detail not in details:
            details.append(detail)
    return {
        "level": level,
        "level_source": "rule",
        "level_label": _level_label(level),
        "noise": level == 0,
        "headline": f"检测到 {len(signals)} 处变化（自动解读未生成）",
        "what_changed": "；".join(details) or "检测到页面变化，详细解读未生成。",
        "why_matters": "自动解读没有生成，请结合原始页面和下方变化明细判断。",
        "action": "打开原始页面核对变化。",
        "evidence": details,
        "model": "规则兜底",
        "model_note": why,
        "degraded": True,
    }


def _resolve_evidence_dir(job: dict, docwatch_root: Path) -> Path:
    raw = str(job.get("evidence_dir") or "").strip()
    if not raw:
        return Path("")
    path = Path(raw)
    if path.is_absolute():
        return path
    # docwatch 的 evidence_dir 有时是 data/jobs/<id> 这种相对路径。
    if (docwatch_root / path).exists():
        return docwatch_root / path
    return docwatch_root / "data" / path


def _load_docs_jobs(db_path: Path, limit: int = DOCS_JOB_QUERY_LIMIT) -> list[dict]:
    if not db_path.is_file():
        raise FileNotFoundError(f"docwatch 数据库不存在：{db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(v2_jobs)").fetchall()
        }
        # notified=1 表示 docwatch 已完成本轮处理（verdict / 规则兜底已就绪）；
        # 老库没有该列时按历史已处理处理，避免升级前数据全部消失。
        notified_filter = " AND COALESCE(notified, 0) = 1" if "notified" in columns else ""
        rows = con.execute(
            "SELECT id, created_at, job_json, evidence_dir, n_signals, "
            "pages_changed, assets_changed, n_errors, baseline "
            "FROM v2_jobs WHERE baseline = 0 AND n_signals > 0"
            " AND COALESCE(job_json, '') != ''" + notified_filter +
            " ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        con.close()
    jobs: list[dict] = []
    for row in rows:
        try:
            job = json.loads(row["job_json"] or "{}")
        except (TypeError, ValueError) as exc:
            print(f"[watchsite] 跳过 job {row['id']}：job_json 解析失败 {exc}", file=sys.stderr)
            continue
        if not isinstance(job, dict):
            continue
        job["id"] = row["id"]
        job["created_at"] = row["created_at"]
        job["evidence_dir"] = row["evidence_dir"] or job.get("evidence_dir") or ""
        job["n_signals"] = int(row["n_signals"] or 0)
        job["pages_changed"] = int(row["pages_changed"] or 0)
        job["assets_changed"] = int(row["assets_changed"] or 0)
        job["n_errors"] = int(row["n_errors"] or 0)
        if not isinstance(job.get("signals"), list) or not job["signals"]:
            # n_signals > 0 但 job_json 里没有 signals，说明作业不完整，不能降级成
            # “检测到 0 处变化”推上站。
            continue
        jobs.append(job)
    jobs.reverse()
    return jobs


def _load_docs_latest(db_path: Path) -> dict | None:
    """读取最近一次 docwatch 巡检（包括没有信号的轮次），用于健康度。"""
    if not db_path.is_file():
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, created_at, n_signals, n_errors, pages_total, assets_total"
            " FROM v2_jobs ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "n_signals": int(row["n_signals"] or 0),
        "n_errors": int(row["n_errors"] or 0),
        "pages_total": int(row["pages_total"] or 0),
        "assets_total": int(row["assets_total"] or 0),
    }


def _load_docs_last_success(db_path: Path, latest: dict | None = None) -> dict | None:
    """最近一次「健康」的 docwatch 巡检；失败轮次不能冒充最后成功。

    单个已下线/404 的资源每轮都会让 n_errors=1，如果只认 n_errors=0，
    状态页会在 6 小时后永久显示异常，而 docwatch 本身仍在正常采集。
    这里把「失败数不超过本轮页面+资源总数的 10%」也算健康；真正大面积失败
    仍然会退回到最近一次全清轮次。
    """
    if latest is not None:
        errors = int(latest.get("n_errors") or 0)
        total = int(latest.get("pages_total") or 0) + int(latest.get("assets_total") or 0)
        if errors == 0 or (total > 0 and errors * 10 <= total):
            return {"id": latest["id"], "created_at": latest["created_at"]}
    if not db_path.is_file():
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, created_at FROM v2_jobs WHERE COALESCE(n_errors, 0) = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    if row is None:
        return None
    return {"id": row["id"], "created_at": row["created_at"]}


def _attach_cards(verdict: dict, job: dict) -> dict:
    if _docwatch_attach_cards is not None:
        try:
            return _docwatch_attach_cards(verdict, job)
        except Exception as exc:  # noqa: BLE001
            print(f"[watchsite] attach_cards 失败，使用简版分组：{exc}", file=sys.stderr)
    verdict["cards"] = _minimal_cards(job)
    if not verdict.get("evidence"):
        verdict["evidence"] = [
            item
            for card in verdict["cards"]
            for item in _as_list(card.get("bullets"))
        ][:8]
    return verdict


def _fallback_verdict(level: int, job: dict, why: str) -> dict:
    if _docwatch_fallback is not None:
        try:
            return _docwatch_fallback(level, job, why)
        except Exception as exc:  # noqa: BLE001
            print(f"[watchsite] fallback verdict 失败：{exc}", file=sys.stderr)
    return _minimal_verdict(level, job, why)


def _change_item(
    page: str,
    level: int,
    *,
    bullets: list[str],
    is_asset: bool = False,
    is_error: bool = False,
    noise: bool = False,
    note: str = "",
    diff: str = "",
) -> dict:
    return {
        "page": page,
        "level": level,
        "is_asset": is_asset,
        "is_error": is_error,
        "noise": noise,
        "note": note,
        "bullets": bullets,
        "diff": diff,
    }


def _build_docs_analysis(verdict: dict, action: str) -> dict | None:
    """把 docwatch 的结构化判读收敛成详情页 analysis；老 verdict 没有新字段就返回 None。"""
    points = [
        _text(item, 300)
        for item in _as_list(verdict.get("tldr_points"))[:7]
        if str(item or "").strip()
    ]
    sections: list[dict] = []
    for item in _as_list(verdict.get("sections"))[:20]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"), 200)
        paragraphs = [
            _text(paragraph, 1000)
            for paragraph in _as_list(item.get("paragraphs"))[:12]
            if str(paragraph or "").strip()
        ]
        if title and paragraphs:
            sections.append({"title": title, "paragraphs": paragraphs})
    overview = _text(verdict.get("overview"), 2000)
    # 旧事件没有结构化 analysis 时，由渲染层用 summary/what_changed/impact 收敛成同一套两版模板；
    # 只有模型真的什么结构化字段都没给时才返回 None。
    if not points and verdict.get("tldr"):
        points = [_text(verdict.get("tldr"), 300)]
    if not sections and not points and not overview:
        return None
    return {
        "headline": _text(verdict.get("headline"), 300),
        "tldr": _text(verdict.get("tldr"), 300),
        "tldr_points": points,
        "overview": overview,
        "sections": sections,
        "action": action,
        "degraded": bool(verdict.get("degraded")),
    }


def _changes_from_cards(cards: list[dict]) -> list[dict]:
    changes: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        page = _text(card.get("page"), 500)
        bullets: list[str] = []
        for item in _as_list(card.get("bullets")):
            text = _text(item, 500)
            if text:
                bullets.append(text)
            if len(bullets) >= 4:
                break
        changes.append(
            _change_item(
                page,
                _clamp_level(card.get("level")),
                is_asset=bool(card.get("is_asset")),
                is_error=bool(card.get("is_error")),
                noise=bool(card.get("noise")),
                note=_text(card.get("note"), 1000),
                bullets=bullets,
                diff=_diff_limited(card.get("diff")),
            )
        )
        if len(changes) >= 20:
            break
    return changes


def _build_docs_event(job: dict, docwatch_root: Path, now_epoch: int) -> dict | None:
    job_id = str(job.get("id") or "")
    signals = [s for s in _as_list(job.get("signals")) if isinstance(s, dict)]
    evidence_dir = _resolve_evidence_dir(job, docwatch_root)
    # 证据包只保留最近 JOBS_KEEP 个。证据目录已被剪掉时，仍然用 job_json 里的
    # 规则摘要生成兜底事件：否则推送停摆超过保留窗口后，这段从未上站的变化会永久丢失。
    # 已发送过的作业由 sent_ids 过滤，不会用兜底文案覆盖线上已有解读。
    if job.get("evidence_dir") and (not evidence_dir or not evidence_dir.is_dir()):
        print(f"[watchsite] docwatch job {job_id}：证据目录已清理，按规则摘要补发",
              file=sys.stderr)
        evidence_dir = None
    verdict_path = evidence_dir / "verdict.json" if evidence_dir and str(evidence_dir) != "." else None
    verdict: dict | None = None
    if verdict_path is not None and verdict_path.is_file():
        try:
            loaded = json.loads(verdict_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                verdict = loaded
        except (OSError, ValueError) as exc:
            print(f"[watchsite] job {job_id} verdict 读取失败：{exc}", file=sys.stderr)
    fallback = verdict is None
    if fallback:
        level_info = (
            _docwatch_rules.decide(job)
            if _docwatch_rules is not None
            else {"level": _guess_level(signals)}
        )
        level = _clamp_level(level_info.get("level"))
        verdict = _fallback_verdict(level, job, "未找到可用的 verdict.json")
    level = _clamp_level(verdict.get("level"))
    verdict["level"] = level
    verdict = _attach_cards(verdict, job)
    cards = [c for c in _as_list(verdict.get("cards")) if isinstance(c, dict)]
    changes = _changes_from_cards(cards)
    changes_directory, changes_detail = _split_changes(changes)
    if not verdict.get("headline"):
        verdict["headline"] = f"检测到 {len(signals)} 处变化（自动解读未生成）"
    if not verdict.get("what_changed"):
        verdict["what_changed"] = "；".join(
            item for change in changes for item in change["bullets"]
        )[:4000] or "检测到页面变化，详细解读未生成。"
    if not verdict.get("why_matters"):
        verdict["why_matters"] = "自动解读没有生成，请结合原始页面和下方变化明细判断。"
    if not verdict.get("action"):
        verdict["action"] = "打开原始页面核对变化。"
    page_url = ""
    for change in changes:
        if not change.get("is_asset") and not change.get("noise") and _http_url(change.get("page")):
            page_url = _http_url(change.get("page"))
            break
    if not page_url:
        for signal in signals:
            candidate = _http_url(signal.get("page"))
            if candidate:
                page_url = candidate
                break
    page_url = page_url or DOCS_HOME
    evidence = _evidence_list(verdict.get("evidence"))
    if not evidence:
        evidence = [item for change in changes for item in change["bullets"]][:8]
    title = _text(verdict.get("headline"), 300) or f"检测到 {len(signals)} 处变化"
    # 省流句优先用判读输出的 tldr；老 verdict / 兜底解读没有 tldr 时回退 what_changed 前 300 字。
    summary = _text(verdict.get("tldr") or verdict.get("what_changed"), 300)
    what_changed = _text(verdict.get("what_changed"), 4000)
    impact = _text(verdict.get("why_matters"), 2000)
    action = _text(verdict.get("action"), 2000)
    # P2-15：缺失 created_at 时用稳定兜底 0，不能用 now（死信签名会每轮漂移）。
    changed_ts = _epoch(job.get("created_at"), 0)
    # 事件 id / slug 只由 job_id 决定：同一个作业无论有没有 verdict.json，
    # 都必须落在同一条记录上。降级信息走 payload.degraded，不再改 id ——
    # 否则同一变化会先以 :fallback 入库、补上解读后又以正式 id 入库一次，
    # 站点上出现两条（其中一条永久 degraded）。
    event_id = f"docwatch:{job_id}"
    analysis = _build_docs_analysis(verdict, action)
    payload = {
        "job_id": job_id,
        "pages_changed": int(job.get("pages_changed") or 0),
        "assets_changed": int(job.get("assets_changed") or 0),
        "signal_count": int(job.get("n_signals") or len(signals)),
        "what_changed": what_changed,
        "changes": changes_directory,
        "model": _text(verdict.get("model"), 100),
        "model_note": _text(verdict.get("model_note"), 2000),
        "degraded": bool(verdict.get("degraded")),
        "level_source": _text(verdict.get("level_source"), 100),
    }
    if analysis:
        payload["analysis"] = analysis
    payload = _fit_payload(payload)
    analysis_text = ""
    fitted_analysis = payload.get("analysis")
    if isinstance(fitted_analysis, dict):
        # 用裁剪后真正会展示的 analysis 建搜索文本：不把已裁掉的内容留在搜索索引里。
        analysis_text = " ".join(
            [fitted_analysis.get("overview", ""), fitted_analysis.get("tldr", "")]
            + fitted_analysis.get("tldr_points", [])
            + [
                text
                for section in fitted_analysis.get("sections", [])
                for text in [section.get("title", "")] + section.get("paragraphs", [])
                if text
            ]
        )
    search_text = _search_text(
        "官方文档",
        title,
        summary,
        what_changed,
        impact,
        action,
        analysis_text,
        " ".join(item for change in changes for item in change["bullets"]),
        _change_search_text(payload, changes_detail),
    )
    return {
        "id": event_id,
        "slug": _slugify(f"docwatch-{job_id}", "docwatch-event"),
        "source": "docs",
        "kind": "docwatch",
        "level": level,
        "level_label": _level_label(level),
        "title": title,
        "summary": summary,
        "impact": impact,
        "action": action,
        "signal_count": int(job.get("n_signals") or len(signals)),
        "changed_ts": changed_ts,
        "detected_ts": now_epoch,
        "page_url": page_url,
        "archive_url": "",
        "evidence": evidence,
        "changes_detail": changes_detail,
        "payload": payload,
        "search_text": search_text,
    }


def _monitor_changes_detail(payload: dict, level: int) -> list[dict]:
    """把 webwatch 的 payload.changes 转成 /changes 页可展示的分组。

    webwatch 没有逐行 diff，但有 commit / assets / text 三类摘要；不转换的话
    详情页会显示「查看完整对比」按钮，点进去却是空页面。
    """
    changes = payload.get("changes") if isinstance(payload.get("changes"), list) else []
    detail: list[dict] = []
    for change in changes[:20]:
        if not isinstance(change, dict):
            continue
        kind = str(change.get("kind") or "")
        bullets: list[str] = []
        if kind == "commit":
            page = "前端版本"
            bullets.append(f"{change.get('old') or '（无）'} → {change.get('new') or '（无）'}")
        elif kind == "assets":
            page = "脚本资源"
            for label, key in (("新增", "added"), ("移除", "removed"), ("变化", "changed")):
                values = [str(item) for item in (change.get(key) or []) if str(item).strip()]
                if values:
                    bullets.append(f"{label}：{'、'.join(values[:8])}")
        elif kind == "text":
            page = "页面正文"
            preview = _text(change.get("preview"), 500)
            if preview:
                bullets.append(preview)
        else:
            page = kind or "页面变化"
            preview = _text(change.get("preview"), 500)
            if preview:
                bullets.append(preview)
        if not bullets:
            continue
        detail.append(_change_item(page, level, is_asset=kind == "assets", bullets=bullets))
    return detail


def _web_changes_detail(raw: object, level: int) -> list[dict]:
    """webwatch 新版事件自带 changes_detail（含证据分组），优先原样透传。"""
    items = _as_list(raw)
    if not items:
        return []
    changes: list[dict] = []
    for item in items[:40]:
        if not isinstance(item, dict):
            continue
        bullets: list[str] = []
        for value in _as_list(item.get("bullets")):
            text = _text(value, 500)
            if text:
                bullets.append(text)
        if not bullets:
            continue
        changes.append(
            _change_item(
                _text(item.get("page"), 500),
                _clamp_level(
                    level if item.get("level") is None else item.get("level")
                ),
                is_asset=bool(item.get("is_asset")),
                is_error=bool(item.get("is_error")),
                noise=bool(item.get("noise")),
                note=_text(item.get("note"), 1000),
                bullets=bullets[:8],
                diff=_diff_limited(item.get("diff"), MAX_DIFF_CHARS),
            )
        )
    _, detail = _split_changes(changes)
    return detail[:20]


def _normalize_monitor_event(
    raw: object, now_epoch: int, changed_ts_fallback: int = 0
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source") or "")
    if source not in WEB_SOURCES:
        return None
    event_id = _text(raw.get("id"), 220)
    if not event_id:
        return None
    level = _clamp_level(raw.get("level"))
    title = _text(raw.get("title"), 300) or "检测到变化"
    # webwatch 事件没有 tldr：summary 收紧到 300 字做省流句，全文同时放进 payload.what_changed。
    full_summary = _text(raw.get("summary"), 4000)
    summary = _text(raw.get("tldr") or raw.get("summary"), 300)
    impact = _text(raw.get("impact"), 2000)
    action = _text(raw.get("action"), 2000)
    evidence = _evidence_list(raw.get("evidence"))
    payload = _as_dict(raw.get("payload"))
    if not payload.get("what_changed") and full_summary:
        payload["what_changed"] = full_summary
    payload = _fit_payload(payload)
    # P2-15：缺失 changed_ts 时不能用 now（每轮都变，死信签名永远累计不到阈值）。
    # 文件读取方传文件的 mtime；直接调用时用 0，保证同一内容每轮签名一致。
    changed_ts = _epoch(
        raw.get("changed_ts") or raw.get("changed_at"), changed_ts_fallback
    )
    # 服务端硬要求 ^[a-z0-9-]+$；这里统一 slugify，不能把 webwatch 的原始
    # slug（可能含大写/下划线）原样透传，否则该事件会被永久拒收。
    slug = _slugify(_text(raw.get("slug"), 120) or f"{source}-{event_id}", "web-event")
    detail_changes = _web_changes_detail(raw.get("changes_detail"), level) or _monitor_changes_detail(payload, level)
    search_text = _search_text(
        SOURCE_LABELS.get(source, source),
        title,
        summary,
        payload.get("what_changed"),
        full_summary,
        impact,
        action,
        " ".join(evidence),
        _change_search_text(payload, detail_changes),
    )
    return {
        "id": event_id,
        "slug": slug,
        "source": source,
        "kind": _text(raw.get("kind"), 100) or "page_change",
        "level": level,
        "level_label": _text(raw.get("level_label"), 25) or _level_label(level),
        "title": title,
        "summary": summary,
        "impact": impact,
        "action": action,
        "signal_count": max(1, int(raw.get("signal_count") or 1)),
        "changed_ts": changed_ts,
        "detected_ts": now_epoch,
        "page_url": _http_url(raw.get("page_url")),
        "archive_url": _http_url(raw.get("archive_url")),
        "evidence": evidence,
        "changes_detail": detail_changes,
        "payload": payload,
        "search_text": search_text,
    }


def _load_monitor_index(events_dir: Path) -> dict[str, dict]:
    """读取事件目录索引；索引不存在、版本不符或目录变了都回退全量读取。"""
    data = _push_state.load_json(MONITOR_INDEX_FILE, {})
    if not isinstance(data, dict) or data.get("version") != MONITOR_INDEX_VERSION:
        return {}
    if str(data.get("dir") or "") != str(events_dir.resolve()):
        return {}
    files = data.get("files")
    if not isinstance(files, dict):
        return {}
    return {str(name): entry for name, entry in files.items() if isinstance(entry, dict)}


def _read_monitor_events(
    events_dir: Path,
    now_epoch: int,
    *,
    sent_ids: Iterable[str] | None = None,
    max_normalize_bytes: int | None = None,
) -> dict:
    """读取事件目录，只完整归一化新增 / 变更的文件（P1-05 / B-6）。

    索引键是文件名，值记录 ``(mtime_ns, size, id, source, changed_ts)``。
    已发送且未变化的文件不再解析，只回填轻量摘要供状态页计算 last_event_ts。
    没有索引（首次运行 / 状态目录不可写）时自然退化为全量读取；单轮解析量
    受字节预算约束，超出的文件记入 ``errors`` 并留到下一轮，不静默丢弃。
    """
    if not events_dir.is_dir():
        return {"events": [], "known": [], "errors": [], "stats": {"parsed": 0}}
    sent = {str(item) for item in (sent_ids or [])}
    previous = _load_monitor_index(events_dir)
    paths = sorted(events_dir.glob("*.json"))
    if max_normalize_bytes is None:
        raw_budget = os.getenv("WATCHSITE_MAX_NORMALIZE_BYTES", "").strip()
        try:
            budget = int(raw_budget) if raw_budget else DEFAULT_MAX_NORMALIZE_BYTES
        except ValueError:
            budget = DEFAULT_MAX_NORMALIZE_BYTES
    else:
        budget = max(0, int(max_normalize_bytes))
    budget = max(0, budget)
    used = 0
    parsed = 0
    events: list[dict] = []
    known: list[dict] = []
    errors: list[dict] = []
    deferred: list[str] = []
    index: dict[str, dict] = {}
    must_parse: list[tuple[Path, os.stat_result]] = []
    reparse: list[tuple[Path, os.stat_result]] = []

    def normalize(path: Path, stat_result: os.stat_result) -> None:
        nonlocal used, parsed
        size = max(1, int(stat_result.st_size))
        # 保证每轮至少处理一个文件：单个超大文件不能因为预算而永久饿死。
        if used and used + size > budget:
            deferred.append(path.name)
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            event = _normalize_monitor_event(raw, now_epoch, int(stat_result.st_mtime))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as exc:
            # P3-03：坏事件文件必须进入 collection_errors（文件名 + 原因），不能只 print。
            errors.append(
                {
                    "source": "watchsite",
                    "detail": _text(
                        f"事件文件 {path.name} 解析失败：{type(exc).__name__}: {exc}", 500
                    ),
                    "file": path.name,
                }
            )
            index[path.name] = {
                "mtime_ns": stat_result.st_mtime_ns,
                "size": stat_result.st_size,
                "bad": type(exc).__name__,
            }
            return
        if event is None:
            errors.append(
                {
                    "source": "watchsite",
                    "detail": _text(
                        f"事件文件 {path.name} 不是有效事件（缺少 id 或 source 非法）", 500
                    ),
                    "file": path.name,
                }
            )
            index[path.name] = {
                "mtime_ns": stat_result.st_mtime_ns,
                "size": stat_result.st_size,
                "bad": "invalid",
            }
            return
        used += size
        parsed += 1
        events.append(event)
        index[path.name] = {
            "mtime_ns": stat_result.st_mtime_ns,
            "size": stat_result.st_size,
            "id": str(event["id"]),
            "source": str(event["source"]),
            "changed_ts": int(event.get("changed_ts") or 0),
        }

    for path in paths:
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            # 与 webwatch 的 prune 并发时会遇到；文件已不存在，不算坏事件。
            continue
        except OSError as exc:
            errors.append(
                {
                    "source": "watchsite",
                    "detail": _text(f"事件文件 {path.name} 无法读取状态：{exc}", 500),
                    "file": path.name,
                }
            )
            continue
        entry = previous.get(path.name)
        unchanged = (
            isinstance(entry, dict)
            and entry.get("mtime_ns") == stat_result.st_mtime_ns
            and entry.get("size") == stat_result.st_size
        )
        if unchanged:
            if entry.get("bad"):
                # 坏文件内容没变：上一轮已报过，避免每 5 分钟重复告警。
                index[path.name] = entry
                continue
            event_id = str(entry.get("id") or "")
            if event_id and event_id in sent:
                # 已发送事件的轻量缓存：不读文件、不归一化。
                known.append(
                    {
                        "id": event_id,
                        "source": str(entry.get("source") or ""),
                        "changed_ts": int(entry.get("changed_ts") or 0),
                        "_cached": True,
                    }
                )
                index[path.name] = entry
                continue
            # 没发出去（push 失败 / 状态丢失）：仍需完整事件，排在新增文件之后。
            reparse.append((path, stat_result))
        else:
            must_parse.append((path, stat_result))

    for path, stat_result in must_parse:
        normalize(path, stat_result)
    for path, stat_result in reparse:
        normalize(path, stat_result)

    if deferred:
        preview = "、".join(deferred[:5])
        suffix = f" 等 {len(deferred)} 个" if len(deferred) > 5 else ""
        errors.append(
            {
                "source": "watchsite",
                "detail": _text(
                    f"事件归一化超出单轮字节预算（{used}/{budget} 字节），"
                    f"本轮延后 {len(deferred)} 个文件：{preview}{suffix}",
                    500,
                ),
            }
        )
    if index != previous:
        _push_state.save_json(
            MONITOR_INDEX_FILE,
            {
                "version": MONITOR_INDEX_VERSION,
                "dir": str(events_dir.resolve()),
                "files": index,
            },
        )
    return {
        "events": events,
        "known": known,
        "errors": errors,
        "stats": {
            "parsed": parsed,
            "known": len(known),
            "deferred": len(deferred),
            "bytes": used,
        },
    }


def _monitor_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


MAX_MODELS_SNAPSHOT = 50


def _models_snapshot(state: dict) -> dict:
    """把 webwatch 的模型目录状态整理成网站可直接展示的快照。

    ``/models`` 是免费接口，后台每 5 分钟查一次；这里只读取 state，
    不额外调用 API。老版本 state 没有 catalog_first_seen 时用目录检查
    时间兜底，页面仍能显示“首次记录”，不会空一排。

    P2-19：同时带出目录检查的连续失败次数、残页保护次数和脱敏错误，
    站点状态页不用直接读 webwatch 的 state 文件。
    """
    models = _as_dict(state.get("models"))
    catalog = [
        str(item).strip()
        for item in _as_list(models.get("catalog"))
        if str(item).strip()
    ]
    versions = _as_dict(models.get("versions"))
    fingerprints = _as_dict(models.get("fingerprints"))
    first_seen = _as_dict(models.get("catalog_first_seen"))
    versions_first_seen = _as_dict(models.get("versions_first_seen"))
    checked_at = _text(models.get("catalog_checked_at"), 40) or _text(
        models.get("versions_checked_at"), 40
    )
    catalog_set = set(catalog)
    ids = list(
        dict.fromkeys(
            catalog + [str(item).strip() for item in versions if str(item).strip()]
        )
    )
    rows: list[dict] = []
    for model_id in ids[:MAX_MODELS_SNAPSHOT]:
        fingerprint = _as_dict(fingerprints.get(model_id))
        rows.append(
            {
                "id": _text(model_id, 120),
                "version": _text(versions.get(model_id), 200),
                "fingerprint": _text(
                    fingerprint.get("readable") or fingerprint.get("value"), 200
                ),
                "fingerprint_checked_at": _text(fingerprint.get("checked_at"), 40),
                "first_seen": _text(
                    first_seen.get(model_id) or versions_first_seen.get(model_id) or checked_at,
                    40,
                ),
                "in_catalog": model_id in catalog_set,
            }
        )
    change = _as_dict(models.get("last_catalog_change")) or _as_dict(
        models.get("last_versions_change")
    )
    last_change = None
    if change:
        last_change = {
            "at": _text(change.get("at"), 40),
            "added": [_text(item, 120) for item in _as_list(change.get("added"))][:20],
            "removed": [_text(item, 120) for item in _as_list(change.get("removed"))][:20],
        }
    return {
        "checked_at": checked_at,
        # D2/P2-19：目录失败信息随快照上站，状态页会显示“连续失败 N 次”和脱敏错误。
        # checked_at 仍是最后一次成功检查时间；catalog_checked_at 与它同义，
        # 单独保留是为了让新消费方不必猜旧字段的含义。
        "catalog_checked_at": checked_at,
        "catalog_available": bool(catalog),
        "catalog_last_error": _text(models.get("catalog_last_error"), 500),
        "catalog_fail_streak": max(0, _int_or(models.get("catalog_fail_streak"), 0)),
        "catalog_incomplete_streak": max(
            0, _int_or(models.get("catalog_incomplete_streak"), 0)
        ),
        "models": rows,
        "last_change": last_change,
    }


def _age_status(last_ts: int, now_epoch: int, fail_streak: int = 0) -> str:
    # 最近一次失败也要显示警告：连续挂三次报错，但不能因为上次成功还新就显示正常。
    if fail_streak >= 3:
        return "error"
    if not last_ts:
        return "warn" if fail_streak else "unknown"
    age = max(0, now_epoch - last_ts)
    if age > 6 * 3600:
        return "error"
    if age > 2 * 3600:
        return "warn"
    if fail_streak:
        return "warn"
    return "ok"


def _status_row(
    source: str,
    last_checked: int,
    last_success: int,
    last_event: int,
    status: str,
    detail: str,
    now_epoch: int,
) -> dict:
    return {
        "source": source,
        "label": SOURCE_LABELS.get(source, source),
        "last_checked_ts": last_checked or None,
        "last_success_ts": last_success or None,
        "last_event_ts": last_event or None,
        "status": status,
        "detail": detail[:500],
        "updated_ts": now_epoch,
    }


def _docs_status(
    jobs: list[dict],
    events: list[dict],
    now_epoch: int,
    latest: dict | None = None,
    last_success: dict | None = None,
) -> dict:
    last = latest or (jobs[-1] if jobs else None)
    if not last:
        return _status_row("docs", 0, 0, 0, "unknown", "还没有采集记录", now_epoch)
    last_ts = _epoch(last.get("created_at"), 0)
    errors = int(last.get("n_errors") or 0)
    success = last_success
    if success is None and errors == 0:
        success = last
    if success is None:
        success = next(
            (job for job in reversed(jobs) if int(job.get("n_errors") or 0) == 0),
            None,
        )
    success_ts = _epoch(success.get("created_at"), 0) if success else 0
    status = _age_status(success_ts, now_epoch, 1 if errors else 0)
    if not success_ts and errors:
        # 从未成功、最近一轮又报错，不能显示成「待采集」。
        status = "error"
    elif errors and status == "ok":
        status = "warn"
    detail = (
        f"最后检查 #{last.get('id')} · {_iso_from_epoch(last_ts) if last_ts else '时间未知'}"
        f" · 变化 {int(last.get('n_signals') or 0)} 处"
    )
    if success:
        success_stamp = _iso_from_epoch(success_ts)
        detail += f" · 最后成功 #{success.get('id')} · {success_stamp}"
    else:
        detail += " · 还没有成功记录"
    if errors:
        detail += f" · 抓取错误 {errors} 个"
    last_event = max(
        [int(e.get("changed_ts") or 0) for e in events]
        + [_epoch(job.get("created_at"), 0) for job in jobs
           if int(job.get("n_signals") or 0) > 0],
        default=0,
    )
    return _status_row("docs", last_ts, success_ts, last_event, status, detail, now_epoch)


def _monitor_status(
    state: dict,
    source: str,
    events: list[dict],
    now_epoch: int,
) -> dict:
    """按来源生成状态行；每个来源独立记录 last_checked / last_success / fail_streak。"""
    sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    entry = sources.get(source) if isinstance(sources.get(source), dict) else {}
    if not entry:
        return _status_row(source, 0, 0, 0, "unknown", "还没有自建监控状态", now_epoch)
    last_ok = _epoch(entry.get("last_success"), 0)
    last_checked = _epoch(entry.get("last_checked"), 0)
    fail_streak = _int_or(entry.get("fail_streak"), 0)
    status = _age_status(last_ok, now_epoch, fail_streak)
    detail = f"最后成功：{_iso_from_epoch(last_ok) if last_ok else '未知'}"
    if fail_streak:
        detail += f"；连续失败 {fail_streak} 次"
    if entry.get("last_error"):
        detail += f"；最近错误：{_text(entry.get('last_error'), 200)}"
    last_event = max(
        (int(e.get("changed_ts") or 0) for e in events if e.get("source") == source),
        default=0,
    )
    return _status_row(
        source,
        last_checked,
        last_ok,
        last_event,
        status,
        detail,
        now_epoch,
    )


def build_batch(
    docwatch_db: Path,
    docwatch_root: Path,
    monitor_events_dir: Path,
    monitor_state_path: Path,
    now: dt.datetime | None = None,
    sent_ids: Iterable[str] | None = None,
    max_normalize_bytes: int | None = None,
) -> dict:
    """生成一次推送所需的 events / statuses / meta。

    ``sent_ids`` 在构建事件前就传进来：已发送的 docwatch 作业不再读证据包 /
    verdict.json；已发送且未变化的 webwatch 事件文件也只走轻量索引。

    注意（P3-02 现状，2026-11-18）：docwatch 修订不会自动重推。docwatch 的
    ``v2_jobs`` 只有 created_at、没有 updated_at，watchsite 又只读、不能给
    docwatch 加列；因此这里只能按 id 跳过已发送作业。verdict.json 事后被修正
    时不会产生新事件；需要重推时得手工从 push-state 的 sent_ids 删除对应
    ``docwatch:<id>``（这是当前契约，不在本次改动里改）。
    """
    now_dt = now or _now()
    now_epoch = int(now_dt.timestamp())
    events: list[dict] = []
    statuses: list[dict] = []
    sent = {str(item) for item in (sent_ids or [])}

    latest: dict | None = None
    docs_last_success: dict | None = None
    collection_errors: list[dict] = []
    try:
        jobs = _load_docs_jobs(docwatch_db)
        latest = _load_docs_latest(docwatch_db)
        docs_last_success = _load_docs_last_success(docwatch_db, latest)
    except Exception as exc:  # noqa: BLE001
        jobs = []
        statuses.append(
            _status_row("docs", 0, 0, 0, "error", f"读取 docwatch 失败：{type(exc).__name__}", now_epoch)
        )
        # 数据库文件还不存在是全新安装的常态，状态页会显示 docs=error，
        # 但不必每 5 分钟发一次「推送失败」邮件；文件存在却读不了才是故障。
        if not isinstance(exc, FileNotFoundError):
            collection_errors.append(
                {"source": "docs", "detail": f"读取 docwatch 失败：{type(exc).__name__}"}
            )
        print(f"[watchsite] 读取 docwatch 失败：{exc}", file=sys.stderr)
    docs_events: list[dict] = []
    for job in jobs:
        if f"docwatch:{job.get('id')}" in sent:
            # P3-02：已发送的 docwatch 作业直接跳过，修订不重推（见 build_batch 文档）。
            continue
        try:
            event = _build_docs_event(job, docwatch_root, now_epoch)
        except Exception as exc:  # noqa: BLE001
            print(f"[watchsite] 跳过 docwatch job {job.get('id')}：{exc}", file=sys.stderr)
            continue
        if event is not None:
            docs_events.append(event)
    events.extend(docs_events)
    if jobs or latest:
        statuses.append(_docs_status(jobs, docs_events, now_epoch, latest, docs_last_success))
    elif not any(item.get("source") == "docs" for item in statuses):
        statuses.append(_status_row("docs", 0, 0, 0, "unknown", "还没有采集记录", now_epoch))

    monitor_events: list[dict] = []
    monitor_known: list[dict] = []
    monitor_dir_error = ""
    try:
        if not monitor_events_dir.is_dir():
            raise FileNotFoundError(f"事件目录不存在：{monitor_events_dir}")
        monitor_read = _read_monitor_events(
            monitor_events_dir,
            now_epoch,
            sent_ids=sent,
            max_normalize_bytes=max_normalize_bytes,
        )
        monitor_events = monitor_read["events"]
        monitor_known = monitor_read["known"]
        collection_errors.extend(monitor_read["errors"])
    except Exception as exc:  # noqa: BLE001
        detail = f"读取自建监控事件失败：{type(exc).__name__}: {exc}"
        collection_errors.append({"source": "watchsite", "detail": _text(detail, 300)})
        monitor_dir_error = _text(detail, 300)
        print(f"[watchsite] {detail}", file=sys.stderr)
    events.extend(monitor_events)
    monitor_state = _monitor_state(monitor_state_path)
    # 状态页只需要 changed_ts；把已发送事件的轻量缓存也带上，避免为算时间再解析全文。
    monitor_status_events = monitor_known + monitor_events
    for source in WEB_SOURCES:
        if monitor_dir_error:
            # 事件目录缺失/不可读时，webwatch 的 4 个来源不能沿用 state 里的「最近成功」假健康。
            # 只认事件目录自身错误，不能把 docwatch 的读取错误也算到网页来源头上。
            statuses.append(
                _status_row(source, 0, 0, 0, "error", monitor_dir_error, now_epoch)
            )
        else:
            statuses.append(
                _monitor_status(monitor_state, source, monitor_status_events, now_epoch)
            )

    # 事件去重：同一个 id 只保留最后读到的一条。轻量缓存只参与计数 / 状态，
    # 不能进入待推送事件（它没有 payload，推上去会变成空事件）。
    unique: dict[str, dict] = {}
    for event in events:
        unique[str(event["id"])] = event
    for light in monitor_known:
        unique.setdefault(str(light["id"]), light)
    ordered_all = sorted(
        unique.values(),
        key=lambda e: (int(e.get("changed_ts") or 0), str(e.get("id"))),
    )
    ordered = [event for event in ordered_all if not event.get("_cached")]
    counts = {source: 0 for source in ALL_SOURCES}
    for event in ordered_all:
        counts[event["source"]] = counts.get(event["source"], 0) + 1
    meta = {
        "generated_at": _iso(now_dt),
        "last_push_ts": _iso(now_dt),
        "event_count": len(ordered_all),
        "source_counts": counts,
        "collection_errors": collection_errors,
    }
    models_snapshot = _models_snapshot(monitor_state)
    # 只有确实读到过 /models 的完整目录才带上快照。状态文件丢失、回滚或
    # 仅抓到价格页子集时不能覆盖线上已有的好快照。
    if models_snapshot["catalog_available"] and models_snapshot["models"]:
        meta["models_snapshot"] = models_snapshot
    return {"events": ordered, "statuses": statuses, "meta": meta}


def default_paths() -> dict[str, Path]:
    return {
        "docwatch_db": Path(os.getenv("DOCWATCH_DB", "/opt/docwatch-v2/data/docs.db")),
        "docwatch_root": Path(os.getenv("DOCWATCH_ROOT", "/opt/docwatch-v2")),
        "monitor_events_dir": Path(
            os.getenv("WEBWATCH_EVENTS_DIR", "/var/lib/deepseek-webwatch/events")
        ),
        "monitor_state_path": Path(
            os.getenv("WEBWATCH_STATE", "/var/lib/deepseek-webwatch/state.json")
        ),
    }
