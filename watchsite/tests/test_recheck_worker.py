from __future__ import annotations

import json
import os
import stat
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from watchsite import recheck, recheck_agent, recheck_config as cfg, recheck_notify
from watchsite.tests.support import cleanup, temp_dir
from watchsite.tests.test_recheck import make_event, make_verdict


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_dir("recheck-worker-")
        self.addCleanup(cleanup, self.root)
        self.events = self.root / "events"
        self.state = self.root / "state"
        self.vault = self.root / "vault"
        self.push = self.root / "push"
        self.events.mkdir()
        self.state.mkdir()
        self.vault.mkdir()
        self.push.mkdir()
        env = mock.patch.dict(
            os.environ,
            {
                "RECHECK_EVENTS_DIR": str(self.events),
                "RECHECK_STATE_DIR": str(self.state),
                "RECHECK_VAULT_DIR": str(self.vault),
                "RECHECK_PUSH_STATE_DIR": str(self.push),
                "RECHECK_REQUIRE_PUSHED": "0",
                "RECHECK_APPLY_LEVEL": "0",
                "RECHECK_LEVEL_MIN_GAP": "2",
                "RECHECK_AGENT_HOME_CONFIG": "none",
                "RECHECK_MAX_ATTEMPTS": "3",
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
        self.now = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)

    def write_event(self, event: dict, name: str | None = None) -> Path:
        path = self.events / (name or f"{event.get('slug') or event.get('id')}.json")
        path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def queue(self) -> recheck.Queue:
        value = recheck.Queue(self.state / "queue.db")
        self.addCleanup(value.close)
        return value

    def enqueue(self, event: dict) -> tuple[recheck.Queue, Path]:
        path = self.write_event(event)
        queue = self.queue()
        recheck.scan(queue, self.events, now=self.now, baseline=False)
        return queue, path


class ScanTests(WorkerCase):
    def test_dry_run_is_read_only(self) -> None:
        self.write_event(make_event("e1"))
        stats = recheck.run_once(dry_run=True, now=self.now, agent_probe=lambda: False)
        self.assertEqual(stats["job"]["status"], "dry_run")
        self.assertEqual(stats["scan"]["baseline"], 1)  # 只是「本来会建基线」的统计
        self.assertFalse((self.state / "queue.db").exists())
        self.assertFalse((self.state / "state.json").exists())
        self.assertFalse((self.state / "jobs").exists())

    def test_init_baseline_rejects_missing_events_dir(self) -> None:
        missing = self.root / "missing-events"
        with mock.patch.dict(os.environ, {"RECHECK_EVENTS_DIR": str(missing)}):
            code = recheck.main(["--init-baseline"])
        self.assertEqual(code, 1)

    def test_push_state_missing_after_baseline_fails(self) -> None:
        with mock.patch.dict(os.environ, {"RECHECK_REQUIRE_PUSHED": "1"}):
            recheck.run_once(now=self.now, scan_only=True, agent_probe=lambda: False)
            self.write_event(make_event("e1"))
            stats = recheck.run_once(now=self.now, scan_only=True, agent_probe=lambda: False)
        self.assertEqual(stats.get("error"), "push-state missing")

    def test_first_run_baselines_existing_events(self) -> None:
        old = make_event("old")
        self.write_event(old)
        queue = self.queue()
        stats = recheck.scan(queue, self.events, now=self.now)
        self.assertEqual(stats["baseline"], 1)
        self.assertEqual(stats["enqueued"], 0)
        self.assertEqual(queue.get("old")["status"], "ignored")

        new = make_event("new")
        self.write_event(new)
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=5))
        self.assertEqual(stats["enqueued"], 1)
        self.assertEqual(queue.get("new")["status"], "queued")
        # 幂等：再扫一次不重复入队。
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=6))
        self.assertEqual(stats["enqueued"], 0)

    def test_audit_rewrite_does_not_requeue_history(self) -> None:
        event = make_event("old")
        path = self.write_event(event)
        queue = self.queue()
        recheck.scan(queue, self.events, now=self.now)
        self.assertEqual(queue.get("old")["status"], "ignored")

        # 审计只改 quality_state，不应让基线历史事件复活。
        event["quality_state"] = "repaired"
        path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=1))
        self.assertEqual(stats["requeued"], 0)
        self.assertEqual(queue.get("old")["status"], "ignored")

        # 真正的展示内容变化才重新入队。
        event["title"] = "新的标题"
        path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=2))
        self.assertEqual(stats["requeued"], 1)
        self.assertEqual(queue.get("old")["status"], "queued")

    def test_ineligible_events_are_skipped(self) -> None:
        self.write_event(make_event("docs", source="docs"))
        self.write_event(make_event("banner", kind="platform_banner"))
        no_frontend = make_event("no-front")
        no_frontend["payload"].pop("frontend")
        self.write_event(no_frontend)
        queue = self.queue()
        stats = recheck.scan(queue, self.events, now=self.now, baseline=False)
        self.assertEqual(stats["eligible"], 0)
        self.assertEqual(stats["enqueued"], 0)

    def test_scope_env_cannot_enable_docs(self) -> None:
        with mock.patch.dict(os.environ, {"RECHECK_SOURCES": "docs"}):
            self.assertEqual(cfg.sources(), ("chat", "platform", "harness"))
        self.write_event(make_event("docs", source="docs"))
        queue = self.queue()
        stats = recheck.scan(queue, self.events, now=self.now, baseline=False)
        self.assertEqual(stats["eligible"], 0)

    def test_done_event_is_requeued_when_content_changes(self) -> None:
        event = make_event("e1")
        queue, path = self.enqueue(event)
        runner = recheck_agent.FakeRunner(
            lambda job_dir, value: make_verdict(value, title="复核后的标题")
        )
        result = recheck.process_one(queue, runner, now=self.now)
        self.assertEqual(result["status"], "done")
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=1))
        self.assertEqual(stats["requeued"], 0)
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["title"] = "上游又改了一次"
        path.write_text(json.dumps(changed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=2))
        self.assertEqual(stats["requeued"], 1)


class ProcessTests(WorkerCase):
    def test_valid_verdict_updates_event_and_hash(self) -> None:
        event = make_event("e1")
        original = json.loads(json.dumps(event))
        queue, path = self.enqueue(event)
        runner = recheck_agent.FakeRunner(
            lambda job_dir, value: make_verdict(
                value,
                title="对话页新增安全提示",
                summary="对话页新增一条安全提示，提醒用户保护账号。",
            )
        )
        result = recheck.process_one(queue, runner, now=self.now)
        self.assertEqual(result["status"], "done")
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["title"], "对话页新增安全提示")
        self.assertEqual(updated["summary"], "对话页新增一条安全提示，提醒用户保护账号。")
        self.assertEqual(updated["payload"]["recheck"]["from"]["title"], original["title"])
        self.assertEqual(updated["level"], original["level"])
        self.assertEqual(queue.get("e1")["status"], "done")
        self.assertEqual(queue.get("e1")["event_hash"], recheck.content_hash(updated))
        # 写回后哈希已更新，下一轮扫描不能重复复核。
        stats = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=1))
        self.assertEqual(stats["requeued"], 0)

    def test_audit_rewrite_during_review_is_preserved(self) -> None:
        event = make_event("e1")
        queue, path = self.enqueue(event)

        def writer(job_dir, value):
            latest = json.loads(path.read_text(encoding="utf-8"))
            latest["payload"]["analysis"]["headline"] = "审计在复核期间写入的新解读"
            latest["payload"]["quality"] = "partial"
            latest["payload"]["quality_state"] = "repaired"
            path.write_text(
                json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return make_verdict(value, title="复核后的标题")

        result = recheck.process_one(queue, recheck_agent.FakeRunner(writer), now=self.now)
        self.assertEqual(result["status"], "done")
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["title"], "复核后的标题")
        self.assertEqual(
            updated["payload"]["analysis"]["headline"], "审计在复核期间写入的新解读"
        )
        # audit 在复核期间写的 quality / quality_state 不能被旧 verdict 覆盖。
        self.assertEqual(updated["payload"]["quality"], "partial")
        self.assertEqual(updated["payload"]["quality_state"], "repaired")

    def test_event_changed_during_review_discards_verdict(self) -> None:
        event = make_event("e1")
        queue, path = self.enqueue(event)

        def writer(job_dir, value):
            changed = json.loads(json.dumps(value))
            changed["title"] = "巡检写入的新标题"
            path.write_text(
                json.dumps(changed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return make_verdict(value, title="复核想写的标题")

        result = recheck.process_one(queue, recheck_agent.FakeRunner(writer), now=self.now)
        self.assertEqual(result["status"], "discarded")
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["title"], "巡检写入的新标题")
        record = queue.get("e1")
        self.assertEqual(record["status"], "queued")
        self.assertIn("旧 verdict", record["last_error"])
        self.assertEqual(record["attempts"], 0)

    def test_invalid_verdict_retries_three_times_then_fails(self) -> None:
        event = make_event("e1")
        original = json.loads(json.dumps(event))
        queue, path = self.enqueue(event)
        runner = recheck_agent.FakeRunner(
            lambda job_dir, value: make_verdict(value, evidence=[])
        )
        for index in range(3):
            result = recheck.process_one(
                queue, runner, now=self.now + timedelta(minutes=index)
            )
            self.assertIn(result["status"], ("queued", "failed"))
        record = queue.get("e1")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], 3)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_stale_verdict_is_not_reused(self) -> None:
        event = make_event("e1")
        original = json.loads(json.dumps(event))
        queue, path = self.enqueue(event)
        job_dir = recheck.job_dir_for(event, recheck.content_hash(event))
        (job_dir / "out").mkdir(parents=True, exist_ok=True)
        (job_dir / "out" / "verdict.json").write_text(
            json.dumps(make_verdict(event, title="上一轮的旧结论"), ensure_ascii=False),
            encoding="utf-8",
        )
        result = recheck.process_one(queue, recheck_agent.FakeRunner(), now=self.now)
        self.assertIn(result["status"], ("queued", "failed"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_sandbox_unavailable_fails_without_downgrade(self) -> None:
        event = make_event("e1")
        original = json.loads(json.dumps(event))
        queue, path = self.enqueue(event)

        class UnavailableRunner:
            def run(self, job_dir, value, *, timeout=None):
                raise recheck_agent.SandboxUnavailable("systemd-run missing")

        result = recheck.process_one(queue, UnavailableRunner(), now=self.now)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(queue.get("e1")["status"], "failed")
        self.assertEqual(queue.get("e1")["attempts"], 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_write_preserves_mode(self) -> None:
        event = make_event("e1")
        path = self.write_event(event)
        os.chmod(path, 0o640)
        before = path.stat()
        event["title"] = "新标题"
        recheck.write_event_preserving(path, event)
        after = path.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["title"], "新标题")

    def test_busy_probe_does_not_launch_agent(self) -> None:
        # 先建立空基线，避免新事件被当成历史。
        queue = self.queue()
        recheck.scan(queue, self.events, now=self.now)
        queue.close()
        self.write_event(make_event("e1"))
        runner = recheck_agent.FakeRunner(lambda job_dir, value: make_verdict(value))
        stats = recheck.run_once(
            runner=runner, now=self.now, agent_probe=lambda: True
        )
        self.assertEqual(stats["job"]["status"], "busy")
        self.assertEqual(runner.calls, [])
        queue = self.queue()
        self.assertEqual(queue.get("e1")["status"], "queued")

    def test_run_once_waits_for_successful_push(self) -> None:
        with mock.patch.dict(os.environ, {"RECHECK_REQUIRE_PUSHED": "1"}):
            recheck.run_once(now=self.now, scan_only=True, agent_probe=lambda: False)
            self.write_event(make_event("e1"))
            runner = recheck_agent.FakeRunner(lambda job_dir, value: make_verdict(value))
            blocked = recheck.run_once(runner=runner, now=self.now, agent_probe=lambda: False)
            self.assertEqual(blocked.get("error"), "push-state missing")
            self.assertEqual(runner.calls, [])
            # 模拟 watchsite 推送成功后写 push-state。
            (self.push / "push-state.json").write_text(
                json.dumps({"sent_ids": ["e1"], "sent_versions": {"e1": "digest"}}),
                encoding="utf-8",
            )
            second = recheck.run_once(runner=runner, now=self.now, agent_probe=lambda: False)
        self.assertEqual(second["job"]["status"], "done")
        self.assertEqual(len(runner.calls), 1)

    def test_run_once_end_to_end(self) -> None:
        # 首次空跑建立基线。
        recheck.run_once(now=self.now, scan_only=True, agent_probe=lambda: False)
        event = make_event("e1")
        self.write_event(event)
        runner = recheck_agent.FakeRunner(
            lambda job_dir, value: make_verdict(value, title="端到端修正标题")
        )
        stats = recheck.run_once(runner=runner, now=self.now, agent_probe=lambda: False)
        self.assertEqual(stats["job"]["status"], "done")
        updated = json.loads((self.events / "chat-e1.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["title"], "端到端修正标题")
        # 第二次运行不应重复处理。
        second = recheck.run_once(now=self.now + timedelta(minutes=1), agent_probe=lambda: False)
        self.assertEqual(second["job"]["status"], "empty")


class LatencyTests(WorkerCase):
    def test_start_latency_reads_push_state(self) -> None:
        push_dir = self.push
        pushed = self.now - timedelta(seconds=42)
        (push_dir / "state.json").write_text(
            json.dumps({"metrics": {"last_push_ts": iso(pushed)}}), encoding="utf-8"
        )
        path = self.write_event(make_event("e1"))
        with mock.patch.dict(os.environ, {"RECHECK_PUSH_STATE_DIR": str(push_dir)}):
            value = recheck._start_latency(path, self.now)
        self.assertEqual(value["push_ts"], int(pushed.timestamp()))
        self.assertAlmostEqual(value["push_age_seconds"], 42.0, delta=0.2)
        self.assertLessEqual(value["push_age_seconds"], 60)


class QueueTests(WorkerCase):
    def test_claim_attempts_and_retry(self) -> None:
        queue, _ = self.enqueue(make_event("e1"))
        record = queue.claim_next(self.now)
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["status"], "running")
        self.assertEqual(queue.mark_failed("e1", now=self.now, error="x"), "queued")
        record = queue.claim_next(self.now)
        self.assertEqual(record["attempts"], 2)
        queue.mark_failed("e1", now=self.now, error="x")
        record = queue.claim_next(self.now)
        self.assertEqual(record["attempts"], 3)
        self.assertEqual(queue.mark_failed("e1", now=self.now, error="x"), "failed")

    def test_stale_running_is_recovered(self) -> None:
        queue, _ = self.enqueue(make_event("e1"))
        queue.claim_next(self.now)
        recovered = queue.recover_stale(self.now + timedelta(days=3))
        self.assertEqual(recovered, 1)
        self.assertEqual(queue.get("e1")["status"], "queued")

    def test_cleanup_jobs_keeps_newest_and_respects_size(self) -> None:
        jobs = self.state / "jobs"
        for index in range(4):
            path = jobs / f"job-{index}"
            path.mkdir(parents=True)
            (path / "data.bin").write_bytes(b"x" * (index + 1) * 1000)
            stamp = (self.now - timedelta(days=index)).timestamp()
            os.utime(path, (stamp, stamp))
        stats = recheck.cleanup_jobs(
            jobs, now=self.now, keep=2, max_bytes=10**9, keep_days=3650
        )
        self.assertEqual(stats["removed"], 2)
        self.assertTrue((jobs / "job-0").exists())
        self.assertTrue((jobs / "job-1").exists())
        self.assertFalse((jobs / "job-3").exists())

    def test_cleanup_failed_writes_tombstone(self) -> None:
        queue = self.queue()
        for index in range(2):
            queue.insert(
                f"f{index}",
                event_path=self.events / f"chat-f{index}.json",
                event_hash_value=f"hash-{index}",
                source="chat",
                quality="ok",
                status="failed",
                now=self.now - timedelta(minutes=index),
            )
        with mock.patch.dict(os.environ, {"RECHECK_FAILED_KEEP": "1"}):
            stats = queue.cleanup(self.now)
        self.assertEqual(stats["failed"], 1)
        # 被删的失败记录必须留墓碑，下一轮不能重新入队。
        self.assertIsNotNone(queue.seen("f1"))
        self.assertIsNone(queue.get("f1"))

    def test_queue_cleanup_keeps_baseline_tombstone(self) -> None:
        self.write_event(make_event("old"))
        queue = self.queue()
        recheck.scan(queue, self.events, now=self.now)
        self.assertEqual(queue.get("old")["status"], "ignored")
        queue._write("old", status="ignored", now=self.now - timedelta(days=90), last_error="首次部署基线，不跑历史事件")
        stats = queue.cleanup(self.now)
        self.assertGreaterEqual(stats["archived"], 1)
        self.assertIsNotNone(queue.seen("old"))
        # 事件文件还在，但墓碑阻止它重新入队。
        again = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=1))
        self.assertEqual(again["enqueued"], 0)

    def test_queue_cleanup_archives_done_and_prevents_requeue(self) -> None:
        queue, _ = self.enqueue(make_event("e1"))
        queue.mark_done("e1", now=self.now - timedelta(days=90))
        stats = queue.cleanup(self.now)
        self.assertGreaterEqual(stats["done"], 1)
        self.assertIsNone(queue.get("e1"))
        self.assertIsNotNone(queue.seen("e1"))
        again = recheck.scan(queue, self.events, now=self.now + timedelta(minutes=1))
        self.assertEqual(again["enqueued"], 0)


class NotifyPendingTests(WorkerCase):
    def test_pending_upgrade_is_notified_and_written(self) -> None:
        event = make_event("e1")
        event["level"] = 3
        event["level_label"] = "重要"
        event["payload"]["recheck"] = {
            "state": "done",
            "level_applied": True,
            "from_level": 2,
            "to_level": 3,
            "applied": ["level"],
            "from": {"level": 2},
        }
        path = self.write_event(event)
        result = {
            "attempted": True,
            "complete": True,
            "channels": [],
            "sent": ["qq"],
            "skipped": "",
            "errors": [],
        }
        with mock.patch.object(recheck_notify, "notify_upgrade", return_value=result):
            stats = recheck.notify_pending(self.events, now=self.now)
        self.assertEqual(stats["notified"], 1)
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("notified_at", updated["payload"]["recheck"])
        self.assertEqual(updated["payload"]["recheck"]["notified_channels"], ["qq"])


if __name__ == "__main__":
    unittest.main()
