"""verdict.json 的读取、校验与归一化。

任何不合规都抛 `VerdictError`；worker 会把这次复核记为失败并重试。
校验只依赖标准库，不访问网络、不读生产数据。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import recheck_config as cfg

VERDICT_VALUES = ("confirmed", "contradicted", "uncertain")
CONFIDENCE_VALUES = ("high", "medium", "low")
QUALITY_VALUES = ("ok", "internal", "partial", "repaired")
FORBIDDEN_PUBLIC_TERMS = (
    "i18n", "commit", "chunk", "manifest", "payload", "sha256", "webpack", "bundle",
    "分片", "哈希", "字节", "构建产物", "沙箱", "verdict", "job 目录",
)
# 只拦内部路由/供应商标识；公开产品名（如页面上真的出现 DeepSeek V4）不拦。
MODEL_NAME_RE = re.compile(
    r"(example-provider|opencode|llm|example-model-b|group/|deepseek[-_ ]v4\.1[-_ ]flash)",
    re.I,
)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class VerdictError(ValueError):
    """verdict 不合规；消息会写进队列 last_error / 日志。"""


def _text(value, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _text_list(value, *, limit: int, item_chars: int = cfg.MAX_TEXT_ITEM_CHARS) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item, item_chars)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _public_text_problem(text: str, label: str) -> str:
    lowered = str(text or "").lower()
    if CONTROL_RE.search(lowered):
        return f"{label} 含控制字符"
    for term in FORBIDDEN_PUBLIC_TERMS:
        if term.lower() in lowered:
            return f"{label} 含内部术语「{term}」"
    match = MODEL_NAME_RE.search(lowered)
    if match:
        return f"{label} 含模型/内部名称「{match.group(0)}」"
    return ""


def _allowed_host(host: str, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return False
    for item in allowed:
        item = item.lower().strip(".")
        if host == item or host.endswith("." + item):
            return True
    return False


def _validate_url(url: str, allowed: tuple[str, ...]) -> str:
    text = str(url or "").strip()
    if not text:
        raise VerdictError("evidence.url 为空")
    if CONTROL_RE.search(text):
        raise VerdictError("evidence.url 含控制字符")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise VerdictError(f"evidence.url 必须是 http(s)：{text[:120]}")
    if not text.startswith(("http://", "https://")):
        raise VerdictError(f"evidence.url 不是完整地址：{text[:120]}")
    if parts.username or parts.password:
        raise VerdictError("evidence.url 不允许包含账号密码")
    host = parts.hostname or ""
    if not _allowed_host(host, allowed):
        raise VerdictError(f"evidence.url 不在允许域名：{host or '（空）'}")
    if parts.port not in (None, 80, 443):
        raise VerdictError(f"evidence.url 端口不允许：{parts.port}")
    return text


def _evidence(value, allowed: tuple[str, ...]) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise VerdictError("evidence 必须是数组")
    out: list[dict] = []
    for item in value:
        if isinstance(item, str):
            raise VerdictError("evidence 每条必须是 {url, finding}")
        if not isinstance(item, dict):
            raise VerdictError("evidence 每条必须是对象")
        url = _validate_url(item.get("url"), allowed)
        finding = _text(item.get("finding"), 500)
        if len(finding) < 4:
            raise VerdictError("evidence.finding 太短，必须写具体发现")
        if CONTROL_RE.search(finding):
            raise VerdictError("evidence.finding 含控制字符")
        out.append({"url": url, "finding": finding})
        if len(out) >= cfg.MAX_EVIDENCE_ITEMS:
            break
    if not out:
        raise VerdictError("evidence 至少需要 1 条")
    return out


def _level_suggestion(value, current_level: int) -> dict:
    if not isinstance(value, dict):
        raise VerdictError("缺少 level_suggestion 对象")
    # 任务书用 suggested_level；模型也可能按示例写 suggested。两者都收，
    # 统一归一化成 suggested，写回和渲染只认一个字段。
    raw = value.get("suggested_level", value.get("suggested"))
    if isinstance(raw, bool):
        raise VerdictError("level_suggestion.suggested_level 不能是布尔值")
    try:
        suggested = int(raw)
    except (TypeError, ValueError):
        raise VerdictError("level_suggestion.suggested_level 必须是 0-3 的整数") from None
    if not 0 <= suggested <= 3:
        raise VerdictError("level_suggestion.suggested_level 必须是 0-3 的整数")
    reason = _text(value.get("reason"), 300)
    if not reason:
        raise VerdictError("level_suggestion.reason 不能为空")
    problem = _public_text_problem(reason, "level_suggestion.reason")
    if problem:
        raise VerdictError(problem)
    evidence = _text_list(value.get("evidence"), limit=6, item_chars=300)
    if suggested != current_level and not evidence:
        raise VerdictError("改变级别建议时必须给出 level_suggestion.evidence")
    return {
        "suggested": suggested,
        "suggested_level": suggested,
        "reason": reason,
        "evidence": evidence,
    }


def _optional_text(obj: dict, key: str, limit: int) -> str:
    text = _text(obj.get(key), limit)
    if not text:
        return ""
    problem = _public_text_problem(text, key)
    if problem:
        raise VerdictError(problem)
    return text


def validate_verdict(
    data,
    *,
    event: dict,
    allowed_hosts: tuple[str, ...] | None = None,
) -> dict:
    """校验并归一化 verdict；返回只含白名单字段的 dict。"""
    if not isinstance(data, dict):
        raise VerdictError("verdict.json 必须是 JSON 对象")
    event_id = _text(data.get("event_id"), 220)
    expected_id = _text(event.get("id"), 220)
    if not event_id or event_id != expected_id:
        raise VerdictError(f"event_id 不一致：{event_id!r} != {expected_id!r}")

    verdict = _text(data.get("verdict"), 20).lower()
    if verdict not in VERDICT_VALUES:
        raise VerdictError(f"verdict 非法：{verdict!r}")
    confidence = _text(data.get("confidence"), 20).lower()
    if confidence not in CONFIDENCE_VALUES:
        raise VerdictError(f"confidence 非法：{confidence!r}")
    quality = _text(data.get("quality"), 20).lower()
    if quality not in QUALITY_VALUES:
        raise VerdictError(f"quality 非法：{quality!r}")

    if verdict == "uncertain" and quality != "partial":
        raise VerdictError("uncertain 时 quality 必须是 partial")
    if verdict == "contradicted" and quality != "repaired":
        raise VerdictError("contradicted 时 quality 必须是 repaired")
    if verdict == "confirmed" and quality not in ("ok", "internal", "partial"):
        raise VerdictError("confirmed 时 quality 只能是 ok/internal/partial")

    try:
        current_level = max(0, min(3, int(event.get("level") or 0)))
    except (TypeError, ValueError):
        current_level = 0
    level_suggestion = _level_suggestion(data.get("level_suggestion"), current_level)

    title = _optional_text(data, "title", cfg.MAX_TITLE_CHARS)
    summary = _optional_text(data, "summary", cfg.MAX_SUMMARY_CHARS)
    impact = _optional_text(data, "impact", cfg.MAX_IMPACT_CHARS)
    action = _optional_text(data, "action", cfg.MAX_ACTION_CHARS)
    if verdict == "uncertain" and any((title, summary, impact, action)):
        raise VerdictError("uncertain 时不允许改标题/摘要/影响/建议")

    reason = _text(data.get("reason"), cfg.MAX_REASON_CHARS)
    if not reason:
        raise VerdictError("reason 不能为空")
    problem = _public_text_problem(reason, "reason")
    if problem:
        raise VerdictError(problem)
    basis = _text(data.get("basis"), 240) or reason[:240]
    problem = _public_text_problem(basis, "basis")
    if problem:
        # basis 会显示在详情页；含内部术语时退回 reason，不因此作废整次复核。
        basis = reason[:240]

    evidence = _evidence(data.get("evidence"), allowed_hosts or cfg.allowed_hosts())
    checks = _text_list(data.get("checks"), limit=cfg.MAX_TEXT_LIST_ITEMS)
    limitations = _text_list(data.get("limitations"), limit=cfg.MAX_TEXT_LIST_ITEMS)
    major = bool(data.get("major_misjudgement"))
    if major and len(evidence) < 2:
        raise VerdictError("major_misjudgement=true 时至少需要 2 条独立证据")

    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    current_quality = _text(payload.get("quality"), 20)
    if verdict == "contradicted":
        changed = any((title, summary, impact, action))
        changed = changed or quality != current_quality
        changed = changed or level_suggestion["suggested"] != current_level
        if not changed:
            raise VerdictError("contradicted 但没有任何修正或级别建议差异")

    return {
        "event_id": event_id,
        "verdict": verdict,
        "confidence": confidence,
        "quality": quality,
        "title": title,
        "summary": summary,
        "impact": impact,
        "action": action,
        "reason": reason,
        "basis": basis,
        "level_suggestion": level_suggestion,
        "major_misjudgement": major,
        "evidence": evidence,
        "checks": checks,
        "limitations": limitations,
    }


def load_verdict(
    path: Path,
    *,
    event: dict,
    allowed_hosts: tuple[str, ...] | None = None,
    max_bytes: int = cfg.VERDICT_MAX_BYTES,
) -> dict:
    """读取文件并校验；文件不存在/过大/非法 JSON 都抛 VerdictError。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise VerdictError(f"读取 verdict.json 失败：{exc}") from exc
    if stat.st_size > max_bytes:
        raise VerdictError(f"verdict.json 过大：{stat.st_size} > {max_bytes}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerdictError(f"verdict.json 不是合法 JSON：{exc}") from exc
    return validate_verdict(data, event=event, allowed_hosts=allowed_hosts)
