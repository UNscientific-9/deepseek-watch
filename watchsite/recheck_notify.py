"""复核升级到 3 级时的二次通知门控。

规则（任务书 §3.7）：
- 降级 / 升级未到 3 / 仅文案修正：不通知；
- 原级别 < 3 且复核后 = 3：QQ + 邮件各通知一次；
- 同一事件只通知一次：事件里的 `payload.recheck.notified_at` 是主闸门，
  `notify-state.json` 按渠道记录成功，避免一个渠道失败导致另一个渠道重发。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import recheck_config as cfg


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_upgrade_to_three(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    recheck = payload.get("recheck") if isinstance(payload.get("recheck"), dict) else {}
    if not recheck.get("level_applied"):
        return False
    return (
        _as_int(recheck.get("from_level"), 0) < 3
        and _as_int(recheck.get("to_level"), 0) == 3
        and _as_int(event.get("level"), 0) == 3
    )


def notification_pending(event: dict) -> bool:
    if not is_upgrade_to_three(event):
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    recheck = payload.get("recheck") if isinstance(payload.get("recheck"), dict) else {}
    return not bool(recheck.get("notified_at"))


def _state_path() -> Path:
    return cfg.state_dir() / "notify-state.json"


def _load_state() -> dict:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> bool:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _record_channel(event_id: str, channel: str) -> None:
    data = _load_state()
    entry = data.get(event_id)
    if not isinstance(entry, dict):
        entry = {}
    entry[channel] = int(time.time())
    data[event_id] = entry
    # 只保留最近 500 条，避免状态文件无限增长。
    if len(data) > 500:
        ordered = sorted(
            data.items(),
            key=lambda item: max((v for v in item[1].values() if isinstance(v, int)), default=0)
            if isinstance(item[1], dict)
            else 0,
            reverse=True,
        )[:500]
        data = dict(ordered)
    _save_state(data)


def _already_sent(event_id: str, channel: str) -> bool:
    entry = _load_state().get(event_id)
    return bool(isinstance(entry, dict) and entry.get(channel))


def _qq_enabled() -> bool:
    try:
        from . import qq

        settings = qq.settings()
        return bool(settings.get("enabled") and settings.get("groups"))
    except Exception:  # noqa: BLE001
        return False


def _mail_enabled() -> bool:
    try:
        from webwatch.mail import mail_enabled

        return bool(mail_enabled())
    except Exception:  # noqa: BLE001
        return False


def _default_qq_sender(event: dict):
    from . import qq

    return qq.notify_recheck_upgrade(event)


def _default_mail_sender(event: dict):
    from webwatch.mail import send_events

    return send_events([event])


def notify_upgrade(
    event: dict,
    *,
    now_ts: int | None = None,
    dry_run: bool = False,
    qq_sender=None,
    mail_sender=None,
    qq_enabled: bool | None = None,
    mail_enabled: bool | None = None,
) -> dict:
    """给一条「复核升到 3 级」的事件发二次通知；返回渠道结果。"""
    now_ts = int(now_ts if now_ts is not None else time.time())
    result = {
        "attempted": False,
        "complete": False,
        "channels": [],
        "sent": [],
        "skipped": "",
        "errors": [],
    }
    if not is_upgrade_to_three(event):
        result["skipped"] = "not_upgrade"
        return result
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    recheck = payload.get("recheck") if isinstance(payload.get("recheck"), dict) else {}
    if recheck.get("notified_at"):
        result["skipped"] = "already_notified"
        return result

    event_id = str(event.get("id") or "")
    qq_on = _qq_enabled() if qq_enabled is None else bool(qq_enabled)
    mail_on = _mail_enabled() if mail_enabled is None else bool(mail_enabled)
    if not qq_on and not mail_on:
        result.update(
            {
                "attempted": True,
                "complete": True,
                "skipped": "no_channels",
                "channels": [],
            }
        )
        return result
    result["attempted"] = True
    if dry_run:
        result["channels"] = [
            name for name, enabled in (("qq", qq_on), ("mail", mail_on)) if enabled
        ]
        result["skipped"] = "dry_run"
        return result

    if qq_on and not _already_sent(event_id, "qq"):
        try:
            sender = qq_sender or _default_qq_sender
            qq_result = sender(event)
            if isinstance(qq_result, dict) and qq_result.get("sent"):
                _record_channel(event_id, "qq")
                result["sent"].append("qq")
            elif isinstance(qq_result, dict) and qq_result.get("skipped"):
                # 未启用 / 没有群等：不算失败，也不再重试。
                _record_channel(event_id, "qq")
                result["channels"].append(f"qq:{qq_result.get('skipped')}")
            else:
                result["errors"].append("qq 未确认发送成功")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"qq: {type(exc).__name__}: {exc}")
    elif qq_on:
        result["sent"].append("qq")

    if mail_on and not _already_sent(event_id, "mail"):
        try:
            sender = mail_sender or _default_mail_sender
            mail_result = sender(event)
            if isinstance(mail_result, tuple):
                sent_count = int(mail_result[0] or 0)
                failed_count = int(mail_result[1] or 0) if len(mail_result) > 1 else 0
            else:
                sent_count = 1 if mail_result else 0
                failed_count = 0 if mail_result else 1
            if failed_count:
                result["errors"].append("mail 发送失败")
            elif sent_count:
                _record_channel(event_id, "mail")
                result["sent"].append("mail")
            else:
                # 现有邮件渠道会跳过纯 CSS/不值得看的事件；这不是失败，
                # 记一次 skipped，避免每分钟重试同一条。
                _record_channel(event_id, "mail")
                result["channels"].append("mail:skipped")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"mail: {type(exc).__name__}: {exc}")
    elif mail_on:
        result["sent"].append("mail")

    result["complete"] = not result["errors"]
    return result


def apply_notification_result(event: dict, result: dict, *, now_iso: str) -> None:
    """把通知结果写进 payload.recheck；只在所有启用渠道完成后写 notified_at。"""
    if not result.get("attempted"):
        return
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    recheck = payload.get("recheck") if isinstance(payload.get("recheck"), dict) else None
    if recheck is None:
        return
    if result.get("skipped") == "no_channels":
        recheck["notified_at"] = now_iso
        recheck["notified_channels"] = []
        recheck["notify_skipped"] = "no_channels"
        return
    if not result.get("complete"):
        return
    recheck["notified_at"] = now_iso
    recheck["notified_channels"] = sorted(set(result.get("sent") or []))
    recheck.pop("notify_skipped", None)
