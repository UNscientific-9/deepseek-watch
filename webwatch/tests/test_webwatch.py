from __future__ import annotations

import gzip
import io
import json
import time
import unittest
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from webwatch import config
from webwatch import fingerprint as fingerprint_module
from webwatch import run as run_module
from webwatch.events import build_event, iso, slugify, write_event
from webwatch.extract import extract_page, normalize_text
from webwatch.fingerprint import (
    ASSET_SCOPE_VERSION,
    ApiError,
    _diff_target,
    _event_key,
    _platform_summary,
    _source_fail,
    _source_failed_this_round,
    _source_ok,
    _versions_look_incomplete,
    build_target_snapshot,
    check_api,
    check_catalog,
    check_model_versions,
    check_platform_banner,
    check_targets,
    probe_model,
)
from webwatch.models import parse_model_versions
from webwatch.run import _is_due, _round_successes
from webwatch import safe_fetch
from webwatch.safe_fetch import (
    ContentError,
    FetchError,
    HTTPStatusError,
    ParseError,
    Response,
    TooLarge,
    UnsafeURL,
    _read_body,
    fetch,
    validate_url,
)
from webwatch.tests.support import cleanup as _cleanup_temp
from webwatch.tests.support import temp_dir as _make_temp_dir


@contextmanager
def _temp_dir():
    """系统临时目录（Windows 沙箱自动回退，见 tests/support.py）。"""
    path = _make_temp_dir("webwatch-")
    try:
        yield path
    finally:
        _cleanup_temp(path)


def _html_response(url: str, text: str, *, content_type: str = "text/html; charset=utf-8") -> Response:
    return Response(
        url=url,
        final_url=url,
        status=200,
        body=text.encode("utf-8"),
        headers={"content-type": content_type},
    )


def _pricing_html(models: dict[str, str]) -> str:
    names = "".join(f"<td>{name}</td>" for name in models)
    versions = "".join(f"<td>{version}</td>" for version in models.values())
    return (
        "<!doctype html><html><head><title>模型价格 - DeepSeek</title></head><body>"
        f"<table><tr><td>模型</td>{names}</tr>"
        f"<tr><td>模型版本</td>{versions}</tr></table></body></html>"
    )


def _page_html() -> str:
    return (
        "<!doctype html><html><head><title>DeepSeek Chat</title>"
        '<script src="/static/main.js"></script></head>'
        "<body>DeepSeek Chat 页面正文内容，长度足够用于结构校验。</body></html>"
    )


class SafeFetchTests(unittest.TestCase):
    def test_accepts_official_hosts(self) -> None:
        for url in (
            "https://chat.deepseek.com/",
            "https://platform.deepseek.com/api/v0/client/settings?did=x",
            "https://www.deepseek.com/harness/",
            "https://fe-static.deepseek.com/chat/static/main.js",
        ):
            self.assertTrue(validate_url(url).hostname)

    def test_rejects_unsafe_urls(self) -> None:
        for url in (
            "http://chat.deepseek.com/",
            "https://chat.deepseek.com.evil.example/",
            "https://127.0.0.1/",
            "https://chat.deepseek.com:8443/",
            "https://user:fake@chat.deepseek.com/",
            "https://evil.example/",
        ):
            with self.assertRaises(UnsafeURL):
                validate_url(url)

    def test_rejects_private_dns_resolution(self) -> None:
        safe_fetch._DNS_CACHE.clear()
        infos = [(2, 1, 6, "", ("169.254.169.254", 443))]
        with mock.patch.object(safe_fetch.socket, "getaddrinfo", return_value=infos):
            with self.assertRaises(UnsafeURL):
                safe_fetch._resolve_public("api-docs.deepseek.com")
        safe_fetch._DNS_CACHE.clear()

    def test_round_deadline_stops_before_any_connection(self) -> None:
        with mock.patch.object(safe_fetch, "_ROUND_DEADLINE", time.monotonic() - 1):
            with self.assertRaises(FetchError) as caught:
                fetch("https://api-docs.deepseek.com/zh-cn/")
        self.assertIn("时间预算", str(caught.exception))

    def test_round_deadline_stops_during_body_read(self) -> None:
        class FakeResponse:
            def getheader(self, name: str) -> str | None:
                return None

            def read(self, size: int) -> bytes:
                raise AssertionError("截止时间已到，不应继续读取正文")

        with mock.patch.object(safe_fetch, "_ROUND_DEADLINE", time.monotonic() - 1):
            with self.assertRaises(FetchError) as caught:
                _read_body(FakeResponse(), "https://api-docs.deepseek.com/")
        self.assertIn("时间预算", str(caught.exception))

    def test_port_zero_is_not_treated_as_default(self) -> None:
        # parts.port or 443 会把显式 :0 当成未指定；这里必须拒绝。
        with self.assertRaises(UnsafeURL):
            validate_url("https://chat.deepseek.com:0/")

    def test_fetch_error_does_not_echo_url_userinfo(self) -> None:
        from webwatch.redact import redact

        url = "https://user:fake@chat.deepseek.com/private"
        message = str(UnsafeURL(url, "测试"))
        self.assertNotIn("pa55word", message)
        self.assertIn("***@", message)
        self.assertNotIn("pa55word", redact(url))

    def test_truncated_gzip_is_rejected_instead_of_silently_short(self) -> None:
        raw = gzip.compress(b"a" * 100)
        with self.assertRaises(ParseError):
            safe_fetch._decompress_limited(
                raw[: len(raw) // 2], "gzip", "https://api-docs.deepseek.com/"
            )

    def test_multi_member_gzip_is_fully_decoded(self) -> None:
        raw = gzip.compress(b"a" * 100) + gzip.compress(b"b" * 100)
        self.assertEqual(
            safe_fetch._decompress_limited(raw, "gzip", "https://api-docs.deepseek.com/"),
            b"a" * 100 + b"b" * 100,
        )

    def test_content_length_over_limit_is_rejected(self) -> None:
        class FakeResponse:
            def getheader(self, name: str) -> str | None:
                return str(safe_fetch.MAX_COMPRESSED_BYTES + 1) if name == "Content-Length" else None

        with self.assertRaises(TooLarge):
            _read_body(FakeResponse(), "https://api-docs.deepseek.com/")

    def test_gzip_decompression_bomb_is_rejected(self) -> None:
        raw = gzip.compress(b"\0" * (safe_fetch.MAX_BODY_BYTES + 1))
        with self.assertRaises(TooLarge):
            safe_fetch._decompress_limited(raw, "gzip", "https://api-docs.deepseek.com/")


class ExtractTests(unittest.TestCase):
    def test_extract_page(self) -> None:
        html = """
        <html><head><title>标题</title>
        <meta name="commit-id" content="abc123">
        <script src="/static/main.js"></script>
        <link rel="stylesheet" href="/static/main.css">
        </head><body><p>你好 世界</p><script>ignored()</script></body></html>
        """
        page = extract_page(html, "https://chat.deepseek.com/")
        self.assertEqual(page["title"], "标题")
        self.assertEqual(page["commit_id"] if "commit_id" in page else page["meta"]["commit-id"], "abc123")
        self.assertEqual(page["scripts"], ["https://chat.deepseek.com/static/main.js"])
        self.assertEqual(page["styles"], ["https://chat.deepseek.com/static/main.css"])
        self.assertEqual(normalize_text(" 你 好\n 世界 "), "你 好 世界")
        self.assertIn("你好 世界", page["text"])


class ModelTests(unittest.TestCase):
    def test_pricing_url_keeps_directory_trailing_slash(self) -> None:
        # 官方文档站无斜杠的 /pricing 会回退到「Your First API Call」页面。
        self.assertTrue(config.MODEL_PRICING_URL.endswith("/quick_start/pricing/"))

    def test_parse_pricing_table(self) -> None:
        html = """
        <table><tr><td colspan="3">模型</td><td>deepseek-flash<sup>(1)</sup></td><td>deepseek-v4-pro</td></tr>
        <tr><td colspan="3">模型版本</td><td>DeepSeek-V4.1-Flash-1017</td><td>DeepSeek-V4-Pro-0813</td></tr>
        </table>
        """
        versions = parse_model_versions(html)
        self.assertEqual(
            versions,
            [
                {"model": "deepseek-flash", "version": "DeepSeek-V4.1-Flash-1017"},
                {"model": "deepseek-v4-pro", "version": "DeepSeek-V4-Pro-0813"},
            ],
        )

    def test_empty_table(self) -> None:
        self.assertEqual(parse_model_versions("<table><tr><td>无关</td></tr></table>"), [])


class EventTests(unittest.TestCase):
    def test_build_event_and_slug(self) -> None:
        event = build_event(
            event_id="web:chat:1",
            slug="web-chat-1",
            source="chat",
            kind="page_change",
            level=2,
            title="Chat 页面更新",
            summary="正文变化",
            changed_at="2026-11-15T00:00:00Z",
            page_url="https://chat.deepseek.com/",
        )
        self.assertEqual(event["level_label"], "值得看")
        self.assertEqual(event["slug"], "web-chat-1")
        self.assertEqual(slugify(" A B / C "), "a-b-c")

    def test_diff_target_detects_commit(self) -> None:
        previous = {
            "commit_id": "old",
            "assets": [{"url": "https://fe-static.deepseek.com/a.js", "sha256": "1"}],
            "text_sha256": "t1",
            "text_excerpt": "旧正文",
            "final_url": "https://chat.deepseek.com/",
        }
        current = {
            "commit_id": "new",
            "assets": [{"url": "https://fe-static.deepseek.com/a.js", "sha256": "2"}],
            "text_sha256": "t2",
            "text_excerpt": "新正文",
            "final_url": "https://chat.deepseek.com/",
        }
        event = _diff_target("chat", "Chat 网页", previous, current)
        self.assertIsNotNone(event)
        # 前端版本变化只给默认档「值得看」，是否升「重要」由判级终审结合证据决定。
        self.assertEqual(event["level"], 2)
        self.assertEqual(event["payload"]["level_rule"], 2)
        self.assertIn("old", event["summary"])
        self.assertIn("new", event["summary"])

    def test_diff_target_treats_pure_text_change_as_minor(self) -> None:
        previous = {
            "commit_id": "same",
            "assets": [],
            "text_sha256": "t1",
            "text_excerpt": "旧正文",
            "final_url": "https://chat.deepseek.com/",
        }
        current = {
            "commit_id": "same",
            "assets": [],
            "text_sha256": "t2",
            "text_excerpt": "新正文",
            "final_url": "https://chat.deepseek.com/",
        }
        event = _diff_target("chat", "Chat 网页", previous, current)
        self.assertIsNotNone(event)
        self.assertEqual(event["level"], 1)
        self.assertIn("正文", event["title"])

    def test_platform_summary(self) -> None:
        settings = {
            "banner": {"value": {"items": [{"content": {"zh_CN": "公告一 [详情](https://x)"}}]}},
            "notice_usage": {"value": [{}, {}]},
        }
        summary = _platform_summary(settings)
        self.assertIn("公告一详情", summary)
        self.assertTrue(any("用量页公告：2 条" in line for line in summary))

    def test_platform_summary_tracks_notice_body_not_only_count(self) -> None:
        """同条数、正文 A→B 也必须体现在 summary 里（P2-58）。"""
        first = _platform_summary(
            {"notice_usage": {"value": [{"content": {"zh_CN": "额度说明 A"}}]}}
        )
        second = _platform_summary(
            {"notice_usage": {"value": [{"content": {"zh_CN": "额度说明 B（价格调整）"}}]}}
        )
        self.assertNotEqual(first, second)
        self.assertTrue(any("额度说明 B（价格调整）" in line for line in second))


class EventKeyTests(unittest.TestCase):
    def test_same_minute_gets_different_keys(self) -> None:
        stamp = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        first = _event_key(stamp)
        second = _event_key(stamp)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("20261115T120000Z-"))


class RoundHealthTests(unittest.TestCase):
    def test_only_current_round_successes_count(self) -> None:
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        stamp = iso(now)
        sources = {
            "chat": {"last_checked": stamp, "last_success": stamp},
            "platform": {"last_checked": stamp, "last_success": "2026-11-15T11:00:00Z"},
            "harness": {"last_checked": "2026-11-15T11:40:00Z", "last_success": "2026-11-15T11:40:00Z"},
        }
        self.assertEqual(_round_successes(sources, now), ["chat"])

    def test_historical_success_does_not_hide_current_total_failure(self) -> None:
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        sources = {
            "chat": {"last_checked": iso(now), "last_success": "2026-11-15T11:00:00Z"},
            "fingerprint": {"last_checked": iso(now), "last_success": ""},
        }
        self.assertEqual(_round_successes(sources, now), [])

    def test_later_success_cannot_clear_this_round_failure(self) -> None:
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        state = {"sources": {}}
        _source_fail(state, "fingerprint", now, "价格页失败")
        self.assertTrue(_source_failed_this_round(state, "fingerprint", now))
        if not _source_failed_this_round(state, "fingerprint", now):
            _source_ok(state, "fingerprint", now)
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 1)

    def test_multiple_model_failures_count_as_one_round(self) -> None:
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        state = {"sources": {}}
        _source_fail(state, "fingerprint", now, "模型 A 失败")
        _source_fail(state, "fingerprint", now, "模型 B 失败")
        _source_fail(state, "fingerprint", now, "模型 C 失败")
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 1)
        later = datetime(2026, 11, 15, 12, 30, tzinfo=timezone.utc)
        _source_fail(state, "fingerprint", later, "下一轮仍失败")
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 2)


class ScheduleTests(unittest.TestCase):
    def test_due_intervals(self) -> None:
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(_is_due(None, now, 20 * 60))
        self.assertFalse(_is_due((now - timedelta(minutes=10)).isoformat(), now, 20 * 60))
        self.assertTrue(_is_due((now - timedelta(minutes=19)).isoformat(), now, 20 * 60))
        self.assertFalse(_is_due((now - timedelta(minutes=20)).isoformat(), now, 30 * 60))
        self.assertTrue(_is_due((now - timedelta(minutes=29)).isoformat(), now, 30 * 60))


class SourceHealthOrderTests(unittest.TestCase):
    def test_success_then_failure_is_not_a_round_success(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        sources = {
            "fingerprint": {
                "last_checked": iso(now),
                "last_success": iso(now),
                "fail_streak": 1,
            }
        }
        self.assertEqual(_round_successes(sources, now), [])

    def test_price_page_success_then_api_total_failure(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state: dict = {}
        pricing = _pricing_html({"deepseek-flash": "V1", "deepseek-v4-pro": "R1"})
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.MODEL_PRICING_URL, pricing),
        ):
            self.assertEqual(check_model_versions(state, now), [])
        self.assertEqual(_round_successes(state["sources"], now), ["fingerprint"])

        with mock.patch(
            "webwatch.fingerprint.probe_catalog",
            side_effect=ApiError(401, "HTTP 401: unauthorized"),
        ):
            check_api(state, now, "fake-key")

        self.assertEqual(_round_successes(state["sources"], now), [])
        entry = state["sources"]["fingerprint"]
        self.assertEqual(entry["fail_streak"], 1)
        self.assertIn("401", entry["last_error"])

    def test_api_failure_streak_accumulates_while_price_page_succeeds(self) -> None:
        state: dict = {}
        pricing = _pricing_html({"deepseek-flash": "V1", "deepseek-v4-pro": "R1"})
        for index in range(3):
            now = datetime(2026, 11, 16, 10 + index, 0, tzinfo=timezone.utc)
            with mock.patch(
                "webwatch.fingerprint.fetch",
                return_value=_html_response(config.MODEL_PRICING_URL, pricing),
            ):
                check_model_versions(state, now, defer_source_ok=True)
            with mock.patch(
                "webwatch.fingerprint.probe_catalog",
                side_effect=ApiError(401, "HTTP 401: unauthorized"),
            ):
                check_api(state, now, "fake-key")
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 3)

    def test_paid_probe_budget_stops_before_network(self) -> None:
        old = fingerprint_module._paid_requests_this_round
        fingerprint_module._paid_requests_this_round = fingerprint_module.MAX_PAID_REQUESTS_PER_ROUND
        try:
            with self.assertRaises(RuntimeError) as caught:
                fingerprint_module._api_request("POST", fingerprint_module.CHAT_PATH, "sk-test", {})
        finally:
            fingerprint_module._paid_requests_this_round = old
        self.assertIn("超过上限", str(caught.exception))

    def test_versions_half_size_is_not_a_broken_page(self) -> None:
        # 2→1 是合法下架；只有少于一半才按残页处理。
        self.assertFalse(_versions_look_incomplete({"a": "1", "b": "1"}, {"a": "1"}))
        # 少掉的模型名仍出现在页面正文里 → 是解析残页，不是下架。
        self.assertTrue(
            _versions_look_incomplete(
                {"a": "1", "b": "1"}, {"a": "1"}, page_text="模型 b 当前可用"
            )
        )
        self.assertTrue(
            _versions_look_incomplete(
                {f"m{index}": "1" for index in range(8)},
                {f"m{index}": "1" for index in range(3)},
            )
        )

    def test_partial_model_failure_is_warning_not_source_error(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state = {"models": {"versions": {"model-a": "v1", "model-b": "v1"}}}
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a", "model-b"]
        ), mock.patch(
            "webwatch.fingerprint.probe_model",
            side_effect=[{"fingerprint": "fp-a"}, ApiError(500, "boom")],
        ):
            check_api(state, now, "fake-key")
        entry = state["sources"]["fingerprint"]
        self.assertEqual(entry["fail_streak"], 0)
        self.assertEqual(entry["last_success"], iso(now))
        self.assertIn("1/2", entry["last_error"])

    def test_catalog_state_records_first_seen_and_last_change(self) -> None:
        now1 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state: dict = {}
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a"]
        ), mock.patch(
            "webwatch.fingerprint.probe_model",
            return_value={"fingerprint": "fp-model-a", "model": "model-a"},
        ):
            self.assertEqual(check_api(state, now1, "fake-key"), [])
        self.assertEqual(state["models"]["catalog"], ["model-a"])
        self.assertEqual(state["models"]["catalog_first_seen"]["model-a"], iso(now1))
        self.assertNotIn("last_catalog_change", state["models"])

        now2 = now1 + timedelta(minutes=30)
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a", "model-b"]
        ), mock.patch(
            "webwatch.fingerprint.probe_model",
            side_effect=lambda _key, model: {"fingerprint": f"fp-{model}", "model": model},
        ):
            events = check_api(state, now2, "fake-key")
        self.assertEqual(state["models"]["catalog_first_seen"]["model-b"], iso(now2))
        self.assertEqual(state["models"]["last_catalog_change"], {
            "at": iso(now2),
            "added": ["model-b"],
            "removed": [],
        })
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "catalog")
        self.assertIn("model-b", events[0]["summary"])

    def test_old_catalog_list_does_not_report_everything_as_new(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state = {"models": {"catalog": ["model-a"], "versions": {}}}
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a"]
        ), mock.patch(
            "webwatch.fingerprint.probe_model",
            return_value={"fingerprint": "fp-a", "model": "model-a"},
        ):
            events = check_api(state, now, "fake-key")
        self.assertEqual(events, [])
        self.assertEqual(state["models"]["catalog_first_seen"]["model-a"], iso(now))

    def test_check_catalog_refreshes_snapshot_without_source_health(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state: dict = {}
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a"]
        ):
            events = check_catalog(state, now, "fake-key")
        self.assertEqual(events, [])
        self.assertEqual(state["models"]["catalog"], ["model-a"])
        self.assertEqual(state["models"]["catalog_checked_at"], iso(now))
        self.assertEqual(state["models"]["catalog_first_seen"]["model-a"], iso(now))
        self.assertNotIn("sources", state)

    def test_check_catalog_failure_prints_and_marks_source(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state: dict = {}
        stderr = io.StringIO()
        with mock.patch(
            "webwatch.fingerprint.probe_catalog",
            side_effect=ApiError(401, "HTTP 401: unauthorized"),
        ), redirect_stderr(stderr):
            events = check_catalog(state, now, "fake-key")
        self.assertEqual(events, [])
        # models 上仍保留连续失败次数和脱敏错误。
        self.assertEqual(state["models"]["catalog_fail_streak"], 1)
        self.assertIn("401", state["models"]["catalog_last_error"])
        # P2-19：catalog-only 轮失败也要进 fingerprint 来源健康度，/status 才能看到。
        entry = state["sources"]["fingerprint"]
        self.assertEqual(entry["fail_streak"], 1)
        self.assertIn("模型目录", entry["last_error"])
        self.assertIn("模型目录检查失败", stderr.getvalue())
        self.assertIn("401", stderr.getvalue())

        # 目录恢复后清掉它自己造成的来源失败，不残留“连续失败”。
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a"]
        ):
            check_catalog(state, now + timedelta(minutes=5), "fake-key")
        self.assertEqual(entry["fail_streak"], 0)
        self.assertEqual(entry["last_error"], "")

    def test_check_catalog_emits_catalog_change_event(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state = {
            "models": {
                "catalog": ["model-a"],
                "catalog_first_seen": {"model-a": "2026-11-16T09:00:00Z"},
            }
        }
        with mock.patch(
            "webwatch.fingerprint.probe_catalog", return_value=["model-a", "model-b"]
        ):
            events = check_catalog(state, now, "fake-key")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "catalog")
        self.assertIn("model-b", events[0]["summary"])
        self.assertEqual(state["models"]["last_catalog_change"]["added"], ["model-b"])

    def test_platform_banner_success_marks_source_ok(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state: dict = {}
        body = {"data": {"biz_data": {"settings": {"banner": {"value": {"items": []}}}}}}
        with mock.patch("webwatch.fingerprint.fetch_json", return_value=body):
            self.assertEqual(check_platform_banner(state, now), [])
        entry = state["sources"]["platform"]
        self.assertEqual(entry["last_success"], iso(now))
        self.assertEqual(entry["fail_streak"], 0)

    def test_platform_banner_success_cannot_wash_page_failure(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        state: dict = {}
        _source_fail(state, "platform", now, "页面抓取失败")
        body = {"data": {"biz_data": {"settings": {"banner": {"value": {"items": []}}}}}}
        with mock.patch("webwatch.fingerprint.fetch_json", return_value=body):
            check_platform_banner(state, now)
        entry = state["sources"]["platform"]
        self.assertEqual(entry["fail_streak"], 1)
        self.assertNotEqual(entry.get("last_success"), iso(now))


class ProbeModelTests(unittest.TestCase):
    def test_prefers_fim_minimal_request(self) -> None:
        with mock.patch(
            "webwatch.fingerprint._api_request",
            return_value={"system_fingerprint": "fp-fim", "model": "deepseek-flash"},
        ) as api_request:
            result = probe_model("fake-key", "deepseek-flash")
        self.assertEqual(result, {"fingerprint": "fp-fim", "model": "deepseek-flash"})
        args = api_request.call_args.args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/beta/completions")
        self.assertEqual(args[3]["prompt"], "hi")
        self.assertEqual(args[3]["max_tokens"], 1)
        self.assertNotIn("messages", args[3])

    def test_falls_back_to_chat_when_fim_unavailable(self) -> None:
        with mock.patch(
            "webwatch.fingerprint._api_request",
            side_effect=[
                ApiError(404, "HTTP 404: not found"),
                {"system_fingerprint": "fp-chat", "model": "deepseek-v4-pro"},
            ],
        ) as api_request:
            result = probe_model("fake-key", "deepseek-v4-pro")
        self.assertEqual(result, {"fingerprint": "fp-chat", "model": "deepseek-v4-pro"})
        self.assertEqual(api_request.call_count, 2)
        self.assertEqual(api_request.call_args_list[1].args[1], "/chat/completions")
        self.assertIn("messages", api_request.call_args_list[1].args[3])

    def test_chat_fallback_can_drop_thinking_for_compatibility(self) -> None:
        with mock.patch(
            "webwatch.fingerprint._api_request",
            side_effect=[
                ApiError(404, "HTTP 404: not found"),
                ApiError(400, "HTTP 400: unknown field thinking"),
                {"system_fingerprint": "fp-chat", "model": "deepseek-flash"},
            ],
        ) as api_request:
            result = probe_model("fake-key", "deepseek-flash")
        self.assertEqual(result["fingerprint"], "fp-chat")
        self.assertEqual(api_request.call_count, 3)
        self.assertNotIn("thinking", api_request.call_args_list[2].args[3])


class PlatformBannerTests(unittest.TestCase):
    def test_empty_to_non_empty_announcement_emits_event(self) -> None:
        state: dict = {}
        empty = {"data": {"biz_data": {"settings": {"banner": {"value": {"items": []}}}}}}
        filled = {
            "data": {
                "biz_data": {
                    "settings": {
                        "banner": {
                            "value": {
                                "items": [
                                    {"content": {"zh_CN": "重要公告 [详情](https://example.com)"}}
                                ]
                            }
                        }
                    }
                }
            }
        }
        now1 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        now2 = datetime(2026, 11, 16, 10, 20, tzinfo=timezone.utc)
        with mock.patch("webwatch.fingerprint.fetch_json", return_value=empty):
            self.assertEqual(check_platform_banner(state, now1), [])
        self.assertEqual(state["platform_banner"]["summary"], [])
        self.assertTrue(state["platform_banner"]["initialized"])
        with mock.patch("webwatch.fingerprint.fetch_json", return_value=filled):
            events = check_platform_banner(state, now2)
        self.assertEqual(len(events), 1)
        self.assertIn("新增公告", events[0]["summary"])
        self.assertIn("重要公告详情", events[0]["summary"])

    def test_old_state_with_empty_summary_is_treated_as_initialized(self) -> None:
        state = {"platform_banner": {"did": "old-did", "summary": []}}
        filled = {
            "data": {
                "biz_data": {
                    "settings": {
                        "banner": {"value": {"items": [{"content": {"zh_CN": "新公告"}}]}}
                    }
                }
            }
        }
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        with mock.patch("webwatch.fingerprint.fetch_json", return_value=filled):
            events = check_platform_banner(state, now)
        self.assertEqual(len(events), 1)
        self.assertIn("新公告", events[0]["summary"])


class AssetDiffTests(unittest.TestCase):
    def test_asset_scope_upgrade_does_not_report_baseline_assets(self) -> None:
        frontend = {
            "engine": "rspack",
            "manifest": [
                {
                    "key": "css:chat/static/main.css",
                    "url": "https://fe-static.deepseek.com/chat/static/main.css",
                    "hash": "csshash",
                    "kind": "css",
                    "role": "entry",
                    "name": "chat/static/main.css",
                }
            ],
            "main_js_url": "https://fe-static.deepseek.com/chat/static/main.js",
            "main_css_url": "https://fe-static.deepseek.com/chat/static/main.css",
            "build_meta": {},
        }
        previous = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [
                {"url": "https://fe-static.deepseek.com/chat/static/main.js", "sha256": "1"}
            ],
            "frontend": frontend,
        }
        current = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [
                {"url": "https://fe-static.deepseek.com/chat/static/main.css", "sha256": "2"},
                {"url": "https://fe-static.deepseek.com/chat/static/main.js", "sha256": "1"},
            ],
            "asset_scope": ASSET_SCOPE_VERSION,
            "frontend": frontend,
        }
        self.assertIsNone(_diff_target("chat", "Chat 网页", previous, current))

    def test_asset_only_change_without_frontend_evidence_is_ignored(self) -> None:
        previous = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [
                {"url": "https://fe-static.deepseek.com/chat/static/main.js", "sha256": "1"}
            ],
            "asset_scope": ASSET_SCOPE_VERSION,
        }
        current = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [
                {"url": "https://fe-static.deepseek.com/chat/static/main.css", "sha256": "2"},
                {"url": "https://fe-static.deepseek.com/chat/static/main.js", "sha256": "1"},
            ],
            "asset_scope": ASSET_SCOPE_VERSION,
        }
        self.assertIsNone(_diff_target("chat", "Chat 网页", previous, current))

    def test_real_asset_change_with_frontend_evidence_still_reports(self) -> None:
        old_front = {
            "engine": "rspack",
            "manifest": [{"key": "css:chat/static/main.css", "hash": "old", "kind": "css", "role": "entry", "name": "main.css", "url": "https://fe-static.deepseek.com/chat/static/main.old.css"}],
            "main_js_url": "https://fe-static.deepseek.com/chat/static/main.js",
            "main_css_url": "https://fe-static.deepseek.com/chat/static/main.old.css",
            "build_meta": {},
        }
        new_front = {
            **old_front,
            "manifest": [{"key": "css:chat/static/main.css", "hash": "new", "kind": "css", "role": "entry", "name": "main.css", "url": "https://fe-static.deepseek.com/chat/static/main.new.css"}],
            "main_css_url": "https://fe-static.deepseek.com/chat/static/main.new.css",
        }
        previous = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [{"url": "https://fe-static.deepseek.com/chat/static/main.old.css", "sha256": "1"}],
            "asset_scope": ASSET_SCOPE_VERSION,
            "frontend": old_front,
        }
        current = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [{"url": "https://fe-static.deepseek.com/chat/static/main.new.css", "sha256": "2"}],
            "asset_scope": ASSET_SCOPE_VERSION,
            "frontend": new_front,
            "frontend_evidence": {"totals": {"manifest": 1, "added": 1, "removed": 1, "changed": 0, "unchanged": 0}},
        }
        event = _diff_target("chat", "Chat 网页", previous, current)
        self.assertIsNotNone(event)
        self.assertIn("前端资源更新", event["title"])

    def test_failed_asset_does_not_participate_in_diff(self) -> None:
        previous = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [{"url": "https://fe-static.deepseek.com/a.js", "sha256": "hash1"}],
        }
        current = {
            "commit_id": "same",
            "text_sha256": "t1",
            "text_excerpt": "正文",
            "assets": [
                {
                    "url": "https://fe-static.deepseek.com/a.js",
                    "sha256": "",
                    "error": "http",
                    "status": 404,
                }
            ],
        }
        self.assertIsNone(_diff_target("chat", "Chat 网页", previous, current))

    def test_failed_asset_keeps_previous_hash_in_snapshot(self) -> None:
        target = config.TARGETS["chat"]
        requested_asset = "https://chat.deepseek.com/static/main.js"
        previous_assets = [{"url": requested_asset, "sha256": "oldhash", "size": 10}]

        def fake_fetch(url: str, **_kwargs) -> Response:
            if url == target["url"]:
                return _html_response(url, _page_html())
            raise HTTPStatusError(url, "HTTP 404", 404)

        with mock.patch("webwatch.fingerprint.fetch", side_effect=fake_fetch):
            snapshot = build_target_snapshot(target, {"assets": previous_assets})

        self.assertEqual(len(snapshot["assets"]), 1)
        self.assertEqual(snapshot["assets"][0]["url"], requested_asset)
        self.assertEqual(snapshot["assets"][0]["sha256"], "oldhash")
        self.assertEqual(snapshot["asset_errors"][0]["error"], "http")
        previous_snapshot = {
            "commit_id": snapshot["commit_id"],
            "text_sha256": snapshot["text_sha256"],
            "text_excerpt": snapshot["text_excerpt"],
            "final_url": snapshot["final_url"],
            "assets": previous_assets,
        }
        self.assertIsNone(_diff_target("chat", "Chat 网页", previous_snapshot, snapshot))

    def test_successful_asset_uses_requested_url_as_key(self) -> None:
        target = config.TARGETS["chat"]

        def fake_fetch(url: str, **_kwargs) -> Response:
            if url == target["url"]:
                return _html_response(url, _page_html())
            return Response(
                url=url,
                final_url="https://fe-static.deepseek.com/cdn/main.js",
                status=200,
                body=b"console.log(1);",
                headers={"content-type": "application/javascript"},
            )

        with mock.patch("webwatch.fingerprint.fetch", side_effect=fake_fetch):
            snapshot = build_target_snapshot(target)
        self.assertEqual(
            snapshot["assets"][0]["url"], "https://chat.deepseek.com/static/main.js"
        )
        self.assertEqual(snapshot["asset_scope"], ASSET_SCOPE_VERSION)
        self.assertNotIn("asset_errors", snapshot)


class ContentValidationTests(unittest.TestCase):
    def test_challenge_page_is_rejected(self) -> None:
        challenge = (
            "<!doctype html><html><head><title>Just a moment...</title></head>"
            "<body>checking your browser before accessing deepseek</body></html>"
        )
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.TARGETS["chat"]["url"], challenge),
        ):
            with self.assertRaises(ContentError):
                build_target_snapshot(config.TARGETS["chat"])

    def test_non_html_pricing_response_is_failure(self) -> None:
        state: dict = {}
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(
                config.MODEL_PRICING_URL, '{"ok": true}', content_type="application/json"
            ),
        ):
            self.assertEqual(check_model_versions(state, now), [])
        self.assertNotIn("versions", state.get("models", {}))
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 1)

    def test_challenge_page_does_not_overwrite_target_baseline(self) -> None:
        challenge = "<!doctype html><html><head><title>Just a moment...</title></head></html>"
        state = {
            "targets": {
                "chat": {
                    "commit_id": "real-commit",
                    "text_sha256": "real-text",
                    "text_excerpt": "真实正文",
                    "assets": [],
                }
            }
        }
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.TARGETS["chat"]["url"], challenge),
        ):
            events = check_targets(state, now)
        self.assertEqual(events, [])
        self.assertEqual(state["targets"]["chat"]["commit_id"], "real-commit")
        self.assertEqual(state["sources"]["chat"]["fail_streak"], 1)


class ModelVersionTests(unittest.TestCase):
    def test_model_added_and_removed_emit_events(self) -> None:
        state: dict = {}
        base = {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"}
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.MODEL_PRICING_URL, _pricing_html(base)),
        ):
            self.assertEqual(
                check_model_versions(state, datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)),
                [],
            )

        self.assertIn("deepseek-flash", state["models"]["versions_first_seen"])
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(
                config.MODEL_PRICING_URL, _pricing_html({**base, "deepseek-v4-flash": "F1"})
            ),
        ):
            events = check_model_versions(
                state, datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)
            )
        self.assertEqual(len(events), 1)
        self.assertIn("新增模型", events[0]["summary"])
        self.assertIn("deepseek-v4-flash", events[0]["summary"])
        self.assertEqual(
            state["models"]["last_versions_change"]["added"], ["deepseek-v4-flash"]
        )
        self.assertEqual(
            state["models"]["versions_first_seen"]["deepseek-v4-flash"],
            "2026-11-16T10:30:00Z",
        )

        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(
                config.MODEL_PRICING_URL,
                _pricing_html({"deepseek-flash": "V1", "deepseek-v4-flash": "F1"}),
            ),
        ):
            events = check_model_versions(
                state, datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(len(events), 1)
        self.assertIn("下架模型", events[0]["summary"])
        self.assertIn("deepseek-v4-pro", events[0]["summary"])
        self.assertEqual(
            state["models"]["versions"],
            {"deepseek-flash": "V1", "deepseek-v4-flash": "F1"},
        )

    def test_partial_table_does_not_shrink_baseline(self) -> None:
        state: dict = {}
        full = {
            "deepseek-flash": "V1",
            "deepseek-v4-pro": "R1",
            "deepseek-v4-flash": "F1",
        }
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.MODEL_PRICING_URL, _pricing_html(full)),
        ):
            check_model_versions(state, datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc))

        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(
                config.MODEL_PRICING_URL, _pricing_html({"deepseek-flash": "V2"})
            ),
        ):
            events = check_model_versions(
                state, datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)
            )
        self.assertEqual(events, [])
        self.assertEqual(state["models"]["versions"], full)
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 1)

    def test_version_change_still_emits_event(self) -> None:
        state: dict = {}
        base = {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"}
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.MODEL_PRICING_URL, _pricing_html(base)),
        ):
            check_model_versions(state, datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc))
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(
                config.MODEL_PRICING_URL, _pricing_html({"deepseek-flash": "V2", "deepseek-v4-pro": "R1"})
            ),
        ):
            events = check_model_versions(
                state, datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)
            )
        self.assertEqual(len(events), 1)
        self.assertIn("deepseek-flash: V1 → V2", events[0]["summary"])


class RunPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        """本类只测落盘顺序，DSH 外部源统一 mock，避免测试真实联网。"""
        patcher = mock.patch.object(
            run_module, "check_dsh_updates", return_value=([], True, "")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _event() -> dict:
        return build_event(
            event_id="web:chat:test",
            slug="web-chat-test",
            source="chat",
            kind="page_change",
            level=2,
            title="Chat 页面更新",
            summary="正文变化",
            changed_at="2026-11-16T10:00:00Z",
            page_url="https://chat.deepseek.com/",
        )

    def test_events_are_written_before_state_is_saved(self) -> None:
        calls: list[str] = []
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"

            def fake_write(*_args, **_kwargs) -> None:
                calls.append("event")

            def fake_save(*_args, **_kwargs) -> None:
                calls.append("state")

            with mock.patch.object(run_module, "check_targets", return_value=[]), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[self._event()]
            ), mock.patch.object(
                run_module, "write_event", side_effect=fake_write
            ), mock.patch.object(
                run_module, "save_state", side_effect=fake_save
            ), mock.patch.object(
                run_module, "_round_successes", return_value=["chat"]
            ), mock.patch.object(
                run_module, "send_events", return_value=(0, 0)
            ):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["event", "state"])

    def test_write_event_failure_does_not_save_state(self) -> None:
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.object(
                run_module, "check_targets", return_value=[]
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[self._event()]
            ), mock.patch.object(
                run_module, "write_event", side_effect=OSError("disk full")
            ), mock.patch.object(
                run_module, "save_state"
            ) as save_mock, mock.patch.object(
                run_module, "_round_successes", return_value=["chat"]
            ), mock.patch.object(
                run_module, "send_events", return_value=(0, 0)
            ):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
        self.assertEqual(rc, 1)
        save_mock.assert_not_called()
        self.assertFalse((state_dir / "state.json").exists())

    def test_save_state_failure_removes_written_events(self) -> None:
        """state 保存失败时撤回本轮事件，避免下一轮生成重复 id。"""
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"

            def fake_write(events_dir_path, event):
                events_dir_path.mkdir(parents=True, exist_ok=True)
                path = events_dir_path / "one.json"
                path.write_text("{}", encoding="utf-8")
                return path

            with mock.patch.object(
                run_module, "check_targets", return_value=[]
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[self._event()]
            ), mock.patch.object(
                run_module, "write_event", side_effect=fake_write
            ), mock.patch.object(
                run_module, "save_state", side_effect=OSError("disk full")
            ), mock.patch.object(
                run_module, "_round_successes", return_value=["chat"]
            ), mock.patch.object(
                run_module, "send_events", return_value=(0, 0)
            ):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
        self.assertEqual(rc, 1)
        self.assertFalse((events_dir / "one.json").exists())

    def test_creates_events_dir_even_when_no_events(self) -> None:
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.object(run_module, "check_targets", return_value=[]), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[]
            ), mock.patch.object(
                run_module, "_round_successes", return_value=["chat"]
            ), mock.patch.object(
                run_module, "send_events", return_value=(0, 0)
            ):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
            self.assertEqual(rc, 0)
            self.assertTrue(events_dir.is_dir())

    def test_dsh_failure_is_not_washed_by_harness_page_success(self) -> None:
        def fake_targets(state, now, **_kwargs):
            _source_ok(state, "harness", now)
            return []

        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.object(
                run_module, "check_targets", side_effect=fake_targets
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[]
            ), mock.patch.object(
                run_module, "check_dsh_updates", return_value=([], False, "dsh down")
            ), mock.patch.object(run_module, "send_events", return_value=(0, 0)):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(state["sources"]["harness"]["fail_streak"], 1)
        self.assertIn("dsh down", state["sources"]["harness"]["last_error"])

    def test_harness_page_and_dsh_failures_keep_both_errors(self) -> None:
        def fake_targets(state, now, **_kwargs):
            _source_fail(state, "harness", now, "page down")
            return []

        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.object(
                run_module, "check_targets", side_effect=fake_targets
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[]
            ), mock.patch.object(
                run_module, "check_dsh_updates", return_value=([], False, "dsh down")
            ), mock.patch.object(run_module, "send_events", return_value=(0, 0)):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(state["sources"]["harness"]["fail_streak"], 1)
        self.assertIn("page down", state["sources"]["harness"]["last_error"])
        self.assertIn("dsh down", state["sources"]["harness"]["last_error"])

    def test_harness_page_failure_is_not_washed_by_dsh_success(self) -> None:
        def fake_targets(state, now, **_kwargs):
            _source_fail(state, "harness", now, "page down")
            return []

        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.object(
                run_module, "check_targets", side_effect=fake_targets
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[]
            ), mock.patch.object(
                run_module, "check_dsh_updates", return_value=([], True, "")
            ), mock.patch.object(run_module, "send_events", return_value=(0, 0)):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=False,
                    force=True,
                )
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(state["sources"]["harness"]["fail_streak"], 1)
        self.assertIn("page down", state["sources"]["harness"]["last_error"])

    def test_dry_run_does_not_call_paid_api(self) -> None:
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fake-key"}), mock.patch.object(
                run_module, "check_targets", return_value=[]
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module, "check_model_versions", return_value=[]
            ), mock.patch.object(
                run_module, "check_api"
            ) as api_mock, mock.patch.object(
                run_module, "check_catalog"
            ) as catalog_mock, mock.patch.object(
                run_module, "_round_successes", return_value=["chat"]
            ), mock.patch.object(
                run_module, "send_events", return_value=(0, 0)
            ):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=True,
                    use_api=True,
                    force=True,
                )
        self.assertEqual(rc, 0)
        api_mock.assert_not_called()
        catalog_mock.assert_not_called()

    def test_catalog_check_runs_without_waiting_for_fingerprint(self) -> None:
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            state_dir.mkdir()
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "schedule": {
                            "pages": "2099-01-01T00:00:00+08:00",
                            "fingerprint": "2099-01-01T00:00:00+08:00",
                            "balance": "2099-01-01T00:00:00+08:00",
                            "catalog": "2026-11-16T09:50:00+00:00",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ", {"DEEPSEEK_API_KEY": "fake-key"}
            ), mock.patch.object(
                run_module, "check_catalog", return_value=[]
            ) as catalog_mock, mock.patch.object(
                run_module, "check_api"
            ) as api_mock, mock.patch.object(
                run_module, "check_targets"
            ) as pages_mock, mock.patch.object(
                run_module, "check_model_versions"
            ) as versions_mock:
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=True,
                    force=False,
                )
            saved = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        catalog_mock.assert_called_once()
        api_mock.assert_not_called()
        pages_mock.assert_not_called()
        versions_mock.assert_not_called()
        self.assertIn("catalog", saved["schedule"])

    def test_catalog_only_failure_is_visible_in_state(self) -> None:
        """P2-19：5 分钟的 catalog-only 轮失败也要留下来源健康度和快照错误。"""
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            state_dir.mkdir()
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "schedule": {
                            "pages": "2099-01-01T00:00:00+08:00",
                            "fingerprint": "2099-01-01T00:00:00+08:00",
                            "balance": "2099-01-01T00:00:00+08:00",
                            "catalog": "2026-11-16T09:50:00+00:00",
                        }
                    }
                ),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with mock.patch.dict(
                "os.environ", {"DEEPSEEK_API_KEY": "fake-key"}
            ), mock.patch(
                "webwatch.fingerprint.probe_catalog",
                side_effect=ApiError(401, "HTTP 401: unauthorized"),
            ), mock.patch.object(
                run_module, "check_targets"
            ) as pages_mock, mock.patch.object(
                run_module, "check_model_versions"
            ) as versions_mock, redirect_stderr(stderr):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=True,
                    force=False,
                )
            saved = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        pages_mock.assert_not_called()
        versions_mock.assert_not_called()
        self.assertEqual(saved["models"]["catalog_fail_streak"], 1)
        self.assertIn("401", saved["models"]["catalog_last_error"])
        self.assertEqual(saved["sources"]["fingerprint"]["fail_streak"], 1)
        self.assertIn("模型目录", saved["sources"]["fingerprint"]["last_error"])
        self.assertIn("模型目录检查失败", stderr.getvalue())

    def test_check_api_dry_run_skips_probe(self) -> None:
        now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
        with mock.patch("webwatch.fingerprint.probe_catalog") as probe_mock:
            self.assertEqual(check_api({}, now, "fake-key", dry_run=True), [])
        probe_mock.assert_not_called()

    def test_write_event_is_atomic(self) -> None:
        with _temp_dir() as temp:
            events_dir = Path(temp) / "events"
            path = write_event(events_dir, self._event())
            self.assertTrue(path.is_file())
            self.assertFalse((events_dir / "web-chat-test.json.tmp").exists())

    def test_run_fails_when_only_price_page_succeeded(self) -> None:
        pricing = _pricing_html({"deepseek-flash": "V1", "deepseek-v4-pro": "R1"})
        with _temp_dir() as temp:
            state_dir = Path(temp) / "state"
            events_dir = Path(temp) / "events"
            with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fake-key"}), mock.patch.object(
                run_module, "check_targets", return_value=[]
            ), mock.patch.object(
                run_module, "check_dsh_updates", return_value=([], False, "mocked")
            ), mock.patch.object(
                run_module, "check_platform_banner", return_value=[]
            ), mock.patch.object(
                run_module,
                "check_model_versions",
                side_effect=lambda state, now, **kwargs: check_model_versions(
                    state, now, **kwargs
                ),
            ), mock.patch(
                "webwatch.fingerprint.fetch",
                return_value=_html_response(config.MODEL_PRICING_URL, pricing),
            ), mock.patch(
                "webwatch.fingerprint.probe_catalog",
                side_effect=ApiError(401, "HTTP 401: unauthorized"),
            ), mock.patch.object(
                run_module, "probe_balance", return_value=("CNY", 5.0)
            ), mock.patch.object(
                run_module, "send_text", return_value=False
            ), mock.patch.object(
                run_module, "send_events", return_value=(0, 0)
            ):
                rc = run_module.run(
                    state_dir=state_dir,
                    events_dir=events_dir,
                    dry_run=False,
                    use_api=True,
                    force=True,
                )
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 1)
        self.assertEqual(state["fail_streak"], 1)
        self.assertEqual(state["sources"]["fingerprint"]["fail_streak"], 1)


class RunLockTests(unittest.TestCase):
    def test_second_run_lock_is_refused_then_released(self) -> None:
        with _temp_dir() as state_dir:
            first = run_module._acquire_run_lock(state_dir)
            self.assertIsNotNone(first)
            try:
                # 同一进程再拿一次也要被拒：模拟手动 --force 撞上 timer 巡检。
                self.assertIsNone(run_module._acquire_run_lock(state_dir))
            finally:
                run_module._release_run_lock(first)
            again = run_module._acquire_run_lock(state_dir)
            self.assertIsNotNone(again)
            run_module._release_run_lock(again)


def _front_manifest_n(count: int = 3, *, health: str = "ok", main_hash: str = "m1") -> dict:
    manifest = [
        {
            "key": f"chunk:js:{index}",
            "url": f"https://fe-static.deepseek.com/platform/static/{index}.{index:04x}.js",
            "hash": f"h{index}",
            "kind": "js",
            "role": "chunk",
            "name": str(index),
        }
        for index in range(count)
    ]
    return {
        "engine": "rspack",
        "manifest": manifest,
        "main_js_url": "https://fe-static.deepseek.com/platform/static/main.abc12345.js",
        "main_css_url": "",
        "css_constant": "",
        "css_prefix": "static/",
        "build_meta": {"appVersion": main_hash},
        "health": health,
        "health_reason": "" if health == "ok" else "主脚本抓取失败或运行时映射为空",
    }


def _snapshot_with_front(front: dict | None, **overrides) -> dict:
    snapshot = {
        "commit_id": "same",
        "text_sha256": "t1",
        "text_excerpt": "正文",
        "assets": [],
        "asset_urls": [],
        "asset_scope": ASSET_SCOPE_VERSION,
        "final_url": "https://chat.deepseek.com/",
    }
    if front is not None:
        snapshot["frontend"] = front
    snapshot.update(overrides)
    return snapshot


class FrontendBaselineHealthTests(unittest.TestCase):
    def test_failed_and_recovery_rounds_do_not_emit_fake_frontend_events(self) -> None:
        healthy = _front_manifest_n(3)
        failed = {
            "engine": "html",
            "manifest": [{"key": "js:chat/static/main.js", "url": "https://chat.deepseek.com/static/main.js", "hash": "x", "kind": "js", "role": "entry", "name": "main.js"}],
            "main_js_url": "",
            "build_meta": {},
            "health": "degraded",
            "health_reason": "主脚本抓取失败或运行时映射为空",
        }
        previous = _snapshot_with_front(healthy)
        failure_round = _snapshot_with_front(failed)
        self.assertIsNone(_diff_target("chat", "Chat 网页", previous, failure_round))
        # 恢复轮：与失败快照相比也不产生事件。
        self.assertIsNone(_diff_target("chat", "Chat 网页", failure_round, _snapshot_with_front(healthy)))
        # 与上一版健康基线相同，同样不重复报。
        self.assertIsNone(_diff_target("chat", "Chat 网页", previous, _snapshot_with_front(healthy)))
        # 真正的下一次发版只产一条事件。
        released = _snapshot_with_front(_front_manifest_n(3, main_hash="m2"))
        event = _diff_target("chat", "Chat 网页", previous, released)
        self.assertIsNotNone(event)
        self.assertIn("前端资源更新", event["title"])

    def test_unhealthy_round_does_not_replace_previous_frontend_baseline(self) -> None:
        healthy = _front_manifest_n(3)
        failed = dict(healthy, health="degraded", health_reason="主脚本抓取失败或运行时映射为空")
        previous = _snapshot_with_front(healthy)

        def fake_build(target, previous=None, **_kwargs):
            if target["source"] == "chat":
                return _snapshot_with_front(failed)
            return _snapshot_with_front(healthy, commit_id="other")

        now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
        state = {"targets": {"chat": previous}}
        with mock.patch("webwatch.fingerprint.build_target_snapshot", side_effect=fake_build):
            events = check_targets(state, now)
        self.assertEqual(events, [])
        self.assertEqual(state["targets"]["chat"]["frontend"], healthy)
        self.assertGreaterEqual(state["sources"]["chat"]["fail_streak"], 1)


class AssetCountAndWindowTests(unittest.TestCase):
    def test_asset_summary_uses_raw_counts_not_truncated_list(self) -> None:
        previous = _snapshot_with_front(
            _front_manifest_n(1),
            assets=[{"url": "https://fe-static.deepseek.com/platform/static/0.js", "sha256": "h0"}],
            asset_urls=["https://fe-static.deepseek.com/platform/static/0.js"],
        )
        urls = [f"https://fe-static.deepseek.com/platform/static/{index}.js" for index in range(13)]
        current = _snapshot_with_front(
            _front_manifest_n(1),
            assets=[{"url": url, "sha256": f"h{index}"} for index, url in enumerate(urls)],
            asset_urls=urls,
            frontend_evidence={"totals": {"manifest": 1, "added": 1, "removed": 0, "changed": 0}},
        )
        event = _diff_target("platform", "Platform 控制台", previous, current)
        self.assertIsNotNone(event)
        self.assertIn("新增 12 个", event["summary"])
        change = event["payload"]["changes"][0]
        self.assertEqual(change["added_count"], 12)
        self.assertEqual(len(change["added"]), 8)

    def test_max_assets_window_slide_does_not_report_removed(self) -> None:
        urls = [f"https://fe-static.deepseek.com/platform/static/{index}.js" for index in range(8)]
        previous = _snapshot_with_front(
            _front_manifest_n(1),
            assets=[{"url": url, "sha256": f"h{index}"} for index, url in enumerate(urls)],
            asset_urls=urls,
        )
        new_url = "https://fe-static.deepseek.com/platform/static/new.js"
        # 新样式插到最前，把第 8 个挤出下载窗口；完整 URL 清单里它仍然存在。
        current = _snapshot_with_front(
            _front_manifest_n(1),
            assets=[{"url": new_url, "sha256": "new"}] + [
                {"url": url, "sha256": f"h{index}"} for index, url in enumerate(urls[:7])
            ],
            asset_urls=[new_url] + urls,
            frontend_evidence={"totals": {"manifest": 1, "added": 1, "removed": 0, "changed": 0}},
        )
        event = _diff_target("platform", "Platform 控制台", previous, current)
        self.assertIsNotNone(event)
        change = next(item for item in event["payload"]["changes"] if item["kind"] == "assets")
        self.assertEqual(change["removed_count"], 0)
        self.assertEqual(change["added_count"], 1)

    def test_asset_only_change_with_unhealthy_frontend_keeps_event(self) -> None:
        healthy = _front_manifest_n(3)
        degraded = dict(healthy, health="degraded", health_reason="资源清单数量骤减，按快照不完整处理")
        previous = _snapshot_with_front(healthy)
        current = _snapshot_with_front(
            degraded,
            assets=[{"url": "https://fe-static.deepseek.com/platform/static/new.css", "sha256": "new"}],
            asset_urls=["https://fe-static.deepseek.com/platform/static/new.css"],
        )
        event = _diff_target("platform", "Platform 控制台", previous, current)
        self.assertIsNotNone(event)
        self.assertTrue(event["payload"].get("evidence_unavailable"))
        self.assertIn("前端证据本轮不可用", event["summary"])


class LongTextPreviewTests(unittest.TestCase):
    def test_change_after_excerpt_window_is_explicit(self) -> None:
        previous = _snapshot_with_front(
            None, text_sha256="old", text_excerpt="前" * 4000
        )
        current = _snapshot_with_front(
            None, text_sha256="new", text_excerpt="前" * 4000
        )
        event = _diff_target("chat", "Chat 网页", previous, current)
        self.assertIsNotNone(event)
        self.assertIn("前 4000 字相同，后部内容有变化", event["summary"])


class CatalogIncompleteTests(unittest.TestCase):
    def test_partial_catalog_8_to_2_to_8_keeps_baseline_and_emits_nothing(self) -> None:
        full = [f"model-{index}" for index in range(8)]
        partial = full[:2]
        state: dict = {}
        now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)

        def run(catalog: list[str], when: datetime) -> list[dict]:
            with mock.patch(
                "webwatch.fingerprint.probe_catalog", return_value=catalog
            ), mock.patch(
                "webwatch.fingerprint.probe_model",
                side_effect=lambda _key, model: {"fingerprint": f"fp-{model}", "model": model},
            ):
                return check_api(state, when, "fake-key")

        self.assertEqual(run(full, now), [])
        self.assertEqual(state["models"]["catalog"], full)
        self.assertEqual(run(partial, now + timedelta(minutes=5)), [])
        self.assertEqual(state["models"]["catalog"], full)
        self.assertIn("残页", state["models"]["catalog_last_error"])
        self.assertEqual(run(full, now + timedelta(minutes=10)), [])
        self.assertEqual(state["models"]["catalog"], full)


class ModelNameNormalizationTests(unittest.TestCase):
    def test_full_width_annotation_is_same_model(self) -> None:
        html = (
            "<table><tr><td>模型</td><td>deepseek-flash</td>"
            "<td>deepseek-v4-pro（Beta）</td></tr>"
            "<tr><td>模型版本</td><td>V1</td><td>R1</td></tr></table>"
        )
        versions = parse_model_versions(html)
        self.assertEqual(
            [(item["model"], item["version"]) for item in versions],
            [("deepseek-flash", "V1"), ("deepseek-v4-pro", "R1")],
        )
        state = {"models": {"versions": {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"}}}
        now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.MODEL_PRICING_URL, html),
        ):
            events = check_model_versions(state, now)
        self.assertEqual(events, [])
        self.assertEqual(
            state["models"]["versions"], {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"}
        )

    def test_hyphenated_model_still_listed_is_not_a_removal(self) -> None:
        # 归一化后必须用同一规则在页面正文里查：deepseek-v4-pro 带连字符也要命中。
        self.assertTrue(
            _versions_look_incomplete(
                {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"},
                {"deepseek-flash": "V1"},
                page_text="当前支持 deepseek-v4-pro 与 deepseek-flash",
            )
        )

    def test_partial_price_page_with_old_model_in_body_is_rejected(self) -> None:
        state = {"models": {"versions": {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"}}}
        html = (
            "<html><body><p>deepseek-v4-pro 仍在售</p>"
            "<table><tr><td>模型</td><td>deepseek-flash</td></tr>"
            "<tr><td>模型版本</td><td>V2</td></tr></table></body></html>"
        )
        now = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
        with mock.patch(
            "webwatch.fingerprint.fetch",
            return_value=_html_response(config.MODEL_PRICING_URL, html),
        ):
            events = check_model_versions(state, now)
        self.assertEqual(events, [])
        self.assertEqual(
            state["models"]["versions"], {"deepseek-flash": "V1", "deepseek-v4-pro": "R1"}
        )


class ChunkShrinkProtectionTests(unittest.TestCase):
    def test_manifest_shrink_from_51_to_1_returns_no_evidence(self) -> None:
        from webwatch import frontend_evidence as frontend_evidence_module

        old = _front_manifest_n(51)
        new = _front_manifest_n(1)
        self.assertIsNone(
            frontend_evidence_module.build_evidence(
                old, new, cache_dir=None, source="platform", label="Platform"
            )
        )


class PlatformNoticeBodyTests(unittest.TestCase):
    def test_same_count_notice_body_change_emits_event(self) -> None:
        def payload(text: str) -> dict:
            return {
                "data": {
                    "biz_data": {
                        "settings": {
                            "notice_usage": {"value": [{"content": {"zh_CN": text}}]}
                        }
                    }
                }
            }

        state: dict = {}
        now1 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
        with mock.patch("webwatch.fingerprint.fetch_json", return_value=payload("额度说明 A")):
            self.assertEqual(check_platform_banner(state, now1), [])
        with mock.patch(
            "webwatch.fingerprint.fetch_json",
            return_value=payload("额度说明 B（价格调整）"),
        ):
            events = check_platform_banner(state, now1 + timedelta(minutes=15))
        self.assertEqual(len(events), 1)
        self.assertIn("额度说明 B（价格调整）", events[0]["summary"])
        self.assertTrue(any("额度说明 B（价格调整）" in item for item in state["platform_banner"]["summary"]))


if __name__ == "__main__":
    unittest.main()
