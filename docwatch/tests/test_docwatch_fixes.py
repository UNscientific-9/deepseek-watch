"""docwatch 修复回归自检。

运行：`python -m docwatch.tests.test_docwatch_fixes`
只用标准库 unittest；不发信、不联网、不启动 pi/模型。
覆盖：夜间合并保留模型解读、apply_llm 不改级、wake 超时参数、budget 检查、
      采集失败告警/健康标记、pending 队列与未通知补发、judge 只读与 auth 隔离。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docwatch import run, wake
from docwatch.collect import signals
from docwatch.render import mail
from docwatch.tests.support import cleanup as _cleanup_temp
from docwatch.tests.support import temp_dir as _make_temp_dir

PAGE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


@contextlib.contextmanager
def _tempdir():
    """系统临时目录（Windows 沙箱自动回退，见 tests/support.py）。"""
    path = _make_temp_dir("docwatch-test-")
    try:
        yield str(path)
    finally:
        _cleanup_temp(path)


def _verdict() -> dict:
    return {
        "level": 3,
        "level_source": "rule",
        "noise": False,
        "headline": "夜间输出价从 13.5 涨到 15 元",
        "what_changed": "pricing 页输出价 13.50 → 15.00 元/百万 tokens。",
        "why_matters": "按现有用量估算，高峰调用成本约 +11%。",
        "action": "下月起预算 +10%。",
        "evidence": ["diff/pricing.diff 第 3 行"],
        "model": "example-model-a",
        "cards": [{
            "level": 3,
            "page": PAGE,
            "bullets": ["正文改了 3 行"],
            "diff": ["－ 输出 13.50 元", "＋ 输出 15.00 元"],
        }],
    }


def _job(job_id: int = 101) -> dict:
    return {
        "id": job_id,
        "created_at": "2026-11-16T02:00:00+08:00",
        "mode": "high",
        "pages_total": 58,
        "pages_changed": 1,
        "assets_total": 24,
        "signals": [{"kind": "page_text", "page": PAGE,
                     "detail": "正文变了：+1 行 / -1 行（共 30 行，80 字改动）"}],
    }


class NightDigestTests(unittest.TestCase):
    def test_night_digest_keeps_verdict_interpretation(self):
        with _tempdir() as td:
            data_dir = Path(td)
            verdict, job = _verdict(), _job()
            out = run.render(verdict, job)
            self.assertTrue(run.hold(data_dir, verdict, job, out=out))
            captured = {}

            def fake_send(subject, text, html):
                captured.update(subject=subject, text=text, html=html)
                return True

            with mock.patch.object(run, "send_mail", side_effect=fake_send):
                sent = run.flush(data_dir)
            self.assertEqual(sent, 1)
            blob = captured["text"] + captured["html"]
            for key in ("headline", "what_changed", "why_matters", "action"):
                self.assertIn(verdict[key], blob, f"夜间合并信缺 {key}")
            self.assertEqual(list((data_dir / "queue").glob("*.json")), [],
                             "发送成功后队列应清空")

    def test_night_digest_render_fallback_keeps_queue(self):
        with _tempdir() as td:
            data_dir = Path(td)
            run.hold(data_dir, _verdict(), _job(), out=run.render(_verdict(), _job()))
            before = list((data_dir / "queue").glob("*.json"))
            self.assertEqual(len(before), 1)
            fake_out = {"subject": "s", "text": "t", "html": "h"}
            with mock.patch.object(run, "render_with_status", return_value=(fake_out, True)), \
                    mock.patch.object(run, "send_mail", return_value=True):
                sent = run.flush(data_dir)
            self.assertEqual(sent, 0)
            self.assertEqual(len(list((data_dir / "queue").glob("*.json"))), 1,
                             "排版走兜底时不能删队列")
            self.assertTrue((data_dir / "queue").is_dir())

    def test_hold_saves_rendered_out(self):
        with _tempdir() as td:
            data_dir = Path(td)
            out = {"subject": "主题", "text": "正文", "html": "<p>正文</p>"}
            run.hold(data_dir, _verdict(), _job(), out=out, retry=True)
            path = next((data_dir / "queue").glob("*.json"))
            item = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(item["out"]["subject"], "主题")
            self.assertTrue(item["retry"])


class FlushRetryTests(unittest.TestCase):
    def test_retry_sent_even_when_night_included_off(self):
        """静默期只补发 retry 项，夜间积压保留。"""
        with _tempdir() as td:
            data_dir = Path(td)
            run.hold(data_dir, _verdict(), _job(1), out={"subject": "重试", "text": "x", "html": ""},
                     retry=True)
            run.hold(data_dir, _verdict(), _job(2), out=run.render(_verdict(), _job(2)))
            subjects = []

            def fake_send(subject, text, html):
                subjects.append(subject)
                return True

            with mock.patch.object(run, "send_mail", side_effect=fake_send):
                sent = run.flush(data_dir, include_night=False)
            self.assertEqual(sent, 1)
            self.assertEqual(subjects, ["重试"])
            self.assertEqual(len(list((data_dir / "queue").glob("*.json"))), 1)


class WakeBudgetTests(unittest.TestCase):
    def test_wake_cmd_passes_timeout_and_budget(self):
        cmd = run._wake_cmd(Path("job"), Path("data"), 2, True, Path("verdict.json"),
                            timeout=300, budget=720)
        self.assertIn("--timeout", cmd)
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "300")
        self.assertIn("--budget", cmd)
        self.assertEqual(cmd[cmd.index("--budget") + 1], "720")
        self.assertLess(run.WAKE_BUDGET, run.WAKE_TIMEOUT)

    def test_wake_passes_pi_timeout_not_outer_timeout(self):
        """run.py 给 wake.py 的 --timeout 是单次 pi 上限，不是外层 780s 硬上限。"""
        with _tempdir() as td:
            job_dir = Path(td)
            calls = []

            def fake_cmd(*args, **kwargs):
                calls.append(kwargs)
                return ["wake"]

            with mock.patch.object(run, "_wake_cmd", side_effect=fake_cmd), \
                    mock.patch.object(run, "_run_process_group", return_value=0), \
                    mock.patch.object(run, "_read_verdict", return_value={"headline": "h"}):
                run.wake({"evidence_dir": str(job_dir)}, 2, False, data_dir=job_dir)
            self.assertEqual(calls[0]["timeout"], run.WAKE_PI_TIMEOUT)
            self.assertEqual(calls[0]["budget"], run.WAKE_BUDGET)
            self.assertLess(run.WAKE_PI_TIMEOUT, run.WAKE_TIMEOUT)

    def test_has_budget(self):
        self.assertFalse(wake.has_budget(time.monotonic() - 100, 10))
        self.assertTrue(wake.has_budget(time.monotonic(), 1000))

    def test_strict_retry_skipped_when_budget_exhausted(self):
        """预算为 0 时第一次解析失败不再重试，且仍产出兜底 verdict。"""
        with _tempdir() as td:
            tmp = Path(td)
            job_dir = tmp / "job"
            job_dir.mkdir()
            (job_dir / "manifest.json").write_text(json.dumps({
                "id": 7, "created_at": "2026-11-16T02:00:00+08:00", "mode": "high",
                "pages_total": 58, "pages_changed": 1, "signals": [],
            }), encoding="utf-8")
            agent_src = tmp / "no-agent"
            calls = []

            def fake_run_judge(*args, **kwargs):
                calls.append(kwargs.get("label"))
                return ({"final_text": ""},
                        {"rc": 0, "elapsed_ms": 1, "timed_out": False, "usage": {},
                         "tool_calls": [], "turns": 0,
                         "raw_path": str(job_dir / "pi-raw.ndjson"), "sandbox": "none",
                         "session_id": "x"},
                        "")

            argv = ["--job", str(job_dir), "--data-dir", str(tmp), "--rule-level", "2",
                    "--budget", "0", "--timeout", "5", "--pi", "no-such-pi",
                    "--sandbox", "none", "--agent-src", str(agent_src),
                    "--raw-keep", "none", "--json"]
            with mock.patch.object(wake, "run_judge", side_effect=fake_run_judge), \
                    mock.patch("sys.stdout", new=io.StringIO()):
                rc = wake.main(argv)
            self.assertEqual(rc, 0)
            self.assertEqual(calls, ["judge"], "预算耗尽时不应再跑严格重试")
            verdict = json.loads((job_dir / "verdict.json").read_text(encoding="utf-8"))
            self.assertTrue(verdict.get("degraded"))
            self.assertEqual(verdict["level"], 2)
            self.assertNotIn("draft", verdict)


class QueueRecoveryTests(unittest.TestCase):
    def test_recover_empty_job_json_marks_attention_not_notified(self):
        """空 job_json 不能当成已通知；否则一次中断会静默丢掉一条变化。"""
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            job = _job(56)
            job["id"] = store.insert_job(job)
            store.update_job(job)
            store.mark_job_ready(job["id"])
            store.conn.execute(
                "UPDATE v2_jobs SET job_json = '', notified = 0 WHERE id = ?",
                (job["id"],),
            )
            store.conn.commit()
            store.close()
            with mock.patch.object(run, "send_mail") as sender:
                count = run.recover_unnotified_jobs(data_dir)
            self.assertEqual(count, 0)
            sender.assert_not_called()
            con = sqlite3.connect(data_dir / "docs.db")
            notified = con.execute(
                "SELECT notified FROM v2_jobs WHERE id = ?", (job["id"],)
            ).fetchone()[0]
            con.close()
            self.assertEqual(notified, 2)

    def test_recover_skips_job_before_snapshot_commit(self):
        """快照提交前被杀：作业 ready=0，不能补发，否则下一轮会重复发同一变化。"""
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            job = _job(57)
            job["id"] = store.insert_job(job)
            store.update_job(job)
            store.close()
            with mock.patch.object(run, "send_mail") as sender:
                count = run.recover_unnotified_jobs(data_dir)
            self.assertEqual(count, 0)
            sender.assert_not_called()

    def test_stage_pending_is_sendable(self):
        with _tempdir() as td:
            data_dir = Path(td)
            job = _job(33)
            out = run.render(_verdict(), job)
            path = run.stage_pending(data_dir, _verdict(), job, out, night=False)
            self.assertIsNotNone(path)
            item = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(item["pending"])
            self.assertEqual(item["job_id"], 33)
            self.assertTrue(item["out"]["subject"])
            sent = []
            with mock.patch.object(run, "send_mail",
                                   side_effect=lambda s, t, h: sent.append(s) or True):
                self.assertEqual(run.flush(data_dir), 1)
            self.assertEqual(len(sent), 1)
            self.assertFalse(path.exists(), "发送成功后 pending 应删除")

    def test_recover_unnotified_jobs(self):
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            job = _job(55)
            job["id"] = store.insert_job(job)
            store.update_job(job)
            store.mark_job_ready(job["id"])
            store.close()
            sent = []
            # 23:00 后是夜间静默，recover 会把邮件入队而不是直发；这里固定成白天，
            # 让测试与运行时刻无关。
            with mock.patch.object(run, "send_mail",
                                   side_effect=lambda s, t, h: sent.append(s) or True), \
                    mock.patch.object(run, "is_quiet", return_value=False):
                count = run.recover_unnotified_jobs(data_dir)
            self.assertEqual(count, 1)
            self.assertEqual(len(sent), 1)
            con = sqlite3.connect(data_dir / "docs.db")
            notified = con.execute("SELECT notified FROM v2_jobs WHERE id = ?",
                                   (job["id"],)).fetchone()[0]
            con.close()
            self.assertEqual(notified, 1)


class HealthAlertTests(unittest.TestCase):
    def _db_with_fail(self, data_dir: Path) -> None:
        store = signals.Store(data_dir)
        store.conn.execute(
            "INSERT INTO v2_pages(url, fail_streak, last_error) VALUES(?, ?, ?)",
            (PAGE, 5, "HTTP 500"))
        store.conn.commit()
        store.close()

    def test_health_not_marked_when_mail_fails(self):
        with _tempdir() as td:
            data_dir = Path(td)
            self._db_with_fail(data_dir)
            with mock.patch.object(run, "send_mail", return_value=False):
                run._check_health(data_dir, {"signals": []})
            self.assertFalse((data_dir / "health-last").is_file(),
                             "只有 send_mail 真正成功才写 health-last")
            alerts = [json.loads(p.read_text(encoding="utf-8"))
                      for p in (data_dir / "queue").glob("*.json")]
            self.assertTrue(any(item.get("alert") == "health" for item in alerts))

    def test_health_marked_after_success(self):
        with _tempdir() as td:
            data_dir = Path(td)
            self._db_with_fail(data_dir)
            with mock.patch.object(run, "send_mail", return_value=True):
                run._check_health(data_dir, {"signals": []})
            self.assertTrue((data_dir / "health-last").is_file())

    def test_collect_failure_alert_bypasses_quiet_and_marks(self):
        with _tempdir() as td:
            data_dir = Path(td)
            sent = []
            with mock.patch.object(run, "send_mail",
                                   side_effect=lambda s, t, h: sent.append(s) or True):
                ok = run.send_collect_failure_alert(data_dir, RuntimeError("db locked"))
            self.assertTrue(ok)
            self.assertEqual(len(sent), 1)
            self.assertTrue((data_dir / "collect-fail-last").is_file())
            # 冷却期内第二次不再发
            with mock.patch.object(run, "send_mail", return_value=True) as second:
                self.assertFalse(run.send_collect_failure_alert(data_dir, RuntimeError("db locked")))
                second.assert_not_called()


class WakeSecurityTests(unittest.TestCase):
    def test_pi_argv_always_read_only(self):
        args = SimpleNamespace(pi="pi", provider="p", model="m", thinking="high",
                               keep_session=False)
        for review in (False, True):
            argv = wake.pi_argv("hi", "sid", args, review=review)
            self.assertIn("--tools", argv)
            self.assertEqual(argv[argv.index("--tools") + 1], "read")
            self.assertIn("--no-mcp", argv)

    def test_sandbox_hides_credentials_from_model(self):
        text = Path(wake.__file__).read_text(encoding="utf-8")
        self.assertIn("ProtectHome=yes", text)
        self.assertIn("InaccessiblePaths=-/opt/llm-gateway/data", text)
        self.assertIn("InaccessiblePaths=", text)
        self.assertIn("/opt/docwatch-v2/data/docs.db", text)

    def test_sandbox_argv_contains_required_isolation(self):
        """直接检查最终 argv；只在源码里留常量字符串不算数。"""
        with _tempdir() as td:
            job_dir = Path(td) / "jobs" / "9"
            job_dir.mkdir(parents=True)
            auth_root = Path(td) / "pi-auth"
            auth_root.mkdir()
            args = SimpleNamespace(
                sandbox="systemd", pi_user="pi", pi_group="pi",
                memory_high="256M", memory_max="512M", cpu_quota="50%",
                timeout=120, private_tmp=True,
            )
            with mock.patch.object(wake.shutil, "which", return_value="/usr/bin/systemd-run"), \
                    mock.patch.object(wake.os, "geteuid", create=True, return_value=0):
                argv, used = wake.build_command(
                    {"DEEPSEEK_API_KEY": "x"}, ["pi", "run"], job_dir,
                    args, unit="docwatch-test", auth_root=auth_root,
                )
        self.assertEqual(used, "systemd")
        joined = "\n".join(argv)
        self.assertIn("--property=ProtectHome=yes", joined)
        self.assertIn("--property=ProtectSystem=strict", joined)
        self.assertIn("/opt/docwatch-v2/data/docs.db", joined)
        self.assertIn("/opt/docwatch-v2/env", joined)
        self.assertIn("/opt/docwatch-v2/data/pi-auth", joined)
        self.assertIn("--property=BindReadOnlyPaths=" + str(job_dir), joined)
        self.assertIn("--property=BindPaths=" + str(job_dir / "out"), joined)
        self.assertIn("--property=BindPaths=" + str(auth_root), joined)
        self.assertIn("--uid=pi", argv)

    def test_isolated_auth_keeps_credentials_out_of_job_dir(self):
        """root + RuntimeDirectory 时，pi 的 agent 目录就是私有 auth_root。"""
        with _tempdir() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            job_dir = data_dir / "jobs" / "11"
            job_dir.mkdir(parents=True)
            src = tmp / "agent"
            src.mkdir()
            (src / "models.json").write_text("{}", encoding="utf-8")
            (src / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
            runtime = tmp / "run"
            runtime.mkdir()
            args = SimpleNamespace(
                data_dir=str(data_dir), agent_src=str(src),
                agent_md=str(Path(wake.__file__).with_name("AGENT.md")),
                provider="llm", model="m", pi="pi", max_steps=12,
                toolbox=None, pi_user="pi", pi_group="pi",
                sandbox="systemd",
            )
            with mock.patch.object(wake.os, "geteuid", create=True, return_value=0), \
                    mock.patch.object(wake.shutil, "which", return_value="/usr/bin/systemd-run"), \
                    mock.patch.object(wake.os, "chown", create=True), \
                    mock.patch.dict(wake.os.environ, {"RUNTIME_DIRECTORY": str(runtime)}):
                work = wake.prepare_workspace(job_dir, {"id": 11}, args)
            auth_path = Path(work["auth_path"])
            self.assertTrue(str(auth_path).startswith(str(runtime)))
            self.assertEqual(Path(work["agent_dir"]), auth_path.parent)
            self.assertFalse((job_dir / "out" / ".pi-agent" / "auth.json").exists())
            self.assertTrue((auth_path.parent / "models.json").is_file())
            wake.cleanup_workspace(job_dir, work, remove_config=True)

    def test_auth_outside_job_dir_and_cleaned(self):
        with _tempdir() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            job_dir = data_dir / "jobs" / "9"
            job_dir.mkdir(parents=True)
            src = tmp / "agent"
            src.mkdir()
            (src / "models.json").write_text("{}", encoding="utf-8")
            (src / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
            args = SimpleNamespace(
                data_dir=str(data_dir), agent_src=str(src),
                agent_md=str(Path(wake.__file__).with_name("AGENT.md")),
                provider="llm", model="m", pi="pi", max_steps=12,
                toolbox=None, pi_user="", pi_group="")
            work = wake.prepare_workspace(job_dir, {"id": 9}, args)
            auth_path = Path(work["auth_path"])
            self.assertTrue(auth_path.is_file())
            self.assertFalse(str(auth_path).startswith(str(job_dir)),
                             "auth.json 必须在可写作业目录之外")
            wake.cleanup_workspace(job_dir, work, remove_config=True)
            self.assertFalse(auth_path.exists())
            self.assertFalse(Path(work["agent_dir"]).exists())

    @unittest.skipUnless(os.name == "posix", "POSIX 权限位语义")
    def test_auth_root_group_writable_for_pi_lock(self):
        """pi 1.5.2 要在 auth.json 同目录建 <auth>.lock，目录必须 group 可写。"""
        with _tempdir() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            job_dir = data_dir / "jobs" / "10"
            job_dir.mkdir(parents=True)
            agent_dir = job_dir / "out" / ".pi-agent"
            agent_dir.mkdir(parents=True)
            src = tmp / "agent"
            src.mkdir()
            (src / "auth.json").write_text('{"token":"x"}', encoding="utf-8")
            runtime = tmp / "run"
            runtime.mkdir()
            args = SimpleNamespace(data_dir=str(data_dir), pi_user="pi", pi_group="pi",
                                   sandbox="none")
            with mock.patch.object(wake.os, "geteuid", return_value=0), \
                    mock.patch.object(wake.os, "chown"), \
                    mock.patch.dict(wake.os.environ, {"RUNTIME_DIRECTORY": str(runtime)}), \
                    mock.patch.object(wake, "resolve_uid_gid",
                                      return_value=(os.getuid(), os.getgid())):
                work = wake._prepare_auth_copy(src, job_dir, args)
            root = Path(work["auth_root"])
            target = Path(work["auth_path"])
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o770)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)


    def test_auth_symlink_is_rejected(self):
        """P1-01：pi 可写目录里的 auth.json 符号链接不能被 root 跟随复制。"""
        if os.name != "posix":
            self.skipTest("POSIX 符号链接语义")
        with _tempdir() as td:
            tmp = Path(td)
            job_dir = tmp / "data" / "jobs" / "12"
            job_dir.mkdir(parents=True)
            src = tmp / "agent"
            src.mkdir()
            victim = tmp / "victim.txt"
            victim.write_text("TOP-SECRET", encoding="utf-8")
            os.symlink(victim, src / "auth.json")
            args = SimpleNamespace(data_dir=str(tmp / "data"), sandbox="none",
                                   pi_user="", pi_group="")
            work = wake._prepare_auth_copy(src, job_dir, args)
            self.assertIsNone(work.get("auth_path"))
            self.assertFalse((tmp / "data" / "pi-auth").exists())

    def test_models_json_is_sanitized_and_mcp_skipped(self):
        """P1-03：models.json 只保留当前 provider，真实 apiKey 不复制；mcp 不复制。"""
        with _tempdir() as td:
            tmp = Path(td)
            job_dir = tmp / "data" / "jobs" / "13"
            job_dir.mkdir(parents=True)
            src = tmp / "agent"
            src.mkdir()
            secret = "oc_" + "a" * 48
            models = {
                "$schema": "https://example.invalid/schema.json",
                "providers": {
                    "llm": {"baseUrl": "http://127.0.0.1:3425", "api": "openai-completions",
                               "apiKey": "llm", "models": []},
                    "other-provider": {"baseUrl": "https://example.invalid", "api": "openai-completions",
                                    "apiKey": secret, "models": []},
                },
            }
            mcp = {"mcpServers": {"tavily": {"headers": {"Authorization": "Bearer " + "b" * 40}}}}
            (src / "models.json").write_text(json.dumps(models), encoding="utf-8")
            (src / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
            (src / "models-store.json").write_text("{}", encoding="utf-8")
            args = SimpleNamespace(data_dir=str(tmp / "data"), agent_src=str(src),
                                   provider="llm", sandbox="none", pi_user="", pi_group="",
                                   agent_md=str(Path(wake.__file__).with_name("AGENT.md")),
                                   max_steps=12, toolbox="", pi="/usr/bin/pi", model="m")
            work = wake.prepare_workspace(job_dir, {"id": 13}, args)
            copied = (work["agent_dir"] / "models.json").read_text(encoding="utf-8")
            self.assertIn("llm", copied)
            self.assertNotIn("oc_", copied)
            self.assertNotIn("other-provider", copied)
            self.assertFalse((work["agent_dir"] / "mcp.json").exists())
            self.assertFalse((work["agent_dir"] / "models-store.json").exists())
            self.assertIn(secret, work.get("extra_secrets") or [])

    def test_prepare_workspace_chowns_only_out_dir(self):
        """P1-02/B-9：job_dir 保持 root，只把 out/ 交给 pi。"""
        with _tempdir() as td:
            tmp = Path(td)
            job_dir = tmp / "data" / "jobs" / "14"
            job_dir.mkdir(parents=True)
            src = tmp / "agent"
            src.mkdir()
            (src / "models.json").write_text('{"providers":{}}', encoding="utf-8")
            args = SimpleNamespace(data_dir=str(tmp / "data"), agent_src=str(src),
                                   agent_md=str(Path(wake.__file__).with_name("AGENT.md")),
                                   provider="llm", sandbox="none", pi_user="pi", pi_group="pi",
                                   max_steps=12, toolbox="", pi="/usr/bin/pi", model="m")
            calls = []

            def fake_chown(path, user, group=""):
                calls.append((Path(path), user, group))
                return True

            with mock.patch.object(wake.os, "geteuid", create=True, return_value=0), \
                    mock.patch.object(wake.os, "chown", create=True), \
                    mock.patch.object(wake, "chown_job", side_effect=fake_chown):
                wake.prepare_workspace(job_dir, {"id": 14}, args)
            self.assertEqual([item[0] for item in calls], [job_dir / "out"])
            self.assertEqual(calls[0][1], "pi")

    def test_scrub_secrets_covers_third_party_keys(self):
        text = "key=oc_" + "c" * 48
        cleaned, hits = wake.scrub_secrets(text, [])
        self.assertNotIn("oc_", cleaned)
        self.assertGreaterEqual(hits, 1)


class MainFlowTests(unittest.TestCase):
    def test_main_stages_pending_then_cleans_after_send(self):
        with _tempdir() as td:
            data_dir = Path(td)
            job = _job(77)
            job["baseline"] = False
            job["evidence_dir"] = ""
            sent = []

            with mock.patch.object(run, "run_once", return_value=dict(job)), \
                    mock.patch.object(run, "send_mail",
                                      side_effect=lambda s, t, h: sent.append(s) or True), \
                    mock.patch.object(run, "is_quiet", return_value=False), \
                    mock.patch.object(run, "send_heartbeat", return_value=False):
                rc = run.main(["--data-dir", str(data_dir), "--no-llm", "--now"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(sent), 1)
            self.assertEqual(list((data_dir / "queue").glob("*.json")), [],
                             "发送成功后 pending 必须删除")

    def test_main_collect_exception_alerts_and_heartbeats(self):
        with _tempdir() as td:
            data_dir = Path(td)
            with mock.patch.object(run, "run_once", side_effect=RuntimeError("db locked")), \
                    mock.patch.object(run, "send_collect_failure_alert") as alert, \
                    mock.patch.object(run, "send_heartbeat") as heartbeat:
                rc = run.main(["--data-dir", str(data_dir), "--now"])
            self.assertEqual(rc, 1)
            alert.assert_called_once()
            heartbeat.assert_called_once()


class ReviewTriStateTests(unittest.TestCase):
    def test_review_pass_requires_explicit_boolean(self):
        self.assertIs(wake.review_pass(True), True)
        self.assertIs(wake.review_pass(False), False)
        self.assertIs(wake.review_pass("yes"), True)
        self.assertIs(wake.review_pass("false"), False)
        # 漏字段 / 非法值必须视为「未完成复核」，不能默认通过。
        self.assertIsNone(wake.review_pass(None))
        self.assertIsNone(wake.review_pass("unknown"))
        self.assertIsNone(wake.review_pass({"pass": True}))

    def test_summary_distinguishes_unfinished_review_from_failed(self):
        cases = [
            (True, "AI 复核通过"),
            (False, "AI 复核未通过，仅供参考"),
            (None, "AI 复核未完成"),
        ]
        for value, expected in cases:
            out = mail._summary_evidence({"review": {"pass": value}, "cards": []}, [])
            self.assertIn(expected, out, value)


class ReadyMigrationTests(unittest.TestCase):
    """P2-21：旧库 ALTER 默认值不能把新作业直接标成 ready。"""

    OLD_SCHEMA = """
        CREATE TABLE v2_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, mode TEXT, baseline INTEGER DEFAULT 0,
            pages_total INTEGER, pages_changed INTEGER,
            assets_total INTEGER, assets_changed INTEGER,
            n_signals INTEGER, n_errors INTEGER, duration_ms INTEGER,
            wire_bytes INTEGER, evidence_dir TEXT, job_json TEXT
        )
    """

    def _make_old_db(self, data_dir: Path) -> None:
        con = sqlite3.connect(data_dir / "docs.db")
        con.executescript(self.OLD_SCHEMA)
        con.execute("INSERT INTO v2_jobs(created_at, mode, n_signals, job_json)"
                    " VALUES('2026-11-01T00:00:00+08:00','high',0,'{}')")
        con.commit()
        con.close()

    def test_store_upgrade_marks_only_old_jobs_ready(self):
        with _tempdir() as td:
            data_dir = Path(td)
            self._make_old_db(data_dir)
            store = signals.Store(data_dir)
            rows = dict(store.conn.execute("SELECT id, ready FROM v2_jobs").fetchall())
            self.assertEqual(rows[1], 1, "迁移前的老作业应视为 ready")
            job = _job(2)
            job["id"] = store.insert_job(job)
            ready = store.conn.execute(
                "SELECT ready FROM v2_jobs WHERE id = ?", (job["id"],)).fetchone()[0]
            self.assertEqual(ready, 0, "迁移后新插入的作业必须 ready=0")
            store.close()

    def test_recover_upgrade_marks_only_old_jobs_ready(self):
        with _tempdir() as td:
            data_dir = Path(td)
            self._make_old_db(data_dir)
            run.recover_unnotified_jobs(data_dir)  # 先走 run.py 的独立迁移路径
            con = sqlite3.connect(data_dir / "docs.db")
            old_ready = con.execute("SELECT ready FROM v2_jobs WHERE id=1").fetchone()[0]
            con.close()
            self.assertEqual(old_ready, 1)
            store = signals.Store(data_dir)
            job = _job(3)
            job["id"] = store.insert_job(job)
            ready = store.conn.execute(
                "SELECT ready FROM v2_jobs WHERE id = ?", (job["id"],)).fetchone()[0]
            self.assertEqual(ready, 0)
            store.close()


class SnapshotTransactionTests(unittest.TestCase):
    """P2-22：快照和 ready=1 必须同一事务，失败时下一轮还能重新 diff。"""

    def _page_rec(self, url: str) -> dict:
        return signals.parse_page(
            url, f"<html><head><title>{url}</title></head>"
                 f"<body><article><p>{url} 正文</p></article></body></html>", url)

    def test_save_snapshots_sets_ready_in_same_commit(self):
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            job = _job(71)
            job["id"] = store.insert_job(job)
            url = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
            rec = self._page_rec(url)
            new = {"ts": "2026-11-18T10:00:00+08:00", "pages": {url: rec}, "assets": {},
                   "page_list": [url], "sitemap": [url], "partial": False}
            store.save_snapshots(None, new, {}, ready_job_id=job["id"])
            self.assertEqual(store.conn.execute(
                "SELECT COUNT(*) FROM v2_pages").fetchone()[0], 1)
            self.assertEqual(store.conn.execute(
                "SELECT ready FROM v2_jobs WHERE id=?", (job["id"],)).fetchone()[0], 1)
            store.close()

    def test_save_snapshots_failure_rolls_back_and_keeps_ready_zero(self):
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            job = _job(72)
            job["id"] = store.insert_job(job)
            url = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
            rec = self._page_rec(url)
            bad = dict(rec)
            bad["refs"] = {"js": {url}}  # set 不能 JSON 序列化，模拟落库中途异常
            new = {"ts": "2026-11-18T10:00:00+08:00",
                   "pages": {url: rec, url + "?bad/": bad}, "assets": {},
                   "page_list": [url], "sitemap": [url], "partial": False}
            with self.assertRaises(TypeError):
                store.save_snapshots(None, new, {}, ready_job_id=job["id"])
            # 不手动 rollback：save_snapshots 自己必须回滚，别靠调用方。
            self.assertEqual(store.conn.execute(
                "SELECT COUNT(*) FROM v2_pages").fetchone()[0], 0,
                "失败的快照不能落一半")
            self.assertEqual(store.conn.execute(
                "SELECT ready FROM v2_jobs WHERE id=?", (job["id"],)).fetchone()[0], 0,
                "快照失败时作业不能标 ready")
            # 下一轮仍能从旧快照（空）重新 diff 出变化，不会永久丢失。
            retry_sigs = signals.diff_snapshot(
                {"pages": {}, "assets": {}},
                {"pages": {url: rec}, "assets": {}, "sitemap": [url],
                 "page_list": [url], "partial": False})
            self.assertTrue(retry_sigs, "快照失败后下一轮应能重新产出信号")
            store.close()


class AssetEvidenceTests(unittest.TestCase):
    """P2-23：证据包 after_text 用本轮抓到的 body，而不是还没落库的 blob。"""

    def test_asset_after_text_uses_current_bodies(self):
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            old_url = "https://api-docs.deepseek.com/zh-cn/assets/js/main.0c15113f.js"
            new_url = "https://api-docs.deepseek.com/zh-cn/assets/js/main.9ab31f02.js"
            store.conn.execute(
                "INSERT INTO v2_asset_blobs(sha256, size, data, created_at)"
                " VALUES('oldsha', 3, ?, '2026-11-01')", (sqlite3.Binary(b"OLD"),))
            store.conn.commit()
            old = {"pages": {"https://api-docs.deepseek.com/zh-cn/a/":
                             {"refs": {"js": [old_url]}}},
                   "assets": {old_url: {"sha256": "oldsha", "strings": ["old"]}}}
            new = {"pages": {"https://api-docs.deepseek.com/zh-cn/a/":
                             {"refs": {"js": [new_url]}}},
                   "assets": {new_url: {"sha256": "newsha", "strings": ["new"]}}}
            views = signals._asset_views(old, new, store, {new_url: b"NEW BODY"})
            self.assertEqual(len(views), 1)
            self.assertEqual(views[0]["before_text"], "OLD")
            self.assertEqual(views[0]["after_text"], "NEW BODY")
            job = {"id": 73, "created_at": "2026-11-18T10:00:00+08:00",
                   "mode": "high", "baseline": False, "pages_total": 1,
                   "pages_changed": 0, "assets_total": 1, "assets_changed": 1,
                   "n_errors": 0, "signals": []}
            ev = signals.write_evidence(job, old, new, [], views, [], {}, data_dir)
            slug = views[0]["slug"]
            after = ev / "assets" / f"{slug}.after.txt"
            self.assertTrue(after.is_file(), "assets/<slug>.after.txt 必须落盘")
            self.assertIn("NEW BODY", after.read_text(encoding="utf-8"))
            store.close()


class SitemapErrorTests(unittest.TestCase):
    """P2-24：sitemap 失败必须写进 job 错误字段并触发健康告警。"""

    def _site(self):
        from docwatch.collect.selftest import FakeFetcher, FakeSite, docs_page
        site = FakeSite()
        site.set_sitemap(["/quick_start/pricing/"])
        site.set("https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                 docs_page("价格", ["正文"], scripts=(), styles=()))
        site.set("https://www.deepseek.com/zh/",
                 docs_page("首页", ["首页"], scripts=(), styles=()))
        return site, FakeFetcher(site), docs_page

    def test_sitemap_failure_goes_into_job_and_health_alert(self):
        with _tempdir() as td:
            data_dir = Path(td)
            site, fx, _ = self._site()
            signals.run_once(data_dir, fetcher=fx)  # 基线，缓存 sitemap
            site.fail[signals.A.DOCS_SITEMAP] = "500"
            job = signals.run_once(data_dir, fetcher=fx)
            self.assertIn("sitemap 抓取失败", job.get("sitemap_error") or "")
            self.assertGreaterEqual(job.get("n_errors", 0), 1)
            captured = {}
            with mock.patch.object(run, "send_mail",
                                   side_effect=lambda s, t, h: captured.update(
                                       subject=s, text=t, html=h) or True):
                run._check_health(data_dir, job)
            self.assertIn("监控告警", captured.get("subject", ""))
            self.assertIn("sitemap 抓取失败", captured.get("text", ""))

    def test_child_sitemap_failure_is_collected(self):
        from docwatch.collect.selftest import FakeFetcher, FakeSite
        site = FakeSite()
        index = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 f'<sitemap><loc>{signals.A.DOCS_ROOT}/zh-cn/good.xml</loc></sitemap>'
                 f'<sitemap><loc>{signals.A.DOCS_ROOT}/zh-cn/bad.xml</loc></sitemap>'
                 '</sitemapindex>')
        good = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                '<url><loc>https://api-docs.deepseek.com/zh-cn/guides/x/</loc></url></urlset>')
        site.set(f"{signals.A.DOCS_ROOT}/zh-cn/good.xml", good, "application/xml")
        site.fail[f"{signals.A.DOCS_ROOT}/zh-cn/bad.xml"] = "500"
        errors: list[str] = []
        urls = signals._sitemap_locs(FakeFetcher(site), signals.A.DOCS_SITEMAP, index,
                                     errors=errors)
        self.assertEqual(urls, ["https://api-docs.deepseek.com/zh-cn/guides/x/"])
        self.assertTrue(any("子 sitemap 抓取失败" in err for err in errors), errors)


class SelftestGuardTests(unittest.TestCase):
    """P2-26：selftest 不能误删 /opt 或已有 docs.db 的目录。"""

    def test_reject_opt_even_with_force(self):
        from docwatch.collect import selftest as collect_selftest
        with self.assertRaises(SystemExit):
            collect_selftest._prepare_workdir(Path("/opt/docwatch-v2/data"), force=True)

    def test_reject_outside_verify_tmp_without_force(self):
        from docwatch.collect import selftest as collect_selftest
        with _tempdir() as td:
            with self.assertRaises(SystemExit):
                collect_selftest._prepare_workdir(Path(td) / "scout", force=False)

    def test_reject_existing_docs_db_and_force_renames_backup(self):
        from docwatch.collect import selftest as collect_selftest
        root = _make_temp_dir("docwatch-verify-")
        self.addCleanup(_cleanup_temp, root)
        with mock.patch.object(collect_selftest, "VERIFY_TMP", root):
            path = root / f"guard-{uuid.uuid4().hex[:10]}"
            path.mkdir(parents=True)
            (path / "docs.db").write_bytes(b"production-like")
            with self.assertRaises(SystemExit):
                collect_selftest._prepare_workdir(path, force=False)
            resolved = collect_selftest._prepare_workdir(path, force=True)
            self.assertEqual(resolved, path.resolve())
            self.assertFalse((path / "docs.db").exists(), "原 docs.db 应被改名")
            self.assertTrue(list(path.glob("docs.db.bak-*")), "必须保留 .bak 备份")

    def test_default_paths_are_inside_verify_tmp(self):
        from docwatch.collect import selftest as collect_selftest
        root = _make_temp_dir("docwatch-verify-")
        self.addCleanup(_cleanup_temp, root)
        with mock.patch.object(collect_selftest, "VERIFY_TMP", root):
            default = root / f"scout-guard-{uuid.uuid4().hex[:10]}"
            self.assertEqual(collect_selftest._prepare_workdir(default), default.resolve())


class RenderFallbackQueueTests(unittest.TestCase):
    """P2-27：主发信路径排版兜底时不能标 notified / 删 pending。"""

    def test_main_render_fallback_keeps_pending_and_unnotified(self):
        with _tempdir() as td:
            data_dir = Path(td)
            store = signals.Store(data_dir)
            job = _job(88)
            job["baseline"] = False
            job["evidence_dir"] = ""
            job["id"] = store.insert_job(job)
            store.update_job(job)
            store.close()
            fake_out = {"subject": "s", "text": "t", "html": "h"}
            with mock.patch.object(run, "run_once", return_value=dict(job)), \
                    mock.patch.object(run, "render_with_status", return_value=(fake_out, True)), \
                    mock.patch.object(run, "send_mail") as sender, \
                    mock.patch.object(run, "is_quiet", return_value=False), \
                    mock.patch.object(run, "send_heartbeat", return_value=False):
                rc = run.main(["--data-dir", str(data_dir), "--no-llm", "--now"])
            self.assertEqual(rc, 0)
            sender.assert_not_called()
            queued = list((data_dir / "queue").glob("*.json"))
            self.assertEqual(len(queued), 1, "兜底时必须保留 pending 队列")
            item = json.loads(queued[0].read_text(encoding="utf-8"))
            self.assertTrue(item.get("pending"))
            con = sqlite3.connect(data_dir / "docs.db")
            notified = con.execute(
                "SELECT notified FROM v2_jobs WHERE id=?", (job["id"],)).fetchone()[0]
            con.close()
            self.assertEqual(notified, 0, "兜底时不能把作业标成已通知")


class StructuredVerdictTests(unittest.TestCase):
    """详情页两版解读字段：normalize 必须保留并清洗。"""

    def test_normalize_keeps_structured_brief_fields(self):
        verdict = wake.normalize_verdict(
            {
                "level": 3,
                "headline": "价格调整",
                "tldr": "价格页高峰输出价从 13.5 涨到 15 元，输入价没变。",
                "tldr_points": ["价格页高峰输出价从 13.5 元涨到 15 元/百万 tokens。", "高峰调用成本约增加 11%。"],
                "overview": "本轮变化集中在价格页，输入价未动。",
                "sections": [
                    {
                        "title": "价格页：高峰输出价上调",
                        "paragraphs": ["高峰输出价 13.50 → 15.00 元/百万 tokens，输入价 4 元未动。"],
                    }
                ],
                "what_changed": "价格页输出价 13.50 → 15.00 元/百万 tokens。",
                "why_matters": "成本上升",
                "action": "检查预算",
                "evidence": ["价格页"],
            },
            {"rule_level": 3, "noise": False, "model": "test", "job_id": "1"},
        )
        self.assertEqual(len(verdict["tldr_points"]), 2)
        self.assertEqual(verdict["overview"], "本轮变化集中在价格页，输入价未动。")
        self.assertEqual(verdict["sections"][0]["title"], "价格页：高峰输出价上调")
        self.assertEqual(verdict["level"], 3, "结构化字段不能影响规则级别")

    def test_normalize_drops_malformed_sections(self):
        verdict = wake.normalize_verdict(
            {
                "headline": "测试",
                "tldr": "测试省流。",
                "tldr_points": ["测试要点。"],
                "sections": [{"title": "空组", "paragraphs": []}, "坏数据"],
            },
            {"rule_level": 2, "noise": False, "model": "test", "job_id": "2"},
        )
        self.assertEqual(verdict["sections"], [])
        self.assertTrue(any("sections" in warning for warning in verdict["warnings"]))

    def test_usable_rejects_empty_or_object_shaped_sections(self):
        self.assertFalse(wake._usable({"sections": [{"title": "", "paragraphs": []}]}))
        self.assertFalse(wake._usable({"sections": {"主题": ["段落"]}}))
        self.assertTrue(wake._usable({"tldr_points": ["有效要点。"]}))
        self.assertTrue(wake._usable({"sections": [{"title": "主题", "paragraphs": ["有效段落。"]}]}))

    def test_coerce_text_list_skips_dict_entries(self):
        self.assertEqual(wake.coerce_text_list([{"point": "要点"}, "正常要点"]), ["正常要点"])

    def test_degraded_verdict_is_reader_facing(self):
        verdict = wake.degraded_verdict(
            "bad json",
            "EACCES /opt/docwatch/data/jobs/1/BRIEF.md",
            {"rule_level": 2, "noise": False, "raw_path": "/opt/docwatch/data/jobs/1/pi-raw.ndjson"},
        )
        joined = json.dumps(verdict, ensure_ascii=False)
        self.assertNotIn("BRIEF.md", joined)
        self.assertNotIn("pi-raw", joined)
        self.assertNotIn("作业目录", joined)
        self.assertIn("打开原始页面核对变化", verdict["action"])
        self.assertTrue(verdict["degraded"])

    def test_scrub_verdict_filters_internal_tokens_from_public_fields(self):
        verdict = wake.scrub_verdict(
            {
                "headline": "EACCES in /opt/docwatch",
                "tldr": "BRIEF.md 读取失败",
                "tldr_points": ["manifest.json 里没有信号"],
                "overview": "作业目录不可用",
                "sections": [{"title": "主题", "paragraphs": ["pi-raw.ndjson 片段"]}],
                "what_changed": "正常内容",
                "why_matters": "正常影响",
                "action": "打开原始页面",
                "evidence": ["diff/pricing.diff 第 3 行：13.50 → 15.00"],
                "model_note": "",
            },
            {"auth_secrets": []},
        )
        joined = json.dumps(verdict, ensure_ascii=False)
        for token in ("EACCES", "BRIEF.md", "manifest.json", "作业目录", "pi-raw"):
            self.assertNotIn(token, joined)
        # evidence 里的正常证据文件名要保留。
        self.assertIn("diff/pricing.diff", verdict["evidence"][0])

    def test_inherit_structured_fields_keeps_old_brief_after_rewrite(self):
        previous = {
            "tldr": "原省流",
            "tldr_points": ["原要点"],
            "overview": "原概述",
            "sections": [{"title": "原主题", "paragraphs": ["原段落"]}],
        }
        fixed = wake.inherit_structured_fields({"tldr": "", "tldr_points": [], "overview": "", "sections": []}, previous)
        self.assertEqual(fixed["tldr"], "原省流")
        self.assertEqual(fixed["tldr_points"], ["原要点"])
        self.assertEqual(fixed["overview"], "原概述")
        self.assertEqual(fixed["sections"][0]["title"], "原主题")
        self.assertTrue(fixed["warnings"])


class BriefLimitTests(unittest.TestCase):
    """S-A12：BRIEF 截断提示要说清剩余行数，且提示行计入上限。"""

    def test_render_ops_reports_remaining_not_zero(self):
        ops = [{"kind": "change", "before": [f"旧行{i}" for i in range(10)],
                "after": [f"新行{i}" for i in range(10)]}]
        text = signals._render_ops(ops, max_lines=5)
        self.assertIn("还有 15 行改动未显示", text)
        self.assertNotIn("还有 0 行", text)

    def test_brief_output_never_exceeds_limit(self):
        brief = signals._Brief(limit=5)
        brief.add("第一行", "第二行", "第三行", "第四行", "第五行", "第六行")
        lines = brief.text().rstrip("\n").split("\n")
        self.assertLessEqual(len(lines), 5, lines)
        self.assertIn("BRIEF 已到 5 行上限", lines[-1])
        self.assertIn("剩余明细见 manifest.json", lines[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
