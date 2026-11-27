"""把 watchsite 采集到的事件推送到 Cloudflare Pages / D1。

用法：
    python -m watchsite.push --dry-run     # 只打印，不发网络请求
    python -m watchsite.push --once        # systemd timer 使用

环境变量：
    DOCWATCH_DB / DOCWATCH_ROOT / WEBWATCH_EVENTS_DIR / WEBWATCH_STATE
    INGEST_URL / INGEST_TOKEN
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

from . import alert, qq, state
from .collect import WEB_SOURCES, build_batch, default_paths

DEFAULT_INGEST_URL = "https://deepseek-watch.pages.dev/internal/ingest"
DEFAULT_INGEST_HOST = "deepseek-watch.pages.dev"
MAX_EVENTS_PER_REQUEST = 50
# 服务端单请求最多 500 条 statement；这里留 50 条余量，避免状态/元数据波动顶到上限。
MAX_STATEMENTS_PER_REQUEST = 450
# 服务端上限是 4 MB；留 0.5 MB 余量给 JSON 结构 / 状态 / meta。
MAX_REQUEST_BYTES = 3_500_000
MAX_SENT_IDS = 5000
# 服务端永久拒收的事件（slug / URL / payload 不合规）会被每轮重推并每 6 小时
# 告警一次。连续拒收达到该次数后转入死信，不再重推；事件内容变化时自动解除。
MAX_REJECT_STREAK = 5
MAX_REJECTED_EVENTS = 500
PUSH_STATE_FILE = "push-state.json"
TIMEOUT_SECONDS = 30
# 与服务端 ingest.js 的 QUALITY_VALUES 保持一致；白名单外的值统一算 unknown。
QUALITY_KEYS = ("ok", "internal", "partial", "contradiction", "missing", "repaired", "unknown")
PUSH_METRIC_KEYS = (
    "new_event_count",
    "pushed_count",
    "rejected_count",
    "dead_letter_count",
    "backlog_count",
)


class RateLimited(RuntimeError):
    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PayloadTooLarge(RuntimeError):
    """HTTP 413：当前块需要拆小后重试。"""


class InvalidResponse(RuntimeError):
    """2xx 但响应内容不符合 ingest 契约。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向，避免 Bearer token 被带到其它主机/协议。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _allowed_ingest_hosts() -> set[str]:
    hosts = {DEFAULT_INGEST_HOST}
    raw = os.getenv("INGEST_ALLOWED_HOST", "")
    hosts.update(item.strip().lower() for item in raw.split(",") if item.strip())
    return hosts


def _validate_ingest_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    local = os.getenv("INGEST_ALLOW_LOCAL", "").strip().lower() in {"1", "true", "yes", "on"}
    if parts.scheme != "https" and not (local and parts.scheme == "http"):
        raise ValueError("INGEST_URL 必须是 https://")
    if local and parts.scheme == "http":
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("本地 http 只允许 127.0.0.1 / localhost / ::1")
    elif host not in _allowed_ingest_hosts():
        raise ValueError(f"INGEST_URL 主机必须是 {DEFAULT_INGEST_HOST} 或 INGEST_ALLOWED_HOST")
    if parts.port not in (None, 443) and not (local and parts.scheme == "http"):
        raise ValueError("INGEST_URL 端口必须是 443")
    if parts.username or parts.password:
        raise ValueError("INGEST_URL 不允许包含账号密码")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("INGEST_URL 含控制字符")


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _post_json(url: str, token: str, payload: dict) -> dict:
    _validate_ingest_url(url)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
            "User-Agent": "deepseek-watch-push/1.0",
            "Accept": "application/json",
        },
    )
    with _NO_REDIRECT_OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", 200)
        raw = response.read().decode("utf-8", "replace")
    if not raw.strip():
        raise InvalidResponse(f"推送响应为空（HTTP {status}）")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidResponse(f"推送响应不是合法 JSON（HTTP {status}）：{raw[:200]}") from exc
    if not isinstance(data, dict):
        raise InvalidResponse(f"推送响应不是 JSON 对象（HTTP {status}）")
    if data.get("ok") is not True:
        raise InvalidResponse(f"服务端返回失败：{str(data)[:300]}")
    return data


def _post_with_retry(url: str, token: str, payload: dict) -> dict:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _post_json(url, token, payload)
        except (InvalidResponse, PayloadTooLarge):
            # 响应内容不符合契约 / 请求体过大：重试同一份内容不会变好。
            raise
        except urllib.error.HTTPError as exc:
            # 4xx（除 429）重试也不会成功：直接失败，避免每 5 分钟浪费三次请求。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 429:
                retry_after = 0
                try:
                    headers = getattr(exc, "headers", None)
                    retry_after = int((headers.get("retry-after") if headers else 0) or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                raise RateLimited(f"推送被限流：HTTP 429 {detail}".strip(), retry_after) from exc
            if exc.code == 413:
                raise PayloadTooLarge(f"推送请求体过大：HTTP 413 {detail}".strip()) from exc
            if exc.code == 400 and "too many statements" in detail.lower():
                raise PayloadTooLarge(
                    f"推送 statement 数超过服务端上限：HTTP 400 {detail}".strip()
                ) from exc
            if 400 <= exc.code < 500:
                raise RuntimeError(f"推送失败：HTTP {exc.code} {detail}".strip()) from exc
            last_error = exc
            if attempt < 2:
                time.sleep(2 if attempt == 0 else 5)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(2 if attempt == 0 else 5)
    raise RuntimeError(f"推送失败：{type(last_error).__name__}: {last_error}") from last_error


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_ts(timestamp: int) -> str:
    """把 epoch 秒转成本地时区的 ISO 时间，站点 parseTimestamp 可直接解析。"""
    return dt.datetime.fromtimestamp(int(timestamp)).astimezone().isoformat(timespec="seconds")


def _quality_counts(events: list[dict]) -> dict[str, int]:
    """按 payload.quality 统计本轮事件；未知/缺失一律算 unknown，键名与服务端白名单一致。"""
    counts = {key: 0 for key in QUALITY_KEYS}
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        quality = str(payload.get("quality") or "").strip()
        if quality not in counts:
            quality = "unknown"
        counts[quality] += 1
    return {key: value for key, value in counts.items() if value}


def _dead_letter_count(rejected: dict[str, dict]) -> int:
    """当前处于死信状态的记录数；直接读已加载的 push state，不碰事件大文件。"""
    total = 0
    for record in rejected.values():
        if not isinstance(record, dict):
            continue
        if max(0, _as_int(record.get("count")) or 0) >= MAX_REJECT_STREAK:
            total += 1
    return total


def _backlog_count(events: list[dict], sent_ids: list[str], rejected: dict[str, dict]) -> int:
    """本轮结束后仍未推送的事件数：排除已入库和内容未变的死信，其余都算积压。"""
    sent = set(sent_ids)
    total = 0
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id or event_id in sent:
            continue
        record = rejected.get(event_id) or {}
        if (
            max(0, _as_int(record.get("count")) or 0) >= MAX_REJECT_STREAK
            and record.get("signature") == _event_signature(event)
        ):
            continue
        total += 1
    return total


def _metrics_meta(metrics: dict) -> dict:
    """把上一轮保存的推送指标转成 meta 键；缺字段不补默认值，站点显示「暂无」。"""
    if not isinstance(metrics, dict):
        return {}
    out: dict = {}
    last_push = str(metrics.get("last_push_ts") or "").strip()
    if last_push:
        out["last_push_ts"] = last_push[:40]
    for key in PUSH_METRIC_KEYS:
        value = _as_int(metrics.get(key))
        if value is not None and value >= 0:
            out[key] = value
    quality = metrics.get("quality_counts")
    if isinstance(quality, dict):
        clean: dict[str, int] = {}
        for key, raw in quality.items():
            value = _as_int(raw)
            if str(key) in QUALITY_KEYS and value is not None and value >= 0:
                clean[str(key)] = value
        if clean:
            out["quality_counts"] = clean
    return out


def _build_push_metrics(
    *,
    now_ts: int,
    events: list[dict],
    new_event_count: int,
    stats: dict,
    sent_ids: list[str],
    rejected: dict[str, dict],
    quality_counts: dict[str, int],
    previous: dict,
) -> dict:
    """汇总本轮推送结果，写入 push state，供下一轮 meta 带上站。"""
    previous = previous if isinstance(previous, dict) else {}
    last_push = str(previous.get("last_push_ts") or "")
    # 整轮请求都失败/被 429 时不能刷新“最近推送”，否则站点会把失败轮当成成功轮。
    if stats.get("push_ok"):
        last_push = _iso_ts(now_ts)
    metrics = {
        "new_event_count": max(0, _as_int(new_event_count) or 0),
        "pushed_count": max(0, _as_int(stats.get("accepted")) or 0),
        "rejected_count": max(0, _as_int(stats.get("rejected")) or 0),
        "dead_letter_count": _dead_letter_count(rejected),
        "backlog_count": _backlog_count(events, sent_ids, rejected),
        "quality_counts": quality_counts,
    }
    if last_push:
        metrics["last_push_ts"] = last_push
    return metrics


def _make_payload(events: list[dict], statuses: list[dict], meta: dict) -> dict:
    return {"events": events, "statuses": statuses, "meta": meta}


def _payload_bytes(events: list[dict], statuses: list[dict], meta: dict) -> int:
    body = json.dumps(
        _make_payload(events, statuses, meta),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(body)


def _event_bytes(event: dict) -> int:
    """单条事件的 UTF-8 字节数。

    这里是整条 event 序列化，所以 ``changes_detail``（diff 正文，单条 ≤ 20,000 字符）
    已经算在体积里；不要为了「省体积」只算 payload，否则会出现「按体积分批合规、
    实际请求超 3.5MB」的偏差。
    """
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _split_by_size(events: list[dict], statuses: list[dict], meta: dict) -> list[list[dict]]:
    """按序列化后的 UTF-8 字节切块；单条超限时保留为独立块，交给 413 分支报错。"""
    if not events:
        return [[]]
    base_size = _payload_bytes([], statuses, meta)
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = base_size
    for event in events:
        event_size = _event_bytes(event)
        added = event_size + (1 if current else 0)
        if current and current_size + added > MAX_REQUEST_BYTES:
            chunks.append(current)
            current = [event]
            current_size = base_size + event_size
        else:
            current.append(event)
            current_size += added
    if current:
        chunks.append(current)
    return chunks


def _event_statement_weight(event: dict) -> int:
    """估算一条事件在服务端占用的 statement 数。

    主事件 1 条；有 changes_detail（哪怕是空数组）时，服务端固定先 DELETE
    再逐条 INSERT。statuses / meta 的开销在 _split_by_statements 里单独算。
    """
    detail = event.get("changes_detail")
    if not isinstance(detail, list):
        return 1
    return 2 + len(detail)


def _split_by_statements(
    events: list[dict], statuses: list[dict], meta: dict
) -> list[list[dict]]:
    """按服务端 statement 预算切块，避免单请求超过 D1/Worker 限制。"""
    if not events:
        return [[]]
    # 每个 status 最多 upsert + history；服务端固定写 last_ingest / last_ingest_at
    # 两条，再加 meta 里的每个键（含 models_snapshot）各一条。
    base = 2 * len(statuses) + 2 + len(meta)
    budget = max(1, MAX_STATEMENTS_PER_REQUEST - base)
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_weight = 0
    for event in events:
        weight = _event_statement_weight(event)
        if current and current_weight + weight > budget:
            chunks.append(current)
            current = [event]
            current_weight = weight
        else:
            current.append(event)
            current_weight += weight
    if current:
        chunks.append(current)
    return chunks


def _build_chunks(events: list[dict], statuses: list[dict], meta: dict) -> list[list[dict]]:
    groups = _chunks(events, MAX_EVENTS_PER_REQUEST) or [[]]
    chunks: list[list[dict]] = []
    for group in groups:
        for size_group in _split_by_size(group, statuses, meta):
            chunks.extend(_split_by_statements(size_group, statuses, meta))
    return chunks or [[]]


def _validate_response(result: dict, chunk: list[dict]) -> dict:
    """严格校验 2xx 响应：ok=True，计数安全转换且与块大小自洽。"""
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise InvalidResponse(f"服务端响应不符合契约：{str(result)[:300]}")
    event_count = _as_int(result.get("event_count"))
    rejected_count = _as_int(result.get("rejected_count"))
    if event_count is None or rejected_count is None:
        raise InvalidResponse(
            f"响应缺少有效的 event_count/rejected_count：{str(result)[:300]}"
        )
    if event_count < 0 or rejected_count < 0 or event_count + rejected_count != len(chunk):
        raise InvalidResponse(
            f"响应计数与请求块不自洽：event_count={event_count} "
            f"rejected_count={rejected_count} 块大小={len(chunk)}"
        )

    raw_rejected = result.get("rejected")
    rejected: list[dict] = []
    if isinstance(raw_rejected, list):
        for item in raw_rejected:
            index: int | None = None
            event_id = ""
            reason = "unknown"
            if isinstance(item, dict):
                index = _as_int(item.get("index"))
                event_id = str(item.get("id") or "")
                reason = str(item.get("reason") or "unknown")[:200]
            elif isinstance(item, str):
                # 兼容只返回 rejected id 列表的旧版/简化版服务端。
                event_id = item
            if index is None and event_id:
                for position, event in enumerate(chunk):
                    if isinstance(event, dict) and str(event.get("id") or "") == event_id:
                        index = position
                        break
            if index is None or index < 0 or index >= len(chunk):
                continue
            if not event_id:
                event = chunk[index]
                event_id = str(event.get("id") or "") if isinstance(event, dict) else ""
            rejected.append({"index": index, "id": event_id, "reason": reason})
    if len(rejected) < rejected_count:
        raise InvalidResponse(
            f"服务端拒绝了 {rejected_count} 条事件但未返回完整明细：{str(result)[:300]}"
        )

    rejected_indices = {item["index"] for item in rejected}
    accepted_ids: list[str] = []
    for index, event in enumerate(chunk):
        if index in rejected_indices:
            continue
        event_id = str(event.get("id") or "") if isinstance(event, dict) else ""
        if event_id:
            accepted_ids.append(event_id)
    if len(accepted_ids) != event_count:
        raise InvalidResponse(
            f"响应 accepted 明细与 event_count 不一致：accepted={len(accepted_ids)} "
            f"event_count={event_count}"
        )
    return {"accepted_ids": accepted_ids, "rejected": rejected, "event_count": event_count}


def _send_chunk(
    url: str,
    token: str,
    chunk: list[dict],
    statuses: list[dict],
    meta: dict,
    outcomes: list[dict],
) -> None:
    """发送一个块；413 时自动二分重试，成功部分记录到 outcomes。"""
    payload = _make_payload(chunk, statuses, meta)
    try:
        result = _post_with_retry(url, token, payload)
    except PayloadTooLarge:
        if len(chunk) <= 1:
            raise
        middle = len(chunk) // 2
        _send_chunk(url, token, chunk[:middle], statuses, meta, outcomes)
        _send_chunk(url, token, chunk[middle:], statuses, meta, outcomes)
        return
    outcomes.append(_validate_response(result, chunk))


def _load_push_state() -> tuple[list[str], dict[str, dict], dict[str, str], dict]:
    data = state.load_json(PUSH_STATE_FILE, {})
    raw = data.get("sent_ids")
    sent_ids = (
        [str(item) for item in raw if isinstance(item, str)][-MAX_SENT_IDS:]
        if isinstance(raw, list)
        else []
    )
    raw_rejected = data.get("rejected")
    rejected: dict[str, dict] = {}
    if isinstance(raw_rejected, dict):
        for event_id, record in raw_rejected.items():
            if isinstance(record, dict):
                rejected[str(event_id)] = record
    raw_versions = data.get("sent_versions")
    versions: dict[str, str] = {}
    if isinstance(raw_versions, dict):
        for event_id, digest in raw_versions.items():
            if isinstance(digest, str):
                versions[str(event_id)] = digest
    raw_metrics = data.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    return sent_ids, rejected, versions, metrics


def _save_push_state(
    sent_ids: list[str],
    rejected: dict[str, dict],
    versions: dict[str, str],
    metrics: dict | None = None,
) -> bool:
    if len(rejected) > MAX_REJECTED_EVENTS:
        newest = sorted(
            rejected.items(),
            key=lambda item: max(0, _as_int(item[1].get("last_ts")) or 0),
            reverse=True,
        )[:MAX_REJECTED_EVENTS]
        rejected = dict(newest)
    keep = set(sent_ids[-MAX_SENT_IDS:])
    # P3-02 现状：versions 与 sent_ids 同窗口裁剪，窗口外的老事件不再比较内容版本，
    # 即使被修正也不会自动重推（docwatch 更是按 id 直接跳过）。改契约前先保持此行为。
    versions = {event_id: digest for event_id, digest in versions.items() if event_id in keep}
    payload: dict = {
        "sent_ids": sent_ids[-MAX_SENT_IDS:],
        "rejected": rejected,
        "sent_versions": versions,
    }
    if isinstance(metrics, dict) and metrics:
        payload["metrics"] = metrics
    return state.save_json(PUSH_STATE_FILE, payload)


def _event_digest(event: dict | None) -> str:
    """事件内容的稳定摘要，用于识别「已推送后被修正」的事件。"""
    if not isinstance(event, dict):
        return ""
    fields = {
        key: event.get(key)
        for key in (
            "id", "slug", "source", "kind", "level", "title", "summary",
            "impact", "action", "signal_count", "changed_at", "page_url",
            "archive_url", "evidence", "payload", "changes_detail",
        )
    }
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _stable_digest(value: object) -> str:
    """对任意 JSON 值求稳定摘要；序列化失败时退回 repr，保证签名不会报错。"""
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _event_signature(event: dict | None) -> str:
    """只哈希服务端校验会看的字段；detected_ts 每轮变化，不能算进去。

    P2-15：payload / changes_detail 也参与签名——服务端以 payload 不合规为由
    拒收时，只修 payload 必须能解除死信；缺少 changed_ts 的事件由采集端填
    稳定兜底值（0 或文件 mtime），签名不会每轮漂移。
    """
    if not isinstance(event, dict):
        return ""
    changed_ts = event.get("changed_ts")
    try:
        changed_ts_value: object = int(changed_ts) if changed_ts is not None else 0
    except (TypeError, ValueError):
        changed_ts_value = str(changed_ts)
    fields = {
        "id": event.get("id"),
        "slug": event.get("slug"),
        "source": event.get("source"),
        "level": event.get("level"),
        "title": event.get("title"),
        "changed_ts": changed_ts_value,
        "page_url": event.get("page_url"),
        "archive_url": event.get("archive_url"),
        "payload": _stable_digest(event.get("payload")),
        "changes_detail": _stable_digest(event.get("changes_detail")),
    }
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _split_dead_letters(
    events: list[dict], rejected: dict[str, dict]
) -> tuple[list[dict], list[str]]:
    live: list[dict] = []
    dead: list[str] = []
    for event in events:
        event_id = str(event.get("id") or "")
        record = rejected.get(event_id) or {}
        if (
            max(0, _as_int(record.get("count")) or 0) >= MAX_REJECT_STREAK
            and record.get("signature") == _event_signature(event)
        ):
            dead.append(event_id)
        else:
            live.append(event)
    return live, dead


def _quality_blocked(event: dict) -> bool:
    """只拦「证据与结论矛盾且未修复」的前端页面事件，其余正常推送。

    证据不完整（partial）的事件按「值得看」发布并在页面上标注待核实，
    不能整条丢弃；平台公告、纯正文变化没有前端证据，不参与质量闸门。
    """
    if str(event.get("source") or "") not in WEB_SOURCES:
        return False
    if str(event.get("kind") or "") != "page_change":
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if not isinstance(payload.get("frontend"), dict):
        return False
    return str(payload.get("quality") or "") == "contradiction"


def _new_events(
    events: list[dict], sent_ids: list[str], versions: dict[str, str] | None = None
) -> list[dict]:
    """返回新事件，以及「已推送但内容被修正」的事件。

    老状态没有 sent_versions：对已在 sent_ids 里的 id 按已发送处理，
    避免升级后把历史事件全量重推；新事件正常记录版本。
    """
    versions = versions or {}
    seen = set(sent_ids)
    out: list[dict] = []
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        if event_id not in seen:
            out.append(event)
            continue
        previous = versions.get(event_id)
        if previous and previous != _event_digest(event):
            out.append(event)
    return out


def _remember_ids(sent_ids: list[str], ids: Iterable[str]) -> list[str]:
    seen = set(sent_ids)
    for event_id in ids:
        text = str(event_id or "")
        if text and text not in seen:
            sent_ids.append(text)
            seen.add(text)
    return sent_ids[-MAX_SENT_IDS:]


def _remember(sent_ids: list[str], events: Iterable[dict]) -> list[str]:
    return _remember_ids(sent_ids, (str(event.get("id") or "") for event in events))


def _push_new_events(
    settings: dict,
    events: list[dict],
    statuses: list[dict],
    meta: dict,
    sent_ids: list[str],
    rejected: dict[str, dict],
    versions: dict[str, str],
    metrics: dict | None = None,
) -> tuple[int, dict, list[str], list[str]]:
    failures: list[str] = []
    total = 0
    rejected_total = 0
    send_errors = 0
    rate_limited = False
    chunks = _build_chunks(events, statuses, meta)
    for index, chunk in enumerate(chunks):
        outcomes: list[dict] = []
        stop_after = False
        try:
            _send_chunk(
                settings["ingest_url"],
                settings["ingest_token"],
                chunk,
                statuses,
                meta,
                outcomes,
            )
        except RateLimited as exc:
            # 429 是服务端主动限流，不是故障：剩余事件下次巡检会重推，
            # 不能每 5 分钟发一封「推送失败」告警。持续限流会体现在状态页
            # 的 last_ingest 时间上，由健康检查兜底。
            print(f"[watchsite] {exc}；本轮停止，剩余事件下次再推", file=sys.stderr)
            rate_limited = True
            stop_after = True
        except Exception as exc:  # noqa: BLE001
            send_errors += 1
            failures.append(str(exc))
            print(f"[watchsite] {exc}", file=sys.stderr)

        accepted_this = 0
        rejected_this = 0
        chunk_by_id = {str(event.get("id") or ""): event for event in chunk}
        for outcome in outcomes:
            accepted_this += len(outcome["accepted_ids"])
            rejected_this += len(outcome["rejected"])
            sent_ids = _remember_ids(sent_ids, outcome["accepted_ids"])
            for event_id in outcome["accepted_ids"]:
                rejected.pop(event_id, None)
                event = chunk_by_id.get(event_id)
                if event is not None:
                    versions[event_id] = _event_digest(event)
            if outcome["rejected"]:
                preview = "；".join(
                    f"{item['id']}({item['reason']})" for item in outcome["rejected"][:5]
                )
                suffix = "" if len(outcome["rejected"]) <= 5 else f" 等 {len(outcome['rejected'])} 条"
                failures.append(
                    f"服务端拒绝 {len(outcome['rejected'])} 条事件：{preview}{suffix}"
                )
                for item in outcome["rejected"]:
                    event_id = str(item["id"] or "")
                    if not event_id:
                        continue
                    signature = _event_signature(chunk_by_id.get(event_id))
                    previous = rejected.get(event_id) or {}
                    count = (
                        max(0, _as_int(previous.get("count")) or 0) + 1
                        if previous.get("signature") == signature
                        else 1
                    )
                    rejected[event_id] = {
                        "count": count,
                        "reason": str(item.get("reason") or "unknown")[:200],
                        "signature": signature,
                        "last_ts": int(time.time()),
                    }
                    if count == MAX_REJECT_STREAK:
                        failures.append(
                            f"事件 {event_id} 连续 {count} 轮被拒（{item['reason']}），"
                            "已转为死信；修好事件内容后会自动重试"
                        )
        if (accepted_this or rejected_this) and not _save_push_state(
            sent_ids, rejected, versions, metrics
        ):
            # 写盘失败不能只打一行警告：下一轮会把已入库的事件整批重发，
            # 服务端虽然按 id 幂等，但 sent_ids 会一直追不上，属于必须修的故障。
            failures.append("push-state 写盘失败，已入库事件下一轮会被重复推送")
            print(
                "[watchsite] 错误：push-state 写盘失败，本轮成功事件下一轮会重发",
                file=sys.stderr,
            )
        total += accepted_this
        rejected_total += rejected_this
        print(
            f"[watchsite] 已推送第 {index + 1}/{len(chunks)} 块："
            f"{accepted_this} 条成功，{rejected_this} 条被服务端跳过"
        )
        if stop_after:
            break
    stats = {
        "accepted": total,
        "rejected": rejected_total,
        "send_errors": send_errors,
        "rate_limited": rate_limited,
        "push_ok": send_errors == 0 and not rate_limited,
    }
    return total, stats, sent_ids, failures


def _load_settings() -> dict:
    paths = default_paths()
    return {
        "docwatch_db": paths["docwatch_db"],
        "docwatch_root": paths["docwatch_root"],
        "monitor_events_dir": paths["monitor_events_dir"],
        "monitor_state_path": paths["monitor_state_path"],
        "ingest_url": os.getenv("INGEST_URL", DEFAULT_INGEST_URL).strip(),
        "ingest_token": os.getenv("INGEST_TOKEN", "").strip(),
    }


def _config_error(message: str, now_ts: int) -> int:
    """配置错误也要发一次告警：否则 token 被轮换 / 写错后站点会静默停更。"""
    print(f"[watchsite] {message}", file=sys.stderr)
    sent = alert.notify(
        "push-failure",
        "[DeepSeek 观测站] 推送配置错误",
        "推送器配置错误，本轮无法推送。\n\n" + message,
        now_ts,
    )
    print(f"[watchsite] 告警邮件：{'已发' if sent else '未发'}", file=sys.stderr)
    return 2


def _notify_qq(events: list[dict], now_ts: int, dry_run: bool) -> None:
    """QQ 群播报旁路：任何异常都只写日志，不影响网站推送。"""
    try:
        qq_summary = qq.notify_events(events, now_ts=now_ts, dry_run=dry_run)
        if qq_summary.get("enabled"):
            print(
                "[watchsite] QQ 播报："
                f"候选 {qq_summary.get('candidates', 0)} 条，"
                f"{qq_summary.get('cards', 0)} 张卡，"
                f"成功 {qq_summary.get('sent', 0)} 条，"
                f"失败 {qq_summary.get('failed', 0)} 张，"
                f"待补 {qq_summary.get('pending', 0)} 条"
                + (f"，过期跳过 {qq_summary.get('expired', 0)} 条" if qq_summary.get("expired") else "")
                + (f"，同版本合并 {qq_summary.get('merged', 0)} 条" if qq_summary.get("merged") else "")
                + ("，qq-state 写盘失败" if qq_summary.get("state_error") else "")
                + (f"（{qq_summary.get('skipped')}）" if qq_summary.get("skipped") else "")
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[watchsite] QQ 播报异常（不影响网站推送）：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 观测站事件推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不发请求")
    parser.add_argument("--once", action="store_true", help="单次执行（systemd 使用）")
    parser.add_argument("--verbose", action="store_true", help="打印每条事件标题")
    args = parser.parse_args(argv)

    now_ts = int(time.time())
    sent_ids, rejected, versions, metrics = _load_push_state()
    try:
        settings = _load_settings()
        batch = build_batch(
            settings["docwatch_db"],
            settings["docwatch_root"],
            settings["monitor_events_dir"],
            settings["monitor_state_path"],
            sent_ids=sent_ids,
        )
    except Exception as exc:  # noqa: BLE001
        message = f"采集失败：{type(exc).__name__}: {exc}"
        print(f"[watchsite] {message}", file=sys.stderr)
        if args.dry_run:
            # dry-run 是只读调试模式，不能因为本地路径没配好就往生产邮箱发告警。
            print("[watchsite] dry-run：跳过告警邮件", file=sys.stderr)
        else:
            sent = alert.notify(
                "push-failure",
                "[DeepSeek 观测站] 推送失败",
                "推送器本轮没有把数据完整送达。\n\n" + message,
                now_ts,
            )
            print(f"[watchsite] 告警邮件：{'已发' if sent else '未发'}", file=sys.stderr)
        return 1

    events = batch["events"]
    statuses = batch["statuses"]
    meta = batch["meta"]
    # 同一轮推送结果无法进入本轮请求，所以把上一轮落盘的指标带进 meta；
    # 站点面板显示的是「最近一次已完成推送」的汇总，缺数据时由前端显示「暂无」。
    meta.update(_metrics_meta(metrics))
    counts = meta.get("source_counts", {})

    failures: list[str] = []
    for item in meta.get("collection_errors") or []:
        if isinstance(item, dict):
            failures.append(str(item.get("detail") or item))
        else:
            failures.append(str(item))

    new_events = _new_events(events, sent_ids, versions)
    blocked = [event for event in new_events if _quality_blocked(event)]
    if blocked:
        print(
            f"[watchsite] 暂缓推送 {len(blocked)} 条证据与结论矛盾的前端事件；修复后会自动重推",
            file=sys.stderr,
        )
    if blocked:
        new_events = [event for event in new_events if not _quality_blocked(event)]
    new_events, dead_ids = _split_dead_letters(new_events, rejected)
    if dead_ids:
        print(
            f"[watchsite] 跳过 {len(dead_ids)} 条连续被拒事件（死信，内容修好后自动重试）",
            file=sys.stderr,
        )
    if state.state_dir() is None:
        print("[watchsite] 没有可写状态目录：本次按全量发送，且无法记录增量游标", file=sys.stderr)

    print(
        f"[watchsite] 采集完成：{int(meta.get('event_count', len(events)))} 条事件"
        f"（本轮新增 {len(new_events)} 条），{len(statuses)} 个来源状态；{counts}"
    )
    if args.verbose or args.dry_run:
        for event in new_events[:50]:
            print(f"  - {event['id']} [{event['level']}] {event['title']}")
        if len(new_events) > 50:
            print(f"  … 另有 {len(new_events) - 50} 条")
    for failure in failures:
        print(f"[watchsite] 采集异常：{failure}", file=sys.stderr)

    if args.dry_run:
        _notify_qq(new_events, now_ts, True)
        print("[watchsite] dry-run：未发送任何网络请求")
        return 0

    if not settings["ingest_url"].startswith(("http://", "https://")):
        return _config_error("INGEST_URL 必须是 http(s) 地址", now_ts)
    try:
        _validate_ingest_url(settings["ingest_url"])
    except ValueError as exc:
        return _config_error(str(exc), now_ts)
    if not settings["ingest_token"]:
        return _config_error("缺少 INGEST_TOKEN，推送器无法认证", now_ts)

    sent_before = set(sent_ids)
    total, stats, sent_ids, push_failures = _push_new_events(
        settings,
        new_events,
        statuses,
        meta,
        sent_ids,
        rejected,
        versions,
        metrics,
    )
    failures.extend(push_failures)

    # 推送结果落盘（无论是否有个别失败），下一轮 meta 会把它们带上站。
    metrics = _build_push_metrics(
        now_ts=now_ts,
        events=events,
        new_event_count=len(new_events),
        stats=stats,
        sent_ids=sent_ids,
        rejected=rejected,
        quality_counts=_quality_counts(events),
        previous=metrics,
    )
    if state.state_dir() is not None:
        if not _save_push_state(sent_ids, rejected, versions, metrics):
            failures.append("push-state 写盘失败（最终指标/已发游标未保存）")
            print(
                "[watchsite] 错误：push-state 最终写盘失败，下一轮可能重复推送",
                file=sys.stderr,
            )

    # QQ 放在网站推送之后，只播报服务端已接受的事件；不能拖慢或改变网站推送。
    sent_after = set(sent_ids)
    accepted_new = [
        event
        for event in new_events
        if str(event.get("id") or "") in sent_after
        and str(event.get("id") or "") not in sent_before
    ]
    # 即使本轮没有新的已接受事件，也要调用一次 QQ：qq.notify_events 会把
    # qq-state 里上一轮发送失败/超出卡片上限的 pending 继续重试；否则在群里
    # 长时间没有新事件时，pending 会一直压到过期（24h）而永远不会重发。
    # 网站推送整轮失败（push_ok=False 且没有已接受事件）时不碰 QQ，保持
    # 「QQ 只在网站推送成功后执行」的边界。
    if accepted_new or stats.get("push_ok"):
        _notify_qq(accepted_new, now_ts, False)

    if failures:
        body = "\n".join(failures[:10])
        sent = alert.notify(
            "push-failure",
            "[DeepSeek 观测站] 推送失败",
            "推送器本轮没有把数据完整送达。\n\n" + body,
            now_ts,
        )
        print(f"[watchsite] 推送失败 {len(failures)} 项；告警邮件：{'已发' if sent else '未发'}", file=sys.stderr)
        return 1

    alert.clear("push-failure")
    print(f"[watchsite] 推送完成：{total} 条新事件，{len(statuses)} 个来源状态")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
