"""前端事件发布后的自动复盘。

B-8：审计从「每 5 分钟盲扫最近事件」改为事件驱动的显式状态机：
  - 事件写入时由 quality.gate_events(mark_audit_state=True) 记录
    quality_state / next_check_at；
  - 只有 pending 且到期的事件会被处理；
  - 状态迁移 pending → repaired / partial / final，终态不再复查；
  - 没有 quality_state 的旧事件默认 final，避免升级后一次性重扫/重推。

审计不调用模型，只复用 quality.apply_event() 的同一套 Decision 纯函数。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import quality
from .events import parse_iso, write_event


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _is_due(event: dict, now: datetime) -> bool:
    """pending 且 next_check_at 已到（缺失到期时间视为立即到期）。"""
    if quality.quality_state(event) != "pending":
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    next_at = parse_iso(event.get("next_check_at") or payload.get("next_check_at"))
    return next_at is None or next_at <= now


def _target_state(decision: dict) -> str:
    """把 Decision 映射为审计终态；只允许 pending 出发的显式迁移。"""
    targets = quality.AUDIT_TRANSITIONS["pending"]
    quality_value = str(decision.get("quality") or "")
    target = (
        "repaired"
        if quality_value == "repaired"
        else "partial"
        if quality_value == "partial"
        else "final"
    )
    if target not in targets:
        raise ValueError(f"非法审计迁移：pending → {target}")
    return target


def _ready_terminal_state(event: dict, payload: dict) -> str | None:
    """已有完整 Decision 的事件只需落终态，不重新跑判级（避免改写/重推）。"""
    quality_value = str(payload.get("quality") or "")
    decision = payload.get("level_decision")
    if quality_value not in ("ok", "internal", "partial", "repaired"):
        return None
    if not isinstance(decision, dict) or decision.get("final") != event.get("level"):
        return None
    if quality_value == "repaired":
        return "repaired"
    if quality_value == "partial":
        return "partial"
    return "final"


def audit_recent_events(
    events_dir: Path,
    *,
    limit: int = 30,
    now: datetime | None = None,
    ai_enabled: bool | None = None,
) -> dict:
    """只复核 pending 且到期的事件；返回统计。"""
    counts = {"checked": 0, "repaired": 0, "flagged": 0, "finalized": 0}
    if not events_dir.is_dir():
        return counts
    if ai_enabled is None:
        ai_enabled = quality.ai_is_enabled()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    paths = sorted(
        events_dir.glob("*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )[:limit]
    for path in paths:
        event = _load(path)
        if not event:
            continue
        # 平台公告、纯正文/HTML 变化没有前端证据，不属于质量闸门的范围。
        if quality.frontend_evidence(event) is None:
            continue
        # 旧事件没有状态记录，默认 final：不重扫、不改写、不重推。
        if not _is_due(event, current):
            continue
        # AI 关闭时不做规则兜底/改写，保持 pending 等 AI 恢复后再复核。
        if not ai_enabled:
            continue
        counts["checked"] += 1
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        ready = _ready_terminal_state(event, payload)
        if ready is not None:
            quality.mark_quality_state(event, ready)
            counts["finalized"] += 1
        else:
            before_quality = str(payload.get("quality") or "")
            before_level = event.get("level")
            before_decision = payload.get("level_decision")
            decision = quality.apply_event(event, ai_enabled=True)
            if not decision:
                quality.mark_quality_state(event, "final")
                counts["finalized"] += 1
            else:
                quality.mark_quality_state(event, _target_state(decision))
                changed = (
                    before_quality != decision.get("quality")
                    or before_level != decision.get("level")
                    or before_decision is None
                )
                if decision.get("quality") == "repaired" and changed:
                    counts["repaired"] += 1
                elif decision.get("quality") == "partial" and changed:
                    counts["flagged"] += 1
                else:
                    counts["finalized"] += 1
        try:
            write_event(events_dir, event)
        except OSError:
            if counts["repaired"]:
                counts["repaired"] = max(0, counts["repaired"] - 1)
    return counts
