from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from webwatch import quality
from webwatch.audit import audit_recent_events
from webwatch.tests.support import cleanup, temp_dir

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _evidence(*, complete: bool = True, signal: bool = True) -> dict:
    evidence = {
        "totals": {"manifest": 2, "added": 0, "removed": 0, "changed": 1},
        "chunk_changes": (
            {"changed": 1, "changed_checked": 1, "checked": True}
            if complete
            else {"changed": 2, "changed_checked": 1}
        ),
        "i18n": {"checked": 1, "added": {}},
    }
    if signal:
        evidence["cjk_added"] = ["登录页面新增了「使用微信扫码登录」"]
    return evidence


def _analysis(**extra) -> dict:
    analysis = {
        "headline": "登录方式更新",
        "tldr": "登录页面新增微信扫码登录",
        "visible_changes": ["登录页面新增了「使用微信扫码登录」"],
        "impact": [],
        "action": "无需操作",
        "level": 3,
        "level_reason": "核心登录流程调整",
        "confidence": "高",
    }
    analysis.update(extra)
    return analysis


def _event(
    *,
    event_id: str = "web:platform:test",
    source: str = "platform",
    level: int = 3,
    evidence: dict | None = None,
    analysis: dict | None = None,
    quality_state: str | None = "pending",
) -> dict:
    event = {
        "id": event_id,
        "slug": event_id.replace(":", "-"),
        "source": source,
        "kind": "page_change",
        "level": level,
        "level_label": "重要",
        "title": "Platform 控制台前端版本更新",
        "summary": "规则摘要",
        "impact": "",
        "action": "无需操作",
        "signal_count": 1,
        "changed_at": "2026-11-17T10:00:00Z",
        "page_url": "https://platform.deepseek.com/",
        "evidence": [],
        "payload": {"frontend": evidence or _evidence()},
    }
    if analysis is not None:
        event["payload"]["analysis"] = analysis
    if quality_state is not None:
        event["quality_state"] = quality_state
        event["next_check_at"] = "2020-01-01T00:00:00Z"  # 已到期
    return event


class AuditStateMachineTests(unittest.TestCase):
    def _root(self) -> Path:
        root = temp_dir("audit-")
        self.addCleanup(cleanup, root)
        return root

    def _write(self, root: Path, event: dict) -> Path:
        path = root / f"{event['slug']}.json"
        path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        return path

    def test_repairs_pending_contradiction_once(self) -> None:
        root = self._root()
        event = _event(
            evidence=_evidence(complete=True, signal=True),
            analysis=_analysis(
                headline="本次为内部调整，未发现界面变化",
                tldr="没有可见变化",
                visible_changes=["未发现用户可见的界面变化"],
                level=3,
                level_reason="核心登录流程调整",
            ),
        )
        path = self._write(root, event)

        counts = audit_recent_events(root, now=NOW)
        self.assertEqual(counts["checked"], 1)
        self.assertEqual(counts["repaired"], 1)
        repaired = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["level"], 2)
        self.assertEqual(repaired["payload"]["quality"], "repaired")
        self.assertEqual(repaired["payload"]["level_decision"]["ai"], 3)
        self.assertEqual(repaired["payload"]["level_decision"]["ai_reason"], "核心登录流程调整")
        self.assertTrue(repaired["payload"]["level_decision"]["discarded"])
        self.assertEqual(repaired["quality_state"], "repaired")
        self.assertNotIn("next_check_at", repaired)

        before = path.read_bytes()
        second = audit_recent_events(root, now=NOW)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(path.read_bytes(), before)  # 终态不再二次改写/重推

    def test_partial_pending_is_flagged_and_level_corrected(self) -> None:
        """P2-02：partial 必须调用 level 修正并写 level_decision。"""
        root = self._root()
        event = _event(
            evidence=_evidence(complete=False, signal=True),
            analysis=_analysis(),
            level=3,
        )
        path = self._write(root, event)

        counts = audit_recent_events(root, now=NOW)
        self.assertEqual(counts["checked"], 1)
        self.assertEqual(counts["flagged"], 1)
        flagged = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(flagged["payload"]["quality"], "partial")
        self.assertEqual(flagged["level"], 2)
        decision = flagged["payload"]["level_decision"]
        self.assertIsNotNone(decision)
        self.assertEqual(decision["final"], 2)
        self.assertEqual(decision["source"], "guard")
        self.assertEqual(flagged["quality_state"], "partial")

    def test_pending_not_due_is_untouched(self) -> None:
        root = self._root()
        event = _event(analysis=_analysis())
        event["next_check_at"] = "2099-01-01T00:00:00Z"
        path = self._write(root, event)
        before = path.read_bytes()

        counts = audit_recent_events(root, now=NOW)
        self.assertEqual(counts, {"checked": 0, "repaired": 0, "flagged": 0, "finalized": 0})
        self.assertEqual(path.read_bytes(), before)

    def test_old_event_without_state_defaults_final(self) -> None:
        """旧事件没有 quality_state：默认 final，即使矛盾也不重扫/重推。"""
        root = self._root()
        event = _event(
            evidence=_evidence(complete=True, signal=True),
            analysis=_analysis(
                headline="本次为内部调整，未发现界面变化",
                tldr="没有可见变化",
                visible_changes=["未发现用户可见的界面变化"],
            ),
            quality_state=None,
        )
        path = self._write(root, event)
        before = path.read_bytes()

        counts = audit_recent_events(root, now=NOW)
        self.assertEqual(counts["checked"], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_gate_written_event_is_finalized_without_content_change(self) -> None:
        """run 写入的 pending 事件只需落终态；payload 不再被 audit 改写。"""
        root = self._root()
        event = _event(
            evidence=_evidence(complete=True, signal=True),
            analysis=_analysis(
                headline="本次为内部调整，未发现界面变化",
                tldr="没有可见变化",
                visible_changes=["未发现用户可见的界面变化"],
            ),
        )
        event["quality_state"] = None
        quality.gate_events([event], mark_audit_state=True)
        self.assertEqual(event["quality_state"], "pending")
        event["next_check_at"] = "2020-01-01T00:00:00Z"
        path = self._write(root, event)
        before_payload = copy.deepcopy(event["payload"])

        counts = audit_recent_events(root, now=NOW)
        self.assertEqual(counts["finalized"], 1)
        audited = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(audited["quality_state"], "repaired")
        self.assertEqual(audited["payload"], before_payload)

    def test_ignores_non_frontend_events(self) -> None:
        root = self._root()
        banner = {
            "id": "web:platform-banner:test",
            "slug": "web-platform-banner-test",
            "source": "platform",
            "kind": "platform_banner",
            "level": 2,
            "title": "Platform 公告更新",
            "payload": {"quality": "partial", "notices": ["新公告"]},
            "quality_state": "pending",
            "next_check_at": "2020-01-01T00:00:00Z",
        }
        path = self._write(root, banner)
        before = path.read_bytes()

        counts = audit_recent_events(root, now=NOW)
        self.assertEqual(counts["checked"], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_audit_matches_gate_decision_for_same_event(self) -> None:
        """同一事件经 audit 与 run 的 gate 得到一致 level/quality/level_decision。"""
        root = self._root()
        raw = _event(evidence=_evidence(complete=False), analysis=_analysis(), level=3)
        gate_event = copy.deepcopy(raw)
        quality.gate_events([gate_event])

        path = self._write(root, copy.deepcopy(raw))
        audit_recent_events(root, now=NOW)
        audited = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(gate_event["level"], audited["level"])
        self.assertEqual(gate_event["payload"]["quality"], audited["payload"]["quality"])
        self.assertEqual(
            gate_event["payload"]["level_decision"], audited["payload"]["level_decision"]
        )


if __name__ == "__main__":
    unittest.main()
