from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from watchsite import qq, state
from watchsite.tests.support import cleanup, temp_dir


def _event(event_id: str, **overrides) -> dict:
    event = {
        "id": event_id,
        "source": "platform",
        "kind": "page_change",
        "level": 2,
        "title": "支付失败与收据下载提示更新",
        "summary": "这次主要调整支付失败、支付过于频繁、服务被禁用时的提示文案。",
        "impact": "正常浏览不受影响，只有遇到支付或收据异常时才会看到新提示。",
        "action": "无需操作",
        "page_url": "https://platform.deepseek.com/",
        "slug": "platform-20261120t120019z-8cfc7c8d",
        "changed_ts": 1793088000,
    }
    event.update(overrides)
    return event


def _commit_event(event_id: str, source: str, commit: str, old: str = "old000") -> dict:
    """构造一条「前端版本变化」的 page_change 事件。"""
    return _event(
        event_id,
        source=source,
        payload={
            "commit_id": commit,
            "changes": [{"kind": "commit", "old": old, "new": commit}],
        },
    )


class QQCardTests(unittest.TestCase):
    def test_event_node_contains_requested_fields(self) -> None:
        cards = qq.build_cards([_event("a")], now_ts=1793091000)
        self.assertEqual(len(cards), 1)
        text = "\n".join(cards[0]["nodes"])
        self.assertIn("DeepSeek 最新动向（1 条）", text)
        self.assertIn("【值得看】支付失败与收据下载提示更新", text)
        self.assertIn("来源：Platform 控制台", text)
        self.assertIn("摘要：这次主要调整", text)
        self.assertIn("影响：正常浏览不受影响", text)
        self.assertIn("原文：https://platform.deepseek.com/", text)

    def test_level_label_and_source_label(self) -> None:
        text = "\n".join(qq.build_cards([_event("a", level=3, source="docs")], now_ts=0)[0]["nodes"])
        self.assertIn("【重要】", text)
        self.assertIn("来源：官方文档", text)

    def test_build_cards_respects_node_and_char_limits(self) -> None:
        events = [
            _event(
                f"e{index}",
                title="标题" + "长" * 80,
                summary="摘要" + "长" * 200,
                impact="影响" + "长" * 160,
            )
            for index in range(80)
        ]
        cards, leftovers = qq._pack_cards(events, now_ts=1793091000)
        self.assertTrue(cards)
        self.assertLessEqual(len(cards), qq.MAX_CARDS)
        self.assertEqual(sum(len(card["events"]) for card in cards) + len(leftovers), len(events))
        for card in cards:
            text = "\n".join(card["nodes"])
            self.assertLessEqual(len(card["nodes"]), qq.CARD_MAX_NODES)
            self.assertLessEqual(len(text), qq.CARD_MAX_CHARS)
            for node in card["nodes"]:
                self.assertLessEqual(len(node), qq.NODE_TEXT_LIMIT)

    def test_min_level_zero_is_not_treated_as_default(self) -> None:
        config = {
            "enabled": True,
            "groups": ["100000001"],
            "min_level": 0,
            "ws_url": "ws://127.0.0.1:3001",
            "bot_id": "100000002",
            "timeout": 3,
            "nickname": "DeepSeek 观测站",
        }
        with mock.patch.object(qq, "send_group_forward", side_effect=AssertionError("不应发送")):
            with mock.patch("sys.stdout"):
                summary = qq.notify_events(
                    [_event("zero", level=0), _event("one", level=1)],
                    now_ts=1793091000,
                    dry_run=True,
                    config=config,
                )
        self.assertEqual(summary["candidates"], 2)

    def test_empty_events_returns_empty_cards(self) -> None:
        self.assertEqual(qq.build_cards([], now_ts=0), [])
        self.assertIn("没有达到播报级别", qq.build_preview([], now_ts=0))

    def test_dry_run_prints_without_sending(self) -> None:
        events = [_event("a")]
        with mock.patch.object(qq, "send_group_forward", side_effect=AssertionError("不应发送")):
            with mock.patch("sys.stdout"):
                summary = qq.notify_events(
                    events,
                    now_ts=1793091000,
                    dry_run=True,
                    config={
                        "enabled": True,
                        "groups": ["100000001"],
                        "min_level": 2,
                        "ws_url": "ws://127.0.0.1:3001",
                        "bot_id": "100000002",
                        "timeout": 3,
                        "nickname": "DeepSeek 观测站",
                    },
                )
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["cards"], 1)
        self.assertEqual(summary["sent"], 0)
        self.assertIn("dry-run", summary["skipped"])


class QQStateTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "enabled": True,
            "groups": ["100000001"],
            "min_level": 2,
            "ws_url": "ws://127.0.0.1:3001",
            "bot_id": "100000002",
            "timeout": 3,
            "nickname": "DeepSeek 观测站",
        }

    def test_filters_below_min_level(self) -> None:
        root = temp_dir("qq-level-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                summary = qq.notify_events(
                    [_event("low", level=1), _event("ok", level=2)],
                    now_ts=1793091000,
                    config=self._config(),
                )
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["sent"], 1)
        sender.assert_called_once()

    def test_sent_ids_dedupe_next_run(self) -> None:
        root = temp_dir("qq-dedupe-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                first = qq.notify_events([_event("a")], now_ts=1793091000, config=self._config())
                second = qq.notify_events([_event("a")], now_ts=1793091100, config=self._config())
            saved_sent_ids = state.load_json(qq.QQ_STATE_FILE, {}).get("sent_ids")
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["candidates"], 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(saved_sent_ids, ["a"])

    def test_below_min_level_old_pending_is_expired(self) -> None:
        root = temp_dir("qq-pending-old-low-")
        self.addCleanup(cleanup, root)
        old = _event("old-low", level=1, changed_ts=1792915200)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            state.save_json(qq.QQ_STATE_FILE, {"sent_ids": [], "pending": [old]})
            with mock.patch.object(
                qq, "send_group_forward", side_effect=AssertionError("不应发送")
            ):
                summary = qq.notify_events(
                    [], now_ts=1793091000, config=self._config()
                )
            saved = state.load_json(qq.QQ_STATE_FILE, {})
        self.assertEqual(summary["expired"], 1)
        self.assertEqual(saved.get("pending"), [])
        self.assertIn("old-low", saved.get("sent_ids", []))

    def test_pending_already_in_sent_ids_is_cleaned_up(self) -> None:
        root = temp_dir("qq-pending-stale-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            state.save_json(
                qq.QQ_STATE_FILE,
                {
                    "sent_ids": ["a"],
                    # a 已在 sent_ids 里，属于历史遗留；b 低于播报级别，保持 pending。
                    "pending": [_event("a"), _event("b", level=1)],
                },
            )
            with mock.patch.object(
                qq, "send_group_forward", side_effect=AssertionError("不应发送")
            ):
                summary = qq.notify_events(
                    [], now_ts=1793091000, config=self._config()
                )
            saved = state.load_json(qq.QQ_STATE_FILE, {})
        self.assertEqual(summary["pending"], 1)
        self.assertEqual([item["id"] for item in saved.get("pending", [])], ["b"])

    def test_failure_keeps_pending_and_retry_succeeds(self) -> None:
        root = temp_dir("qq-pending-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", side_effect=qq.QQNotifyError("NapCat 挂了")):
                first = qq.notify_events([_event("a")], now_ts=1793091000, config=self._config())
            saved = state.load_json(qq.QQ_STATE_FILE, {})
            self.assertEqual(first["failed"], 1)
            self.assertEqual([item["id"] for item in saved.get("pending", [])], ["a"])
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                second = qq.notify_events([], now_ts=1793091100, config=self._config())
        self.assertEqual(second["sent"], 1)
        self.assertEqual(second["pending"], 0)
        self.assertEqual(sender.call_count, 1)

    def test_no_state_dir_skips_without_sending(self) -> None:
        with mock.patch.object(state, "state_dir", return_value=None):
            with mock.patch.object(qq, "send_group_forward", side_effect=AssertionError("不应发送")):
                summary = qq.notify_events([_event("a")], now_ts=0, config=self._config())
        self.assertEqual(summary["skipped"], "没有可写状态目录，跳过 QQ 播报")
        self.assertEqual(summary["sent"], 0)

    def test_state_write_failure_is_reported(self) -> None:
        root = temp_dir("qq-state-error-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}):
                with mock.patch.object(qq.state, "save_json", return_value=False):
                    summary = qq.notify_events([_event("a")], now_ts=0, config=self._config())
        self.assertTrue(summary["state_error"])
        self.assertEqual(summary["sent"], 1)

    def test_pending_is_bounded(self) -> None:
        root = temp_dir("qq-pending-bound-")
        self.addCleanup(cleanup, root)
        pending = {f"e{index}": _event(f"e{index}") for index in range(qq.MAX_PENDING + 10)}
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            ok, kept, dropped = qq._save_state([], pending)
        self.assertTrue(ok)
        self.assertEqual(kept, qq.MAX_PENDING)
        self.assertEqual(dropped, 10)

    def test_same_commit_across_sources_broadcasts_once(self) -> None:
        """同一前端版本先后部署到 chat / platform，群里只播第一条。"""
        root = temp_dir("qq-commit-cross-")
        self.addCleanup(cleanup, root)
        chat = _commit_event("web:chat:a", "chat", "eec5cf91")
        platform = _commit_event("web:platform:b", "platform", "eec5cf91")
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                first = qq.notify_events([chat], now_ts=1793091000, config=self._config())
                second = qq.notify_events([platform], now_ts=1793091900, config=self._config())
            saved = state.load_json(qq.QQ_STATE_FILE, {})
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["merged"], 1)
        self.assertEqual(sender.call_count, 1)
        self.assertIn("web:chat:a", saved.get("sent_ids", []))
        self.assertIn("web:platform:b", saved.get("sent_ids", []))
        self.assertEqual(saved.get("sent_commits"), {"eec5cf91": 1793091000})

    def test_same_commit_in_one_round_packs_one_card(self) -> None:
        root = temp_dir("qq-commit-same-round-")
        self.addCleanup(cleanup, root)
        chat = _commit_event("web:chat:a", "chat", "eec5cf91")
        platform = _commit_event("web:platform:b", "platform", "eec5cf91")
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                summary = qq.notify_events(
                    [chat, platform], now_ts=1793091000, config=self._config()
                )
            saved = state.load_json(qq.QQ_STATE_FILE, {})
        self.assertEqual(summary["cards"], 1)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["merged"], 1)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(saved.get("sent_commits"), {"eec5cf91": 1793091000})
        self.assertIn("web:chat:a", saved.get("sent_ids", []))
        self.assertIn("web:platform:b", saved.get("sent_ids", []))

    def test_different_commit_still_broadcasts(self) -> None:
        root = temp_dir("qq-commit-different-")
        self.addCleanup(cleanup, root)
        first_event = _commit_event("web:chat:a", "chat", "aaa111")
        second_event = _commit_event("web:platform:b", "platform", "bbb222")
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                first = qq.notify_events([first_event], now_ts=1793091000, config=self._config())
                second = qq.notify_events(
                    [second_event], now_ts=1793091900, config=self._config()
                )
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(second["merged"], 0)
        self.assertEqual(sender.call_count, 2)

    def test_text_change_with_same_commit_is_not_suppressed(self) -> None:
        """纯正文变化即使 commit 相同也不能被版本去重吞掉。"""
        root = temp_dir("qq-commit-text-")
        self.addCleanup(cleanup, root)
        commit_event = _commit_event("web:chat:a", "chat", "eec5cf91")
        text_event = _event(
            "web:chat:text",
            source="chat",
            payload={
                "commit_id": "eec5cf91",
                "changes": [{"kind": "text", "preview": "正文更新"}],
            },
        )
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                first = qq.notify_events([commit_event], now_ts=1793091000, config=self._config())
                second = qq.notify_events([text_event], now_ts=1793091900, config=self._config())
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(second["merged"], 0)
        self.assertEqual(sender.call_count, 2)

    def test_same_commit_merge_failure_retries_both(self) -> None:
        root = temp_dir("qq-commit-failure-")
        self.addCleanup(cleanup, root)
        chat = _commit_event("web:chat:a", "chat", "eec5cf91")
        platform = _commit_event("web:platform:b", "platform", "eec5cf91")
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(
                qq, "send_group_forward", side_effect=qq.QQNotifyError("NapCat 挂了")
            ):
                first = qq.notify_events(
                    [chat, platform], now_ts=1793091000, config=self._config()
                )
            saved = state.load_json(qq.QQ_STATE_FILE, {})
            self.assertEqual(first["failed"], 1)
            self.assertEqual(
                sorted(item["id"] for item in saved.get("pending", [])),
                ["web:chat:a", "web:platform:b"],
            )
            self.assertEqual(saved.get("sent_commits"), {})
            with mock.patch.object(qq, "send_group_forward", return_value={}) as sender:
                second = qq.notify_events([], now_ts=1793091900, config=self._config())
            saved = state.load_json(qq.QQ_STATE_FILE, {})
        self.assertEqual(second["sent"], 1)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(saved.get("pending"), [])
        self.assertIn("web:chat:a", saved.get("sent_ids", []))
        self.assertIn("web:platform:b", saved.get("sent_ids", []))


class QQWebSocketCodecTests(unittest.TestCase):
    def test_accept_key_matches_rfc_example(self) -> None:
        self.assertEqual(
            qq._ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_frame_roundtrip_masked_and_unmasked(self) -> None:
        for mask in (True, False):
            payload = "你好，DeepSeek".encode("utf-8")
            frame = qq._encode_frame(0x1, payload, mask=mask)
            parsed = qq._decode_frame(frame)
            self.assertIsNotNone(parsed)
            fin, opcode, decoded, consumed = parsed  # type: ignore[misc]
            self.assertTrue(fin)
            self.assertEqual(opcode, 0x1)
            self.assertEqual(decoded, payload)
            self.assertEqual(consumed, len(frame))

    def test_extended_length_frames(self) -> None:
        for size in (125, 126, 65535, 65536):
            payload = b"x" * size
            frame = qq._encode_frame(0x1, payload, mask=False)
            parsed = qq._decode_frame(frame)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed[2], payload)  # type: ignore[index]


class QQPushIntegrationTests(unittest.TestCase):
    def test_push_main_survives_qq_failure(self) -> None:
        from watchsite import push

        root = temp_dir("qq-push-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [_event("a")],
            "statuses": [],
            "meta": {},
        }
        with mock.patch.dict(
            os.environ,
            {
                "PUSH_STATE_DIR": str(root),
                "INGEST_TOKEN": "test-token",
                "INGEST_URL": "https://deepseek-watch.pages.dev/internal/ingest",
            },
            clear=False,
        ):
            with mock.patch.object(push, "build_batch", return_value=batch):
                with mock.patch.object(
                    push,
                    "_post_with_retry",
                    return_value={"ok": True, "event_count": 1, "rejected_count": 0},
                ):
                    with mock.patch.object(push.qq, "notify_events", side_effect=RuntimeError("QQ 挂了")):
                        self.assertEqual(push.main(["--once"]), 0)

    def test_push_retries_qq_pending_without_new_site_events(self) -> None:
        """群里长时间没有新事件时，上一轮失败的 QQ 卡片仍要每轮重试到成功。"""
        from watchsite import push

        root = temp_dir("qq-push-pending-")
        self.addCleanup(cleanup, root)
        fresh = _event("pending-1", changed_ts=int(time.time()))
        with mock.patch.dict(
            os.environ,
            {
                "PUSH_STATE_DIR": str(root),
                "INGEST_TOKEN": "test-token",
                "INGEST_URL": "https://deepseek-watch.pages.dev/internal/ingest",
                "QQ_NOTIFY_ENABLED": "1",
                "QQ_NOTIFY_GROUPS": "100000001",
                "QQ_NOTIFY_MIN_LEVEL": "2",
            },
            clear=False,
        ):
            self.assertTrue(
                state.save_json(
                    qq.QQ_STATE_FILE,
                    {"sent_ids": [], "pending": [fresh]},
                )
            )
            batch = {"events": [], "statuses": [], "meta": {}}
            with mock.patch.object(push, "build_batch", return_value=batch):
                with mock.patch.object(
                    push,
                    "_post_with_retry",
                    return_value={"ok": True, "event_count": 0, "rejected_count": 0},
                ):
                    with mock.patch.object(
                        qq, "send_group_forward", return_value={}
                    ) as sender:
                        self.assertEqual(push.main(["--once"]), 0)
            saved = state.load_json(qq.QQ_STATE_FILE, {})
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(saved.get("pending"), [])
        self.assertIn("pending-1", saved.get("sent_ids", []))


class _FakeWSServer(threading.Thread):
    """最小 OneBot WebSocket 服务端，只用来验证握手、帧和 request/response 流程。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[dict] = []
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            conn, _ = self.sock.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
                key = ""
                for line in head.split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept = qq._ws_accept_key(key)
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                # 先发一个 ping，客户端应回 pong；再发正式 response。
                conn.sendall(qq._encode_frame(0x9, b"ping", mask=False))
                buffer = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
                message = None
                while message is None:
                    parsed = qq._decode_frame(buffer)
                    if parsed is None:
                        chunk = conn.recv(65536)
                        if not chunk:
                            return
                        buffer += chunk
                        continue
                    _, opcode, payload, used = parsed
                    buffer = buffer[used:]
                    if opcode == 0x1:
                        message = json.loads(payload.decode("utf-8"))
                self.received.append(message)
                response = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 123},
                    "echo": message.get("echo"),
                }
                conn.sendall(
                    qq._encode_frame(
                        0x1,
                        json.dumps(response, ensure_ascii=False).encode("utf-8"),
                        mask=False,
                    )
                )
                time.sleep(0.1)
        except Exception as exc:  # noqa: BLE001
            self.error = exc

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class QQWebSocketIntegrationTests(unittest.TestCase):
    def test_send_group_forward_against_fake_server(self) -> None:
        server = _FakeWSServer()
        server.start()
        self.addCleanup(server.close)
        result = qq.send_group_forward(
            f"ws://127.0.0.1:{server.port}",
            "100000001",
            ["标题", "正文"],
            nickname="测试播报",
            bot_id="100000002",
            timeout=3,
        )
        server.join(timeout=3)
        self.assertIsNone(server.error)
        self.assertEqual(result.get("message_id"), 123)
        self.assertEqual(len(server.received), 1)
        request = server.received[0]
        self.assertEqual(request.get("action"), "send_group_forward_msg")
        self.assertEqual(request["params"]["group_id"], "100000001")
        nodes = request["params"]["messages"]
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["data"]["nickname"], "测试播报")
        self.assertEqual(nodes[1]["data"]["content"][0]["data"]["text"], "正文")


if __name__ == "__main__":
    unittest.main()
