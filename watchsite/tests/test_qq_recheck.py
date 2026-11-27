from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from watchsite import qq, state
from watchsite.tests.support import cleanup, temp_dir


def _event(event_id: str = "recheck-1", **overrides) -> dict:
    event = {
        "id": event_id,
        "source": "chat",
        "kind": "page_change",
        "level": 3,
        "level_label": "重要",
        "title": "登录入口出现调整",
        "summary": "登录入口的位置和提示文案发生变化。",
        "impact": "所有需要登录的用户都会看到新的入口。",
        "action": "按页面提示登录即可",
        "page_url": "https://chat.deepseek.com/",
        "slug": f"chat-{event_id}",
        "changed_ts": 1795500000,
        "payload": {
            "recheck": {
                "state": "done",
                "level_applied": True,
                "from_level": 2,
                "to_level": 3,
                "applied": ["level", "title"],
            }
        },
    }
    event.update(overrides)
    return event


class RecheckQQTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_dir("qq-recheck-")
        self.addCleanup(cleanup, self.root)
        env = mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(self.root)}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_card_marks_recheck_level_upgrade(self) -> None:
        cards = qq.build_cards([_event()], now_ts=1795500060)
        text = "\n".join(cards[0]["nodes"])
        self.assertIn("复核：级别由 2 上调为 3 级", text)
        self.assertIn("【重要】登录入口出现调整", text)

    def test_compact_event_keeps_recheck_marker(self) -> None:
        compact = qq._compact_event(_event())
        self.assertTrue(compact["recheck_upgrade"])
        self.assertEqual(compact["recheck_from_level"], 2)
        self.assertEqual(compact["recheck_to_level"], 3)

    def test_normal_broadcast_skips_recheck_upgrade(self) -> None:
        candidates = qq._merge_candidates([_event()], {}, [], min_level=2)
        self.assertEqual(candidates, [])

    def test_notify_recheck_upgrade_sends_once(self) -> None:
        sent = []

        def sender(group_id, lines):
            sent.append((str(group_id), list(lines)))

        config = {
            "enabled": True,
            "groups": ["100000001"],
            "ws_url": "ws://127.0.0.1:3001",
            "nickname": "冉宝",
            "bot_id": "100000002",
            "timeout": 8,
            "token": "",
        }
        first = qq.notify_recheck_upgrade(
            _event(), now_ts=1795500060, config=config, sender=sender
        )
        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "100000001")
        self.assertTrue(any("复核：级别由 2 上调为 3 级" in line for line in sent[0][1]))

        data = json.loads((self.root / "qq-state.json").read_text(encoding="utf-8"))
        self.assertIn("recheck-1", data["recheck_sent"])

        second = qq.notify_recheck_upgrade(
            _event(), now_ts=1795500061, config=config, sender=sender
        )
        self.assertEqual(second["sent"], 0)
        self.assertIn("已播报", second["skipped"])
        self.assertEqual(len(sent), 1)

    def test_notify_recheck_upgrade_requires_enabled_and_groups(self) -> None:
        event = _event()
        disabled = qq.notify_recheck_upgrade(
            event, config={"enabled": False, "groups": ["1"]}
        )
        self.assertEqual(disabled["sent"], 0)
        self.assertIn("未开启", disabled["skipped"])
        no_groups = qq.notify_recheck_upgrade(
            event, config={"enabled": True, "groups": []}
        )
        self.assertEqual(no_groups["sent"], 0)
        self.assertIn("为空", no_groups["skipped"])

    def test_save_state_preserves_recheck_sent(self) -> None:
        qq._save_recheck_sent({"a": {"ts": 1, "level": 3}})
        ok, kept, truncated = qq._save_state(["x"], {}, last_error="")
        self.assertTrue(ok)
        self.assertEqual(kept, 0)
        self.assertEqual(truncated, 0)
        data = json.loads((self.root / "qq-state.json").read_text(encoding="utf-8"))
        self.assertIn("a", data["recheck_sent"])
        self.assertIn("x", data["sent_ids"])


if __name__ == "__main__":
    unittest.main()
