"""DeepSeek 官方账户余额采样。

`webwatch` 每 5 分钟唤醒一次，余额每 10 分钟采样一次；第一次可以用同一个 `DEEPSEEK_API_KEY` 读一次
`/user/balance`（免费，不消耗 token），把余额点存到 state 目录的
`balance-history.json`，并在当天余额下降超过 0.01 元时发一封邮件。

只存余额数字和时间，不存 key；也不进入站点事件/来源枚举，避免破坏 watchsite
的 `docs | chat | platform | harness | fingerprint` 契约。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import iso, parse_iso
from .fingerprint import _api_request
from .redact import redact

BALANCE_HISTORY_NAME = "balance-history.json"
THRESHOLD_YUAN = 0.01
KEEP_DAYS = 90
MAX_POINTS = 8000
BJ = timezone(timedelta(hours=8))


class BalanceError(Exception):
    pass


def history_path(state_dir: Path) -> Path:
    return state_dir / BALANCE_HISTORY_NAME


def load_history(state_dir: Path) -> dict:
    path = history_path(state_dir)
    if not path.is_file():
        return {"points": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"points": []}
    if not isinstance(data, dict) or not isinstance(data.get("points"), list):
        return {"points": []}
    return data


def save_history(state_dir: Path, history: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = history_path(state_dir)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "posix":
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
    os.replace(tmp, path)


def _amount(value: object) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise BalanceError(f"余额不是数字：{value!r}") from exc
    if number < 0:
        raise BalanceError(f"余额为负：{number}")
    return number


def parse_balance(body: dict) -> tuple[str, float]:
    """从 /user/balance 响应里取 CNY（没有就取第一个）的总余额。"""
    if not isinstance(body, dict):
        raise BalanceError("余额响应不是对象")
    infos = body.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise BalanceError("余额响应里没有 balance_infos")
    chosen = None
    for item in infos:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").strip().upper()
        if currency == "CNY":
            chosen = item
            break
        if chosen is None:
            chosen = item
    if chosen is None:
        raise BalanceError("余额响应里没有可用币种")
    currency = str(chosen.get("currency") or "").strip().upper() or "CNY"
    return currency, _amount(chosen.get("total_balance"))


def probe_balance(api_key: str) -> tuple[str, float]:
    body = _api_request("GET", "/user/balance", api_key)
    if body.get("is_available") is False:
        raise BalanceError("账户当前不可用（is_available=false）")
    return parse_balance(body)


def _point_time(point: dict) -> datetime | None:
    return parse_iso(point.get("at"))


def add_point(points: list[dict], at: datetime, amount: float, currency: str) -> list[dict]:
    """把一次读数放进点列；同一金额的连续读数只保留最后一个时间。"""
    stamp = iso(at)
    if points:
        last = points[-1]
        last_at = _point_time(last)
        last_amount = last.get("amount")
        if last_amount == amount and last.get("currency", currency) == currency and last_at is not None:
            if at >= last_at:
                last["at"] = stamp
                return points
    points.append({"at": stamp, "amount": amount, "currency": currency})
    return points


def _ordered_points(points: list[dict]) -> list[tuple[datetime, dict]]:
    ordered: list[tuple[datetime, dict]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        point_at = _point_time(point)
        if point_at is not None:
            ordered.append((point_at, point))
    ordered.sort(key=lambda item: item[0])
    return ordered


def daily_spend(
    points: list[dict],
    since: datetime | None = None,
    *,
    ordered: list[tuple[datetime, dict]] | None = None,
) -> dict[str, float]:
    """按北京时间把相邻两次余额的负向变化记到前一次采样那天。"""
    days: dict[str, float] = {}
    if ordered is None:
        ordered = _ordered_points(points)
    for (first_at, first), (_second_at, second) in zip(ordered, ordered[1:]):
        if since is not None and first_at < since:
            continue
        # 币种不一致的相邻点不能相减：CNY/USD 切换、赠送额度到期、退款冲正
        # 都会被误算成「消耗」。缺币种的老数据按同一币种处理，避免历史点全部失效。
        first_currency = str(first.get("currency") or "").strip().upper()
        second_currency = str(second.get("currency") or "").strip().upper()
        if first_currency and second_currency and first_currency != second_currency:
            continue
        try:
            delta = float(second["amount"]) - float(first["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if delta < 0:
            day = first_at.astimezone(BJ).date().isoformat()
            days[day] = round(days.get(day, 0.0) + (-delta), 6)
    return days


def record_balance(
    state: dict,
    state_dir: Path,
    now: datetime,
    currency: str,
    amount: float,
) -> dict:
    """记录一次余额，更新 state["balance"]，返回本次摘要。"""
    balance = state.get("balance")
    if not isinstance(balance, dict):
        balance = {}
    started = parse_iso(balance.get("watch_started_at")) or now
    balance["watch_started_at"] = iso(started)

    history = load_history(state_dir)
    points = history.setdefault("points", [])
    if not isinstance(points, list):
        points = []
        history["points"] = points
    add_point(points, now, amount, currency)

    cutoff = now - timedelta(days=KEEP_DAYS)
    kept = []
    for point in points:
        point_at = _point_time(point) if isinstance(point, dict) else None
        if point_at is not None and point_at >= cutoff:
            kept.append(point)
    if len(kept) > MAX_POINTS:
        kept = kept[-MAX_POINTS:]
    history["points"] = kept
    save_history(state_dir, history)

    ordered = _ordered_points(kept)
    days = daily_spend(kept, ordered=ordered)
    since_days = daily_spend(kept, since=started, ordered=ordered)
    today_date = now.astimezone(BJ).date()
    today = today_date.isoformat()
    yesterday = (today_date - timedelta(days=1)).isoformat()
    last7 = [(today_date - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    summary = {
        "currency": currency,
        "amount": amount,
        "today": today,
        "today_spend": days.get(today, 0.0),
        "today_spend_since_watch": since_days.get(today, 0.0),
        "yesterday": yesterday,
        "yesterday_spend": days.get(yesterday, 0.0),
        "last7_spend": round(sum(days.get(day, 0.0) for day in last7), 6),
        "points": len(kept),
    }
    balance.update(
        {
            "last_checked": iso(now),
            "last_success": iso(now),
            "last_error": "",
            "fail_streak": 0,
            **summary,
        }
    )
    state["balance"] = balance
    return summary


def record_balance_error(state: dict, now: datetime, exc: object) -> None:
    balance = state.get("balance")
    if not isinstance(balance, dict):
        balance = {}
    balance["last_checked"] = iso(now)
    balance["last_error"] = redact(f"{type(exc).__name__}: {exc}")[:500]
    balance["fail_streak"] = int(balance.get("fail_streak") or 0) + 1
    balance.setdefault("last_success", "")
    state["balance"] = balance


def alert_wanted(summary: dict, state: dict) -> bool:
    """今天开始监控后的消耗超过阈值，且今天还没告过。"""
    balance = state.get("balance") if isinstance(state.get("balance"), dict) else {}
    return (
        float(summary.get("today_spend_since_watch") or 0) > THRESHOLD_YUAN
        and str(balance.get("last_alert_day") or "") != str(summary.get("today") or "")
    )


def mark_alerted(state: dict, summary: dict) -> None:
    balance = state.get("balance")
    if isinstance(balance, dict):
        balance["last_alert_day"] = str(summary.get("today") or "")
