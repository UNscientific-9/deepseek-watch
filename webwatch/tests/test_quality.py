from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from webwatch import ai, quality
from webwatch import run as run_module
from tools.backfill_frontend_analysis import finalize_event


def _event(
    evidence: dict,
    analysis: dict | None = None,
    *,
    kind: str = "page_change",
    source: str = "platform",
    level: int = 2,
    detail: list | None = None,
) -> dict:
    event = {
        "source": source,
        "kind": kind,
        "level": level,
        "level_label": "",
        "title": "Platform 控制台前端版本更新",
        "payload": {"frontend": evidence},
    }
    if analysis is not None:
        event["payload"]["analysis"] = analysis
    if detail is not None:
        event["changes_detail"] = detail
    return event


def _complete(**extra) -> dict:
    evidence = {
        "totals": {"manifest": 1, "added": 0, "removed": 0, "changed": 1},
        "chunk_changes": {"changed": 1, "changed_checked": 1, "checked": True},
        "i18n": {"checked": 1, "added": {}},
    }
    evidence.update(extra)
    return evidence


def _no_change_analysis() -> dict:
    return {
        "headline": "本次为内部调整，未发现界面变化",
        "tldr": "没有可见变化",
        "visible_changes": ["未发现用户可见的界面变化"],
        "impact": [],
        "action": "无需操作",
    }


class QualityGateTests(unittest.TestCase):
    def test_evidence_with_signal_replaces_no_change_analysis(self) -> None:
        event = _event(
            {
                "totals": {"manifest": 1, "added": 0, "removed": 0, "changed": 1},
                "chunk_changes": {"changed": 1, "changed_checked": 1},
                "css_rule_changes": {"checked": 1, "added": [".modal{margin:12px}"]},
            },
            _no_change_analysis(),
        )
        events, counts = quality.gate_events([event])
        self.assertEqual(counts["contradiction"], 1)
        self.assertEqual(event["payload"]["quality"], "repaired")
        self.assertNotIn("本次为内部调整", event["title"])
        self.assertIn("样式", " ".join(event["payload"]["analysis"]["visible_changes"]))
        # 证据与结论矛盾时按证据发布，级别由闸门定在「值得看」。
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "gate")

    def test_internal_rebuild_without_signal_is_allowed(self) -> None:
        event = _event(_complete(), _no_change_analysis())
        events, counts = quality.gate_events([event])
        self.assertEqual(counts["internal"], 1)
        self.assertEqual(event["payload"]["quality"], "internal")
        self.assertEqual(event["level"], 1)
        self.assertEqual(event["payload"]["level_decision"]["source"], "rule")

    def test_main_code_snippet_is_a_visible_signal(self) -> None:
        event = _event(
            _complete(changed_code_snippets=[{"file": "main.js", "text": "let wechatLogin=true;"}]),
            _no_change_analysis(),
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["contradiction"], 1)
        self.assertEqual(event["payload"]["quality"], "repaired")
        self.assertEqual(event["level"], 2)
        self.assertNotIn("本次为内部调整", event["title"])

    def test_removed_i18n_reference_is_a_visible_signal(self) -> None:
        evidence = _complete(
            cjk_added=[],
            removed_i18n_refs=[
                {
                    "key": "deleteAccountNoMoneyBackTitle",
                    "file": "2653",
                    "values": ["充值余额无法恢复", "Unused balance will be forfeited"],
                    "old_refs": 1,
                    "new_refs": 0,
                }
            ],
        )
        event = _event(evidence, _no_change_analysis())
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["contradiction"], 1)
        self.assertEqual(event["payload"]["quality"], "repaired")
        self.assertEqual(event["level"], 2)
        self.assertIn("充值余额无法恢复", " ".join(event["payload"]["analysis"]["visible_changes"]))

    def test_partial_coverage_stays_visible_not_downgraded(self) -> None:
        event = _event(
            {
                "totals": {"manifest": 1, "added": 0, "removed": 0, "changed": 1},
                "chunk_changes": {"changed": 2, "changed_checked": 1},
            },
            _no_change_analysis(),
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["partial"], 1)
        self.assertEqual(event["payload"]["quality"], "partial")
        # 旧证据没取全时不能压成「轻微」，按「值得看」发布并标注待核实。
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "guard")

    def test_unpaired_chunks_are_partial(self) -> None:
        evidence = _complete()
        evidence["chunk_changes"] = {
            "added": 1,
            "removed": 1,
            "paired": 0,
            "partial_pairing": True,
            "checked": False,
        }
        evidence["cjk_added"] = ["新增了一条提示"]
        event = _event(
            evidence,
            {
                "headline": "新增了一条提示",
                "tldr": "页面新增提示",
                "visible_changes": ["新增了一条提示"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "所有用户都会看到新提示",
                "confidence": "高",
            },
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["partial"], 1)
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "guard")

    def test_missing_baseline_is_partial(self) -> None:
        evidence = _complete(baseline_missing=True)
        event = _event(evidence, _no_change_analysis())
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["partial"], 1)
        self.assertEqual(event["level"], 2)

    def test_ai_can_promote_to_important_with_strong_evidence(self) -> None:
        evidence = _complete(
            cjk_added=["登录页面新增了「使用微信扫码登录」"],
            i18n={"checked": 1, "added": {"login.wechat": "使用微信扫码登录"}},
        )
        event = _event(
            evidence,
            {
                "headline": "登录方式更新",
                "tldr": "登录页面新增微信扫码登录",
                "visible_changes": ["登录页面新增了「使用微信扫码登录」"],
                "impact": ["影响所有用户登录"],
                "action": "需要时可以用微信扫码登录",
                "level": 3,
                "level_reason": "登录入口变化，所有用户都会看到",
                "confidence": "高",
            },
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(event["payload"]["quality"], "ok")
        self.assertEqual(event["level"], 3)
        decision = event["payload"]["level_decision"]
        self.assertEqual(decision["source"], "ai")
        self.assertEqual(decision["ai"], 3)
        self.assertEqual(decision["rule"], 2)

    def test_ai_cannot_promote_with_low_confidence(self) -> None:
        evidence = _complete(
            cjk_added=["登录页面新增了「使用微信扫码登录」"],
        )
        event = _event(
            evidence,
            {
                "headline": "登录方式更新",
                "tldr": "登录页面新增微信扫码登录",
                "visible_changes": ["登录页面新增了「使用微信扫码登录」"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "登录入口变化，所有用户都会看到",
                "confidence": "低",
            },
        )
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "rule")

    def test_ai_cannot_promote_with_incomplete_coverage(self) -> None:
        evidence = {
            "totals": {"manifest": 1, "added": 0, "removed": 0, "changed": 1},
            "chunk_changes": {"changed": 2, "changed_checked": 1},
            "cjk_added": ["登录页面新增了「使用微信扫码登录」"],
        }
        event = _event(
            evidence,
            {
                "headline": "登录方式更新",
                "tldr": "登录页面新增微信扫码登录",
                "visible_changes": ["登录页面新增了「使用微信扫码登录」"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "登录入口变化，所有用户都会看到",
                "confidence": "高",
            },
        )
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "guard")

    def test_ai_level_two_is_kept(self) -> None:
        evidence = _complete(cjk_added=["帮助中心新增了一条说明"])
        detail = [{"title": "界面文案", "level": 2, "items": ["新增说明"]}]
        event = _event(
            evidence,
            {
                "headline": "帮助中心文案更新",
                "tldr": "新增一条说明",
                "visible_changes": ["帮助中心新增了一条说明"],
                "impact": [],
                "action": "无需操作",
                "level": 2,
                "level_reason": "局部文案更新，不影响主要流程",
                "confidence": "高",
            },
            detail=detail,
        )
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "ai")
        self.assertEqual(detail[0]["level"], 2)

    def test_ai_reason_with_tech_terms_blocks_promotion(self) -> None:
        evidence = _complete(
            cjk_added=["登录页面新增了「使用微信扫码登录」"],
        )
        event = _event(
            evidence,
            {
                "headline": "登录方式更新",
                "tldr": "登录页面新增微信扫码登录",
                "visible_changes": ["登录页面新增了「使用微信扫码登录」"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "新的 chunk 和 hash 都变了",
                "confidence": "高",
            },
        )
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 2)
        decision = event["payload"]["level_decision"]
        self.assertEqual(decision["source"], "rule")
        self.assertEqual(decision["ai"], 3)

    def test_non_frontend_events_are_untouched(self) -> None:
        banner = {
            "source": "platform",
            "kind": "platform_banner",
            "level": 2,
            "title": "Platform 公告更新",
            "payload": {"notices": ["新公告"]},
        }
        text_only = {
            "source": "chat",
            "kind": "page_change",
            "level": 1,
            "title": "Chat 网页正文更新",
            "payload": {"changes": [{"kind": "text"}], "analysis": _no_change_analysis()},
        }
        events, counts = quality.gate_events([banner, text_only])
        self.assertEqual(counts, {})
        self.assertNotIn("quality", banner["payload"])
        self.assertNotIn("quality", text_only["payload"])
        self.assertEqual(banner["level"], 2)
        self.assertEqual(text_only["level"], 1)


class FalsePartialRegressionTests(unittest.TestCase):
    """真实样本回归：有具体文案/可对照变化时，预算截断不应压成待核实。"""

    def _analysis(self, headline: str) -> dict:
        return {
            "headline": headline,
            "tldr": headline,
            "visible_changes": [headline],
            "impact": [],
            "action": "无需操作",
            "level": 2,
            "level_reason": "局部界面更新",
            "confidence": "中",
        }

    def test_truncated_but_concrete_copy_is_ok(self) -> None:
        """11-25 10:20 样本：8 条新增文案 + 6 个 i18n key，仅因预算截断被标待核实。"""
        evidence = {
            "truncated": True,
            "truncation_reasons": ["取证预算在核对 CSS 分片时耗尽"],
            "totals": {"manifest": 323, "added": 1, "removed": 1, "changed": 1},
            "chunk_changes": {
                "changed": 1,
                "changed_checked": 1,
                "checked": True,
                "paired": 1,
                "added": 1,
                "removed": 1,
            },
            "css_rule_changes": {"changed": 0, "checked": 0},
            "i18n": {
                "checked": 1,
                "changed_chunks": 1,
                "added": {"settings": 1, "settingsAccount": 1, "settingsTitle": 1},
                "removed": {},
            },
            "cjk_added": ["主题", "服务协议", "查看", "浅色", "深色", "登出", "登出所有设备", "系统设置"],
            "main_js": {"old_size": 77403, "new_size": 77403},
        }
        event = _event(evidence, self._analysis("控制台新增系统设置"))
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("ok"), 1)
        self.assertEqual(event["payload"]["quality"], "ok")
        self.assertEqual(event["level"], 2)
        decision = event["payload"]["level_decision"]
        self.assertIn("取证预算", decision["reason"])
        self.assertTrue(decision["coverage_gaps"])

    def test_checked_shortfall_with_strong_copy_is_ok(self) -> None:
        """11-23 样本：87 个内容变化只核对到 85 个，但已有大量可读文案。"""
        evidence = {
            "truncated": True,
            "totals": {"manifest": 612, "added": 0, "removed": 0, "changed": 87},
            "chunk_changes": {"changed": 87, "changed_checked": 85},
            "i18n": {
                "checked": 85,
                "changed_chunks": 87,
                "added": {"deleteAccountBalanceWarnTitle": 3},
                "removed": {},
            },
            "cjk_added": ["如账户仍有余额或欠费可能会导致注销失败", "确认放弃余额并注销我的账号"],
            "main_js": {"old_size": 1664075, "new_size": 1666669},
        }
        event = _event(evidence, self._analysis("注销账号新增余额提示"))
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("ok"), 1)
        self.assertEqual(event["payload"]["quality"], "ok")
        self.assertIn("85/87", event["payload"]["level_decision"]["reason"])

    def test_i18n_key_without_copy_does_not_claim_specific_text(self) -> None:
        """i18n 只新增 key、没取到文案时，不能编造具体提示，也不能升 3 级。"""
        evidence = _complete(
            cjk_added=[],
            i18n={"checked": 1, "added": {"settingsAccount": 1}, "removed": {}},
        )
        event = _event(
            evidence,
            {
                "headline": "界面文案有调整",
                "tldr": "界面文案有调整",
                "visible_changes": ["界面上有文字措辞调整，具体位置无法确认。"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "账号相关文案调整",
                "confidence": "高",
            },
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("ok"), 1)
        self.assertEqual(event["level"], 2)
        joined = " ".join(event["payload"]["analysis"]["visible_changes"])
        self.assertIn("措辞调整", joined)
        self.assertNotIn("「settingsAccount」", joined)

    def test_truncated_without_copy_stays_partial(self) -> None:
        evidence = {
            "truncated": True,
            "totals": {"manifest": 10, "added": 0, "removed": 0, "changed": 2},
            "chunk_changes": {"changed": 2, "changed_checked": 1},
            "i18n": {"checked": 0, "added": {}, "removed": {}},
        }
        event = _event(evidence, None)
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("partial"), 1)
        self.assertEqual(event["payload"]["quality"], "partial")
        self.assertIn("1/2", event["payload"]["level_decision"]["reason"])

    def test_technical_only_internal_names_the_changed_file(self) -> None:
        """11-20 样本：main.js +3 字节、没有可读文案，不能只说「未捕获」。"""
        evidence = _complete(
            totals={"manifest": 612, "added": 0, "removed": 0, "changed": 1},
            main_js={"old_size": 1664072, "new_size": 1664075},
            main_css={"old_size": 303321, "new_size": 303321},
            changed_files=[{"key": "js:entry", "name": "main.js", "kind": "js", "role": "entry"}],
        )
        event = _event(evidence, None)
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts.get("internal"), 1)
        self.assertEqual(event["payload"]["quality"], "internal")
        self.assertEqual(event["level"], 1)
        self.assertTrue(event["title"].startswith("仅有技术变化"))
        self.assertIn("main.js", event["title"])
        self.assertIn("+3", event["title"])
        self.assertIn("main.js", event["summary"])
        decision = event["payload"]["level_decision"]
        self.assertIn("仅技术变化", decision["reason"])
        self.assertIn("未提取到用户可见文案", decision["reason"])


class DecisionFunctionTests(unittest.TestCase):
    """B-10：Decision 纯函数 + 状态矩阵。"""

    def _analysis(self, **extra) -> dict:
        analysis = {
            "headline": "登录方式更新",
            "tldr": "登录页面新增微信扫码登录",
            "visible_changes": ["登录页面新增了「使用微信扫码登录」"],
            "impact": ["影响所有用户登录"],
            "action": "需要时可以用微信扫码登录",
            "level": 3,
            "level_reason": "登录入口变化，所有用户都会看到",
            "confidence": "高",
        }
        analysis.update(extra)
        return analysis

    def test_decide_is_pure_and_returns_contract(self) -> None:
        event = _event(
            _complete(cjk_added=["登录页面新增了「使用微信扫码登录」"]),
            self._analysis(),
        )
        before = copy.deepcopy(event)
        decision = quality.decide(event, event["payload"]["frontend"], "ok")
        self.assertEqual(event, before)  # 纯函数：不改事件
        for key in ("level", "quality", "reason", "ai_suggestion", "discarded"):
            self.assertIn(key, decision)
        self.assertEqual(decision["level"], 3)
        self.assertEqual(decision["quality"], "ok")
        self.assertEqual(decision["ai_suggestion"], 3)
        self.assertFalse(decision["discarded"])

    def test_decide_status_matrix(self) -> None:
        cases = (
            ("ok", True, True, 3, "ok"),
            ("partial", True, False, 2, "partial"),
            ("internal", True, False, 1, "internal"),
            ("contradiction", True, False, 2, "repaired"),
            ("missing", True, False, 2, "repaired"),
            ("contradiction", False, False, 2, "contradiction"),
            ("missing", False, False, 2, "missing"),
        )
        for status, ai_enabled, with_analysis, level, quality_value in cases:
            with self.subTest(status=status, ai_enabled=ai_enabled, analysis=with_analysis):
                evidence = _complete(cjk_added=["登录页面新增了「使用微信扫码登录」"])
                event = _event(evidence)
                if with_analysis:
                    event["payload"]["analysis"] = self._analysis()
                decision = quality.decide(
                    event, evidence, status, ai_enabled=ai_enabled
                )
                self.assertEqual(decision["level"], level)
                self.assertEqual(decision["quality"], quality_value)

    def test_internal_discards_ai_hallucination(self) -> None:
        """P2-01：internal 不能保留「新增登录验证入口」这类 AI 幻觉文本。"""
        event = _event(
            _complete(),
            {
                "headline": "新增登录验证入口",
                "tldr": "界面新增了登录验证码入口",
                "visible_changes": ["新增登录验证入口"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "登录入口变化，所有用户都会看到",
                "confidence": "高",
            },
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["internal"], 1)
        self.assertEqual(event["level"], 1)
        self.assertEqual(event["payload"]["quality"], "internal")
        self.assertNotIn("登录验证入口", event["title"])
        self.assertNotIn("登录验证码", event["summary"])
        self.assertTrue(event["payload"]["analysis"]["degraded"])
        decision = event["payload"]["level_decision"]
        self.assertTrue(decision["discarded"])
        self.assertEqual(decision["ai"], 3)  # 原 AI 建议仍留档

    def test_contradiction_keeps_ai_suggestion_and_reason(self) -> None:
        """P2-14：矛盾修复后 level_decision 保留原 AI 级别/理由并标 discarded。"""
        evidence = _complete(css_rule_changes={"checked": 1, "added": [".modal{margin:12px}"]})
        event = _event(
            evidence,
            {
                "headline": "本次为内部调整，未发现界面变化",
                "tldr": "没有可见变化",
                "visible_changes": ["未发现用户可见的界面变化"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "核心登录流程调整",
                "confidence": "高",
            },
        )
        _events, counts = quality.gate_events([event])
        self.assertEqual(counts["contradiction"], 1)
        self.assertEqual(event["payload"]["quality"], "repaired")
        decision = event["payload"]["level_decision"]
        self.assertEqual(decision["final"], 2)
        self.assertEqual(decision["source"], "gate")
        self.assertEqual(decision["ai"], 3)
        self.assertEqual(decision["ai_reason"], "核心登录流程调整")
        self.assertTrue(decision["discarded"])


class PromotionThresholdTests(unittest.TestCase):
    """P2-03：同一句文案不能中文 + i18n key 各算一条。"""

    def _promotion_analysis(self) -> dict:
        return {
            "headline": "欢迎使用新版界面",
            "tldr": "欢迎使用新版界面",
            "visible_changes": ["欢迎使用新版界面"],
            "impact": [],
            "action": "无需操作",
            "level": 3,
            "level_reason": "所有用户都会看到新界面",
            "confidence": "高",
        }

    def test_same_text_in_cjk_and_i18n_counts_once(self) -> None:
        evidence = _complete(
            cjk_added=["欢迎使用新版界面"],
            i18n={"checked": 1, "added": {"welcome.title": "欢迎使用新版界面"}},
        )
        event = _event(evidence, self._promotion_analysis())
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "rule")

    def test_i18n_key_without_text_does_not_count(self) -> None:
        """审查报告原探针：cjk 一条 + i18n key 计数一条，不能算两条证据。"""
        evidence = _complete(
            cjk_added=["欢迎使用新版界面"],
            i18n={"checked": 1, "added": {"welcome.title": 1}},
        )
        self.assertFalse(
            quality._can_promote(evidence, self._promotion_analysis())
        )

    def test_two_distinct_user_texts_can_promote(self) -> None:
        evidence = _complete(cjk_added=["欢迎使用新版界面", "帮助中心新增使用说明"])
        event = _event(evidence, self._promotion_analysis())
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 3)
        self.assertEqual(event["payload"]["level_decision"]["source"], "ai")

    def test_generic_button_words_do_not_promote(self) -> None:
        evidence = _complete(cjk_added=["确定", "取消"])
        event = _event(evidence, self._promotion_analysis())
        _events, _counts = quality.gate_events([event])
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_decision"]["source"], "rule")


class AiDisabledGateTests(unittest.TestCase):
    """P2-04：WEBWATCH_AI=0 时 gate 只判 quality/level，不写 fallback/apply。"""

    def _event(self) -> dict:
        return {
            "source": "chat",
            "kind": "page_change",
            "level": 2,
            "level_label": "值得看",
            "title": "规则标题",
            "summary": "规则摘要",
            "impact": "",
            "action": "",
            "payload": {"frontend": _complete(cjk_added=["帮助中心新增一条说明"])},
        }

    def test_gate_does_not_write_analysis_when_ai_disabled(self) -> None:
        event = self._event()
        _events, counts = quality.gate_events([event], ai_enabled=False)
        self.assertEqual(counts["missing"], 1)
        self.assertEqual(event["payload"]["quality"], "missing")
        self.assertNotIn("analysis", event["payload"])
        self.assertEqual(event["title"], "规则标题")
        self.assertEqual(event["summary"], "规则摘要")
        self.assertEqual(event["level"], 2)
        self.assertIsNone(event["payload"]["level_decision"]["ai"])

    def test_run_prepare_respects_webwatch_ai_zero(self) -> None:
        event = self._event()
        with mock.patch.dict(os.environ, {"WEBWATCH_AI": "0"}):
            events, counts = run_module._prepare_events_for_publish(
                [event], now=datetime(2026, 11, 18, tzinfo=timezone.utc)
            )
        self.assertEqual(counts["missing"], 1)
        self.assertNotIn("analysis", events[0]["payload"])
        self.assertEqual(events[0]["title"], "规则标题")
        self.assertEqual(events[0]["summary"], "规则摘要")
        self.assertEqual(events[0]["quality_state"], "pending")
        self.assertIn("next_check_at", events[0])


class AuditStateTests(unittest.TestCase):
    """B-8：只有未决事件进入 pending，终态/旧事件不被复核。"""

    def test_all_new_events_start_pending(self) -> None:
        repaired = _event(
            _complete(css_rule_changes={"checked": 1, "added": [".modal{margin:12px}"]}),
            {
                "headline": "本次为内部调整，未发现界面变化",
                "tldr": "没有可见变化",
                "visible_changes": ["未发现用户可见的界面变化"],
                "impact": [],
                "action": "无需操作",
                "level": 3,
                "level_reason": "登录入口变化",
                "confidence": "高",
            },
        )
        quality.gate_events([repaired], mark_audit_state=True)
        self.assertEqual(repaired["quality_state"], "pending")
        self.assertIn("next_check_at", repaired)
        self.assertEqual(repaired["payload"]["quality"], "repaired")

        partial = _event(
            {
                "totals": {"manifest": 1, "added": 0, "removed": 0, "changed": 1},
                "chunk_changes": {"changed": 2, "changed_checked": 1},
            },
            _no_change_analysis(),
        )
        quality.gate_events([partial], mark_audit_state=True)
        self.assertEqual(partial["quality_state"], "pending")

        unresolved = _event(
            _complete(cjk_added=["帮助中心新增一条说明"]),
            None,
        )
        quality.gate_events([unresolved], ai_enabled=False, mark_audit_state=True)
        self.assertEqual(unresolved["quality_state"], "pending")

        legacy = _event(_complete(), _no_change_analysis())
        self.assertEqual(quality.quality_state(legacy), "final")

    def test_terminal_state_cannot_go_back_to_pending(self) -> None:
        event = {"quality_state": "final"}
        with self.assertRaises(ValueError):
            quality.mark_quality_state(event, "pending")


class BackfillGateTests(unittest.TestCase):
    """P1-06：回填必须走 quality.gate_events，并清掉旧 quality/level_decision。"""

    def _evidence(self) -> dict:
        return {
            "totals": {"manifest": 2, "added": 0, "removed": 0, "changed": 2},
            "chunk_changes": {"changed": 2, "changed_checked": 1},
            "cjk_added": ["新增了一条登录提示"],
        }

    def _analysis(self) -> dict:
        return {
            "headline": "登录提示更新",
            "tldr": "登录页新增提示",
            "visible_changes": ["新增了一条登录提示"],
            "impact": ["登录用户会看到提示"],
            "action": "无需操作",
            "level": 3,
            "level_reason": "登录入口变化，所有用户都会看到",
            "confidence": "高",
            "model": "test",
            "degraded": False,
        }

    def test_backfill_runs_gate_and_clears_old_state(self) -> None:
        event = {
            "id": "web:platform:backfill",
            "source": "platform",
            "level": 3,
            "payload": {
                "quality": "contradiction",
                "level_decision": {"final": 3, "source": "ai"},
            },
        }
        finalize_event(event, self._evidence(), self._analysis(), source="platform")
        self.assertEqual(event["kind"], "page_change")
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["quality"], "partial")
        decision = event["payload"]["level_decision"]
        self.assertEqual(decision["final"], 2)
        self.assertEqual(decision["source"], "guard")
        self.assertEqual(event["quality_state"], "final")
        self.assertTrue(all(group["level"] == 2 for group in event["changes_detail"]))

    def test_backfill_level_zero_is_not_raised_to_three(self) -> None:
        event = {"id": "web:platform:zero", "source": "platform", "level": 0, "payload": {}}
        evidence = _complete()
        finalize_event(event, evidence, _no_change_analysis(), source="platform", level=0)
        self.assertEqual(event["level"], 1)  # internal 规则级
        self.assertEqual(event["payload"]["level_decision"]["rule"], 0)

    def test_backfill_missing_kind_defaults_page_change(self) -> None:
        event = {"id": "web:chat:kindless", "source": "chat", "level": 2, "payload": {}}
        finalize_event(event, _complete(), _no_change_analysis(), source="chat")
        self.assertEqual(event["kind"], "page_change")

    def test_backfill_rejects_other_kind(self) -> None:
        event = {
            "id": "web:chat:banner",
            "source": "chat",
            "kind": "platform_banner",
            "level": 2,
            "payload": {},
        }
        with self.assertRaises(ValueError):
            finalize_event(event, _complete(), _no_change_analysis(), source="chat")

    def test_run_and_backfill_same_event_same_result(self) -> None:
        raw = {
            "id": "web:platform:same",
            "source": "platform",
            "kind": "page_change",
            "level": 3,
            "title": "Platform 控制台前端版本更新",
            "summary": "规则摘要",
            "payload": {"quality": "contradiction"},
        }
        run_event = copy.deepcopy(raw)
        run_event["payload"]["frontend"] = copy.deepcopy(self._evidence())
        run_event["payload"]["analysis"] = self._analysis()
        with mock.patch.object(run_module, "enrich_events", side_effect=lambda events: events):
            run_module._prepare_events_for_publish([run_event], ai_enabled=True)

        backfill_event = copy.deepcopy(raw)
        finalize_event(
            backfill_event,
            copy.deepcopy(self._evidence()),
            self._analysis(),
            source="platform",
            level=3,
        )
        for key in ("level", "level_label"):
            self.assertEqual(run_event[key], backfill_event[key])
        self.assertEqual(run_event["payload"]["quality"], backfill_event["payload"]["quality"])
        self.assertEqual(
            run_event["payload"]["level_decision"], backfill_event["payload"]["level_decision"]
        )


if __name__ == "__main__":
    unittest.main()
