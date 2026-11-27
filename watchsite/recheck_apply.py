"""把合法 verdict 应用到事件：纯函数、字段白名单、原值保留。

约束（任务书 §8）：
- 不调用 `quality.gate_events()` / `apply_event()`，不重写 `payload.level_decision`；
- 只改 level / level_label / quality / title / summary / impact / action；
- 原值写进 `payload.recheck.from`，可用 `revert_recheck()` 回滚。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from . import recheck_config as cfg

FIELD_LIMITS = {
    "title": cfg.MAX_TITLE_CHARS,
    "summary": cfg.MAX_SUMMARY_CHARS,
    "impact": cfg.MAX_IMPACT_CHARS,
    "action": cfg.MAX_ACTION_CHARS,
}
TEXT_FIELDS = ("title", "summary", "impact", "action")


class ApplyError(RuntimeError):
    pass


def _iso(now: datetime | None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int_level(value) -> int:
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return 0


def snapshot_fields(event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "level": _int_level(event.get("level")),
        "quality": str(payload.get("quality") or ""),
        "title": str(event.get("title") or ""),
        "summary": str(event.get("summary") or ""),
        "impact": str(event.get("impact") or ""),
        "action": str(event.get("action") or ""),
    }


def _level_gate(event: dict, verdict: dict, *, apply_level: bool) -> tuple[bool, str]:
    if not apply_level:
        return False, "开关关闭（RECHECK_APPLY_LEVEL=0）"
    if verdict.get("verdict") not in ("confirmed", "contradicted"):
        return False, "uncertain 不改级"
    suggested = int((verdict.get("level_suggestion") or {}).get("suggested"))
    current = _int_level(event.get("level"))
    if suggested == current:
        return False, "建议级别与当前一致"
    if verdict.get("confidence") not in ("high", "medium"):
        return False, "置信度不足"
    evidence = verdict.get("evidence") or []
    need = 2 if suggested > current else 1
    if len(evidence) < need:
        return False, f"证据条数不足（{len(evidence)}/{need}）"
    gap = abs(suggested - current)
    major = bool(verdict.get("major_misjudgement"))
    if gap < cfg.level_min_gap() and not major:
        return False, f"级别差 {gap} 级，未达到严重误判阈值"
    return True, "证据门槛满足"


def _apply_text(event: dict, verdict: dict) -> list[str]:
    applied: list[str] = []
    if verdict.get("verdict") == "uncertain":
        return applied
    for field in TEXT_FIELDS:
        value = str(verdict.get(field) or "").strip()
        if not value:
            continue
        limit = FIELD_LIMITS[field]
        value = value[:limit]
        if value != str(event.get(field) or ""):
            event[field] = value
            applied.append(field)
    return applied


def _compact_record(record: dict, max_bytes: int) -> dict:
    """payload.recheck 只存小字段；超限时按优先级裁剪。"""
    compact = copy.deepcopy(record)

    def size() -> int:
        return len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if size() <= max_bytes:
        return compact
    for item in compact.get("evidence") or []:
        item["finding"] = str(item.get("finding") or "")[:200]
    compact["checks"] = [str(item)[:200] for item in (compact.get("checks") or [])[:4]]
    compact["limitations"] = [str(item)[:200] for item in (compact.get("limitations") or [])[:4]]
    suggestion = compact.get("level_suggestion") or {}
    suggestion["evidence"] = [str(item)[:200] for item in (suggestion.get("evidence") or [])[:4]]
    suggestion["reason"] = str(suggestion.get("reason") or "")[:300]
    compact["reason"] = str(compact.get("reason") or "")[:300]
    compact["basis"] = str(compact.get("basis") or "")[:240]
    if size() <= max_bytes:
        return compact
    compact.pop("checks", None)
    compact.pop("limitations", None)
    compact["evidence"] = (compact.get("evidence") or [])[:3]
    if size() <= max_bytes:
        return compact
    compact["evidence"] = (compact.get("evidence") or [])[:1]
    suggestion["evidence"] = (suggestion.get("evidence") or [])[:1]
    if size() <= max_bytes:
        return compact
    compact["evidence"] = []
    compact["reason"] = str(compact.get("reason") or "")[:160]
    compact["basis"] = str(compact.get("basis") or "")[:160]
    if size() > max_bytes:
        raise ApplyError(f"payload.recheck 超过 {max_bytes} 字节，无法安全写入")
    return compact


def _settle_audit_state(event: dict, quality_value: str) -> str:
    """pending 事件落终态；其它终态不动，避免非法迁移。"""
    try:
        from webwatch import quality
    except Exception:  # noqa: BLE001
        return ""
    current = quality.quality_state(event)
    if current != "pending":
        return current
    target = (
        "repaired"
        if quality_value == "repaired"
        else "partial"
        if quality_value == "partial"
        else "final"
    )
    quality.mark_quality_state(event, target)
    return target


def apply_recheck(
    event: dict,
    verdict: dict,
    *,
    apply_level: bool | None = None,
    now: datetime | None = None,
    job: str = "",
    duration_seconds: int = 0,
    attempts: int = 1,
    skip_quality: bool = False,
) -> dict:
    """应用 verdict 到事件（原地修改），返回结果摘要。

    skip_quality=True 用于 agent 运行期间 audit 已改写 quality：保留更新的审计结果，
    不用 agent 启动前的旧 verdict 覆盖它。
    """
    if not isinstance(event, dict) or not isinstance(verdict, dict):
        raise ApplyError("event / verdict 必须是对象")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        event["payload"] = payload
    before = snapshot_fields(event)
    applied = _apply_text(event, verdict)

    quality_value = str(verdict.get("quality") or "").strip()
    if quality_value and quality_value != before["quality"] and not skip_quality:
        payload["quality"] = quality_value
        applied.append("quality")

    if apply_level is None:
        apply_level = cfg.apply_level_enabled()
    level_applied, level_reason = _level_gate(event, verdict, apply_level=apply_level)
    suggested = _int_level((verdict.get("level_suggestion") or {}).get("suggested"))
    from_level = before["level"]
    if level_applied:
        event["level"] = suggested
        event["level_label"] = cfg.LEVEL_LABELS.get(suggested, "值得看")
        applied.append("level")
    to_level = _int_level(event.get("level"))

    record = {
        "state": "done",
        "at": _iso(now),
        "verdict": str(verdict.get("verdict") or ""),
        "confidence": str(verdict.get("confidence") or ""),
        "reason": str(verdict.get("reason") or "")[:cfg.MAX_REASON_CHARS],
        "basis": str(verdict.get("basis") or "")[:240],
        "evidence": [
            {
                "url": str(item.get("url") or "")[:500],
                "finding": str(item.get("finding") or "")[:300],
            }
            for item in (verdict.get("evidence") or [])[:cfg.MAX_EVIDENCE_ITEMS]
        ],
        "checks": [str(item) for item in (verdict.get("checks") or [])[:cfg.MAX_TEXT_LIST_ITEMS]],
        "limitations": [
            str(item) for item in (verdict.get("limitations") or [])[:cfg.MAX_TEXT_LIST_ITEMS]
        ],
        "from": before,
        "applied": applied,
        "level_suggestion": {
            "suggested": suggested,
            "reason": str((verdict.get("level_suggestion") or {}).get("reason") or "")[:300],
            "evidence": [
                str(item)
                for item in ((verdict.get("level_suggestion") or {}).get("evidence") or [])[:6]
            ],
        },
        "level_applied": bool(level_applied),
        "from_level": from_level,
        "to_level": to_level,
        "level_gate": level_reason,
        "major_misjudgement": bool(verdict.get("major_misjudgement")),
        "attempts": max(0, int(attempts)),
        "duration_seconds": max(0, int(duration_seconds)),
        "job": str(job or "")[:120],
    }
    if level_applied:
        record["level_source"] = "recheck"
    payload["recheck"] = _compact_record(record, cfg.recheck_max_bytes())
    audit_state = _settle_audit_state(event, str(payload.get("quality") or ""))
    return {
        "applied": applied,
        "level_applied": bool(level_applied),
        "from": before,
        "to": snapshot_fields(event),
        "audit_state": audit_state,
        "level_gate": level_reason,
    }


def revert_recheck(event: dict, *, now: datetime | None = None) -> dict:
    """用 payload.recheck.from 恢复六个字段；保留回滚记录。"""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    record = payload.get("recheck") if isinstance(payload.get("recheck"), dict) else {}
    before = record.get("from") if isinstance(record.get("from"), dict) else None
    if not before:
        raise ApplyError("事件没有 payload.recheck.from，无法回滚")
    restored: list[str] = []
    level = _int_level(before.get("level"))
    if level != _int_level(event.get("level")):
        event["level"] = level
        event["level_label"] = cfg.LEVEL_LABELS.get(level, "值得看")
        restored.append("level")
    for field in ("quality", "title", "summary", "impact", "action"):
        if field == "quality":
            current = str(payload.get("quality") or "")
            target = str(before.get("quality") or "")
            if target and current != target:
                payload["quality"] = target
                restored.append("quality")
        elif field in event:
            target = str(before.get(field) or "")
            if target and str(event.get(field) or "") != target:
                event[field] = target
                restored.append(field)
    record["reverted_at"] = _iso(now)
    record["restored"] = restored
    record["level_applied"] = False
    record.pop("notified_at", None)
    record.pop("notified_channels", None)
    payload["recheck"] = record
    return {"restored": restored, "from": before, "to": snapshot_fields(event)}
