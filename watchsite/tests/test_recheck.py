from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from watchsite import recheck, recheck_agent, recheck_apply, recheck_config as cfg, recheck_job
from watchsite import recheck_notify, recheck_verdict
from watchsite.tests.support import cleanup, temp_dir

FRONTEND = {
    "totals": {"manifest": 12, "added": 2, "removed": 1, "changed": 4},
    "cjk_added": ["新的提示文案"],
    "changed_files": [
        {
            "url": "https://chat.deepseek.com/static/main.js",
            "new_url": "https://chat.deepseek.com/static/main.js?v=2",
            "old_url": "https://chat.deepseek.com/static/main.js?v=1",
        }
    ],
    "changed_code_snippets": ["+ 新的提示文案"],
}


def make_event(event_id: str = "evt-1", **overrides) -> dict:
    event = {
        "schema_version": 2,
        "id": event_id,
        "slug": f"chat-{event_id}",
        "source": "chat",
        "kind": "page_change",
        "level": 2,
        "level_label": "值得看",
        "title": "对话页新增提示",
        "summary": "对话页出现一条新的提示文案。",
        "impact": "普通用户可能看到新的提示。",
        "action": "无需操作",
        "signal_count": 1,
        "changed_at": "2026-11-24T02:00:00Z",
        "page_url": "https://chat.deepseek.com/",
        "archive_url": "https://chat.deepseek.com/",
        "evidence": ["https://chat.deepseek.com/"],
        "payload": {
            "quality": "ok",
            "quality_state": "final",
            "frontend": FRONTEND,
            "changes": [{"kind": "text", "old": "旧提示", "new": "新的提示文案"}],
            "analysis": {
                "headline": "对话页新增提示",
                "tldr": "对话页出现一条新的提示文案。",
                "impact": "普通用户可能看到新的提示。",
                "action": "无需操作",
            },
            "level_decision": {"level": 2, "reason": "规则判级"},
        },
    }
    event.update(overrides)
    return event


def make_verdict(event: dict, **overrides) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    value = {
        "event_id": event.get("id"),
        "verdict": "confirmed",
        "confidence": "high",
        "quality": str(payload.get("quality") or "ok"),
        "title": "",
        "summary": "",
        "impact": "",
        "action": "",
        "reason": "核对官方页面后，变化与证据一致。",
        "basis": "官方页面出现新的提示文案",
        "level_suggestion": {
            "suggested": int(event.get("level") or 0),
            "reason": "与当前级别一致",
            "evidence": [],
        },
        "major_misjudgement": False,
        "evidence": [
            {
                "url": "https://chat.deepseek.com/",
                "finding": "页面源码中出现了新的提示文案",
            }
        ],
        "checks": ["核对页面源码"],
        "limitations": [],
    }
    value.update(overrides)
    return value


class RecheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_dir("recheck-test-")
        self.addCleanup(cleanup, self.root)
        self.events = self.root / "events"
        self.state = self.root / "state"
        self.vault = self.root / "vault"
        self.events.mkdir()
        self.state.mkdir()
        self.vault.mkdir()
        env = mock.patch.dict(
            os.environ,
            {
                "RECHECK_EVENTS_DIR": str(self.events),
                "RECHECK_STATE_DIR": str(self.state),
                "RECHECK_VAULT_DIR": str(self.vault),
                "RECHECK_APPLY_LEVEL": "0",
                "RECHECK_LEVEL_MIN_GAP": "2",
                "RECHECK_AGENT_HOME_CONFIG": "none",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        for target, value in (
            ("watchsite.recheck._alert", lambda *a, **k: None),
            ("watchsite.recheck_notify._qq_enabled", lambda: False),
            ("watchsite.recheck_notify._mail_enabled", lambda: False),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_event(self, event: dict, name: str | None = None) -> Path:
        path = self.events / (name or f"{event.get('slug') or event.get('id')}.json")
        path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


class ConfigTests(RecheckCase):
    def test_sources_are_hard_limited(self) -> None:
        self.assertEqual(cfg.sources(), ("chat", "platform", "harness"))
        with mock.patch.dict(os.environ, {"RECHECK_SOURCES": "docs,fingerprint,chat"}):
            self.assertEqual(cfg.sources(), ("chat",))

    def test_bool_and_int_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RECHECK_APPLY_LEVEL": "true", "RECHECK_LEVEL_MIN_GAP": "1"},
        ):
            self.assertTrue(cfg.apply_level_enabled())
            self.assertEqual(cfg.level_min_gap(), 1)
        with mock.patch.dict(os.environ, {"RECHECK_APPLY_LEVEL": "0"}):
            self.assertFalse(cfg.apply_level_enabled())


class JobTests(RecheckCase):
    def test_sanitize_keeps_file_diffs_and_gap_fields(self) -> None:
        """复核 agent 需要看到具体 diff 和缺口原因，而不是只有资源清单。"""
        event = make_event()
        event["payload"]["frontend"] = dict(
            FRONTEND,
            truncated=True,
            truncation_reasons=["变更分片达到核对上限"],
            file_diffs=[
                {
                    "file": "main.js",
                    "kind": "js",
                    "added_count": 1,
                    "removed_count": 0,
                    "diff": "--- old/main.js\n+++ new/main.js\n+ 新的提示文案",
                }
            ],
            removed_i18n_refs=[
                {"key": "oldKey", "file": "2653", "values": ["旧提示"], "old_refs": 1, "new_refs": 0}
            ],
        )
        event["payload"]["analysis"] = {
            "headline": "对话页新增提示",
            "tldr": "对话页新增一条提示",
            "visible_changes": ["对话页新增「新的提示文案」"],
            "impact": [],
            "action": "无需操作",
            "evidence_gaps": ["旧版分片未取到"],
        }
        clean = recheck_job.sanitize_event(event)
        frontend = clean["payload"]["frontend"]
        self.assertEqual(frontend["truncation_reasons"], ["变更分片达到核对上限"])
        self.assertEqual(frontend["file_diffs"][0]["file"], "main.js")
        self.assertIn("新的提示文案", frontend["file_diffs"][0]["diff"])
        self.assertEqual(frontend["removed_i18n_refs"][0]["values"], ["旧提示"])
        self.assertEqual(clean["payload"]["analysis"]["evidence_gaps"], ["旧版分片未取到"])

    def test_job_is_deterministic(self) -> None:
        event = make_event()
        first = recheck_job.build_job(
            self.root / "job-a", event, events_dir=self.events, vault_dir=self.vault
        )
        second = recheck_job.build_job(
            self.root / "job-b", event, events_dir=self.events, vault_dir=self.vault
        )
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(first["bytes"], second["bytes"])
        self.assertLessEqual(first["bytes"], 5 * 1024 * 1024)

    def test_job_reads_matching_vault_snapshot(self) -> None:
        snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        snap.mkdir(parents=True)
        manifest = {
            "snapshot_id": snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "main_js_url": "https://chat.deepseek.com/static/main.js?v=2",
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "x"},
            "files": [
                {
                    "url": "https://chat.deepseek.com/static/main.js?v=2",
                    "blob": "blob://deadbeef",
                    "hash": "deadbeef",
                    "kind": "js",
                    "status": "downloaded",
                    "key": "main_js",
                    "name": "main.js",
                }
            ],
        }
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event = make_event()
        event["payload"]["frontend"] = dict(FRONTEND, new_commit="abc12345")
        recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        evidence = json.loads(
            (self.root / "job" / "evidence" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["snapshot"], snap.name)

    def test_manifest_is_pruned_to_event_entries(self) -> None:
        snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        snap.mkdir(parents=True)
        main_url = "https://chat.deepseek.com/static/main.js?v=2"
        files = [
            {
                "url": main_url,
                "blob": "main-blob",
                "hash": "main",
                "kind": "js",
                "status": "downloaded",
                "key": "main_js",
                "name": "main.js",
            }
        ]
        for index in range(10):
            files.append(
                {
                    "url": f"https://chat.deepseek.com/static/chunk-{index}.js",
                    "blob": f"blob-{index}",
                    "hash": f"hash-{index}",
                    "kind": "js",
                    "status": "downloaded",
                    "key": f"chunk:{index}",
                    "name": f"chunk-{index}",
                }
            )
        manifest = {
            "snapshot_id": snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "main_js_url": main_url,
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "x"},
            "files": files,
        }
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event = make_event()
        event["payload"]["frontend"] = dict(FRONTEND, new_commit="abc12345")
        recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        evidence = json.loads(
            (self.root / "job" / "evidence" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["entries_total"], 11)
        self.assertEqual([item["url"] for item in evidence["entries"]], [main_url])

    def test_large_blob_is_copied_without_old_400kb_truncation(self) -> None:
        snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        snap.mkdir(parents=True)
        manifest = {
            "snapshot_id": snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "main_js_url": "https://chat.deepseek.com/static/main.js?v=2",
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "x"},
            "files": [
                {
                    "url": "https://chat.deepseek.com/static/main.js?v=2",
                    "blob": "blob://deadbeef",
                    "hash": "deadbeef",
                    "kind": "js",
                    "status": "downloaded",
                    "key": "main_js",
                    "name": "main.js",
                }
            ],
        }
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event = make_event()
        event["payload"]["frontend"] = dict(FRONTEND, new_commit="abc12345")
        large = b"x" * (1200 * 1024)
        with mock.patch("webwatch.bundle_vault.BundleVault.read", return_value=large):
            recheck_job.build_job(
                self.root / "job", event, events_dir=self.events, vault_dir=self.vault
            )
        files = list((self.root / "job" / "evidence" / "files").iterdir())
        self.assertEqual(len(files), 1)
        self.assertGreaterEqual(files[0].stat().st_size, len(large))

    def test_old_and_new_files_are_both_copied(self) -> None:
        old_snap = self.vault / "snapshots" / "chat" / "20261124T010000Z-old12345"
        new_snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        old_url = "https://chat.deepseek.com/static/main.old.js"
        new_url = "https://chat.deepseek.com/static/main.new.js"
        old_snap.mkdir(parents=True)
        new_snap.mkdir(parents=True)
        old_manifest = {
            "snapshot_id": old_snap.name,
            "complete": True,
            "build_meta": {"commitId": "old12345"},
            "main_js_url": old_url,
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "old"},
            "files": [
                {
                    "url": old_url,
                    "blob": "old-blob",
                    "hash": "old",
                    "kind": "js",
                    "status": "downloaded",
                    "key": "main_js",
                    "name": "main.js",
                }
            ],
        }
        new_manifest = {
            "snapshot_id": new_snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "main_js_url": new_url,
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "new"},
            "files": [
                {
                    "url": new_url,
                    "blob": "new-blob",
                    "hash": "new",
                    "kind": "js",
                    "status": "downloaded",
                    "key": "main_js",
                    "name": "main.js",
                }
            ],
        }
        (old_snap / "manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
        (new_snap / "manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
        event = make_event()
        event["payload"]["frontend"] = dict(
            FRONTEND,
            old_commit="old12345",
            new_commit="abc12345",
            main_js={"old_url": old_url, "new_url": new_url},
            changed_files=[{"old_url": old_url, "new_url": new_url}],
        )
        with mock.patch("webwatch.bundle_vault.BundleVault.read", return_value=b"body"):
            info = recheck_job.build_job(
                self.root / "job", event, events_dir=self.events, vault_dir=self.vault
            )
        self.assertEqual(info["files"], 2)
        brief = (self.root / "job" / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("旧版", brief)
        self.assertIn("新版", brief)

    def test_cleanup_job_evidence_keeps_verdict_and_logs(self) -> None:
        job = self.root / "job"
        files = job / "evidence" / "files"
        files.mkdir(parents=True)
        (files / "big.js").write_bytes(b"x" * 1200)
        (job / "evidence" / "changes.diff").write_text("diff" * 200, encoding="utf-8")
        (job / "out").mkdir()
        (job / "out" / "verdict.json").write_text("{}", encoding="utf-8")
        (job / "BRIEF.md").write_text("brief", encoding="utf-8")
        stats = recheck.cleanup_job_evidence(job)
        self.assertEqual(stats["removed_files"], 2)
        self.assertGreaterEqual(stats["removed_bytes"], 1200)
        self.assertFalse(files.exists())
        self.assertFalse((job / "evidence" / "changes.diff").exists())
        self.assertTrue((job / "out" / "verdict.json").exists())
        self.assertTrue((job / "BRIEF.md").exists())

    def test_job_redacts_secrets(self) -> None:
        event = make_event(
            title="sk-verysecret123 页面提示",
            payload={
                "quality": "ok",
                "frontend": {
                    "totals": {"manifest": 1},
                    "changed_files": [
                        {
                            "new_url": "https://chat.deepseek.com/a.js?token=supersecret",
                            "old_url": "https://chat.deepseek.com/a.js?api_key=anothersecret",
                        }
                    ],
                    "api_key": "plain-secret",
                },
                "changes": [{"token": "nested-secret"}],
            },
        )
        info = recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        self.assertLessEqual(info["bytes"], 5 * 1024 * 1024)
        joined = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.root / "job").rglob("*")
            if path.is_file()
        )
        for secret in ("verysecret123", "supersecret", "anothersecret", "plain-secret", "nested-secret"):
            self.assertNotIn(secret, joined)
        self.assertIn("[redacted]", joined)

    def test_job_trims_to_small_budget(self) -> None:
        event = make_event(changes_detail=[{"page": "chat", "diff": "x" * 500_000}])
        info = recheck_job.build_job(
            self.root / "job",
            event,
            events_dir=self.events,
            vault_dir=self.vault,
            max_bytes=120_000,
        )
        self.assertLessEqual(info["bytes"], 120_000)
        self.assertTrue((self.root / "job" / "out").is_dir())

    def test_job_files_are_readable_by_agent(self) -> None:
        event = make_event()
        job = self.root / "job"
        recheck_job.build_job(job, event, events_dir=self.events, vault_dir=self.vault)
        recheck_agent.prepare_job_permissions(job)
        self.assertEqual(stat.S_IMODE(job.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((job / "event.json").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((job / "evidence" / "frontend.json").stat().st_mode), 0o644)

    def test_job_keeps_analysis_and_strips_internal_records(self) -> None:
        event = make_event()
        event["payload"]["recheck"] = {"state": "done", "from": {"title": "x"}}
        event["payload"]["level_decision"] = {"level": 2}
        recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        payload = json.loads((self.root / "job" / "event.json").read_text(encoding="utf-8"))[
            "payload"
        ]
        self.assertNotIn("recheck", payload)
        self.assertNotIn("level_decision", payload)
        self.assertIn("analysis", payload)


class VerdictTests(RecheckCase):
    def test_accepts_valid_verdict(self) -> None:
        event = make_event()
        value = recheck_verdict.validate_verdict(make_verdict(event), event=event)
        self.assertEqual(value["verdict"], "confirmed")
        self.assertEqual(value["level_suggestion"]["suggested"], 2)

    def test_rejects_event_id_mismatch(self) -> None:
        event = make_event()
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(event, event_id="other"), event=event
            )

    def test_uncertain_cannot_change_copy(self) -> None:
        event = make_event()
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(event, verdict="uncertain", quality="partial", title="新标题"),
                event=event,
            )

    def test_contradicted_must_change_something(self) -> None:
        event = make_event()
        event["payload"]["quality"] = "repaired"
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(event, verdict="contradicted", quality="repaired"),
                event=event,
            )

    def test_rejects_unknown_evidence_host(self) -> None:
        event = make_event()
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(
                    event,
                    evidence=[{"url": "https://evil.example/x", "finding": "外部页面"}],
                ),
                event=event,
            )

    def test_rejects_internal_terms_and_model_names(self) -> None:
        event = make_event()
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(event, title="commit 发生变化"), event=event
            )
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(event, summary="由 example-provider 复核"), event=event
            )

    def test_major_needs_two_evidence(self) -> None:
        event = make_event()
        with self.assertRaises(recheck_verdict.VerdictError):
            recheck_verdict.validate_verdict(
                make_verdict(event, major_misjudgement=True), event=event
            )

    def test_load_verdict_reads_file(self) -> None:
        event = make_event()
        path = self.root / "verdict.json"
        path.write_text(json.dumps(make_verdict(event), ensure_ascii=False), encoding="utf-8")
        value = recheck_verdict.load_verdict(path, event=event)
        self.assertEqual(value["event_id"], event["id"])


class ApplyTests(RecheckCase):
    def test_only_whitelisted_fields_change(self) -> None:
        event = make_event()
        original = json.loads(json.dumps(event))
        verdict = make_verdict(
            event,
            title="对话页新增安全提示",
            summary="对话页新增一条安全提示，提醒用户保护账号。",
            impact="所有用户会看到新的安全提示。",
            action="阅读提示即可",
            quality="ok",
            unknown_field="should be ignored",
        )
        result = recheck_apply.apply_recheck(event, verdict, apply_level=False)
        self.assertIn("title", result["applied"])
        self.assertIn("summary", result["applied"])
        self.assertNotIn("unknown_field", event)
        self.assertEqual(event["level"], original["level"])
        self.assertEqual(event["level_label"], original["level_label"])
        self.assertEqual(event["payload"]["level_decision"], original["payload"]["level_decision"])
        self.assertEqual(event["payload"]["recheck"]["from"]["title"], original["title"])
        self.assertEqual(event["payload"]["recheck"]["from"]["summary"], original["summary"])

    def test_level_gate_off_by_default(self) -> None:
        event = make_event()
        verdict = make_verdict(
            event,
            verdict="contradicted",
            quality="repaired",
            title="登录入口出现调整",
            level_suggestion={
                "suggested": 3,
                "reason": "影响登录路径",
                "evidence": ["登录按钮位置变化", "登录提示文案变化"],
            },
            evidence=[
                {"url": "https://chat.deepseek.com/", "finding": "登录按钮位置变化"},
                {"url": "https://platform.deepseek.com/", "finding": "登录提示文案变化"},
            ],
        )
        result = recheck_apply.apply_recheck(event, verdict, apply_level=False)
        self.assertFalse(result["level_applied"])
        self.assertEqual(event["level"], 2)
        self.assertIn("开关关闭", result["level_gate"])

    def test_level_gate_applies_with_evidence(self) -> None:
        event = make_event()
        verdict = make_verdict(
            event,
            verdict="contradicted",
            quality="repaired",
            title="登录入口出现调整",
            level_suggestion={
                "suggested": 3,
                "reason": "影响登录路径",
                "evidence": ["登录按钮位置变化", "登录提示文案变化"],
            },
            evidence=[
                {"url": "https://chat.deepseek.com/", "finding": "登录按钮位置变化"},
                {"url": "https://platform.deepseek.com/", "finding": "登录提示文案变化"},
            ],
        )
        with mock.patch.dict(
            os.environ,
            {"RECHECK_APPLY_LEVEL": "1", "RECHECK_LEVEL_MIN_GAP": "1"},
        ):
            result = recheck_apply.apply_recheck(event, verdict)
        self.assertTrue(result["level_applied"])
        self.assertEqual(event["level"], 3)
        self.assertEqual(event["level_label"], "重要")
        self.assertEqual(event["payload"]["recheck"]["level_source"], "recheck")

    def test_revert_restores_original_fields(self) -> None:
        event = make_event()
        original = json.loads(json.dumps(event))
        verdict = make_verdict(event, title="新的标题", summary="新的摘要")
        recheck_apply.apply_recheck(event, verdict, apply_level=False)
        self.assertNotEqual(event["title"], original["title"])
        recheck_apply.revert_recheck(event)
        self.assertEqual(event["title"], original["title"])
        self.assertEqual(event["summary"], original["summary"])
        self.assertEqual(event["payload"]["recheck"]["from"]["title"], original["title"])

    def test_audit_state_transitions_are_legal(self) -> None:
        cases = [
            ("pending", make_verdict(make_event(), verdict="confirmed", quality="ok"), "final"),
            (
                "pending",
                make_verdict(
                    make_event(),
                    verdict="contradicted",
                    quality="repaired",
                    title="修正标题",
                ),
                "repaired",
            ),
            (
                "pending",
                make_verdict(make_event(), verdict="uncertain", quality="partial"),
                "partial",
            ),
        ]
        for current, verdict, expected in cases:
            event = make_event(quality_state=current)
            event["payload"]["quality_state"] = current
            recheck_apply.apply_recheck(event, verdict, apply_level=False)
            self.assertEqual(event["quality_state"], expected)
        for terminal in ("repaired", "partial", "final"):
            event = make_event(quality_state=terminal)
            event["payload"]["quality_state"] = terminal
            recheck_apply.apply_recheck(
                event, make_verdict(event, verdict="confirmed", quality="ok"), apply_level=False
            )
            self.assertEqual(event["quality_state"], terminal)

    def test_recheck_record_is_capped(self) -> None:
        event = make_event()
        verdict = make_verdict(
            event,
            reason="很长的理由" * 500,
            checks=["检查项" * 200 for _ in range(20)],
            limitations=["限制" * 200 for _ in range(20)],
            evidence=[
                {"url": "https://chat.deepseek.com/", "finding": "证据" * 300}
                for _ in range(8)
            ],
        )
        recheck_apply.apply_recheck(event, verdict, apply_level=False)
        body = json.dumps(event["payload"]["recheck"], ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(body), 8 * 1024)


class NotifyTests(RecheckCase):
    def _upgraded_event(self) -> dict:
        event = make_event()
        verdict = make_verdict(
            event,
            verdict="contradicted",
            quality="repaired",
            title="登录入口出现调整",
            level_suggestion={
                "suggested": 3,
                "reason": "影响登录路径",
                "evidence": ["登录按钮位置变化", "登录提示文案变化"],
            },
            evidence=[
                {"url": "https://chat.deepseek.com/", "finding": "登录按钮位置变化"},
                {"url": "https://platform.deepseek.com/", "finding": "登录提示文案变化"},
            ],
        )
        with mock.patch.dict(
            os.environ,
            {"RECHECK_APPLY_LEVEL": "1", "RECHECK_LEVEL_MIN_GAP": "1"},
        ):
            recheck_apply.apply_recheck(event, verdict)
        return event

    def test_non_upgrade_is_skipped(self) -> None:
        result = recheck_notify.notify_upgrade(
            make_event(), qq_enabled=True, mail_enabled=True
        )
        self.assertEqual(result["skipped"], "not_upgrade")
        self.assertFalse(result["attempted"])

    def test_upgrade_sends_qq_once(self) -> None:
        event = self._upgraded_event()
        calls = []

        def qq_sender(value):
            calls.append(value["id"])
            return {"sent": 1}

        result = recheck_notify.notify_upgrade(
            event, qq_enabled=True, mail_enabled=False, qq_sender=qq_sender
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["sent"], ["qq"])
        self.assertEqual(calls, [event["id"]])
        recheck_notify.apply_notification_result(event, result, now_iso="2026-11-24T03:00:00Z")
        self.assertEqual(event["payload"]["recheck"]["notified_at"], "2026-11-24T03:00:00Z")
        again = recheck_notify.notify_upgrade(
            event, qq_enabled=True, mail_enabled=False, qq_sender=qq_sender
        )
        self.assertEqual(again["skipped"], "already_notified")
        self.assertEqual(calls, [event["id"]])

    def test_partial_channel_failure_retries_only_missing_channel(self) -> None:
        event = self._upgraded_event()
        qq_calls = []
        mail_calls = []

        def qq_fail(value):
            qq_calls.append(value["id"])
            raise RuntimeError("qq down")

        def qq_ok(value):
            qq_calls.append(value["id"])
            return {"sent": 1}

        def mail_ok(value):
            mail_calls.append(value["id"])
            return (True, "")

        first = recheck_notify.notify_upgrade(
            event, qq_enabled=True, mail_enabled=True, qq_sender=qq_fail, mail_sender=mail_ok
        )
        self.assertFalse(first["complete"])
        self.assertIn("qq", " ".join(first["errors"]))
        self.assertNotIn("notified_at", event["payload"]["recheck"])
        second = recheck_notify.notify_upgrade(
            event, qq_enabled=True, mail_enabled=True, qq_sender=qq_ok, mail_sender=mail_ok
        )
        self.assertTrue(second["complete"])
        self.assertEqual(len(qq_calls), 2)
        self.assertEqual(len(mail_calls), 1)
        self.assertEqual(second["sent"], ["qq", "mail"])
        recheck_notify.apply_notification_result(event, second, now_iso="2026-11-24T03:00:00Z")
        self.assertIn("notified_at", event["payload"]["recheck"])

    def test_no_channels_marks_complete_without_sending(self) -> None:
        event = self._upgraded_event()
        result = recheck_notify.notify_upgrade(
            event, qq_enabled=False, mail_enabled=False
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["skipped"], "no_channels")
        recheck_notify.apply_notification_result(event, result, now_iso="2026-11-24T03:00:00Z")
        self.assertEqual(event["payload"]["recheck"]["notified_at"], "2026-11-24T03:00:00Z")
        self.assertEqual(event["payload"]["recheck"]["notified_channels"], [])

    def test_dry_run_does_not_call_senders(self) -> None:
        event = self._upgraded_event()
        called = []
        result = recheck_notify.notify_upgrade(
            event,
            dry_run=True,
            qq_enabled=True,
            mail_enabled=True,
            qq_sender=lambda value: called.append("qq"),
            mail_sender=lambda value: called.append("mail"),
        )
        self.assertEqual(result["skipped"], "dry_run")
        self.assertEqual(called, [])


class AgentTests(RecheckCase):
    def test_sandbox_command_contains_required_boundaries(self) -> None:
        job = self.root / "job"
        (job / "out").mkdir(parents=True)
        with mock.patch.object(recheck_agent, "_systemd_available", return_value=True), \
                mock.patch.object(recheck_agent, "resolve_uid_gid", return_value=(1000, 1000)):
            argv = recheck_agent.build_sandbox_command(
                job_dir=job, auth_root=self.root / "auth", prompt="hello"
            )
        joined = "\n".join(argv)
        self.assertIn("--uid=pi", joined)
        self.assertIn("--gid=pi", joined)
        self.assertIn("ProtectHome=yes", joined)
        self.assertIn("ProtectSystem=strict", joined)
        self.assertIn("/home/user/.claude", joined)
        self.assertIn("/home/user/.pi", joined)
        self.assertIn("/opt/llm-gateway/data", joined)
        self.assertIn(f"BindReadOnlyPaths={job}:/run/deepseek-recheck-job-", joined)
        self.assertIn(f"BindPaths={job / 'out'}:/run/deepseek-recheck-job-", joined)
        self.assertIn(
            "BindReadOnlyPaths=/var/lib/private/deepseek-webwatch:/run/recheck-data",
            joined,
        )
        self.assertIn("RECHECK_WEBWATCH_DIR=/run/recheck-data", joined)
        self.assertIn("WorkingDirectory=/run/deepseek-recheck-job-", joined)
        self.assertIn("-/var/lib/deepseek-recheck", joined)
        self.assertIn("--model", joined)
        self.assertIn("your-model-id", joined)
        self.assertIn("--tools", joined)
        self.assertIn("Read,Write,Glob,Grep", joined)
        self.assertIn("--disallowedTools", joined)
        for tool in ("Bash", "WebFetch", "WebSearch", "Task"):
            self.assertIn(tool, joined)
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1], "hello")
        self.assertNotIn("--dangerously-skip-permissions", joined)

    def test_sandbox_unavailable_raises(self) -> None:
        job = self.root / "job"
        (job / "out").mkdir(parents=True)
        with mock.patch.object(recheck_agent, "_systemd_available", return_value=False):
            with self.assertRaises(recheck_agent.SandboxUnavailable):
                recheck_agent.build_sandbox_command(
                    job_dir=job, auth_root=None, prompt="hello"
                )

    def test_auth_copy_only_allowlisted_regular_files(self) -> None:
        source = self.root / "agent-src"
        source.mkdir()
        (source / "settings.json").write_text("{}", encoding="utf-8")
        (source / "secrets.txt").write_text("nope", encoding="utf-8")
        outside = self.root / "outside.json"
        outside.write_text("nope", encoding="utf-8")
        try:
            (source / "linked.json").symlink_to(outside)
        except OSError:
            pass
        with mock.patch.object(cfg, "agent_home_config", return_value=None):
            auth = recheck_agent.prepare_agent_auth(
                source, runtime_parent=self.root / "run"
            )
        try:
            self.assertEqual(auth["files"], ["settings.json"])
            self.assertEqual(stat.S_IMODE((auth["root"] / "settings.json").stat().st_mode), 0o640)
            self.assertFalse((auth["root"] / "secrets.txt").exists())
            self.assertFalse((auth["root"] / "linked.json").exists())
        finally:
            recheck_agent.cleanup_agent_auth(auth)


class JobCompressionTests(RecheckCase):
    """复核 job 压缩路径：裁剪要落盘、blob 不残留、无 URL 锚点要精简。"""

    def test_main_entry_detection_handles_production_and_legacy_names(self) -> None:
        self.assertTrue(
            recheck_job._is_main_entry(
                {"key": "js:chat/static/main.js", "name": "chat/static/main.js"}
            )
        )
        self.assertTrue(recheck_job._is_main_entry({"key": "main_js", "name": "old-name"}))
        self.assertTrue(recheck_job._is_main_entry({"name": "static/main.min.js"}))
        self.assertFalse(recheck_job._is_main_entry({"key": "chunk:js:10025", "name": "10025"}))

    def test_last_resort_trim_is_written_to_disk(self) -> None:
        event = make_event()
        event["payload"]["frontend"] = dict(
            FRONTEND, snippets=["x" * 5_000] * 12
        )
        info = recheck_job.build_job(
            self.root / "job",
            event,
            events_dir=self.events,
            vault_dir=self.vault,
            max_bytes=80_000,
        )
        frontend = json.loads(
            (self.root / "job" / "evidence" / "frontend.json").read_text(encoding="utf-8")
        )
        self.assertLessEqual(len(frontend.get("snippets") or []), 5)
        self.assertLessEqual(info["bytes"], 80_000)

    def test_blob_files_directory_is_cleaned_between_budget_rounds(self) -> None:
        """两轮不同预算后 files/ 里不应残留上一轮的大文件。"""
        snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        snap.mkdir(parents=True)
        url = "https://chat.deepseek.com/static/main.js?v=2"
        old_url = "https://chat.deepseek.com/static/main.js?v=1"
        manifest = {
            "snapshot_id": snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "main_js_url": url,
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "x"},
            "files": [
                {"url": old_url, "blob": "b0", "hash": "h0", "kind": "js",
                 "status": "downloaded", "key": "js:chat/static/main.js", "name": "chat/static/main.js"},
                {"url": url, "blob": "b1", "hash": "h1", "kind": "js",
                 "status": "downloaded", "key": "js:chat/static/main.js", "name": "chat/static/main.js"},
            ],
        }
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event = make_event()
        event["payload"]["frontend"] = dict(FRONTEND, new_commit="abc12345")
        with mock.patch("webwatch.bundle_vault.BundleVault.read", return_value=b"x" * 300_000):
            info = recheck_job.build_job(
                self.root / "job",
                event,
                events_dir=self.events,
                vault_dir=self.vault,
                max_bytes=70_000,
            )
        files_dir = self.root / "job" / "evidence" / "files"
        total = sum(path.stat().st_size for path in files_dir.iterdir() if path.is_file())
        self.assertLessEqual(total, 70_000)
        self.assertLessEqual(info["bytes"], 70_000)

    def test_manifest_without_url_anchor_is_capped(self) -> None:
        snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        snap.mkdir(parents=True)
        files = [
            {"url": f"https://chat.deepseek.com/static/chunk-{i}.js", "blob": f"b{i}",
             "hash": f"h{i}", "kind": "js", "status": "downloaded",
             "key": f"chunk:js:{i}", "name": str(i)}
            for i in range(30)
        ]
        files.append(
            {"url": "https://chat.deepseek.com/static/main.js", "blob": "bm",
             "hash": "hm", "kind": "js", "status": "downloaded",
             "key": "js:chat/static/main.js", "name": "chat/static/main.js"}
        )
        manifest = {
            "snapshot_id": snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "x"},
            "files": files,
        }
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event = make_event()
        # 无 URL 锚点：清掉 changed_files 里的 new_url/old_url。
        event["payload"]["frontend"] = dict(
            FRONTEND, new_commit="abc12345", changed_files=[]
        )
        recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        evidence = json.loads(
            (self.root / "job" / "evidence" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["entries_total"], 31)
        self.assertEqual(len(evidence["entries"]), 12)
        self.assertEqual(evidence["entries"][0]["key"], "js:chat/static/main.js")
        self.assertEqual(evidence["manifest_scope"], "partial-no-url")
        brief = (self.root / "job" / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("仅保留最多 12 条主要条目", brief)

    def test_small_manifest_without_url_anchor_is_complete(self) -> None:
        snap = self.vault / "snapshots" / "chat" / "20261124T020000Z-abc12345"
        snap.mkdir(parents=True)
        files = [
            {"url": f"https://chat.deepseek.com/static/chunk-{i}.js", "blob": f"b{i}",
             "hash": f"h{i}", "kind": "js", "status": "downloaded",
             "key": f"chunk:js:{i}", "name": str(i)}
            for i in range(3)
        ]
        manifest = {
            "snapshot_id": snap.name,
            "complete": True,
            "build_meta": {"commitId": "abc12345"},
            "page": {"url": "https://chat.deepseek.com/", "html_sha256": "x"},
            "files": files,
        }
        (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        event = make_event()
        event["payload"]["frontend"] = dict(
            FRONTEND, new_commit="abc12345", changed_files=[]
        )
        recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        evidence = json.loads(
            (self.root / "job" / "evidence" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["manifest_scope"], "complete-no-url")
        self.assertEqual(len(evidence["entries"]), 3)
        brief = (self.root / "job" / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("已保留全部清单条目", brief)

    def test_missing_vault_explains_no_full_manifest(self) -> None:
        event = make_event()
        recheck_job.build_job(
            self.root / "job", event, events_dir=self.events, vault_dir=self.vault
        )
        evidence = json.loads(
            (self.root / "job" / "evidence" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertFalse(evidence["available"])
        brief = (self.root / "job" / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("版本仓库不可用", brief)
        self.assertIn("无法提供完整资源清单", brief)


if __name__ == "__main__":
    unittest.main()
