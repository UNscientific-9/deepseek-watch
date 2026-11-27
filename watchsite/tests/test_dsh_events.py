from __future__ import annotations

import unittest

from watchsite import qq
from watchsite.collect import _normalize_monitor_event
from watchsite.push import _quality_blocked


def _raw_dsh_event() -> dict:
    return {
        "schema_version": 1,
        "id": "dsh:release:0.2.3-alpha.2",
        "slug": "dsh-release-0-2-3-alpha-2",
        "source": "harness",
        "kind": "dsh_release",
        "level": 3,
        "level_label": "重要",
        "title": "DSH 新版本 v0.2.3-alpha.2",
        "summary": "新增功能 A；修复问题 B",
        "impact": "DSH 发布新版本。",
        "action": "打开 GitHub 发版说明查看完整变更。",
        "signal_count": 2,
        "changed_at": "2026-11-22T11:00:39Z",
        "page_url": "https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.2",
        "evidence": ["新增功能 A", "修复问题 B"],
        "changes_detail": [
            {
                "page": "Release dsh-v0.2.3-alpha.2",
                "level": 3,
                "bullets": ["新增功能 A", "修复问题 B"],
                "note": "https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.2",
                "diff": "",
            }
        ],
        "payload": {
            "version": "0.2.3-alpha.2",
            "tag": "dsh-v0.2.3-alpha.2",
            "notes": ["新增功能 A", "修复问题 B"],
            "release_note": {
                "blocks": [
                    {"type": "heading", "text": "新增功能"},
                    {"type": "bullet", "text": "新增功能 A"},
                    {"type": "bullet", "text": "修复问题 B"},
                ]
            },
            "signals": ["npm", "github_release"],
        },
    }


class DshEventNormalizationTests(unittest.TestCase):
    def test_kind_and_payload_survive_watchsite_normalization(self) -> None:
        event = _normalize_monitor_event(_raw_dsh_event(), 1_763_000_000)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["source"], "harness")
        self.assertEqual(event["kind"], "dsh_release")
        self.assertEqual(event["level"], 3)
        self.assertEqual(event["level_label"], "重要")
        self.assertEqual(event["payload"]["version"], "0.2.3-alpha.2")
        self.assertEqual(
            event["payload"]["release_note"]["blocks"][0]["text"], "新增功能"
        )
        self.assertEqual(event["changes_detail"][0]["page"], "Release dsh-v0.2.3-alpha.2")

    def test_dsh_kind_is_not_blocked_by_frontend_quality_gate(self) -> None:
        event = _normalize_monitor_event(_raw_dsh_event(), 1_763_000_000)
        assert event is not None
        self.assertFalse(_quality_blocked(event))

    def test_level_three_release_reaches_qq_min_level(self) -> None:
        event = _normalize_monitor_event(_raw_dsh_event(), 1_763_000_000)
        assert event is not None
        merged = qq._merge_candidates([event], {}, [], 2)
        self.assertEqual([item["id"] for item in merged], [event["id"]])

    def test_level_one_backfill_is_silent_for_qq(self) -> None:
        raw = _raw_dsh_event()
        raw["level"] = 1
        raw["level_label"] = "轻微"
        raw["changes_detail"][0]["level"] = 1
        raw["payload"]["backfill"] = True
        event = _normalize_monitor_event(raw, 1_763_000_000)
        assert event is not None
        self.assertEqual(event["level"], 1)
        self.assertEqual(event["level_label"], "轻微")
        self.assertEqual(qq._merge_candidates([event], {}, [], 2), [])

    def test_level_three_backfill_is_also_silent_for_qq(self) -> None:
        """历史 DSH 事件在站点统一为 3 级，回填完整更新日志时不能重新刷 QQ。"""
        raw = _raw_dsh_event()
        raw["payload"]["backfill"] = True
        event = _normalize_monitor_event(raw, 1_763_000_000)
        assert event is not None
        self.assertEqual(qq._merge_candidates([event], {}, [], 2), [])
        self.assertTrue(qq._compact_event(event)["backfill"])


if __name__ == "__main__":
    unittest.main()
