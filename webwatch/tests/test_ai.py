from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from webwatch import ai


def _evidence() -> dict:
    return {
        "source": "chat",
        "label": "Chat 网页",
        "old_commit": "old",
        "new_commit": "new",
        "totals": {"manifest": 10, "added": 0, "removed": 0, "changed": 2},
        "main_js": {"old_size": 1634000, "new_size": 1657849},
        "main_css": {"old_size": 300030, "new_size": 303171},
        "api_added": ["/api/v0/chat/status"],
        "cjk_added": ["使用额度已达上限，请额度恢复后再试"],
        "css_vars_added": ["--quota-image"],
        "i18n": {"checked": 1, "changed_chunks": 1, "added": {"usageLimitReached": 1}, "removed": {}},
        "snippets": [],
    }


def _valid_payload(**overrides) -> dict:
    payload = {
        "headline": "额度用完时会出现提示",
        "tldr": "这次更新增加了额度提示。",
        "visible_changes": ["额度用完时，输入框上方会出现「使用额度已达上限，请额度恢复后再试」。"],
        "impact": ["使用受限额度的用户会看到提示。"],
        "action": "无需操作",
        "confidence": "medium",
    }
    payload.update(overrides)
    return payload


def _content(**overrides) -> str:
    return json.dumps(_valid_payload(**overrides), ensure_ascii=False)


class AiTests(unittest.TestCase):
    def test_normalize_filters_technical_terms_from_user_sections(self) -> None:
        parsed = {
            "headline": "额度用完时会出现提示",
            "tldr": "这次更新增加了额度提示。",
            "visible_changes": [
                "新增接口 /api/v0/chat/status（技术内容应被过滤）",
                "额度用完时，输入框上方会出现「使用额度已达上限，请额度恢复后再试」。",
            ],
            "impact": ["使用受限额度的用户会看到提示。", "接口变化（技术内容应被过滤）"],
            "action": "打开 /api/v0/chat/status 查看",
        }
        analysis = ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence())
        assert analysis is not None
        self.assertEqual(len(analysis["visible_changes"]), 1)
        self.assertIn("输入框上方", analysis["visible_changes"][0])
        self.assertEqual(len(analysis["impact"]), 1)
        # 建议里带技术词时整条丢弃，回落到默认文案，而不是把技术词展示给用户。
        self.assertEqual(analysis["action"], "无需操作")

    def test_normalize_rejects_tech_terms_in_headline_or_tldr(self) -> None:
        for overrides in (
            {"headline": "chunk 结构有调整"},
            {"tldr": "这次改动主要落在 hash 上。"},
        ):
            with self.subTest(overrides=overrides):
                parsed = _valid_payload(**overrides)
                self.assertIsNone(ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence()))

    def test_normalize_handles_dirty_json_and_missing_fields(self) -> None:
        self.assertIsNone(ai._normalize_analysis({"tldr": "只有省流句"}, {"title": "旧标题"}, _evidence()))
        judged = ai._normalize_analysis(_valid_payload(visible_changes="不是数组"), {"title": "旧标题"}, _evidence())
        self.assertIsNone(judged)

    def test_normalize_accepts_level_suggestion_with_reason(self) -> None:
        parsed = _valid_payload(level=3, level_reason="登录入口变化，所有用户都会看到")
        analysis = ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence())
        assert analysis is not None
        self.assertEqual(analysis["level"], 3)
        self.assertEqual(analysis["level_reason"], "登录入口变化，所有用户都会看到")

    def test_normalize_accepts_chinese_level_label(self) -> None:
        parsed = _valid_payload(level="值得看", level_reason="局部文案更新")
        analysis = ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence())
        assert analysis is not None
        self.assertEqual(analysis["level"], 2)

    def test_normalize_rejects_invalid_level_or_tech_reason(self) -> None:
        cases = (
            {"level": 4, "level_reason": "越界"},
            {"level": True, "level_reason": "布尔值"},
            {"level": 3, "level_reason": ""},
            {"level": 3, "level_reason": "chunk 和 hash 都变了"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                analysis = ai._normalize_analysis(
                    _valid_payload(**overrides), {"title": "旧标题"}, _evidence()
                )
                assert analysis is not None
                self.assertIsNone(analysis["level"])
                self.assertEqual(analysis["level_reason"], "")

    def test_verify_claims_checks_level_reason(self) -> None:
        analysis = _valid_payload(level=3, level_reason="版本升到 9.9.9，影响所有用户")
        problems = ai.verify_claims(analysis, _evidence())
        self.assertIn("9.9.9", " | ".join(problems))

    def test_fallback_never_suggests_level(self) -> None:
        fallback = ai.fallback_analysis({"title": "旧标题"}, _evidence(), "llm down")
        self.assertIsNone(fallback["level"])
        self.assertEqual(fallback["level_reason"], "")

    def test_extract_json_accepts_code_fence_and_noise(self) -> None:
        fenced = "```json\n" + _content() + "\n```"
        self.assertIsNotNone(ai._extract_json(fenced))
        noisy = "说明文字 " + _content() + " 结束"
        self.assertIsNotNone(ai._extract_json(noisy))
        self.assertIsNone(ai._extract_json("not json at all"))

    def test_analyze_event_uses_llm_and_returns_analysis(self) -> None:
        with mock.patch.object(ai, "_call_model", return_value=(_content(), "test-model")):
            analysis = ai.analyze_event({"title": "旧标题"}, _evidence())
        assert analysis is not None
        self.assertEqual(analysis["model"], "test-model")
        self.assertEqual(analysis["headline"], "额度用完时会出现提示")
        self.assertFalse(analysis["degraded"])

    def test_verify_claims_flags_fabricated_api_version_and_size(self) -> None:
        clean = _valid_payload()
        self.assertEqual(ai.verify_claims(clean, _evidence()), [])

        fabricated = _valid_payload(
            tldr="这次更新把版本从 2.7.1 升到 9.9.9。",
            visible_changes=[
                "额度用完时，输入框上方会出现提示（/v0/billing/limits）。",
                "主包体积减少约 999 KB。",
            ],
        )
        problems = ai.verify_claims(fabricated, _evidence())
        joined = " | ".join(problems)
        self.assertIn("/v0/billing/limits", joined)
        self.assertIn("9.9.9", joined)
        self.assertIn("999", joined)

    def test_verify_claims_accepts_real_paths_and_rounded_sizes(self) -> None:
        analysis = _valid_payload(
            visible_changes=[
                "额度用完时，输入框上方会出现提示（/api/v0/chat/status）。",
                "主包体积约 1.6 MB。",
            ]
        )
        self.assertEqual(ai.verify_claims(analysis, _evidence()), [])

    def test_analyze_event_retries_once_when_claims_not_in_evidence(self) -> None:
        calls: list[str] = []
        responses = [
            _content(tldr="这次更新把版本升到 9.9.9。"),
            _content(),
        ]

        def fake_call(model, prompt, timeout=None):
            calls.append(prompt)
            return responses.pop(0), model

        with mock.patch.object(ai, "_call_model", side_effect=fake_call):
            analysis = ai.analyze_event({"title": "旧标题"}, _evidence())

        assert analysis is not None
        self.assertEqual(len(calls), 2)
        self.assertIn("9.9.9", calls[1])  # 强化提示里带上找不到的内容
        self.assertIn("复核并重试", analysis["model_note"])
        self.assertFalse(analysis["degraded"])

    def test_analyze_event_gives_up_after_failed_retry(self) -> None:
        calls: list[str] = []
        bad = _content(visible_changes=["额度用完时会出现提示（/v0/billing/limits）。"])

        def fake_call(model, prompt, timeout=None):
            calls.append(prompt)
            return bad, model

        with mock.patch.object(ai, "_call_model", side_effect=fake_call):
            analysis = ai.analyze_event({"title": "旧标题"}, _evidence())

        self.assertIsNone(analysis)
        self.assertEqual(len(calls), 2)  # 只重试一次，不换模型硬试

    def test_enrich_events_disabled_leaves_event_untouched(self) -> None:
        event = {
            "source": "chat",
            "title": "规则标题",
            "summary": "规则摘要",
            "payload": {"frontend": _evidence()},
        }
        with mock.patch.object(ai, "_enabled", return_value=False), mock.patch.object(
            ai, "_call_model", side_effect=AssertionError("不该调用模型")
        ):
            events = ai.enrich_events([event])
        self.assertNotIn("analysis", events[0]["payload"])
        self.assertEqual(events[0]["title"], "规则标题")
        self.assertEqual(events[0]["summary"], "规则摘要")

    def test_enrich_events_skips_model_on_dry_run(self) -> None:
        event = {"source": "chat", "title": "规则标题", "payload": {"frontend": _evidence()}}
        with mock.patch.object(ai, "_call_model", side_effect=AssertionError("不该调用模型")):
            events = ai.enrich_events([event], dry_run=True)
        self.assertNotIn("analysis", events[0]["payload"])

    def test_enrich_events_applies_fallback_when_analysis_missing(self) -> None:
        event = {
            "source": "chat",
            "title": "规则标题",
            "summary": "规则摘要",
            "payload": {"frontend": _evidence()},
        }
        with mock.patch.object(ai, "analyze_event", return_value=None):
            events = ai.enrich_events([event])
        analysis = events[0]["payload"]["analysis"]
        self.assertTrue(analysis["degraded"])
        self.assertTrue(analysis["visible_changes"])
        self.assertEqual(analysis["model"], "规则兜底")
        self.assertEqual(events[0]["summary"], analysis["tldr"])

    def test_enrich_events_survives_analysis_exception(self) -> None:
        event = {"source": "chat", "title": "规则标题", "payload": {"frontend": _evidence()}}
        with mock.patch.object(ai, "analyze_event", side_effect=RuntimeError("boom")):
            events = ai.enrich_events([event])
        analysis = events[0]["payload"]["analysis"]
        self.assertTrue(analysis["degraded"])
        self.assertIn("判读异常", analysis["model_note"])

    def test_fallback_never_puts_api_path_or_tech_words_in_visible(self) -> None:
        fallback = ai.fallback_analysis({"title": "Chat 网页前端版本更新"}, _evidence(), "llm down")
        self.assertTrue(fallback["degraded"])
        joined = " ".join(fallback["visible_changes"])
        self.assertNotIn("/api/", joined)
        self.assertNotIn("接口", joined)
        for item in fallback["visible_changes"]:
            self.assertFalse(ai._visible_contains_tech(item), item)
        # 技术细节只允许出现在 model_note 里。
        self.assertIn("/api/v0/chat/status", fallback["model_note"])

    def test_fallback_mentions_css_rule_changes(self) -> None:
        fallback = ai.fallback_analysis(
            {"title": "Platform 控制台前端版本更新"},
            {"css_rule_changes": {"added": [".modal{margin-bottom:12px}"]}},
            "llm down",
        )
        self.assertTrue(fallback["degraded"])
        self.assertIn("样式", " ".join(fallback["visible_changes"]))

    def test_analyze_event_respects_round_deadline(self) -> None:
        with mock.patch.object(ai, "_call_model", side_effect=AssertionError("不该调用模型")):
            analysis = ai.analyze_event({"title": "旧标题"}, _evidence(), deadline=time.monotonic() - 1)
        self.assertIsNone(analysis)

    def test_enrich_events_passes_round_deadline(self) -> None:
        seen = {}

        def fake_analyze(event, evidence, deadline=None):
            seen["deadline"] = deadline
            return None

        event = {"source": "chat", "title": "规则标题", "payload": {"frontend": _evidence()}}
        with mock.patch.object(ai, "analyze_event", side_effect=fake_analyze):
            ai.enrich_events([event])
        self.assertIsNotNone(seen.get("deadline"))

    def test_model_failure_falls_back_to_rule_text(self) -> None:
        with mock.patch.object(ai, "_call_model", side_effect=RuntimeError("llm down")):
            analysis = ai.analyze_event({"title": "规则标题"}, _evidence())
        self.assertIsNone(analysis)
        fallback = ai.fallback_analysis({"title": "规则标题"}, _evidence(), "llm down")
        self.assertTrue(fallback["degraded"])
        self.assertIn("额度", " ".join(fallback["visible_changes"]))

    def test_model_candidates_are_free_first_and_skip_paid_routes(self) -> None:
        with mock.patch.object(ai, "LLM_MODEL", ""):
            candidates = ai._model_candidates()
        self.assertEqual(candidates[0], "example-model-b")
        self.assertFalse(any(ai._is_paid_model(model) for model in candidates))

        with mock.patch.object(ai, "LLM_MODEL", "example-provider/example-model-f"):
            paid_candidates = ai._model_candidates()
        self.assertNotIn("example-provider/example-model-f", paid_candidates)
        self.assertEqual(paid_candidates[0], "example-model-b")

    def test_llm_request_never_sends_authorization(self) -> None:
        captured = {}

        class FakeResponse:
            headers = {}

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": '{"headline":"x"}'}}], "model": "m"}
                ).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch("webwatch.ai.urllib.request.urlopen", side_effect=fake_urlopen):
            ai._call_model("test-model", "prompt", timeout=12)

        headers = {str(key).lower(): value for key, value in captured["request"].header_items()}
        self.assertNotIn("authorization", headers)
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(captured["timeout"], 12)

    def test_ai_module_never_reads_deepseek_key(self) -> None:
        import inspect

        source = inspect.getsource(ai)
        self.assertNotIn('os.getenv("DEEPSEEK_API_KEY"', source)
        self.assertNotIn("os.environ.get(\"DEEPSEEK_API_KEY\"", source)
        self.assertIn("127.0.0.1:3425", source)


class TechTermFilterTests(unittest.TestCase):
    """P2-13：技术词过滤不能误杀真实界面文案。"""

    def test_real_ui_copy_is_not_technical(self) -> None:
        for text in (
            "支持上传 PDF 文件",
            "请求失败，请稍后重试",
            "下载客户端",
            "文件大小不能超过 10 MB",
            "新增了文件上传入口",
        ):
            with self.subTest(text=text):
                self.assertFalse(ai.contains_tech(text))
                self.assertFalse(ai._visible_contains_tech(text))

    def test_build_terms_are_still_technical(self) -> None:
        for text in ("chunk 结构有调整", "hash 变了", "共 12 个分片", "i18n 配置更新", "源码变更"):
            with self.subTest(text=text):
                self.assertTrue(ai.contains_tech(text))

    def test_normalize_accepts_real_ui_copy(self) -> None:
        parsed = _valid_payload(
            headline="支持上传 PDF 文件",
            tldr="现在可以在对话里上传 PDF 文件。",
            visible_changes=["支持上传 PDF 文件", "请求失败，请稍后重试", "下载客户端"],
        )
        analysis = ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence())
        assert analysis is not None
        self.assertEqual(len(analysis["visible_changes"]), 3)

    def test_short_reason_allows_file_upload_copy(self) -> None:
        from webwatch import quality

        self.assertTrue(quality._short_reason({"level_reason": "新增了文件上传入口"}))


class FallbackSummaryTests(unittest.TestCase):
    """P1-10：兜底摘要用 visible[0]，不能把「模型解读未生成」写进列表 summary。"""

    def test_fallback_tldr_uses_first_visible_sentence(self) -> None:
        fallback = ai.fallback_analysis({"title": "Chat 网页前端版本更新"}, _evidence(), "llm down")
        self.assertIn("额度已达上限", fallback["tldr"])
        self.assertNotIn("模型解读未生成", fallback["tldr"])
        self.assertIn("llm down", fallback["model_note"])

        event = {"title": "规则标题", "summary": "规则摘要", "payload": {"frontend": _evidence()}}
        ai.apply_analysis(event, fallback)
        self.assertIn("额度已达上限", event["summary"])
        self.assertNotIn("模型解读未生成", event["summary"])

    def test_fallback_without_signal_reads_naturally(self) -> None:
        fallback = ai.fallback_analysis(
            {"title": "Chat 网页前端版本更新"},
            {"totals": {"manifest": 1}, "chunk_changes": {"changed": 1, "changed_checked": 1}},
            "llm down",
        )
        self.assertEqual(
            fallback["tldr"],
            "本轮没有抓到可读的界面变化；可能是内部调整，也可能是证据覆盖不足。",
        )
        self.assertEqual(fallback["action"], "无需操作")

    def test_pipeline_note_goes_to_model_note_only(self) -> None:
        fallback = ai.fallback_analysis({"title": "规则标题"}, _evidence())
        self.assertIn("模型解读未生成", fallback["model_note"])
        self.assertNotIn("模型解读未生成", fallback["tldr"])

    def test_fallback_keeps_both_added_and_removed_copy(self) -> None:
        """既删又加时两样都要写；摘要仍以新增开头；同一句不重复。"""
        evidence = _evidence()
        evidence["cjk_removed"] = ["删除了手机号登录入口"]
        evidence["cjk_added"] = ["新增：支持深色模式", "使用额度已达上限，请额度恢复后再试"]
        fallback = ai.fallback_analysis({"title": "Chat 网页前端版本更新"}, evidence, "llm down")
        visible = " ".join(fallback["visible_changes"])
        self.assertIn("新增：支持深色模式", visible)
        self.assertIn("删除了手机号登录入口", visible)
        self.assertIn("新增：支持深色模式", fallback["tldr"])  # 新增排前，摘要口径不变
        self.assertEqual(
            sum("新增了一条提示" in item for item in fallback["visible_changes"]), 2
        )

    def test_fallback_headline_matches_added_first_tldr(self) -> None:
        """既删又加时，标题不能只说精简，而摘要以新增开头。"""
        evidence = _evidence()
        evidence["cjk_removed"] = ["删除了手机号登录入口"]
        evidence["cjk_added"] = ["新增：支持深色模式", "短提示"]
        fallback = ai.fallback_analysis({"title": "Chat 网页前端版本更新"}, evidence, "llm down")
        self.assertIn("新增：支持深色模式", fallback["tldr"])
        # 标题与 tldr 用同一条新增文案，不能一个取最短、一个取第一条。
        self.assertIn("新增：支持深色模式", fallback["headline"])
        self.assertNotIn("精简", fallback["headline"])
        self.assertIn("有增有减", fallback["headline"])

    def test_fallback_keeps_api_removed_alongside_copy_signals(self) -> None:
        """api_removed 与新增/删除文案并存时也要出现，且不被 visible[:5] 截掉。"""
        evidence = _evidence()
        evidence["api_removed"] = ["/api/v0/old"]
        evidence["cjk_added"] = [f"新增提示{i}" for i in range(3)]
        evidence["cjk_removed"] = [f"删除提示{i}" for i in range(3)]
        fallback = ai.fallback_analysis({"title": "规则标题"}, evidence, "llm down")
        self.assertIn("有功能入口被移除", " ".join(fallback["visible_changes"]))
        self.assertIn("有功能入口被移除", " ".join(fallback["visible_changes"][:5]))
        self.assertNotIn("没有抓到可读的界面变化", " ".join(fallback["visible_changes"]))

    def test_fallback_removed_copy_stays_ahead_of_generic_api_text(self) -> None:
        """没有新增文案时，摘要仍以具体的删除提示为主，不被 API 提示顶掉。"""
        evidence = _evidence()
        evidence["api_removed"] = ["/api/v0/old"]
        evidence["cjk_added"] = []
        evidence["cjk_removed"] = ["手机号登录入口已下线"]
        fallback = ai.fallback_analysis({"title": "规则标题"}, evidence, "llm down")
        self.assertIn("手机号登录入口已下线", fallback["tldr"])
        self.assertIn("有功能入口被移除", " ".join(fallback["visible_changes"]))

    def test_fallback_removed_signal_has_matching_visible_text(self) -> None:
        """只有删除型信号时也要有人话文案，不再说「没抓到变化」。"""
        cases = [
            {"api_removed": ["/api/v0/old"]},
            {"css_rule_changes": {"removed": [".old{display:none}"]}},
            {"i18n": {"checked": 1, "removed": {"oldKey": 1}}},
            {"added_i18n_refs": [{"key": "k", "values": ["新提示"]}]},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                evidence = {"totals": {"manifest": 1}, "chunk_changes": {"changed": 1}}
                evidence.update(extra)
                fallback = ai.fallback_analysis({"title": "规则标题"}, evidence)
                self.assertNotIn("没有抓到可读的界面变化", " ".join(fallback["visible_changes"]))


class TechnicalOnlyFallbackTests(unittest.TestCase):
    """只有技术变化时，规则兜底必须说清文件和字节，而不是「未捕获」。"""

    def test_fallback_names_changed_file_and_bytes(self) -> None:
        evidence = {
            "totals": {"manifest": 612, "added": 0, "removed": 0, "changed": 1},
            "main_js": {"old_size": 1664072, "new_size": 1664075},
            "main_css": {"old_size": 303321, "new_size": 303321},
            "changed_files": [{"key": "js:entry", "name": "main.js", "kind": "js", "role": "entry"}],
        }
        fallback = ai.fallback_analysis({"title": "未捕获到用户可见变化（可能为内部调整）"}, evidence)
        self.assertTrue(fallback["headline"].startswith("仅有技术变化"))
        self.assertIn("main.js", fallback["headline"])
        self.assertIn("+3", fallback["headline"])
        self.assertIn("main.js", fallback["visible_changes"][0])
        self.assertIn("未提取到用户可见文案", fallback["visible_changes"][0])
        self.assertIn("技术变化", fallback["model_note"])
        self.assertIn("技术变化", fallback["impact"][0])

    def test_manifest_only_does_not_fake_technical_change(self) -> None:
        evidence = {"totals": {"manifest": 1}, "chunk_changes": {"changed": 1, "changed_checked": 1}}
        fallback = ai.fallback_analysis({"title": "规则标题"}, evidence)
        self.assertEqual(
            fallback["tldr"],
            "本轮没有抓到可读的界面变化；可能是内部调整，也可能是证据覆盖不足。",
        )

    def test_normalize_keeps_evidence_gaps(self) -> None:
        parsed = _valid_payload(evidence_gaps=["旧版分片未取到", "语言包未核对"])
        analysis = ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence())
        assert analysis is not None
        self.assertEqual(analysis["evidence_gaps"], ["旧版分片未取到", "语言包未核对"])

    def test_normalize_rejects_bare_uncertain_when_evidence_exists(self) -> None:
        parsed = _valid_payload(visible_changes=["无法确认", "具体位置无法确认"])
        self.assertIsNone(ai._normalize_analysis(parsed, {"title": "旧标题"}, _evidence()))


class PromptTruncationTests(unittest.TestCase):
    """P2-08：截断时控制字段必须先保留。"""

    def test_control_fields_survive_truncation(self) -> None:
        evidence = _evidence()
        evidence.update({
            "truncated": True,
            "baseline_missing": True,
            "chunk_changes": {"changed": 3, "changed_checked": 1},
            "snippets": ["x" * 40_000],
        })
        prompt = ai._prompt_for(evidence)
        self.assertIn("控制字段", prompt)
        self.assertIn('"truncated":true', prompt)
        self.assertIn('"baseline_missing":true', prompt)
        self.assertIn('"chunk_changes"', prompt)


if __name__ == "__main__":
    unittest.main()
