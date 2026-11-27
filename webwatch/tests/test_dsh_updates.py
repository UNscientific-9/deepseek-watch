from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from webwatch import dsh_updates
from webwatch.safe_fetch import (
    ParseError,
    Response,
    UnsafeURL,
    fetch_json,
    validate_url,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def _release(version: str, notes: list[str] | None = None, published: str = "") -> dict:
    return {
        "version": version,
        "tag": f"dsh-v{version}",
        "name": version,
        "published_at": published or "2026-11-01T00:00:00Z",
        "html_url": f"https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v{version}",
        "prerelease": True,
        "notes": list(notes or []),
    }


def _release_with_body(version: str, body: str, published: str = "") -> dict:
    note = dsh_updates.parse_release_note(body)
    item = _release(version, note["notes"], published)
    item["note"] = note
    return item


def _commit(sha: str, title: str, updated: str = "2026-11-22T10:00:00Z") -> dict:
    return {
        "id": f"tag:github.com,2008:Grit::Commit/{sha}",
        "sha": sha,
        "short": sha[:8],
        "title": title,
        "updated": updated,
        "url": f"https://github.com/deepseek-ai/deepseek-harness/commit/{sha}",
    }


def _baseline_state() -> tuple[dict, list[dict]]:
    """跑一次补录，得到「已初始化」状态和补录事件。"""
    state: dict = {}
    with mock.patch.object(
        dsh_updates,
        "_fetch_npm_metadata",
        return_value=(
            ["1.0.0", "1.1.0-alpha.1"],
            {
                "1.0.0": "2026-10-01T00:00:00Z",
                "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
            },
        ),
    ), mock.patch.object(
        dsh_updates,
        "_fetch_releases_all",
        return_value=[
            _release("1.0.0", ["首个版本"], "2026-10-01T00:00:00Z"),
            _release("1.1.0-alpha.1", ["新增功能 A"], "2026-10-02T00:00:00Z"),
        ],
    ), mock.patch.object(
        dsh_updates,
        "_fetch_commits",
        return_value=[_commit("a" * 40, "init"), _commit("b" * 40, "release(dsh): 1.1.0-alpha.1")],
    ), mock.patch.object(
        dsh_updates,
        "_fetch_desktop_channels",
        return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
    ):
        events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
    assert ok and not error
    assert len(events) == 2
    return state, events


class ReleaseNotesParserTests(unittest.TestCase):
    def test_only_chinese_bullets_are_kept(self) -> None:
        body = (
            '<p><a href="#cn-x">中文</a> | <a href="#en-x">English</a></p>'
            '<h3 id="cn-x">新增功能</h3><ul><li>新增 A</li><li>修复 B</li></ul>'
            '<h3 id="en-x">New Features</h3><ul><li>Add A</li></ul>'
        )
        self.assertEqual(dsh_updates.parse_release_notes(body), ["新增 A", "修复 B"])

    def test_plain_text_falls_back(self) -> None:
        self.assertEqual(
            dsh_updates.parse_release_notes("<p>只有一个说明</p>"),
            ["只有一个说明"],
        )

    def test_empty_body_returns_empty(self) -> None:
        self.assertEqual(dsh_updates.parse_release_notes(""), [])

    def test_hr_and_bold_are_not_treated_as_bullets(self) -> None:
        body = "<p>**重点**</p><p>---</p><ul><li>真实条目</li></ul>"
        self.assertEqual(dsh_updates.parse_release_notes(body), ["真实条目"])

    def test_full_release_note_keeps_all_bullets_and_section_headings(self) -> None:
        body = (
            '<h3 id="cn-x">新增功能</h3><ul>'
            + "".join(f"<li>条目 {index}</li>" for index in range(1, 31))
            + '</ul><h3 id="cn-y">问题修复</h3><ul><li>修复 X</li></ul>'
            '<h3 id="en-x">New Features</h3><ul><li>English A</li></ul>'
        )
        document = dsh_updates.parse_release_note(body)
        self.assertEqual(
            [block["text"] for block in document["blocks"] if block["type"] == "heading"],
            ["新增功能", "问题修复"],
        )
        self.assertEqual(len(document["notes"]), 31)
        self.assertIn("修复 X", document["notes"])
        self.assertNotIn("English A", document["notes"])

    def test_short_en_heading_id_is_cut(self) -> None:
        body = (
            '<h3 id="cn">新增功能</h3><ul><li>中文 A</li></ul>'
            '<h3 id="en">New Features</h3><ul><li>English A</li></ul>'
        )
        document = dsh_updates.parse_release_note(body)
        self.assertEqual(document["notes"], ["中文 A"])

    def test_old_style_chinese_heading_is_not_a_section_and_english_is_cut(self) -> None:
        body = (
            '<h2 id="chinese">0.1.3-alpha.2 · 中文</h2>'
            "<h3>新增功能</h3><ul><li>中文 A</li></ul>"
            '<h2 id="english">0.1.3-alpha.2 · English</h2>'
            "<h3>New Features</h3><ul><li>English A</li></ul>"
        )
        document = dsh_updates.parse_release_note(body)
        self.assertEqual(document["notes"], ["中文 A"])
        headings = [block["text"] for block in document["blocks"] if block["type"] == "heading"]
        self.assertEqual(headings, ["新增功能"])


class DshUpdateFlowTests(unittest.TestCase):
    def test_first_run_backfills_all_npm_versions_at_level_one(self) -> None:
        state, events = _baseline_state()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["level"] == 1 for event in events))
        self.assertTrue(all(event["payload"]["backfill"] is True for event in events))
        section = state["dsh"]
        self.assertTrue(section["initialized"])
        self.assertEqual(set(section["versions_seen"]), {"1.0.0", "1.1.0-alpha.1"})
        self.assertEqual(
            set(section["reported_versions"]), {"1.0.0", "1.1.0-alpha.1"}
        )
        self.assertEqual(len(section["commits_seen"]), 2)

        # 再次运行但没有新东西：不应重复出事件
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(copy.deepcopy(state), NOW)
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_backfill_refuses_empty_npm_baseline(self) -> None:
        state: dict = {}
        with mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", return_value=([], {})
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases_all",
            return_value=[_release("1.0.0", ["首个版本"])],
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertEqual(events, [])
        self.assertFalse(ok)
        self.assertIn("空基线", error)
        self.assertFalse(state.get("dsh", {}).get("initialized"))

    def test_backfill_refuses_empty_release_baseline(self) -> None:
        state: dict = {}
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            return_value=(["1.0.0"], {"1.0.0": "2026-11-22T11:00:00Z"}),
        ), mock.patch.object(
            dsh_updates, "_fetch_releases_all", return_value=[]
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertEqual(events, [])
        self.assertFalse(ok)
        self.assertIn("空基线", error)
        self.assertFalse(state.get("dsh", {}).get("initialized"))

    def test_release_is_level_three_while_backfill_and_commit_stay_level_one(self) -> None:
        """DSH 级别口径（2026-11-22）：新发版 3，历史补录 1，提交预告 1。"""
        state, backfill_events = _baseline_state()
        self.assertEqual(len(backfill_events), 2)
        self.assertTrue(all(event["level"] == 1 for event in backfill_events))
        self.assertTrue(all(event["payload"]["backfill"] is True for event in backfill_events))

        new_commits = [_commit("f" * 40, "fix: 修复说明", "2026-11-22T11:30:00Z")]
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.3", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            return_value=(
                ["1.0.0", "1.1.0-alpha.1", "1.0.3"],
                {
                    "1.0.0": "2026-10-01T00:00:00Z",
                    "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                    "1.0.3": "2026-11-22T11:00:00Z",
                },
            ),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.3", ["正式发版说明"], "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=new_commits
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        self.assertEqual(error, "")
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        commit_events = [event for event in events if event["kind"] == "dsh_commit"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(len(commit_events), 1)
        self.assertEqual(release_events[0]["level"], 3)
        self.assertEqual(release_events[0]["level_label"], "重要")
        self.assertFalse(release_events[0]["payload"]["backfill"])
        self.assertEqual(commit_events[0]["level"], 1)
        self.assertEqual(commit_events[0]["level_label"], "轻微")

    def test_backfill_includes_release_only_version(self) -> None:
        state: dict = {}
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            return_value=(
                ["1.0.0"],
                {"1.0.0": "2026-10-01T00:00:00Z"},
            ),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases_all",
            return_value=[
                _release("1.0.0", ["首个版本"], "2026-10-01T00:00:00Z"),
                _release("2.0.0", ["仅 GitHub 发版"], "2026-11-01T00:00:00Z"),
            ],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.0.0"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        self.assertEqual({event["payload"]["version"] for event in events}, {"1.0.0", "2.0.0"})
        self.assertIn("2.0.0", state["dsh"]["releases_seen"])
        self.assertIn("2.0.0", state["dsh"]["reported_versions"])

    def test_pure_channel_change_does_not_emit(self) -> None:
        state, _ = _baseline_state()
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.1.0-alpha.1", "alpha": "1.0.0"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("纯换通道不应拉全量")
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(copy.deepcopy(state), NOW)
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_older_branch_new_version_is_reported(self) -> None:
        state, _ = _baseline_state()
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.1-rc.1", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            return_value=(
                ["1.0.0", "1.1.0-alpha.1", "1.0.1-rc.1"],
                {
                    "1.0.0": "2026-10-01T00:00:00Z",
                    "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                    "1.0.1-rc.1": "2026-11-22T11:00:00Z",
                },
            ),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.1-rc.1", ["旧分支补丁"], "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertIn("1.0.1-rc.1", release_events[0]["title"])
        # 2026-11-22 口径：轮询产生的新版本事件是 3 级「重要」；
        # 历史补录仍是 1 级（见 test_first_run_backfills_all_npm_versions_at_level_one）。
        self.assertEqual(release_events[0]["level"], 3)
        self.assertEqual(release_events[0]["level_label"], "重要")
        self.assertEqual(release_events[0]["changes_detail"][0]["level"], 3)
        self.assertIn("1.0.1-rc.1", state["dsh"]["versions_seen"])

    def test_release_only_version_is_reported(self) -> None:
        state, _ = _baseline_state()
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("2.0.0", ["全新大版本"], "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertIn("2.0.0", release_events[0]["title"])
        self.assertIn("github_release", release_events[0]["payload"]["signals"])
        self.assertIn("2.0.0", state["dsh"]["releases_seen"])
        self.assertIn("2.0.0", state["dsh"]["reported_versions"])

    def test_desktop_only_version_is_reported(self) -> None:
        state, _ = _baseline_state()
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.2.0", "nightly": "1.1.0-alpha.1"},
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertIn("1.2.0", release_events[0]["title"])
        self.assertIn("desktop:stable", release_events[0]["payload"]["signals"])
        self.assertEqual(release_events[0]["level"], 3)

    def test_same_version_from_three_sources_emits_one_event(self) -> None:
        state, _ = _baseline_state()
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            return_value=(
                ["1.0.0", "1.1.0-alpha.1", "1.0.2"],
                {
                    "1.0.0": "2026-10-01T00:00:00Z",
                    "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                    "1.0.2": "2026-11-22T11:00:00Z",
                },
            ),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.2", ["修复 A"], "2026-11-22T11:05:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.2", "nightly": "1.0.2"},
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        signals = release_events[0]["payload"]["signals"]
        self.assertIn("npm", signals)
        self.assertIn("github_release", signals)
        self.assertIn("desktop:stable", signals)
        self.assertIn("desktop:nightly", signals)

    def test_npm_only_version_waits_for_release_grace(self) -> None:
        state, _ = _baseline_state()
        metadata = (
            ["1.0.0", "1.1.0-alpha.1", "1.0.2"],
            {
                "1.0.0": "2026-10-01T00:00:00Z",
                "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                "1.0.2": "2026-11-22T11:00:00Z",
            },
        )
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", return_value=metadata
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            first_events, ok, _ = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        self.assertEqual(first_events, [])
        self.assertIn("1.0.2", state["dsh"]["pending_versions"])

        later = NOW.replace(minute=31)
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            second_events, ok, _ = dsh_updates.check_dsh_updates(state, later)
        self.assertTrue(ok)
        grace_release_events = [e for e in second_events if e["kind"] == "dsh_release"]
        self.assertEqual(len(grace_release_events), 1)
        # 等满 30 分钟、没有等到 release note 的 npm 版本，同样是 3 级新版本事件。
        self.assertEqual(grace_release_events[0]["level"], 3)
        # 宽限期内不再拉全量元数据，但事件时间必须仍是 npm 发布时间，不能变成
        # 「本轮发现时间」。
        self.assertEqual(grace_release_events[0]["changed_at"], "2026-11-22T11:00:00Z")
        self.assertEqual(
            grace_release_events[0]["payload"]["npm_published_at"],
            "2026-11-22T11:00:00Z",
        )
        self.assertNotIn("1.0.2", state["dsh"]["pending_versions"])
        self.assertIn("1.0.2", state["dsh"]["awaiting_notes"])

    def test_future_pending_first_seen_is_rebased_instead_of_sticking_forever(self) -> None:
        state, _ = _baseline_state()
        state["dsh"]["pending_versions"] = {
            "1.0.2": {
                "first_seen": "2099-01-01T00:00:00Z",
                "signals": ["npm"],
                "npm_time": "2026-11-22T11:00:00Z",
            }
        }
        metadata = (
            ["1.0.0", "1.1.0-alpha.1", "1.0.2"],
            {
                "1.0.0": "2026-10-01T00:00:00Z",
                "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                "1.0.2": "2026-11-22T11:00:00Z",
            },
        )
        desktop = {"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", return_value=metadata
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            first_events, ok, _ = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        self.assertEqual(first_events, [])
        self.assertEqual(
            state["dsh"]["pending_versions"]["1.0.2"]["first_seen"],
            "2026-11-22T12:00:00Z",
        )

        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            second_events, ok, _ = dsh_updates.check_dsh_updates(
                state, NOW.replace(minute=31)
            )
        self.assertTrue(ok)
        self.assertEqual(
            [e["id"] for e in second_events if e["kind"] == "dsh_release"],
            ["dsh:release:1.0.2"],
        )

    def test_release_notes_arriving_after_grace_update_the_event(self) -> None:
        state, _ = _baseline_state()
        metadata = (
            ["1.0.0", "1.1.0-alpha.1", "1.0.2"],
            {
                "1.0.0": "2026-10-01T00:00:00Z",
                "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                "1.0.2": "2026-11-22T11:00:00Z",
            },
        )
        desktop = {"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", return_value=metadata
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            dsh_updates.check_dsh_updates(state, NOW)

        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            grace_events, ok, _ = dsh_updates.check_dsh_updates(
                state, NOW.replace(minute=31)
            )
        self.assertTrue(ok)
        self.assertEqual(
            [e["id"] for e in grace_events if e["kind"] == "dsh_release"],
            ["dsh:release:1.0.2"],
        )

        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[
                _release("1.0.2", ["补丁说明"], "2026-11-22T11:05:00Z")
            ],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            update_events, ok, _ = dsh_updates.check_dsh_updates(
                state, NOW.replace(minute=35)
            )
        self.assertTrue(ok)
        release_events = [e for e in update_events if e["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(release_events[0]["id"], "dsh:release:1.0.2")
        self.assertEqual(release_events[0]["level"], 3)
        self.assertEqual(release_events[0]["payload"]["notes"], ["补丁说明"])
        self.assertIn("github_release", release_events[0]["payload"]["signals"])
        self.assertNotIn("1.0.2", state["dsh"]["awaiting_notes"])

    def test_pending_npm_version_gets_release_notes_later(self) -> None:
        state, _ = _baseline_state()
        metadata = (
            ["1.0.0", "1.1.0-alpha.1", "1.0.2"],
            {
                "1.0.0": "2026-10-01T00:00:00Z",
                "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                "1.0.2": "2026-11-22T11:00:00Z",
            },
        )
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", return_value=metadata
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            dsh_updates.check_dsh_updates(state, NOW)

        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.2", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.2", ["补丁说明"], "2026-11-22T11:05:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, _ = dsh_updates.check_dsh_updates(state, NOW.replace(minute=10))
        self.assertTrue(ok)
        release_events = [e for e in events if e["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(release_events[0]["payload"]["notes"], ["补丁说明"])

    def test_release_event_carries_complete_release_note(self) -> None:
        state, _ = _baseline_state()
        body = (
            '<h3 id="cn-x">新增功能</h3><ul>'
            + "".join(f"<li>完整条目 {index}</li>" for index in range(1, 26))
            + "</ul>"
            '<h3 id="en-x">New Features</h3><ul><li>English A</li></ul>'
        )
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.4", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            return_value=(
                ["1.0.0", "1.1.0-alpha.1", "1.0.4"],
                {
                    "1.0.0": "2026-10-01T00:00:00Z",
                    "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                    "1.0.4": "2026-11-22T11:00:00Z",
                },
            ),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release_with_body("1.0.4", body, "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
        ):
            events, ok, _ = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        event = release_events[0]
        self.assertEqual(event["summary"], "官方更新日志共 25 条。")
        self.assertEqual(len(event["payload"]["notes"]), 25)
        self.assertEqual(
            [block["text"] for block in event["payload"]["release_note"]["blocks"] if block["type"] == "heading"],
            ["新增功能"],
        )
        self.assertEqual(
            len([block for block in event["payload"]["release_note"]["blocks"] if block["type"] == "bullet"]),
            25,
        )
        self.assertNotIn("English A", event["payload"]["notes"])

    def test_empty_release_body_is_backfilled_after_notes_appear(self) -> None:
        state, _ = _baseline_state()
        metadata = (
            ["1.0.0", "1.1.0-alpha.1", "1.0.5"],
            {
                "1.0.0": "2026-10-01T00:00:00Z",
                "1.1.0-alpha.1": "2026-10-02T00:00:00Z",
                "1.0.5": "2026-11-22T11:00:00Z",
            },
        )
        desktop = {"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.5", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", return_value=metadata
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.5", [], "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            first_events, ok, _ = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        first_release = [event for event in first_events if event["kind"] == "dsh_release"][0]
        self.assertEqual(first_release["title"], "DSH v1.0.5 发布")
        self.assertEqual(first_release["summary"], "")
        self.assertEqual(first_release["payload"]["notes"], [])
        self.assertIn("1.0.5", state["dsh"]["awaiting_notes"])
        self.assertNotIn("1.0.5", state["dsh"]["notes_versions"])

        body = (
            '<h3 id="cn-x">问题修复</h3><ul><li>修复 A</li><li>修复 B</li></ul>'
            '<h3 id="en-x">Bug Fixes</h3><ul><li>Fix A</li></ul>'
        )
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.5", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release_with_body("1.0.5", body, "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value=desktop
        ):
            second_events, ok, _ = dsh_updates.check_dsh_updates(
                state, NOW.replace(minute=20)
            )
        self.assertTrue(ok)
        release_events = [event for event in second_events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(release_events[0]["id"], "dsh:release:1.0.5")
        self.assertEqual(release_events[0]["payload"]["notes"], ["修复 A", "修复 B"])
        self.assertEqual(release_events[0]["summary"], "官方更新日志共 2 条。")
        self.assertNotIn("1.0.5", state["dsh"]["awaiting_notes"])
        self.assertIn("1.0.5", state["dsh"]["notes_versions"])

    def test_backfill_flag_survives_state_round_trip_for_awaiting_notes(self) -> None:
        state, _ = _baseline_state()
        state["dsh"]["awaiting_notes"] = {
            "1.0.0": {
                "first_seen": "2026-11-01T00:00:00Z",
                "signals": ["github_release"],
                "npm_time": "2026-10-01T00:00:00Z",
                "changed_at": "2026-10-01T00:00:00Z",
                "backfill": True,
            }
        }
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.0", ["补全说明"], "2026-10-01T00:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
        ):
            events, ok, _ = dsh_updates.check_dsh_updates(
                state, NOW.replace(minute=20)
            )
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(release_events[0]["id"], "dsh:release:1.0.0")
        self.assertEqual(release_events[0]["level"], 1)
        self.assertTrue(release_events[0]["payload"]["backfill"])

    def test_legacy_reported_version_gets_full_notes_repaired_once(self) -> None:
        state, _ = _baseline_state()
        # 模拟升级前的旧状态：只有 reported_versions，没有 notes_versions。
        state["dsh"].pop("notes_versions", None)
        body = (
            '<h3 id="cn-x">新增功能</h3><ul>'
            + "".join(f"<li>旧条目 {index}</li>" for index in range(1, 26))
            + "</ul>"
        )
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases_all",
            return_value=[_release_with_body("1.0.0", body, "2026-10-01T00:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
        ):
            events, ok, _ = dsh_updates.check_dsh_updates(
                state,
                NOW.replace(minute=20),
                known_changed_at={"dsh:release:1.0.0": "2026-10-01T00:00:00Z"},
            )
        self.assertTrue(ok)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertEqual(release_events[0]["id"], "dsh:release:1.0.0")
        self.assertEqual(release_events[0]["level"], 1)
        self.assertTrue(release_events[0]["payload"]["backfill"])
        self.assertEqual(release_events[0]["title"], "DSH v1.0.0 发布")
        self.assertEqual(len(release_events[0]["payload"]["notes"]), 25)
        self.assertIn("1.0.0", state["dsh"]["notes_versions"])

        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates,
            "_fetch_npm_metadata",
            side_effect=AssertionError("不应拉全量"),
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release_with_body("1.0.0", body, "2026-10-01T00:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
        ):
            again, ok, _ = dsh_updates.check_dsh_updates(
                state, NOW.replace(minute=40)
            )
        self.assertTrue(ok)
        self.assertEqual([event for event in again if event["kind"] == "dsh_release"], [])

    def test_commit_failure_does_not_block_release_event(self) -> None:
        state, _ = _baseline_state()
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates,
            "_fetch_releases",
            return_value=[_release("1.0.3", ["发布说明"], "2026-11-22T11:00:00Z")],
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", side_effect=RuntimeError("atom down")
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertFalse(ok)
        self.assertIn("atom down", error)
        release_events = [event for event in events if event["kind"] == "dsh_release"]
        self.assertEqual(len(release_events), 1)
        self.assertIn("1.0.3", release_events[0]["title"])

    def test_commit_batch_is_merged_into_one_event(self) -> None:
        state, _ = _baseline_state()
        new_commits = [
            _commit("c" * 40, "release(dsh): 1.1.0-alpha.2", "2026-11-22T11:00:00Z"),
            _commit("d" * 40, "fix: 修复 A"),
            _commit("e" * 40, "feat: 新增 B"),
        ]
        with mock.patch.object(
            dsh_updates,
            "_fetch_npm_dist_tags",
            return_value={"latest": "1.0.0", "alpha": "1.1.0-alpha.1"},
        ), mock.patch.object(
            dsh_updates, "_fetch_npm_metadata", side_effect=AssertionError("不应拉全量")
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_commits",
            return_value=new_commits
            + [_commit("a" * 40, "init"), _commit("b" * 40, "release(dsh): 1.1.0-alpha.1")],
        ), mock.patch.object(
            dsh_updates, "_fetch_desktop_channels", return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"}
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertTrue(ok)
        commit_events = [event for event in events if event["kind"] == "dsh_commit"]
        self.assertEqual(len(commit_events), 1)
        self.assertEqual(commit_events[0]["level"], 1)
        self.assertIn("3 个提交", commit_events[0]["title"])
        self.assertIn("发版准备", commit_events[0]["title"])
        self.assertEqual(commit_events[0]["payload"]["count"], 3)

    def test_fetch_failure_does_not_advance_baseline(self) -> None:
        state, _ = _baseline_state()
        before = copy.deepcopy(state["dsh"])
        with mock.patch.object(
            dsh_updates, "_fetch_npm_dist_tags", side_effect=RuntimeError("registry down")
        ), mock.patch.object(
            dsh_updates, "_fetch_releases", return_value=[]
        ), mock.patch.object(
            dsh_updates, "_fetch_commits", return_value=[]
        ), mock.patch.object(
            dsh_updates,
            "_fetch_desktop_channels",
            return_value={"stable": "1.0.0", "nightly": "1.1.0-alpha.1"},
        ):
            events, ok, error = dsh_updates.check_dsh_updates(state, NOW)
        self.assertFalse(ok)
        self.assertEqual(events, [])
        self.assertIn("registry down", error)
        self.assertEqual(state["dsh"]["versions_seen"], before["versions_seen"])
        self.assertEqual(state["dsh"]["commits_seen"], before["commits_seen"])
        self.assertEqual(state["dsh"]["releases_seen"], before["releases_seen"])


class DshFetchHelperTests(unittest.TestCase):
    def test_validate_url_allows_only_explicit_extra_hosts(self) -> None:
        with self.assertRaises(UnsafeURL):
            validate_url("https://api.github.com/repos/x/y")
        self.assertTrue(
            validate_url(
                "https://api.github.com/repos/x/y",
                allowed_hosts=("api.github.com",),
            ).hostname
        )
        with self.assertRaises(UnsafeURL):
            validate_url(
                "https://api.github.com.evil.example/repos/x/y",
                allowed_hosts=("api.github.com",),
            )

    def test_fetch_json_can_return_list_explicitly(self) -> None:
        response = Response(
            url="https://api.github.com/repos/x/y/releases",
            final_url="https://api.github.com/repos/x/y/releases",
            status=200,
            body=b'[{"tag_name": "dsh-v1.0.0"}]',
            headers={"content-type": "application/json"},
        )
        with mock.patch("webwatch.safe_fetch.fetch", return_value=response) as fetch_mock:
            self.assertEqual(
                fetch_json(
                    "https://api.github.com/repos/x/y/releases",
                    allowed_hosts=("api.github.com",),
                    expect=list,
                ),
                [{"tag_name": "dsh-v1.0.0"}],
            )
            # 按调用传入的白名单必须原样转发给 fetch（否则 DSH 的域名会被默认白名单拒绝）。
            self.assertEqual(
                fetch_mock.call_args.kwargs["allowed_hosts"], ("api.github.com",)
            )
            with self.assertRaises(ParseError):
                fetch_json(
                    "https://api.github.com/repos/x/y/releases",
                    allowed_hosts=("api.github.com",),
                )

    def test_commit_title_uses_only_the_first_line(self) -> None:
        payload = [
            {
                "sha": "a" * 40,
                "commit": {
                    "message": "fix: 修复说明\n\n这段正文不应进入提交标题",
                    "author": {"date": "2026-11-22T11:00:00Z"},
                },
                "html_url": "https://github.com/deepseek-ai/deepseek-harness/commit/" + "a" * 40,
            }
        ]
        with mock.patch.object(dsh_updates, "fetch_json", return_value=payload):
            commits = dsh_updates._fetch_commits()
        self.assertEqual(commits[0]["title"], "fix: 修复说明")

    def test_desktop_channel_failure_does_not_drop_the_other_channel(self) -> None:
        def fake_fetch(url: str, **_kwargs) -> Response:
            if "stable" in url:
                return Response(
                    url=url,
                    final_url=url,
                    status=200,
                    body=b"version: 1.2.3\nfiles:\n  - url: x\n",
                    headers={"content-type": "text/yaml"},
                )
            raise RuntimeError("nightly 清单 404")

        errors: list[str] = []
        with mock.patch.object(dsh_updates, "fetch", side_effect=fake_fetch):
            channels = dsh_updates._fetch_desktop_channels(errors=errors)
        self.assertEqual(channels, {"stable": "1.2.3"})
        self.assertEqual(len(errors), 1)
        self.assertIn("nightly", errors[0])

        # 首次补录不传 errors，仍要求两个通道都成功，避免建立不完整基线。
        with mock.patch.object(dsh_updates, "fetch", side_effect=fake_fetch):
            with self.assertRaises(RuntimeError):
                dsh_updates._fetch_desktop_channels()

    def test_desktop_feed_version_is_parsed(self) -> None:
        def fake_fetch(url: str, **_kwargs) -> Response:
            version = "1.2.3" if "stable" in url else "1.3.0-alpha.1"
            return Response(
                url=url,
                final_url=url,
                status=200,
                body=f"version: {version}\nfiles:\n  - url: x\n".encode("utf-8"),
                headers={"content-type": "text/yaml"},
            )

        with mock.patch.object(dsh_updates, "fetch", side_effect=fake_fetch):
            self.assertEqual(
                dsh_updates._fetch_desktop_channels(),
                {"stable": "1.2.3", "nightly": "1.3.0-alpha.1"},
            )


class DshRunHelperTests(unittest.TestCase):
    def test_changed_at_map_reads_only_dsh_release_events(self) -> None:
        from webwatch.run import _dsh_changed_at_map
        from webwatch.tests.support import cleanup, temp_dir

        root = temp_dir("dsh-events-")
        try:
            (root / "dsh-release-1.json").write_text(
                json.dumps(
                    {
                        "id": "dsh:release:1.0.0",
                        "kind": "dsh_release",
                        "changed_at": "2026-10-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (root / "other.json").write_text(
                json.dumps(
                    {
                        "id": "web:chat:1",
                        "kind": "page_change",
                        "changed_at": "2026-10-02T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (root / "broken.json").write_text("{", encoding="utf-8")
            self.assertEqual(
                _dsh_changed_at_map(root),
                {"dsh:release:1.0.0": "2026-10-01T00:00:00Z"},
            )
        finally:
            cleanup(root)


if __name__ == "__main__":
    unittest.main()
