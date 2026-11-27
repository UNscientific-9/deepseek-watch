"""把站点事件播报到 QQ 群（合并转发卡片）。

设计约束：
- 只读 ``watchsite.push`` 已经归一化好的事件，不重新解释 docwatch / webwatch 数据。
- 发送失败只记日志、保留 pending，不影响网站推送，也不改变 push 的退出码。
- 只用标准库；直连本机 NapCat 的 OneBot v11 WebSocket（默认 ws://127.0.0.1:3001）。
- QQ 播报有自己的 ``qq-state.json``，与网站推送的 ``push-state.json`` 完全隔离。

环境变量（写入你的服务环境文件，权限 600）：
    QQ_NOTIFY_ENABLED=1
    QQ_NOTIFY_GROUPS=<your-group-id>
    QQ_NOTIFY_MIN_LEVEL=2
    QQ_NOTIFY_WS=ws://127.0.0.1:3001
    QQ_NOTIFY_BOT_ID=<your-bot-qq>
    QQ_NOTIFY_TIMEOUT=8
    QQ_NOTIFY_TOTAL_BUDGET=20
    QQ_NOTIFY_MAX_AGE_HOURS=24
    # 可选：NapCat OneBot token（没有就留空）
    QQ_NOTIFY_TOKEN=
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.parse

from . import state
from .collect import BEIJING, LEVEL_LABELS, SOURCE_LABELS

QQ_STATE_FILE = "qq-state.json"
MAX_SENT_IDS = 5000
# 一轮最多能装 MAX_CARDS×CARD_MAX_NODES 条；pending 留足回放窗口，避免截断丢事件。
MAX_PENDING = 2000
# 同一个前端版本（commit）会先后部署到 chat / platform 等多个站点，各站点会
# 各生成一条内容相同的 page_change 事件。只播第一条，后续站点在窗口内不重复播；
# 窗口不宜太短（两站上线可能相隔十几分钟到几小时），也不宜无限长（避免误伤很
# 久以后重新上线的同版本）。
COMMIT_DEDUPE_SECONDS = 24 * 3600
MAX_SENT_COMMITS = 200
# 复核升级播报的独立去重表。它和 sent_ids 分开：普通事件按 id 只播一次，
# 复核升级允许在“已播过的旧事件”上再播一次，但每个事件最多一次。
MAX_RECHECK_SENT = 500


def _payload(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _recheck_upgrade(event: dict) -> bool:
    """事件是否带有「复核把级别升到 3」的标记。"""
    recheck = _payload(event).get("recheck")
    if isinstance(recheck, dict) and recheck.get("level_applied"):
        try:
            from_level = int(recheck.get("from_level") or 0)
            to_level = int(recheck.get("to_level") or 0)
        except (TypeError, ValueError):
            return False
        return from_level < 3 and to_level == 3
    # pending 里的精简事件没有 payload，读 compact 时保存的标记。
    if event.get("recheck_upgrade"):
        return (
            _as_int(event.get("recheck_from_level")) < 3
            and _as_int(event.get("recheck_to_level")) == 3
        )
    return False

# 与 notification-service/pi-qq 的实测边界保持一致：整卡约 5500 字是硬边界，
# 这里取一半作为正常上限，给 QQ 预览文本留出余量。
CARD_MAX_CHARS = 2600
CARD_MAX_NODES = 30
NODE_TEXT_LIMIT = 900
MAX_CARDS = 4
DEFAULT_TIMEOUT = 8
DEFAULT_WS = "ws://127.0.0.1:3001"
DEFAULT_BOT_ID = ""
DEFAULT_NICKNAME = "DeepSeek Watch"

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class QQNotifyError(RuntimeError):
    """QQ 播报失败；调用方只记日志，不影响主流程。"""


# ============================================================
# 配置
# ============================================================


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def _env_groups() -> list[str]:
    raw = os.getenv("QQ_NOTIFY_GROUPS", "").strip()
    if not raw:
        return []
    groups: list[str] = []
    for item in raw.replace(";", ",").split(","):
        text = item.strip()
        if text and text not in groups:
            groups.append(text)
    return groups


def settings() -> dict:
    return {
        "enabled": _env_bool("QQ_NOTIFY_ENABLED", False),
        "groups": _env_groups(),
        "min_level": _env_int("QQ_NOTIFY_MIN_LEVEL", 2, 0, 3),
        "ws_url": os.getenv("QQ_NOTIFY_WS", DEFAULT_WS).strip() or DEFAULT_WS,
        "bot_id": os.getenv("QQ_NOTIFY_BOT_ID", DEFAULT_BOT_ID).strip() or DEFAULT_BOT_ID,
        "timeout": _env_int("QQ_NOTIFY_TIMEOUT", DEFAULT_TIMEOUT, 1, 60),
        "total_budget": _env_int("QQ_NOTIFY_TOTAL_BUDGET", 20, 1, 60),
        "max_age_hours": _env_int("QQ_NOTIFY_MAX_AGE_HOURS", 24, 1, 168),
        "token": os.getenv("QQ_NOTIFY_TOKEN", "").strip(),
        "nickname": os.getenv("QQ_NOTIFY_NICKNAME", DEFAULT_NICKNAME).strip() or DEFAULT_NICKNAME,
    }


# ============================================================
# 卡片排版（纯函数，测试重点）
# ============================================================


def _text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _format_ts(value: object) -> str:
    try:
        seconds = int(value)  # type: ignore[arg-type]
        return dt.datetime.fromtimestamp(seconds, BEIJING).strftime("%m-%d %H:%M") if seconds > 0 else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _level_label(level: object) -> str:
    try:
        number = int(level)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0
    return LEVEL_LABELS.get(max(0, min(3, number)), "未知")


def _source_label(source: object) -> str:
    return SOURCE_LABELS.get(str(source or ""), str(source or "未知来源"))


def _event_node(event: dict) -> str:
    level = _as_int(event.get("level"))
    title = _text(event.get("title") or "检测到变化", 100)
    lines = [f"【{_level_label(level)}】{title}"]
    when = _format_ts(event.get("changed_ts"))
    source = _source_label(event.get("source"))
    lines.append(f"来源：{source} · {when}" if when else f"来源：{source}")
    summary = _text(event.get("summary"), 220)
    if summary:
        lines.append(f"摘要：{summary}")
    impact = _text(event.get("impact"), 180)
    if impact:
        lines.append(f"影响：{impact}")
    if _recheck_upgrade(event):
        recheck = _payload(event).get("recheck")
        recheck = recheck if isinstance(recheck, dict) else {}
        from_level = _as_int(recheck.get("from_level", event.get("recheck_from_level")))
        to_level = _as_int(recheck.get("to_level", event.get("recheck_to_level")))
        lines.append(f"复核：级别由 {from_level} 上调为 {to_level} 级")
    url = str(event.get("page_url") or "").strip()
    if url.startswith(("http://", "https://")):
        lines.append(f"原文：{_text(url, 300)}")
    return _text("\n".join(lines), NODE_TEXT_LIMIT)


def _header_node(count: int, now_ts: int) -> str:
    when = dt.datetime.fromtimestamp(int(now_ts), BEIJING).strftime("%Y-%m-%d %H:%M")
    return f"📢 DeepSeek 最新动向（{count} 条）\n{when}（北京时间）"


def _event_commit(event: dict) -> str:
    """取事件对应的前端 commit；pending 里的精简事件直接从顶层读。"""
    commit = str(event.get("commit_id") or "").strip()
    if commit:
        return commit
    payload = event.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("commit_id") or "").strip()
    return ""


def _commit_change(event: dict) -> str:
    """只有「前端版本变化」事件才参与按 commit 合并，返回变化后的 commit。

    纯正文 / 资源变化事件的 payload 也带当前 commit（并未变化），不能拿来
    判重，否则同一个 commit 下后来的独立变化会被误吞，造成漏播。
    """
    if str(event.get("kind") or "") != "page_change":
        return ""
    commit = _event_commit(event)
    if not commit:
        return ""
    payload = event.get("payload")
    changes = payload.get("changes") if isinstance(payload, dict) else None
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            if str(change.get("kind") or "") != "commit":
                continue
            new = str(change.get("new") or "").strip()
            if new and new == commit:
                return commit
        return ""
    # pending 里的精简事件没有 payload.changes，读取 compact 时保存的标记。
    return commit if event.get("commit_change") else ""


def _compact_event(event: dict) -> dict:
    """pending 只存排版需要的字段，避免状态文件被 payload 撑大。"""
    payload = event.get("payload")
    backfill = bool(payload.get("backfill")) if isinstance(payload, dict) else False
    recheck = payload.get("recheck") if isinstance(payload, dict) else None
    recheck = recheck if isinstance(recheck, dict) else {}
    return {
        "id": str(event.get("id") or ""),
        "source": str(event.get("source") or ""),
        "kind": str(event.get("kind") or ""),
        "level": _as_int(event.get("level")),
        "title": str(event.get("title") or ""),
        "summary": str(event.get("summary") or ""),
        "impact": str(event.get("impact") or ""),
        "action": str(event.get("action") or ""),
        "page_url": str(event.get("page_url") or ""),
        "slug": str(event.get("slug") or ""),
        "changed_ts": _as_int(event.get("changed_ts")),
        # 同一个前端版本可能在多个站点各生成一条事件，播报时用它去重。
        "commit_id": _event_commit(event),
        "commit_change": bool(_commit_change(event)),
        # 历史补录（含 DSH 完整更新日志回填）只更新站点，不重新播 QQ。
        "backfill": backfill,
        # 复核升级由独立播报路径处理；普通 pending 不应重播它。
        "recheck_upgrade": _recheck_upgrade(event),
        "recheck_from_level": _as_int(recheck.get("from_level")),
        "recheck_to_level": _as_int(recheck.get("to_level")),
    }


def _sort_key(event: dict) -> tuple[int, int, str]:
    level = _as_int(event.get("level"))
    changed = _as_int(event.get("changed_ts"))
    return (-level, -changed, str(event.get("id") or ""))


def _pack_cards(events: list[dict], now_ts: int | None = None) -> tuple[list[dict], list[dict]]:
    """把事件排成 1~MAX_CARDS 张合并转发卡片，返回 ``(cards, leftovers)``。

    leftovers 是超出卡片上限、需要下一轮继续播报的事件；调用方必须保留它们。
    """
    now_ts = int(now_ts if now_ts is not None else time.time())
    if not events:
        return [], []
    ordered = sorted((event for event in events if event.get("id")), key=_sort_key)
    header = _header_node(len(ordered), now_ts)
    cards: list[dict] = []
    current_nodes: list[str] = [header]
    current_events: list[dict] = []
    current_chars = len(header)
    leftovers: list[dict] = []

    def flush() -> None:
        nonlocal current_nodes, current_events, current_chars
        if current_events:
            cards.append({"nodes": current_nodes, "events": current_events})
        current_nodes = []
        current_events = []
        current_chars = 0

    for index, event in enumerate(ordered):
        if len(cards) >= MAX_CARDS:
            leftovers = ordered[index:]
            break
        node = _event_node(event)
        extra = len(node) + (1 if current_nodes else 0)
        if current_events and (
            len(current_nodes) + 1 > CARD_MAX_NODES
            or current_chars + extra > CARD_MAX_CHARS
        ):
            flush()
            if len(cards) >= MAX_CARDS:
                leftovers = ordered[index:]
                break
        if not current_nodes:
            current_nodes = [header]
            current_chars = len(header)
        current_nodes.append(node)
        current_events.append(event)
        current_chars += extra

    flush()
    # 卡头按每张卡实际包含的条数重写，避免卡内数量与标题不一致。
    for card in cards:
        if card["nodes"]:
            card["nodes"][0] = _header_node(len(card["events"]), now_ts)
    return cards, leftovers


def build_cards(events: list[dict], now_ts: int | None = None) -> list[dict]:
    """兼容测试/预览的入口：只返回卡片，超出部分由 ``_pack_cards`` 的 leftovers 负责。"""
    cards, _ = _pack_cards(events, now_ts)
    return cards


def render_cards(cards: list[dict]) -> str:
    """把卡片渲染成纯文本预览。"""
    if not cards:
        return "（没有达到播报级别的事件）"
    parts: list[str] = []
    for index, card in enumerate(cards, start=1):
        parts.append(f"===== 卡片 {index}/{len(cards)} =====")
        parts.extend(card["nodes"])
    return "\n\n".join(parts)


def build_preview(events: list[dict], now_ts: int | None = None) -> str:
    """给 --dry-run / 测试用的纯文本预览。"""
    cards, leftovers = _pack_cards(events, now_ts)
    text = render_cards(cards)
    if leftovers:
        text += f"\n\n（另有 {len(leftovers)} 条未展开，下一轮继续）"
    return text


# ============================================================
# OneBot v11 WebSocket（标准库实现，只用于本机 127.0.0.1）
# ============================================================


def _parse_ws_url(url: str) -> tuple[str, int, str]:
    parts = urllib.parse.urlsplit(str(url or ""))
    if parts.scheme != "ws":
        raise QQNotifyError(f"QQ_NOTIFY_WS 只支持 ws://，收到 {url!r}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def _ws_accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _encode_frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        payload = str(payload).encode("utf-8")
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not mask:
        return bytes(header) + bytes(payload)
    key = os.urandom(4)
    header.extend(key)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


def _decode_frame(data: bytes):
    """解析一帧；数据不完整返回 None，完整返回 (fin, opcode, payload, consumed)。"""
    if len(data) < 2:
        return None
    first, second = data[0], data[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    pos = 2
    if length == 126:
        if len(data) < pos + 2:
            return None
        length = struct.unpack("!H", data[pos : pos + 2])[0]
        pos += 2
    elif length == 127:
        if len(data) < pos + 8:
            return None
        length = struct.unpack("!Q", data[pos : pos + 8])[0]
        pos += 8
    key = None
    if masked:
        if len(data) < pos + 4:
            return None
        key = bytes(data[pos : pos + 4])
        pos += 4
    if len(data) < pos + length:
        return None
    payload = bytes(data[pos : pos + length])
    if key:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return fin, opcode, payload, pos + length


class _WSConnection:
    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self.sock = sock
        self.buffer = bytes(initial)
        self._fragment_opcode: int | None = None
        self._fragment_payload = b""

    def send_json(self, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.sock.sendall(_encode_frame(0x1, payload, mask=True))

    def _next_message(self, deadline: float) -> dict:
        while True:
            parsed = _decode_frame(self.buffer)
            while parsed is not None:
                fin, opcode, payload, consumed = parsed
                self.buffer = self.buffer[consumed:]
                if opcode == 0x9:  # ping → pong
                    self.sock.sendall(_encode_frame(0xA, payload, mask=True))
                elif opcode == 0xA:  # pong
                    pass
                elif opcode == 0x8:  # close
                    raise QQNotifyError("NapCat 关闭了 WebSocket 连接")
                elif opcode in (0x1, 0x2, 0x0):
                    if opcode == 0x0:
                        if self._fragment_opcode is None:
                            raise QQNotifyError("收到非法的 WebSocket 续帧")
                        self._fragment_payload += payload
                        if fin:
                            message = self._fragment_payload
                            self._fragment_opcode = None
                            self._fragment_payload = b""
                            if message:
                                return json.loads(message.decode("utf-8", "replace"))
                    elif fin:
                        if payload:
                            return json.loads(payload.decode("utf-8", "replace"))
                    else:
                        self._fragment_opcode = opcode
                        self._fragment_payload = payload
                parsed = _decode_frame(self.buffer)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QQNotifyError("等待 NapCat 回包超时")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                raise QQNotifyError(f"读取 NapCat WebSocket 失败：{type(exc).__name__}: {exc}") from exc
            if not chunk:
                raise QQNotifyError("NapCat WebSocket 连接已断开")
            self.buffer += chunk

    def request(self, action: str, params: dict, timeout: float):
        echo = f"qqnotify-{os.getpid()}-{time.time_ns()}"
        self.send_json({"action": action, "params": params, "echo": echo})
        deadline = time.monotonic() + max(1.0, float(timeout))
        while True:
            message = self._next_message(deadline)
            if not isinstance(message, dict) or message.get("echo") != echo:
                continue
            if message.get("status") == "ok" and message.get("retcode", 0) == 0:
                return message.get("data")
            detail = message.get("message") or message.get("wording") or ""
            raise QQNotifyError(
                f"OneBot 返回失败 status={message.get('status')} "
                f"retcode={message.get('retcode')} {detail}".strip()
            )


def _open_ws(url: str, timeout: float, token: str = "") -> _WSConnection:
    host, port, path = _parse_ws_url(url)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        extra_headers = f"Authorization: Bearer {token}\r\n" if token else ""
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"{extra_headers}"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise QQNotifyError("NapCat 在 WebSocket 握手阶段关闭了连接")
            data += chunk
            if len(data) > 65536:
                raise QQNotifyError("NapCat WebSocket 握手响应过大")
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise QQNotifyError(f"NapCat WebSocket 握手失败：{lines[0] if lines else '空响应'}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = _ws_accept_key(key)
        if headers.get("sec-websocket-accept") != expected:
            raise QQNotifyError("NapCat WebSocket 握手校验失败（accept 不匹配）")
        return _WSConnection(sock, rest)
    except Exception:
        sock.close()
        raise


def _onebot_nodes(lines: list[str], nickname: str, bot_id: str) -> list[dict]:
    return [
        {
            "type": "node",
            "data": {
                "user_id": str(bot_id),
                "nickname": nickname,
                "message_seq": 0,
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }
        for text in lines
    ]


def send_group_forward(
    ws_url: str,
    group_id: str,
    lines: list[str],
    *,
    nickname: str = DEFAULT_NICKNAME,
    bot_id: str = DEFAULT_BOT_ID,
    timeout: float = DEFAULT_TIMEOUT,
    token: str = "",
) -> object:
    """连一次本机 NapCat，发一张合并转发卡片，等 OneBot 明确回包后返回。"""
    if not lines:
        raise QQNotifyError("卡片内容为空，不发送")
    connection = _open_ws(ws_url, timeout, token=token)
    try:
        return connection.request(
            "send_group_forward_msg",
            {
                "group_id": str(group_id),
                "message_seq": 0,
                "messages": _onebot_nodes(lines, nickname, bot_id),
            },
            timeout,
        )
    finally:
        try:
            connection.sock.close()
        except OSError:
            pass


# ============================================================
# 状态与播报主流程
# ============================================================


def _load_state() -> tuple[list[str], dict[str, dict], dict[str, int], str]:
    data = state.load_json(QQ_STATE_FILE, {})
    raw_ids = data.get("sent_ids")
    sent_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
    raw_pending = data.get("pending")
    pending: dict[str, dict] = {}
    if isinstance(raw_pending, list):
        for item in raw_pending:
            if isinstance(item, dict) and item.get("id"):
                pending[str(item["id"])] = _compact_event(item)
    raw_commits = data.get("sent_commits")
    sent_commits: dict[str, int] = {}
    if isinstance(raw_commits, dict):
        for commit, stamp in raw_commits.items():
            text = str(commit or "").strip()
            value = _as_int(stamp)
            if text and value > 0:
                sent_commits[text] = value
    return sent_ids[-MAX_SENT_IDS:], pending, sent_commits, str(data.get("last_error") or "")


def _load_recheck_sent() -> dict[str, dict]:
    data = state.load_json(QQ_STATE_FILE, {})
    raw = data.get("recheck_sent")
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for event_id, record in raw.items():
            if isinstance(record, dict):
                out[str(event_id)] = record
    return out


def _save_recheck_sent(records: dict[str, dict]) -> bool:
    """只更新 recheck_sent，保留 qq-state 的其它键（sent_ids/pending/...）。"""
    ordered = sorted(
        records.items(),
        key=lambda item: max(0, _as_int(item[1].get("ts"))),
        reverse=True,
    )[:MAX_RECHECK_SENT]
    data = state.load_json(QQ_STATE_FILE, {})
    data["recheck_sent"] = dict(ordered)
    data["updated_ts"] = int(time.time())
    return state.save_json(QQ_STATE_FILE, data)


def notify_recheck_upgrade(
    event: dict,
    *,
    now_ts: int | None = None,
    dry_run: bool = False,
    config: dict | None = None,
    sender=None,
) -> dict:
    """复核升级到 3 级时的单独播报；不参与普通 sent_ids 去重。"""
    cfg = config or settings()
    summary = {
        "enabled": bool(cfg.get("enabled")),
        "candidates": 0,
        "sent": 0,
        "failed": 0,
        "skipped": "",
        "cards": 0,
    }
    if not _recheck_upgrade(event):
        summary["skipped"] = "不是复核升级事件"
        return summary
    if not cfg.get("enabled"):
        summary["skipped"] = "QQ_NOTIFY_ENABLED 未开启"
        return summary
    groups = cfg.get("groups") or []
    if not groups:
        summary["skipped"] = "QQ_NOTIFY_GROUPS 为空"
        return summary
    event_id = str(event.get("id") or "")
    if not event_id:
        summary["skipped"] = "事件没有 id"
        return summary
    sent = _load_recheck_sent()
    if event_id in sent:
        summary["skipped"] = "该事件的复核升级已播报"
        return summary
    now_ts = int(now_ts if now_ts is not None else time.time())
    cards, _ = _pack_cards([event], now_ts)
    summary["candidates"] = 1
    summary["cards"] = len(cards)
    if dry_run:
        print(render_cards(cards))
        summary["skipped"] = "dry-run：未发送"
        return summary
    if state.state_dir() is None:
        summary["skipped"] = "没有可写状态目录，跳过 QQ 播报"
        print("[watchsite.qq] 没有可写状态目录，跳过复核升级播报", file=sys.stderr)
        return summary
    send = sender or (
        lambda group_id, lines: send_group_forward(
            str(cfg.get("ws_url") or DEFAULT_WS),
            str(group_id),
            lines,
            nickname=str(cfg.get("nickname") or DEFAULT_NICKNAME),
            bot_id=str(cfg.get("bot_id") or DEFAULT_BOT_ID),
            timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
            token=str(cfg.get("token") or ""),
        )
    )
    failed = 0
    for card in cards:
        for group_id in groups:
            try:
                send(str(group_id), list(card["nodes"]))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(
                    f"[watchsite.qq] 复核升级播报发往群 {group_id} 失败："
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
    if failed:
        summary["failed"] = failed
        return summary
    sent[event_id] = {
        "ts": now_ts,
        "level": _as_int(event.get("level")),
    }
    if not _save_recheck_sent(sent):
        summary["failed"] = 1
        summary["skipped"] = "qq-state 写盘失败"
        print("[watchsite.qq] 错误：复核升级播报已发，但 qq-state 写盘失败", file=sys.stderr)
        return summary
    summary["sent"] = 1
    return summary


def _save_state(
    sent_ids: list[str],
    pending: dict[str, dict],
    sent_commits: dict[str, int] | None = None,
    last_error: str = "",
    recheck_sent: dict[str, dict] | None = None,
) -> tuple[bool, int, int]:
    """保存状态；返回 ``(是否成功, 实际保留条数, 被截断条数)``。"""
    if recheck_sent is None:
        recheck_sent = _load_recheck_sent()
    ordered_pending = sorted(pending.values(), key=_sort_key)
    kept_pending = ordered_pending[:MAX_PENDING]
    dropped = max(0, len(ordered_pending) - len(kept_pending))
    ordered_commits = sorted(
        ((sent_commits or {}).items()),
        key=lambda item: item[1],
        reverse=True,
    )[:MAX_SENT_COMMITS]
    ordered_rechecks = sorted(
        recheck_sent.items(),
        key=lambda item: max(0, _as_int(item[1].get("ts"))),
        reverse=True,
    )[:MAX_RECHECK_SENT]
    ok = state.save_json(
        QQ_STATE_FILE,
        {
            "sent_ids": sent_ids[-MAX_SENT_IDS:],
            "pending": kept_pending,
            "sent_commits": dict(ordered_commits),
            "recheck_sent": dict(ordered_rechecks),
            "last_error": last_error[:500],
            "updated_ts": int(time.time()),
        },
    )
    return ok, len(kept_pending), dropped


def _merge_candidates(
    new_events: list[dict],
    pending: dict[str, dict],
    sent_ids: list[str],
    min_level: int,
) -> list[dict]:
    merged: dict[str, dict] = dict(pending)
    for event in new_events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        # 同 id 的新事件内容优先：pending 里可能是失败轮的旧摘要。
        merged[event_id] = _compact_event(event)
    sent = set(sent_ids)
    result = []
    for event_id, event in merged.items():
        if event_id in sent:
            continue
        # 复核升级走独立播报路径；普通路径跳过，避免同一事件双播。
        if _recheck_upgrade(event):
            continue
        payload = event.get("payload")
        is_backfill = bool(event.get("backfill")) or (
            isinstance(payload, dict) and bool(payload.get("backfill"))
        )
        # 历史补录按约定不播 QQ。过去靠「补录事件级别 1」自然过滤；现在
        # DSH 历史事件在站点统一为 3 级，必须显式排除，否则回填完整更新日志
        # 会把几十条旧版本重新刷到群里。
        if is_backfill:
            continue
        if _as_int(event.get("level")) >= min_level:
            result.append(event)
    return sorted(result, key=_sort_key)


def _dedupe_same_commit(
    candidates: list[dict],
    sent_commits: dict[str, int],
) -> tuple[list[dict], list[dict]]:
    """同一前端版本（commit）只保留一条播报。

    同一个 commit 先后部署到 chat / platform 等站点时，各站点会生成一条
    ``page_change`` 事件，改动内容相同。这里按 commit 去重：窗口内已经播过
    的版本不再播；同一轮里同版本的后续事件也不重复播。

    返回 ``(保留播报的事件, 被合并的事件)``。被合并的事件不能直接丢弃：
    主事件发送失败时它们要随 pending 重试，只有主事件成功后才标记为已发。
    """
    kept: list[dict] = []
    merged: list[dict] = []
    seen = set(sent_commits)
    for event in sorted((item for item in candidates if item.get("id")), key=_sort_key):
        commit = _commit_change(event)
        if commit:
            if commit in seen:
                merged.append(event)
                continue
            seen.add(commit)
        kept.append(event)
    return kept, merged


def notify_events(
    events: list[dict],
    *,
    now_ts: int | None = None,
    dry_run: bool = False,
    config: dict | None = None,
) -> dict:
    """把新事件播报到 QQ 群；失败不抛异常，只返回统计并保留 pending。"""
    cfg = config or settings()
    summary = {
        "enabled": bool(cfg.get("enabled")),
        "candidates": 0,
        "expired": 0,
        "merged": 0,
        "cards": 0,
        "sent": 0,
        "failed": 0,
        "pending": 0,
        "skipped": "",
        "state_error": False,
    }
    if not cfg.get("enabled"):
        summary["skipped"] = "QQ_NOTIFY_ENABLED 未开启"
        return summary
    groups = cfg.get("groups") or []
    if not groups:
        summary["skipped"] = "QQ_NOTIFY_GROUPS 为空"
        return summary
    now_ts = int(now_ts if now_ts is not None else time.time())
    raw_min_level = cfg.get("min_level")
    min_level = 2 if raw_min_level is None else max(0, min(3, _as_int(raw_min_level, 2)))
    sent_ids, pending, sent_commits, _ = _load_state()
    # 只保留窗口内播过的 commit；过期的移出，否则很久以后重新上线的同版本
    # 会被误当成「已播过」而漏播。
    sent_commits = {
        commit: stamp
        for commit, stamp in sent_commits.items()
        if now_ts - stamp <= COMMIT_DEDUPE_SECONDS
    }
    # 已在 sent_ids 里的 pending 是历史遗留（例如状态写入失败后重试成功、或
    # 过期事件被标记为已发）；不清掉会永久占用 MAX_PENDING 名额，最终把真正
    # 待发的事件挤出去。sent_ids 只在整张卡片的所有群都发送成功后才写入，
    # 因此这里删除不会导致漏发。
    sent_set = set(sent_ids)
    if sent_set:
        pending = {
            event_id: event
            for event_id, event in pending.items()
            if event_id not in sent_set
        }
    # 防止状态目录丢失后把 10 天历史一次性刷到群里：只播报最近 N 小时的变化。
    oldest_ts = now_ts - int(cfg.get("max_age_hours") or 24) * 3600
    expired = 0
    # 低于 min_level 的 pending 不会进入 candidates，不能只靠下面的 candidates
    # 过期循环清理；这里按同一时间窗口清掉，避免它们永久占用 MAX_PENDING。
    for event_id, event in list(pending.items()):
        changed = _as_int(event.get("changed_ts"))
        if changed and changed < oldest_ts:
            pending.pop(event_id, None)
            if event_id not in sent_set:
                sent_ids.append(event_id)
                sent_set.add(event_id)
            expired += 1
    candidates_all = _merge_candidates(events, pending, sent_ids, min_level)
    candidates = []
    for event in candidates_all:
        changed = _as_int(event.get("changed_ts"))
        if changed and changed < oldest_ts:
            event_id = str(event.get("id") or "")
            if event_id:
                if event_id not in sent_set:
                    sent_ids.append(event_id)
                    sent_set.add(event_id)
                pending.pop(event_id, None)
            expired += 1
            continue
        candidates.append(event)
    candidates, merged_events = _dedupe_same_commit(candidates, sent_commits)
    summary["candidates"] = len(candidates)
    summary["merged"] = len(merged_events)
    summary["expired"] = expired
    summary["pending"] = len(pending)
    cards, leftovers = _pack_cards(candidates, now_ts)
    summary["cards"] = len(cards)

    if dry_run:
        print(render_cards(cards))
        if leftovers:
            print(f"\n（另有 {len(leftovers)} 条未展开，下一轮继续）")
        if merged_events:
            print(f"（另有 {len(merged_events)} 条同一前端版本的站点事件已合并，不重复播报）")
        summary["skipped"] = "dry-run：未发送"
        return summary

    if not cards and not leftovers:
        # 仍要把被合并事件标记为已发，否则下一轮会被反复当成候选。
        for event in merged_events:
            event_id = str(event.get("id") or "")
            if not event_id:
                continue
            commit = _commit_change(event)
            if commit and commit in sent_commits:
                if event_id not in sent_set:
                    sent_ids.append(event_id)
                    sent_set.add(event_id)
            else:
                pending[event_id] = _compact_event(event)
        summary["pending"] = len(pending)
        _save_state(sent_ids, pending, sent_commits, "")
        return summary

    if state.state_dir() is None:
        summary["skipped"] = "没有可写状态目录，跳过 QQ 播报"
        print("[watchsite.qq] 没有可写状态目录，跳过 QQ 播报", file=sys.stderr)
        return summary

    last_error = ""
    total_budget = float(cfg.get("total_budget") or 20)
    started = time.monotonic()
    for card_index, card in enumerate(cards):
        remaining_budget = total_budget - (time.monotonic() - started)
        if remaining_budget <= 1:
            for rest in cards[card_index:]:
                for event in rest["events"]:
                    event_id = str(event.get("id") or "")
                    if event_id:
                        pending[event_id] = _compact_event(event)
            summary["skipped"] = "QQ 发送总预算用尽，剩余下一轮"
            print("[watchsite.qq] QQ 发送总预算用尽，剩余卡片保留到下一轮", file=sys.stderr)
            break
        failed_groups: list[str] = []
        for group_id in groups:
            try:
                send_group_forward(
                    str(cfg.get("ws_url") or DEFAULT_WS),
                    str(group_id),
                    list(card["nodes"]),
                    nickname=str(cfg.get("nickname") or DEFAULT_NICKNAME),
                    bot_id=str(cfg.get("bot_id") or DEFAULT_BOT_ID),
                    timeout=min(float(cfg.get("timeout") or DEFAULT_TIMEOUT), remaining_budget),
                    token=str(cfg.get("token") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                failed_groups.append(str(group_id))
                last_error = f"{type(exc).__name__}: {exc}"
                print(
                    f"[watchsite.qq] 发往群 {group_id} 失败：{last_error}",
                    file=sys.stderr,
                )
        if failed_groups:
            summary["failed"] += 1
            for event in card["events"]:
                event_id = str(event.get("id") or "")
                if event_id:
                    pending[event_id] = _compact_event(event)
            ok, kept, dropped = _save_state(sent_ids, pending, sent_commits, last_error)
            summary["pending"] = kept
            if not ok:
                summary["state_error"] = True
                print("[watchsite.qq] 错误：qq-state 写盘失败，下一轮可能重复播报或丢失待播报事件", file=sys.stderr)
            if dropped:
                print(f"[watchsite.qq] 警告：pending 超过 {MAX_PENDING} 条，丢弃最旧 {dropped} 条", file=sys.stderr)
            continue
        for event in card["events"]:
            event_id = str(event.get("id") or "")
            if not event_id:
                continue
            if event_id not in sent_ids:
                sent_ids.append(event_id)
            pending.pop(event_id, None)
            commit = _commit_change(event)
            if commit:
                # 只有真正发送成功才记录版本；失败轮不能把同版本的其他站点
                # 事件当成「已播」而丢掉。
                sent_commits[commit] = now_ts
        summary["sent"] += len(card["events"])

    # 超出 MAX_CARDS 的事件留给下一轮；不静默丢弃。
    for event in leftovers:
        event_id = str(event.get("id") or "")
        if event_id:
            pending[event_id] = _compact_event(event)

    # 被合并的同版本事件：主事件已发成功（sent_commits 里有）就直接标记已发；
    # 主事件没发成功则留在 pending，下一轮随主事件一起重试，避免漏播。
    for event in merged_events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        commit = _commit_change(event)
        if commit and commit in sent_commits:
            if event_id not in sent_set:
                sent_ids.append(event_id)
                sent_set.add(event_id)
            pending.pop(event_id, None)
        else:
            pending[event_id] = _compact_event(event)

    ok, kept, dropped = _save_state(
        sent_ids,
        pending,
        sent_commits,
        last_error if summary["failed"] else "",
    )
    summary["pending"] = kept
    if not ok:
        summary["state_error"] = True
        print("[watchsite.qq] 错误：qq-state 写盘失败，下一轮可能重复播报或丢失待播报事件", file=sys.stderr)
    if dropped:
        print(f"[watchsite.qq] 警告：pending 超过 {MAX_PENDING} 条，丢弃最旧 {dropped} 条", file=sys.stderr)
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="DeepSeek 观测站 QQ 群播报")
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片，不发送")
    parser.add_argument("--limit", type=int, default=20, help="dry-run 最多读取多少条事件")
    args = parser.parse_args(argv)

    if args.dry_run:
        from .collect import build_batch, default_paths

        paths = default_paths()
        batch = build_batch(
            paths["docwatch_db"],
            paths["docwatch_root"],
            paths["monitor_events_dir"],
            paths["monitor_state_path"],
            sent_ids=[],
        )
        min_level = settings()["min_level"]
        events = [
            event
            for event in batch["events"]
            if _as_int(event.get("level")) >= min_level
        ]
        # build_batch 按 changed_ts 升序；预览看最新的一批。
        print(build_preview(events[-max(1, args.limit):]))
        return 0

    result = notify_events([])
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
