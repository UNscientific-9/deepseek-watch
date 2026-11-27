from __future__ import annotations

import os
import unittest
from unittest import mock

from watchsite import alert, collect, push, state
from watchsite.push import _new_events, _remember
from watchsite.tests.support import cleanup, temp_dir


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class PushStateTests(unittest.TestCase):
    def setUp(self) -> None:
        # 测试永远不能因为开发机/服务器 shell 里带了 QQ_NOTIFY_* 就真的往群里发消息。
        patcher = mock.patch.object(push.qq, "notify_events", return_value={"enabled": False})
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_new_events_filters_ids_already_sent(self) -> None:
        events = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(_new_events(events, ["a", "c"]), [{"id": "b"}])

    def test_changed_event_content_is_resent(self) -> None:
        old = {"id": "a", "title": "旧标题", "level": 2}
        changed = {"id": "a", "title": "修正后的标题", "level": 2}
        versions = {"a": push._event_digest(old)}
        self.assertEqual(_new_events([changed], ["a"], versions), [changed])
        self.assertEqual(_new_events([old], ["a"], versions), [])

    def test_quality_blocked_blocks_only_unrepaired_contradiction(self) -> None:
        frontend = {"totals": {"manifest": 1, "added": 0, "removed": 0, "changed": 1}}

        def page(source: str, quality: str, *, kind: str = "page_change", evidence=True) -> dict:
            payload = {"frontend": frontend} if evidence else {}
            payload["quality"] = quality
            return {"source": source, "kind": kind, "payload": payload}

        self.assertTrue(push._quality_blocked(page("chat", "contradiction")))
        # 证据不完整、内部调整、旧包没有前端证据：都正常推送，不再整条丢弃。
        self.assertFalse(push._quality_blocked(page("chat", "partial")))
        self.assertFalse(push._quality_blocked(page("chat", "no_evidence")))
        self.assertFalse(push._quality_blocked(page("chat", "internal")))
        self.assertFalse(push._quality_blocked(page("chat", "contradiction", evidence=False)))
        # 平台公告、纯正文变化和 docs 来源不参与前端质量闸门。
        self.assertFalse(push._quality_blocked(page("platform", "contradiction", kind="platform_banner")))
        self.assertFalse(push._quality_blocked(page("docs", "contradiction")))

    def test_remember_deduplicates_and_bounds(self) -> None:
        sent = _remember([], [{"id": "a"}, {"id": "a"}, {"id": "b"}])
        self.assertEqual(sent, ["a", "b"])
        sent = _remember([str(index) for index in range(5001)], [{"id": "new"}])
        self.assertLessEqual(len(sent), 5000)
        self.assertEqual(sent[-1], "new")

    def test_state_roundtrip_with_writable_dir(self) -> None:
        root = temp_dir("push-state-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            self.assertTrue(state.save_json("x.json", {"sent_ids": ["a"]}))
            self.assertEqual(state.load_json("x.json", {}), {"sent_ids": ["a"]})

    def test_corrupt_rejected_state_does_not_crash_dead_letter_check(self) -> None:
        event = {"id": "a", "source": "chat", "kind": "page_change", "payload": {}}
        live, dead = push._split_dead_letters(
            [event], {"a": {"count": "bad", "signature": "x"}}
        )
        self.assertEqual(live, [event])
        self.assertEqual(dead, [])

    def test_corrupt_rejected_state_does_not_crash_state_save(self) -> None:
        root = temp_dir("push-state-corrupt-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            self.assertTrue(
                push._save_push_state(
                    ["a"], {"a": {"count": "bad", "last_ts": "bad"}}, {}
                )
            )

    def test_main_sends_only_new_events(self) -> None:
        root = temp_dir("push-main-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [{"id": "a", "title": "A", "level": 1}],
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
                    side_effect=lambda url, token, payload: {
                        "ok": True,
                        "event_count": len(payload["events"]),
                        "rejected_count": 0,
                    },
                ) as poster:
                    self.assertEqual(push.main(["--once"]), 0)
                    self.assertEqual(poster.call_args.args[2]["events"], batch["events"])
                    self.assertEqual(push.main(["--once"]), 0)
                    self.assertEqual(poster.call_args.args[2]["events"], [])

    def test_rejected_events_are_not_written_to_sent_ids(self) -> None:
        root = temp_dir("push-reject-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [
                {"id": "bad", "title": "Bad", "level": 1},
                {"id": "good", "title": "Good", "level": 1},
            ],
            "statuses": [],
            "meta": {},
        }
        rejected = {
            "ok": True,
            "event_count": 1,
            "rejected_count": 1,
            "rejected": [{"index": 0, "id": "bad", "reason": "invalid slug"}],
        }

        def fake_post(url, token, payload):  # noqa: ANN001
            if len(payload["events"]) == 2:
                return rejected
            return {"ok": True, "event_count": 0, "rejected_count": 1, "rejected": rejected["rejected"]}
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
                with mock.patch.object(push, "_post_with_retry", side_effect=fake_post) as poster:
                    with mock.patch.object(alert, "notify", return_value=True) as notifier:
                        with mock.patch.object(alert, "clear") as clearer:
                            self.assertEqual(push.main(["--once"]), 1)
                            self.assertEqual(
                                state.load_json("push-state.json", {}).get("sent_ids"),
                                ["good"],
                            )
                            notifier.assert_called_once()
                            clearer.assert_not_called()

                            # 第二轮只应重试被服务端拒绝的事件。
                            poster.reset_mock()
                            self.assertEqual(push.main(["--once"]), 1)
                            self.assertEqual(
                                [event["id"] for event in poster.call_args.args[2]["events"]],
                                ["bad"],
                            )

    def test_permanently_rejected_event_is_quarantined_after_threshold(self) -> None:
        root = temp_dir("push-dead-")
        self.addCleanup(cleanup, root)
        event = {
            "id": "bad",
            "slug": "bad",
            "source": "chat",
            "level": 1,
            "title": "Bad",
            "changed_ts": 1700000000,
            "page_url": "https://chat.deepseek.com",
            "archive_url": "",
        }
        batch = {"events": [event], "statuses": [], "meta": {}}
        rejected_response = {
            "ok": True,
            "event_count": 0,
            "rejected_count": 1,
            "rejected": [{"index": 0, "id": "bad", "reason": "invalid slug"}],
        }

        def fake_post(url, token, payload):  # noqa: ANN001
            if not payload["events"]:
                return {"ok": True, "event_count": 0, "rejected_count": 0}
            return rejected_response

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
                with mock.patch.object(push, "_post_with_retry", side_effect=fake_post) as poster:
                    with mock.patch.object(alert, "notify", return_value=True):
                        for _ in range(push.MAX_REJECT_STREAK):
                            self.assertEqual(push.main(["--once"]), 1)
                        stored = state.load_json("push-state.json", {})
                        self.assertEqual(
                            stored["rejected"]["bad"]["count"], push.MAX_REJECT_STREAK
                        )
                        self.assertEqual(stored["metrics"]["dead_letter_count"], 1)
                        self.assertEqual(stored["metrics"]["backlog_count"], 0)

                        # 达到阈值后不再每轮重推，也不再反复告警。
                        poster.reset_mock()
                        with mock.patch.object(alert, "notify", return_value=True) as notifier:
                            self.assertEqual(push.main(["--once"]), 0)
                            self.assertEqual(
                                [item["id"] for item in poster.call_args.args[2]["events"]],
                                [],
                            )
                            notifier.assert_not_called()

    def test_dead_letter_is_released_after_event_content_changes(self) -> None:
        event = {
            "id": "bad",
            "slug": "bad",
            "source": "chat",
            "level": 1,
            "title": "Bad",
            "changed_ts": 1700000000,
            "page_url": "https://chat.deepseek.com",
            "archive_url": "",
        }
        rejected = {
            "bad": {
                "count": push.MAX_REJECT_STREAK,
                "signature": push._event_signature(event),
                "reason": "invalid slug",
            }
        }
        live, dead = push._split_dead_letters([event], rejected)
        self.assertEqual(dead, ["bad"])
        self.assertEqual(live, [])

        fixed = dict(event, title="Fixed title")
        live, dead = push._split_dead_letters([fixed], rejected)
        self.assertEqual(dead, [])
        self.assertEqual(live, [fixed])

    def test_dead_letter_is_released_after_payload_only_fix(self) -> None:
        """P2-15：服务端因 payload 拒收时，只修 payload 也必须解除死信。"""
        event = {
            "id": "bad",
            "slug": "bad",
            "source": "chat",
            "level": 1,
            "title": "Bad",
            "changed_ts": 1700000000,
            "page_url": "https://chat.deepseek.com",
            "archive_url": "",
            "payload": {"a": 1},
            "changes_detail": [{"page": "p", "diff": "旧"}],
        }
        rejected = {
            "bad": {
                "count": push.MAX_REJECT_STREAK,
                "signature": push._event_signature(event),
                "reason": "payload too large",
            }
        }
        live, dead = push._split_dead_letters([event], rejected)
        self.assertEqual(dead, ["bad"])
        self.assertEqual(live, [])

        payload_fixed = dict(event, payload={"a": 2})
        live, dead = push._split_dead_letters([payload_fixed], rejected)
        self.assertEqual(dead, [])
        self.assertEqual(live, [payload_fixed])

        detail_fixed = dict(
            event, changes_detail=[{"page": "p", "diff": "新的可展示明细"}]
        )
        live, dead = push._split_dead_letters([detail_fixed], rejected)
        self.assertEqual(dead, [])
        self.assertEqual(live, [detail_fixed])

    def test_missing_changed_ts_normalization_keeps_dead_letter_signature(self) -> None:
        """P2-15：缺 changed_ts 时采集端必须给稳定兜底，签名不能每轮漂移。"""
        raw = {
            "id": "web:chat:no-time",
            "slug": "web-chat-no-time",
            "source": "chat",
            "kind": "page_change",
            "level": 2,
            "title": "没有时间戳的事件",
            "summary": "摘要",
            "page_url": "https://chat.deepseek.com/",
            "payload": {"target": "chat"},
        }
        first = collect._normalize_monitor_event(raw, 1_000_000)
        second = collect._normalize_monitor_event(raw, 2_000_000)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first["changed_ts"], 0)
        self.assertEqual(
            push._event_signature(first), push._event_signature(second)
        )

    def test_event_without_changed_ts_can_reach_dead_letter_threshold(self) -> None:
        """缺 changed_ts 的事件不能因为签名每轮变化而永远累计不到死信阈值。"""
        root = temp_dir("push-no-ts-")
        self.addCleanup(cleanup, root)
        event = {
            "id": "bad",
            "slug": "bad",
            "source": "chat",
            "level": 1,
            "title": "Bad",
            "page_url": "https://chat.deepseek.com",
            "archive_url": "",
        }
        batch = {"events": [event], "statuses": [], "meta": {}}
        rejected_response = {
            "ok": True,
            "event_count": 0,
            "rejected_count": 1,
            "rejected": [{"index": 0, "id": "bad", "reason": "invalid payload"}],
        }

        def fake_post(url, token, payload):  # noqa: ANN001
            if not payload["events"]:
                return {"ok": True, "event_count": 0, "rejected_count": 0}
            return rejected_response

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
                with mock.patch.object(push, "_post_with_retry", side_effect=fake_post):
                    with mock.patch.object(alert, "notify", return_value=True):
                        for _ in range(push.MAX_REJECT_STREAK):
                            self.assertEqual(push.main(["--once"]), 1)
                        stored = state.load_json("push-state.json", {})
                        self.assertEqual(
                            stored["rejected"]["bad"]["count"], push.MAX_REJECT_STREAK
                        )

    def test_dry_run_build_failure_does_not_send_alert(self) -> None:
        with mock.patch.object(push, "build_batch", side_effect=RuntimeError("boom")):
            with mock.patch.object(alert, "notify", return_value=True) as notifier:
                self.assertEqual(push.main(["--dry-run"]), 1)
                notifier.assert_not_called()

    def test_sent_ids_save_failure_is_nonzero_and_alerts(self) -> None:
        root = temp_dir("push-save-fail-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [{"id": "a", "title": "A", "level": 1}],
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
                    with mock.patch.object(push, "_save_push_state", return_value=False):
                        with mock.patch.object(alert, "notify", return_value=True) as notifier:
                            self.assertEqual(push.main(["--once"]), 1)
                            notifier.assert_called_once()

    def test_rate_limit_is_retried_next_round_without_alert(self) -> None:
        root = temp_dir("push-429-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [{"id": "a", "title": "A", "level": 1}],
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
                    side_effect=push.RateLimited("HTTP 429", retry_after=60),
                ):
                    with mock.patch.object(alert, "notify", return_value=True) as notifier:
                        with mock.patch.object(alert, "clear") as clearer:
                            self.assertEqual(push.main(["--once"]), 0)
                            notifier.assert_not_called()
                            clearer.assert_called_once()

    def test_post_json_rejects_html_and_empty_2xx(self) -> None:
        url = "https://deepseek-watch.pages.dev/internal/ingest"
        for body in (b"", b"<html>challenge</html>", b'{"ok": false}', b'["ok"]'):
            with self.subTest(body=body):
                with mock.patch.object(
                    push._NO_REDIRECT_OPENER,
                    "open",
                    return_value=_FakeHTTPResponse(body),
                ):
                    with self.assertRaises(push.InvalidResponse):
                        push._post_json(url, "token", {"events": []})

    def test_post_json_accepts_contract_response(self) -> None:
        url = "https://deepseek-watch.pages.dev/internal/ingest"
        body = b'{"ok": true, "event_count": 0, "rejected_count": 0}'
        with mock.patch.object(
            push._NO_REDIRECT_OPENER,
            "open",
            return_value=_FakeHTTPResponse(body),
        ):
            result = push._post_json(url, "token", {"events": []})
        self.assertIs(result["ok"], True)

    def test_response_counts_are_safely_converted(self) -> None:
        outcome = push._validate_response(
            {"ok": True, "event_count": "1", "rejected_count": "0"},
            [{"id": "a"}],
        )
        self.assertEqual(outcome["accepted_ids"], ["a"])
        self.assertEqual(outcome["rejected"], [])

    def test_inconsistent_response_counts_are_failure(self) -> None:
        root = temp_dir("push-count-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [{"id": "a", "title": "A", "level": 1}],
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
                    return_value={"ok": True, "event_count": 2, "rejected_count": 0},
                ):
                    with mock.patch.object(alert, "notify", return_value=True):
                        self.assertEqual(push.main(["--once"]), 1)
                        self.assertNotIn(
                            "a", state.load_json("push-state.json", {}).get("sent_ids", [])
                        )

    def test_413_splits_chunk_and_retries(self) -> None:
        root = temp_dir("push-413-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [
                {"id": "a", "title": "A", "level": 1},
                {"id": "b", "title": "B", "level": 1},
            ],
            "statuses": [],
            "meta": {},
        }
        calls: list[list[str]] = []

        def fake_post(url, token, payload):  # noqa: ANN001
            ids = [event["id"] for event in payload["events"]]
            calls.append(ids)
            if len(ids) > 1:
                raise push.PayloadTooLarge("HTTP 413")
            return {"ok": True, "event_count": len(ids), "rejected_count": 0}

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
                with mock.patch.object(push, "_post_with_retry", side_effect=fake_post):
                    with mock.patch.object(alert, "notify", return_value=True) as notifier:
                        self.assertEqual(push.main(["--once"]), 0)
                        notifier.assert_not_called()
                        self.assertEqual(calls, [["a", "b"], ["a"], ["b"]])
                        self.assertEqual(
                            state.load_json("push-state.json", {}).get("sent_ids"),
                            ["a", "b"],
                        )

    def test_chunks_are_split_by_utf8_bytes(self) -> None:
        events = [
            {"id": f"e{index}", "payload": {"blob": "汉" * 200_000}}
            for index in range(8)
        ]
        chunks = push._build_chunks(events, [], {})
        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [event["id"] for chunk in chunks for event in chunk],
            [event["id"] for event in events],
        )
        for chunk in chunks:
            self.assertLessEqual(push._payload_bytes(chunk, [], {}), push.MAX_REQUEST_BYTES)

    def test_event_bytes_include_changes_detail(self) -> None:
        base = {"id": "a"}
        with_detail = {
            "id": "a",
            "changes_detail": [{"page": "p", "diff": "＋" + "新内容" * 5000}],
        }
        plain = push._event_bytes(base)
        detailed = push._event_bytes(with_detail)
        self.assertGreater(detailed, plain + 20_000)
        # 分批用的 _payload_bytes 也必须是整条事件序列化，同样算进 changes_detail。
        self.assertGreater(
            push._payload_bytes([with_detail], [], {}) - push._payload_bytes([base], [], {}),
            20_000,
        )

    def test_chunks_are_split_by_statement_budget(self) -> None:
        events = [
            {
                "id": f"e{index}",
                "changes_detail": [{"page": f"p{j}", "bullets": []} for j in range(20)],
            }
            for index in range(30)
        ]
        chunks = push._build_chunks(events, [], {})
        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [event["id"] for chunk in chunks for event in chunk],
            [event["id"] for event in events],
        )
        for chunk in chunks:
            weight = sum(push._event_statement_weight(event) for event in chunk) + 2
            self.assertLessEqual(weight, push.MAX_STATEMENTS_PER_REQUEST)

    def test_chunks_are_split_by_size_with_changes_detail(self) -> None:
        events = [
            {
                "id": f"e{index}",
                "payload": {"blob": "汉" * 100_000},
                "changes_detail": [{"page": "p", "diff": "＋" + "新" * 20_000}],
            }
            for index in range(12)
        ]
        chunks = push._build_chunks(events, [], {})
        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [event["id"] for chunk in chunks for event in chunk],
            [event["id"] for event in events],
        )
        for chunk in chunks:
            self.assertLessEqual(push._payload_bytes(chunk, [], {}), push.MAX_REQUEST_BYTES)
            self.assertLessEqual(len(chunk), push.MAX_EVENTS_PER_REQUEST)

    def test_quality_counts_group_by_payload_quality(self) -> None:
        events = [
            {"id": "a", "payload": {"quality": "ok"}},
            {"id": "b", "payload": {"quality": "partial"}},
            {"id": "c", "payload": {}},
            {"id": "d", "payload": {"quality": "bogus"}},
        ]
        self.assertEqual(
            push._quality_counts(events),
            {"ok": 1, "partial": 1, "unknown": 2},
        )

    def test_push_metrics_are_saved_and_sent_in_next_round(self) -> None:
        root = temp_dir("push-metrics-")
        self.addCleanup(cleanup, root)
        event = {
            "id": "a",
            "slug": "a",
            "source": "chat",
            "level": 2,
            "title": "A",
            "changed_ts": 1700000000,
            "page_url": "https://chat.deepseek.com",
            "archive_url": "",
            "payload": {"quality": "partial"},
        }
        batch = {"events": [event], "statuses": [], "meta": {}}
        captured: list[dict] = []

        def fake_post(url, token, payload):  # noqa: ANN001
            captured.append(payload["meta"])
            return {
                "ok": True,
                "event_count": len(payload["events"]),
                "rejected_count": 0,
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
                with mock.patch.object(push, "_post_with_retry", side_effect=fake_post):
                    self.assertEqual(push.main(["--once"]), 0)
                    stored = state.load_json("push-state.json", {})["metrics"]
                    self.assertEqual(stored["new_event_count"], 1)
                    self.assertEqual(stored["pushed_count"], 1)
                    self.assertEqual(stored["rejected_count"], 0)
                    self.assertEqual(stored["dead_letter_count"], 0)
                    self.assertEqual(stored["backlog_count"], 0)
                    self.assertEqual(stored["quality_counts"], {"partial": 1})
                    self.assertTrue(stored["last_push_ts"])
                    # 第一轮没有「上一轮」指标可带。
                    self.assertNotIn("pushed_count", captured[0])

                    self.assertEqual(push.main(["--once"]), 0)
                    self.assertEqual(captured[1]["pushed_count"], 1)
                    self.assertEqual(captured[1]["new_event_count"], 1)
                    self.assertEqual(captured[1]["backlog_count"], 0)
                    self.assertEqual(captured[1]["quality_counts"], {"partial": 1})
                    self.assertIn("last_push_ts", captured[1])

    def test_rejected_event_is_counted_as_retry_backlog(self) -> None:
        root = temp_dir("push-metrics-reject-")
        self.addCleanup(cleanup, root)
        good = {"id": "good", "slug": "good", "source": "chat", "level": 1, "title": "Good"}
        bad = {"id": "bad", "slug": "bad", "source": "chat", "level": 1, "title": "Bad"}
        batch = {"events": [good, bad], "statuses": [], "meta": {}}

        def fake_post(url, token, payload):  # noqa: ANN001
            rejected = [
                {"index": index, "id": event["id"], "reason": "invalid slug"}
                for index, event in enumerate(payload["events"])
                if event["id"] == "bad"
            ]
            return {
                "ok": True,
                "event_count": len(payload["events"]) - len(rejected),
                "rejected_count": len(rejected),
                "rejected": rejected,
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
                with mock.patch.object(push, "_post_with_retry", side_effect=fake_post):
                    with mock.patch.object(alert, "notify", return_value=True):
                        self.assertEqual(push.main(["--once"]), 1)
                        metrics = state.load_json("push-state.json", {})["metrics"]
                        self.assertEqual(metrics["pushed_count"], 1)
                        self.assertEqual(metrics["rejected_count"], 1)
                        self.assertEqual(metrics["backlog_count"], 1)
                        self.assertEqual(metrics["dead_letter_count"], 0)

    def test_collection_error_returns_nonzero_and_alerts(self) -> None:
        root = temp_dir("push-collection-")
        self.addCleanup(cleanup, root)
        batch = {
            "events": [],
            "statuses": [],
            "meta": {"collection_errors": [{"source": "watchsite", "detail": "事件目录不存在"}]},
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
                    return_value={"ok": True, "event_count": 0, "rejected_count": 0},
                ):
                    with mock.patch.object(alert, "notify", return_value=True) as notifier:
                        self.assertEqual(push.main(["--once"]), 1)
                        notifier.assert_called_once()

    def test_main_catches_build_batch_exception(self) -> None:
        with mock.patch.object(push, "build_batch", side_effect=RuntimeError("boom")):
            with mock.patch.object(alert, "notify", return_value=True) as notifier:
                self.assertEqual(push.main(["--once"]), 1)
                notifier.assert_called_once()
                self.assertIn("采集失败", notifier.call_args.args[2])

    def test_alert_is_cooldown_limited(self) -> None:
        root = temp_dir("push-alert-")
        self.addCleanup(cleanup, root)
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(root)}, clear=False):
            with mock.patch.object(alert, "send_text", return_value=True) as sender:
                self.assertTrue(alert.notify("push", "subject", "body", 1000, cooldown=60))
                self.assertFalse(alert.notify("push", "subject", "body", 1030, cooldown=60))
                self.assertTrue(alert.notify("push", "subject", "body", 1061, cooldown=60))
                self.assertEqual(sender.call_count, 2)
                alert.clear("push")
                self.assertTrue(alert.notify("push", "subject", "body", 1062, cooldown=60))


if __name__ == "__main__":
    unittest.main()
