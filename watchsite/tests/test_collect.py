from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from watchsite import collect
from watchsite.collect import (
    MAX_DIFF_CHARS,
    _age_status,
    _build_docs_event,
    _changes_from_cards,
    _diff_limited,
    _docs_status,
    _fit_payload,
    _models_snapshot,
    _monitor_status,
    _normalize_monitor_event,
    _split_changes,
    build_batch,
)
from watchsite.tests.support import cleanup, temp_dir


FIXED_NOW = dt.datetime(2026, 11, 13, 16, 0, tzinfo=dt.timezone.utc)


def _monitor_event(index: int, payload_size: int = 0) -> dict:
    return {
        "schema_version": 1,
        "id": f"web:chat:{index:04d}",
        "slug": f"web-chat-{index:04d}",
        "source": "chat",
        "kind": "page_change",
        "level": 2,
        "title": f"事件 {index}",
        "summary": f"摘要 {index}",
        "changed_at": "2026-11-14T13:00:00+08:00",
        "page_url": "https://chat.deepseek.com/",
        "payload": {
            "target": "chat",
            "what_changed": ("变化" * (payload_size // 2)) if payload_size else f"变化 {index}",
        },
    }


def _make_docs_db(path: Path, root: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE v2_jobs (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            mode TEXT,
            baseline INTEGER NOT NULL DEFAULT 0,
            pages_total INTEGER NOT NULL DEFAULT 0,
            pages_changed INTEGER NOT NULL DEFAULT 0,
            assets_total INTEGER NOT NULL DEFAULT 0,
            assets_changed INTEGER NOT NULL DEFAULT 0,
            n_signals INTEGER NOT NULL DEFAULT 0,
            n_errors INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            wire_bytes INTEGER NOT NULL DEFAULT 0,
            evidence_dir TEXT,
            job_json TEXT,
            notified INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    for job_id in (491, 489, 500):
        (root / "data" / "jobs" / str(job_id)).mkdir(parents=True, exist_ok=True)
    job = {
        "signals": [
            {
                "kind": "page_text",
                "page": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "detail": "价格行 13.5→15",
                "before": "输出 13.5 元 / 百万 tokens",
                "after": "输出 15 元 / 百万 tokens",
                "text_changed_chars": 8,
            }
        ],
        "pages_changed": 1,
        "assets_changed": 0,
    }
    con.execute(
        "INSERT INTO v2_jobs (id, created_at, baseline, pages_changed, assets_changed, "
        "n_signals, n_errors, evidence_dir, job_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            491,
            "2026-11-13T23:31:06+08:00",
            0,
            1,
            0,
            1,
            0,
            str(root / "data" / "jobs" / "491"),
            json.dumps(job, ensure_ascii=False),
        ),
    )
    # 没有 verdict 的作业，走规则兜底。
    fallback_job = {
        "signals": [
            {
                "kind": "new_page",
                "page": "https://api-docs.deepseek.com/zh-cn/guides/search/v2/",
                "detail": "新增页面",
            }
        ],
        "pages_changed": 1,
    }
    con.execute(
        "INSERT INTO v2_jobs (id, created_at, baseline, pages_changed, n_signals, "
        "n_errors, evidence_dir, job_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            489,
            "2026-11-13T22:31:12+08:00",
            0,
            1,
            1,
            0,
            str(root / "data" / "jobs" / "489"),
            json.dumps(fallback_job, ensure_ascii=False),
        ),
    )
    # 最近一轮没有信号：健康度必须看这一轮，而不是最后一条信号作业。
    con.execute(
        "INSERT INTO v2_jobs (id, created_at, baseline, n_signals, n_errors, evidence_dir, job_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            500,
            "2026-11-13T23:59:00+08:00",
            0,
            0,
            0,
            str(root / "data" / "jobs" / "500"),
            json.dumps({"signals": []}, ensure_ascii=False),
        ),
    )
    con.commit()
    con.close()


class CollectTests(unittest.TestCase):
    def test_build_batch_from_docs_and_archive(self) -> None:
        root = temp_dir("collect-")
        self.addCleanup(cleanup, root)
        db = root / "docs.db"
        _make_docs_db(db, root)
        verdict_dir = root / "data" / "jobs" / "491"
        verdict_dir.mkdir(parents=True, exist_ok=True)
        (verdict_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "level": 3,
                    "level_label": "重要",
                    "headline": "价格调整",
                    "what_changed": "输出价格从 13.5 调整为 15 元",
                    "why_matters": "调用成本会变化",
                    "action": "检查预算",
                    "evidence": ["价格页有变化"],
                    "model": "test-model",
                    "model_note": "",
                    "degraded": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        events_dir = root / "events"
        events_dir.mkdir()
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "sources": {
                        "chat": {
                            "last_checked": "2026-11-13T15:50:00Z",
                            "last_success": "2026-11-13T15:50:00Z",
                            "fail_streak": 0,
                            "last_error": "",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (events_dir / "chat-event.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "archive:chat:20261113_194139_abc",
                    "slug": "archive-chat-20261113-194139-abc",
                    "source": "chat",
                    "kind": "archive_entry",
                    "level": 2,
                    "level_label": "值得看",
                    "title": "Chat 页面更新",
                    "summary": "新增了一个按钮",
                    "impact": "",
                    "action": "",
                    "signal_count": 1,
                    "changed_at": "2026-11-14T13:00:00+08:00",
                    "page_url": "https://chat.deepseek.com/",
                    "archive_url": "https://example.com/chat/",
                    "evidence": ["新增了一个按钮"],
                    "payload": {"target": "chat", "entry_id": "20261113_194139_abc"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        batch = build_batch(db, root, events_dir, state, now=FIXED_NOW)
        events = {item["id"]: item for item in batch["events"]}
        self.assertEqual(events["docwatch:491"]["level"], 3)
        self.assertEqual(events["docwatch:491"]["source"], "docs")
        self.assertEqual(events["docwatch:491"]["title"], "价格调整")
        # 没有 verdict.json 的作业也必须落在稳定 id 上（不能翻转成 :fallback）
        self.assertIn("docwatch:489", events)
        self.assertNotIn("docwatch:489:fallback", events)
        self.assertTrue(events["docwatch:489"]["payload"]["degraded"])
        self.assertEqual(events["docwatch:489"]["title"], "检测到 1 处变化")
        self.assertEqual(events["archive:chat:20261113_194139_abc"]["level"], 2)
        self.assertEqual(batch["meta"]["event_count"], 3)
        self.assertEqual(batch["meta"]["source_counts"]["chat"], 1)
        statuses = {item["source"]: item for item in batch["statuses"]}
        self.assertEqual(statuses["docs"]["status"], "ok")
        self.assertIn("#500", statuses["docs"]["detail"])
        self.assertEqual(statuses["chat"]["status"], "ok")
        self.assertIsNotNone(statuses["chat"]["last_event_ts"])

    def test_unready_docwatch_job_is_not_pushed(self) -> None:
        """docwatch 还没完成本轮处理（notified=0）时，不能提前推降级事件。"""
        root = temp_dir("collect-unready-")
        self.addCleanup(cleanup, root)
        db = root / "docs.db"
        _make_docs_db(db, root)
        con = sqlite3.connect(db)
        con.execute("UPDATE v2_jobs SET notified = 0 WHERE id = 491")
        con.commit()
        con.close()
        events_dir = root / "events"
        events_dir.mkdir()
        batch = build_batch(db, root, events_dir, root / "state.json", now=FIXED_NOW)
        ids = {item["id"] for item in batch["events"]}
        self.assertNotIn("docwatch:491", ids)
        self.assertIn("docwatch:489", ids)

    def test_cleaned_docwatch_evidence_falls_back_to_rule_summary(self) -> None:
        """证据目录被 JOBS_KEEP 剪掉后，仍要用规则摘要补发，不能永久丢事件。

        没上过站的作业不能因为本地证据被清理就消失；已上站的作业由 sent_ids
        过滤，不会被兜底文案覆盖。
        """
        root = temp_dir("collect-pruned-")
        self.addCleanup(cleanup, root)
        db = root / "docs.db"
        _make_docs_db(db, root)
        # 489 的证据目录被清理
        shutil.rmtree(root / "data" / "jobs" / "489")
        events_dir = root / "events"
        events_dir.mkdir()
        batch = build_batch(db, root, events_dir, root / "state.json", now=FIXED_NOW)
        by_id = {item["id"]: item for item in batch["events"]}
        self.assertIn("docwatch:489", by_id)
        event = by_id["docwatch:489"]
        self.assertTrue(event["payload"]["degraded"])
        self.assertTrue(event["title"])
        self.assertTrue(event["summary"])

    def test_sent_docwatch_jobs_are_not_rebuilt(self) -> None:
        root = temp_dir("collect-sent-")
        self.addCleanup(cleanup, root)
        db = root / "docs.db"
        _make_docs_db(db, root)
        events_dir = root / "events"
        events_dir.mkdir()
        batch = build_batch(
            db,
            root,
            events_dir,
            root / "state.json",
            now=FIXED_NOW,
            sent_ids=["docwatch:491"],
        )
        ids = {item["id"] for item in batch["events"]}
        self.assertNotIn("docwatch:491", ids)
        self.assertIn("docwatch:489", ids)

    def test_docs_event_id_is_stable_after_verdict_appears(self) -> None:
        """verdict.json 从无到有，同一作业的 id / slug 必须保持不变。

        回归 2026-11-16 审查发现：id 曾带 :fallback 后缀，同一变化会先以兜底 id
        入库、补上解读后再以正式 id 入库一次，站点上出现两条（其中一条永久降级）。
        """
        root = temp_dir("collect-stable-")
        self.addCleanup(cleanup, root)
        db = root / "docs.db"
        _make_docs_db(db, root)
        events_dir = root / "events"
        events_dir.mkdir()

        def fresh_state(path: Path) -> Path:
            path.write_text(json.dumps({"sources": {}}), encoding="utf-8")
            return path

        first = build_batch(db, root, events_dir, fresh_state(root / "s1.json"), now=FIXED_NOW)
        first_event = next(item for item in first["events"] if item["id"] == "docwatch:489")
        self.assertTrue(first_event["payload"]["degraded"])

        verdict_dir = root / "data" / "jobs" / "489"
        verdict_dir.mkdir(parents=True, exist_ok=True)
        (verdict_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "level": 2,
                    "level_label": "值得看",
                    "headline": "补上的解读",
                    "what_changed": "补上了解读",
                    "why_matters": "说明原因",
                    "action": "无需操作",
                    "evidence": ["证据"],
                    "model": "test-model",
                    "model_note": "",
                    "degraded": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        second = build_batch(db, root, events_dir, fresh_state(root / "s2.json"), now=FIXED_NOW)
        second_event = next(item for item in second["events"] if item["id"] == "docwatch:489")
        self.assertEqual(second_event["slug"], first_event["slug"])
        self.assertFalse(second_event["payload"]["degraded"])

    def test_diff_list_is_joined_as_lines_not_python_repr(self) -> None:
        changes = _changes_from_cards(
            [
                {
                    "page": "https://example.com",
                    "diff": ["－旧内容", "＋新内容"],
                }
            ]
        )
        self.assertEqual(changes[0]["diff"], "－旧内容\n＋新内容")

        pair_changes = _changes_from_cards(
            [
                {
                    "page": "https://example.com",
                    "diff": [("－", "旧内容"), ("＋", "新内容")],
                }
            ]
        )
        self.assertEqual(pair_changes[0]["diff"], "－旧内容\n＋新内容")

    def test_bad_monitor_file_does_not_drop_all_events(self) -> None:
        root = temp_dir("collect-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        good = {
            "id": "web:chat:good",
            "slug": "web-chat-good",
            "source": "chat",
            "kind": "page_change",
            "level": 2,
            "title": "Chat 页面更新",
            "summary": "新增了一个按钮",
            "changed_at": "2026-11-14T13:00:00+08:00",
            "page_url": "https://chat.deepseek.com/",
            "payload": {"target": "chat"},
        }
        (events_dir / "good.json").write_text(json.dumps(good), encoding="utf-8")
        bad = dict(good, id="web:chat:bad", signal_count="not-a-number")
        (events_dir / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        batch = build_batch(
            root / "missing.db",
            root,
            events_dir,
            root / "state.json",
            now=FIXED_NOW,
        )
        ids = {item["id"] for item in batch["events"]}
        self.assertIn("web:chat:good", ids)
        self.assertNotIn("web:chat:bad", ids)

    def test_bad_event_files_are_reported_in_collection_errors(self) -> None:
        """P3-03：坏文件不能只 print 后静默跳过，必须带文件名和原因进 collection_errors。"""
        root = temp_dir("collect-badfiles-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        (events_dir / "good.json").write_text(
            json.dumps(_monitor_event(1), ensure_ascii=False), encoding="utf-8"
        )
        (events_dir / "broken.json").write_text("{not-json", encoding="utf-8")
        invalid = _monitor_event(3)
        invalid.pop("id")
        (events_dir / "invalid.json").write_text(
            json.dumps(invalid, ensure_ascii=False), encoding="utf-8"
        )
        batch = build_batch(
            root / "missing.db", root, events_dir, root / "state.json", now=FIXED_NOW
        )
        self.assertEqual([event["id"] for event in batch["events"]], ["web:chat:0001"])
        errors = batch["meta"]["collection_errors"]
        details = [str(item.get("detail") or "") for item in errors]
        self.assertTrue(any("broken.json" in detail for detail in details), details)
        self.assertTrue(any("invalid.json" in detail for detail in details), details)
        self.assertTrue(any(item.get("file") == "broken.json" for item in errors), errors)

    def test_missing_changed_ts_uses_stable_fallback(self) -> None:
        """P2-15：缺 changed_ts 时不能用 now 填充，否则死信签名每轮都变。"""
        raw = _monitor_event(9)
        raw.pop("changed_at")
        first = _normalize_monitor_event(raw, 1000)
        second = _normalize_monitor_event(raw, 2000)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first["changed_ts"], 0)
        self.assertEqual(second["changed_ts"], 0)

    def test_monitor_index_corrupt_falls_back_to_full_read(self) -> None:
        root = temp_dir("collect-index-corrupt-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        state_dir = root / "state"
        state_dir.mkdir()
        for index in range(3):
            (events_dir / f"evt-{index}.json").write_text(
                json.dumps(_monitor_event(index), ensure_ascii=False), encoding="utf-8"
            )
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(state_dir)}, clear=False):
            first = build_batch(
                root / "missing.db", root, events_dir, root / "state.json", now=FIXED_NOW
            )
            self.assertEqual(len(first["events"]), 3)
            index_path = state_dir / collect.MONITOR_INDEX_FILE
            self.assertTrue(index_path.is_file())
            index_path.write_text("{not-json", encoding="utf-8")
            sent = [event["id"] for event in first["events"]]
            with mock.patch.object(
                collect, "_normalize_monitor_event", wraps=collect._normalize_monitor_event
            ) as normalizer:
                second = build_batch(
                    root / "missing.db",
                    root,
                    events_dir,
                    root / "state.json",
                    now=FIXED_NOW,
                    sent_ids=sent,
                )
            # 索引坏了必须退回全量读取，不能把已发送事件当成空目录。
            self.assertEqual(normalizer.call_count, 3)
            self.assertEqual(len(second["events"]), 3)
            self.assertEqual(second["meta"]["collection_errors"], [])

    def test_monitor_normalization_budget_reports_and_resumes(self) -> None:
        """单轮字节预算超限要进 collection_errors，且事件按下一轮继续处理不丢。"""
        root = temp_dir("collect-budget-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        state_dir = root / "state"
        state_dir.mkdir()
        all_ids = set()
        for index in range(12):
            event = _monitor_event(index, payload_size=4000)
            all_ids.add(event["id"])
            (events_dir / f"evt-{index:02d}.json").write_text(
                json.dumps(event, ensure_ascii=False), encoding="utf-8"
            )
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(state_dir)}, clear=False):
            sent: list[str] = []
            seen: list[str] = []
            first_errors: list[dict] | None = None
            for _ in range(20):
                batch = build_batch(
                    root / "missing.db",
                    root,
                    events_dir,
                    root / "state.json",
                    now=FIXED_NOW,
                    sent_ids=sent,
                    max_normalize_bytes=12_000,
                )
                if first_errors is None:
                    first_errors = batch["meta"]["collection_errors"]
                new_ids = [event["id"] for event in batch["events"]]
                if new_ids:
                    # 每轮都必须有进展，不能因为排序固定而永远饿死后面的文件。
                    self.assertTrue(any(event_id not in sent for event_id in new_ids))
                seen.extend(new_ids)
                sent.extend(new_ids)
                if set(seen) == all_ids:
                    break
        self.assertEqual(set(seen), all_ids)
        self.assertEqual(len(seen), len(all_ids))
        self.assertIsNotNone(first_errors)
        assert first_errors is not None
        self.assertTrue(any("预算" in str(item.get("detail") or "") for item in first_errors))

    def test_eight_hundred_events_use_incremental_index_and_lower_memory(self) -> None:
        """P1-05：800 条合成事件第二轮不再全量归一化，内存峰值显著下降。"""
        root = temp_dir("collect-800-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        state_dir = root / "state"
        state_dir.mkdir()
        monitor_state = root / "state.json"
        monitor_state.write_text(
            json.dumps(
                {
                    "sources": {
                        "chat": {
                            "last_checked": "2026-11-13T15:50:00Z",
                            "last_success": "2026-11-13T15:50:00Z",
                            "fail_streak": 0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        for index in range(800):
            (events_dir / f"evt-{index:04d}.json").write_text(
                json.dumps(_monitor_event(index, payload_size=2000), ensure_ascii=False),
                encoding="utf-8",
            )
        with mock.patch.dict(os.environ, {"PUSH_STATE_DIR": str(state_dir)}, clear=False):
            with mock.patch.object(
                collect, "_normalize_monitor_event", wraps=collect._normalize_monitor_event
            ) as normalizer:
                tracemalloc.start()
                first = build_batch(
                    root / "missing.db", root, events_dir, root / "state.json", now=FIXED_NOW
                )
                _, first_peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertEqual(normalizer.call_count, 800)
                self.assertEqual(len(first["events"]), 800)
                sent_ids = [event["id"] for event in first["events"]]
                del first

                normalizer.reset_mock()
                tracemalloc.start()
                second = build_batch(
                    root / "missing.db",
                    root,
                    events_dir,
                    root / "state.json",
                    now=FIXED_NOW,
                    sent_ids=sent_ids,
                )
                _, second_peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                # 已发送且未变化的 800 个文件：零解析，只读轻量索引。
                self.assertEqual(normalizer.call_count, 0)
                self.assertEqual(second["events"], [])
                self.assertEqual(second["meta"]["event_count"], 800)
                self.assertEqual(second["meta"]["collection_errors"], [])
                statuses = {item["source"]: item for item in second["statuses"]}
                self.assertIsNotNone(statuses["chat"]["last_event_ts"])
                self.assertLess(second_peak, first_peak * 0.5)

                # 只改一个文件：只归一化这一条。
                changed = _monitor_event(7)
                changed["title"] = "改过的标题"
                (events_dir / "evt-0007.json").write_text(
                    json.dumps(changed, ensure_ascii=False), encoding="utf-8"
                )
                normalizer.reset_mock()
                third = build_batch(
                    root / "missing.db",
                    root,
                    events_dir,
                    root / "state.json",
                    now=FIXED_NOW,
                    sent_ids=sent_ids,
                )
                self.assertEqual(normalizer.call_count, 1)
                self.assertEqual([event["id"] for event in third["events"]], ["web:chat:0007"])

    def test_one_recent_failure_is_warn_not_ok(self) -> None:
        now_epoch = int(FIXED_NOW.timestamp())
        self.assertEqual(_age_status(now_epoch - 60, now_epoch, 1), "warn")
        self.assertEqual(_age_status(now_epoch - 60, now_epoch, 3), "error")
        self.assertEqual(_age_status(now_epoch - 60, now_epoch, 0), "ok")

    def test_failed_latest_run_does_not_overwrite_last_success(self) -> None:
        now_epoch = int(FIXED_NOW.timestamp())
        latest = {"id": 501, "created_at": "2026-11-13T23:59:30+08:00", "n_signals": 0, "n_errors": 4}
        last_success = {"id": 500, "created_at": "2026-11-13T23:59:00+08:00"}
        row = _docs_status([], [], now_epoch, latest, last_success)
        self.assertEqual(row["status"], "warn")
        self.assertIsNotNone(row["last_success_ts"])
        self.assertIn("最后成功 #500", row["detail"])
        self.assertIn("抓取错误 4 个", row["detail"])

    def test_docs_without_any_success_is_error_not_unknown(self) -> None:
        now_epoch = int(FIXED_NOW.timestamp())
        latest = {"id": 501, "created_at": "2026-11-13T23:59:30+08:00", "n_signals": 0, "n_errors": 4}
        row = _docs_status([], [], now_epoch, latest, None)
        self.assertEqual(row["status"], "error")
        self.assertIn("还没有成功记录", row["detail"])

    def test_payload_is_capped_for_ingest(self) -> None:
        payload = {
            "job_id": "491",
            "model_note": "备注" * 5000,
            "changes": [
                {"page": f"https://example.com/{index}", "diff": "x" * 4000}
                for index in range(20)
            ],
        }
        fitted = _fit_payload(payload)
        self.assertLessEqual(len(json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))), 60_000)
        self.assertIn("job_id", fitted)

    def test_payload_cap_keeps_release_note_instead_of_dropping_it(self) -> None:
        payload = {
            "job_id": "dsh",
            "release_note": {
                "blocks": [
                    {"type": "bullet", "text": f"第{index}条：" + "更新说明" * 300}
                    for index in range(100)
                ]
            },
            "notes": ["条目" * 100 for _ in range(100)],
            "what_changed": "完整说明" * 1000,
        }
        fitted = _fit_payload(payload)
        self.assertLessEqual(
            len(json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))),
            60_000,
        )
        self.assertIn("release_note", fitted)
        blocks = fitted["release_note"]["blocks"]
        self.assertTrue(blocks)
        # 超限时保留前面的真实条目，只在末尾标注截断；不能只剩一行提示。
        self.assertTrue(blocks[0]["text"].startswith("第0条："))
        self.assertEqual(blocks[-1]["text"], "…（更新日志过长，已截断）")

    def test_payload_cap_preserves_analysis_core(self) -> None:
        payload = {
            "job_id": "491",
            "analysis": {
                "tldr_points": ["省流要点一。", "省流要点二。"],
                "overview": "总述" * 200,
                "sections": [
                    {"title": f"主题{index}", "paragraphs": ["长段落" * 300]}
                    for index in range(20)
                ],
            },
            "changes": [
                {"page": f"https://example.com/{index}", "diff": "x" * 4000}
                for index in range(20)
            ],
        }
        fitted = _fit_payload(payload)
        self.assertLessEqual(len(json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))), 60_000)
        self.assertEqual(fitted["analysis"]["tldr_points"], ["省流要点一。", "省流要点二。"])
        self.assertIn("overview", fitted["analysis"])
        self.assertLess(len(fitted["analysis"].get("sections", [])), 20)

    def test_docs_analysis_keeps_tldr_points_without_sections(self) -> None:
        root = temp_dir("collect-points-only-")
        self.addCleanup(cleanup, root)
        db = self._docs_verdict_setup(
            root,
            {
                "level": 2,
                "headline": "只有省流和总述",
                "tldr": "一句话省流。",
                "tldr_points": ["要点一。", "要点二。"],
                "overview": "本轮总述。",
                "what_changed": "正文有变化。",
                "why_matters": "影响有限。",
                "action": "无需操作",
                "evidence": ["页面"],
                "degraded": False,
            },
        )
        batch = build_batch(db, root, root / "events", root / "state.json", now=FIXED_NOW)
        event = next(item for item in batch["events"] if item["id"] == "docwatch:491")
        analysis = event["payload"]["analysis"]
        self.assertEqual(analysis["tldr_points"], ["要点一。", "要点二。"])
        self.assertEqual(analysis["overview"], "本轮总述。")
        self.assertEqual(analysis["sections"], [])

    def test_search_text_includes_section_titles(self) -> None:
        root = temp_dir("collect-search-title-")
        self.addCleanup(cleanup, root)
        db = self._docs_verdict_setup(
            root,
            {
                "level": 2,
                "headline": "标题搜索",
                "tldr": "一句话省流。",
                "tldr_points": ["要点一。"],
                "overview": "总述。",
                "sections": [
                    {"title": "只在标题里的关键词", "paragraphs": ["正文内容。"]}
                ],
                "what_changed": "正文有变化。",
                "why_matters": "影响有限。",
                "action": "无需操作",
                "evidence": ["页面"],
                "degraded": False,
            },
        )
        batch = build_batch(db, root, root / "events", root / "state.json", now=FIXED_NOW)
        event = next(item for item in batch["events"] if item["id"] == "docwatch:491")
        self.assertIn("只在标题里的关键词", event["search_text"])

    def test_missing_db_is_reported_as_error(self) -> None:
        root = temp_dir("collect-")
        self.addCleanup(cleanup, root)
        batch = build_batch(
            root / "missing.db",
            root,
            root / "events",
            root / "state.json",
            now=FIXED_NOW,
        )
        statuses = {item["source"]: item for item in batch["statuses"]}
        self.assertEqual(statuses["docs"]["status"], "error")
        self.assertEqual(batch["events"], [])

    def test_missing_events_dir_marks_web_sources_error(self) -> None:
        root = temp_dir("collect-")
        self.addCleanup(cleanup, root)
        batch = build_batch(
            root / "missing.db",
            root,
            root / "missing-events",
            root / "state.json",
            now=FIXED_NOW,
        )
        statuses = {item["source"]: item for item in batch["statuses"]}
        for source in ("chat", "platform", "harness", "fingerprint"):
            self.assertEqual(statuses[source]["status"], "error", source)
            self.assertIn("事件目录", statuses[source]["detail"])
        self.assertTrue(batch["meta"]["collection_errors"])
        self.assertTrue(
            any("事件目录" in item["detail"] for item in batch["meta"]["collection_errors"])
        )

    def test_empty_events_dir_is_not_an_error(self) -> None:
        root = temp_dir("collect-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "sources": {
                        "chat": {
                            "last_checked": "2026-11-13T15:50:00Z",
                            "last_success": "2026-11-13T15:50:00Z",
                            "fail_streak": 0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        batch = build_batch(root / "missing.db", root, events_dir, state, now=FIXED_NOW)
        statuses = {item["source"]: item for item in batch["statuses"]}
        self.assertEqual(statuses["chat"]["status"], "ok")
        self.assertEqual(batch["meta"]["collection_errors"], [])

    def _docs_verdict_setup(self, root: Path, verdict: dict) -> Path:
        db = root / "docs.db"
        _make_docs_db(db, root)
        verdict_dir = root / "data" / "jobs" / "491"
        verdict_dir.mkdir(parents=True, exist_ok=True)
        (verdict_dir / "verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False), encoding="utf-8"
        )
        events_dir = root / "events"
        events_dir.mkdir(exist_ok=True)
        return db

    def test_docs_summary_uses_tldr_and_payload_keeps_full_text(self) -> None:
        root = temp_dir("collect-tldr-")
        self.addCleanup(cleanup, root)
        long_text = "输出价 13.5 → 15 元；输入价 4 元未动。" * 30
        db = self._docs_verdict_setup(
            root,
            {
                "level": 3,
                "headline": "价格调整",
                "tldr": "价格页高峰输出价从 13.5 涨到 15 元，输入价没变。",
                "what_changed": long_text,
                "why_matters": "成本上升",
                "action": "检查预算",
                "evidence": ["价格页"],
                "degraded": False,
            },
        )
        batch = build_batch(db, root, root / "events", root / "state.json", now=FIXED_NOW)
        event = next(item for item in batch["events"] if item["id"] == "docwatch:491")
        self.assertEqual(event["summary"], "价格页高峰输出价从 13.5 涨到 15 元，输入价没变。")
        self.assertLessEqual(len(event["summary"]), 300)
        self.assertEqual(event["payload"]["what_changed"], long_text)

    def test_docs_structured_brief_is_passed_through_to_analysis(self) -> None:
        root = temp_dir("collect-structured-")
        self.addCleanup(cleanup, root)
        db = self._docs_verdict_setup(
            root,
            {
                "level": 3,
                "headline": "价格调整",
                "tldr": "价格页高峰输出价从 13.5 涨到 15 元，输入价没变。",
                "tldr_points": [
                    "价格页高峰输出价从 13.5 元涨到 15 元/百万 tokens，输入价没变。",
                    "按现在用量，高峰调用成本大约增加 11%。",
                ],
                "overview": "本轮变化集中在价格页，输入价和缓存价未动。",
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
                "degraded": False,
            },
        )
        batch = build_batch(db, root, root / "events", root / "state.json", now=FIXED_NOW)
        event = next(item for item in batch["events"] if item["id"] == "docwatch:491")
        analysis = event["payload"]["analysis"]
        self.assertEqual(analysis["tldr_points"][0], "价格页高峰输出价从 13.5 元涨到 15 元/百万 tokens，输入价没变。")
        self.assertEqual(analysis["overview"], "本轮变化集中在价格页，输入价和缓存价未动。")
        self.assertEqual(analysis["sections"][0]["title"], "价格页：高峰输出价上调")
        self.assertEqual(analysis["action"], "检查预算")
        # 旧字段继续保留，供 RSS / 邮件 / 旧渲染回退。
        self.assertEqual(event["payload"]["what_changed"], "价格页输出价 13.50 → 15.00 元/百万 tokens。")

    def test_docs_summary_falls_back_to_what_changed_capped_at_300(self) -> None:
        root = temp_dir("collect-tldr-fallback-")
        self.addCleanup(cleanup, root)
        long_text = "这是一段没有 tldr 的旧判读输出。" * 60
        self.assertGreater(len(long_text), 300)
        db = self._docs_verdict_setup(
            root,
            {
                "level": 2,
                "headline": "旧解读",
                "what_changed": long_text,
                "why_matters": "影响",
                "action": "看看",
                "evidence": [],
                "degraded": False,
            },
        )
        batch = build_batch(db, root, root / "events", root / "state.json", now=FIXED_NOW)
        event = next(item for item in batch["events"] if item["id"] == "docwatch:491")
        self.assertEqual(event["summary"], long_text[:300])
        self.assertLessEqual(len(event["summary"]), 300)
        self.assertEqual(event["payload"]["what_changed"], long_text)

    def test_changes_are_split_into_directory_and_detail(self) -> None:
        root = temp_dir("collect-split-")
        self.addCleanup(cleanup, root)
        diff = "－输出 13.5 元\n＋输出 15 元"
        fake_changes = [
            {
                "page": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "level": 3,
                "is_asset": False,
                "is_error": False,
                "noise": False,
                "note": "价格表",
                "bullets": ["输出价 13.5 → 15"],
                "diff": diff,
            }
        ]
        job = {"id": 9001, "created_at": "2026-11-13T23:31:06+08:00", "signals": []}
        with mock.patch.object(collect, "_changes_from_cards", return_value=fake_changes):
            event = _build_docs_event(job, root, int(FIXED_NOW.timestamp()))

        directory = event["payload"]["changes"]
        self.assertEqual(len(directory), 1)
        # 目录只留计数 / 标记字段：旧键位仍在，但不再带正文。
        self.assertEqual(directory[0]["diff"], "")
        self.assertEqual(directory[0]["note"], "")
        self.assertTrue(directory[0]["has_diff"])
        self.assertEqual(directory[0]["diff_bytes"], len(diff.encode("utf-8")))
        self.assertEqual(directory[0]["bullets"], ["输出价 13.5 → 15"])
        # 正文进 changes_detail，随主事件同一个请求推送。
        detail = event["changes_detail"]
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["diff"], diff)
        self.assertEqual(detail[0]["note"], "价格表")
        self.assertLessEqual(
            len(json.dumps(event["payload"], ensure_ascii=False, separators=(",", ":"))),
            60_000,
        )

    def test_payload_stays_small_when_many_large_diffs_exist(self) -> None:
        """C1 的核心：20 组 × 20,000 字 diff 不能把主 payload 顶过 60,000 字符。"""
        root = temp_dir("collect-bigdiff-")
        self.addCleanup(cleanup, root)
        big_diff = "＋" + "新内容" * 10_000
        fake_changes = [
            {
                "page": f"https://api-docs.deepseek.com/page-{index}",
                "level": 2,
                "bullets": [f"第 {index} 页有变化"],
                "diff": big_diff,
            }
            for index in range(20)
        ]
        job = {"id": 9002, "created_at": "2026-11-13T23:31:06+08:00", "signals": []}
        with mock.patch.object(collect, "_changes_from_cards", return_value=fake_changes):
            event = _build_docs_event(job, root, int(FIXED_NOW.timestamp()))
        payload_size = len(json.dumps(event["payload"], ensure_ascii=False, separators=(",", ":")))
        detail_size = len(
            json.dumps(event["changes_detail"], ensure_ascii=False, separators=(",", ":"))
        )
        self.assertLess(payload_size, 60_000)
        self.assertGreater(detail_size, 100_000)
        self.assertEqual(len(event["payload"]["changes"]), 20)
        self.assertEqual(len(event["changes_detail"]), 20)
        self.assertTrue(all(item["has_diff"] for item in event["payload"]["changes"]))
        self.assertTrue(all(item["diff"] == "" for item in event["payload"]["changes"]))

    def test_split_changes_handles_missing_fields(self) -> None:
        directory, detail = _split_changes([{"page": "p"}])
        self.assertEqual(directory[0]["level"], 0)
        self.assertFalse(directory[0]["has_diff"])
        self.assertEqual(directory[0]["diff_bytes"], 0)
        self.assertEqual(detail[0]["diff"], "")
        self.assertEqual(detail[0]["note"], "")

    def test_overlong_diff_is_truncated_with_marker(self) -> None:
        raw = "＋" + "新内容" * 12_000
        limited = _diff_limited(raw)
        self.assertEqual(len(limited), MAX_DIFF_CHARS)
        self.assertTrue(limited.startswith("＋新内容"))
        self.assertRegex(limited, r"…（已截断，原文 [0-9.]+ KB）$")

        changes = _changes_from_cards([{"page": "https://example.com/x", "diff": [raw]}])
        self.assertEqual(len(changes[0]["diff"]), MAX_DIFF_CHARS)
        self.assertTrue(changes[0]["diff"].endswith("）"))

    def test_web_event_summary_is_capped_and_full_text_moves_to_payload(self) -> None:
        long_summary = "Chat 页面新增了一个入口。" * 80
        event = _normalize_monitor_event(
            {
                "id": "web:chat:1",
                "slug": "web-chat-1",
                "source": "chat",
                "kind": "page_change",
                "level": 2,
                "title": "Chat 页面更新",
                "summary": long_summary,
                "changed_at": "2026-11-14T13:00:00+08:00",
                "page_url": "https://chat.deepseek.com/",
                "payload": {"target": "chat"},
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["summary"], long_summary[:300])
        self.assertEqual(event["payload"]["what_changed"], long_summary)
        self.assertEqual(event["payload"]["target"], "chat")

    def test_web_event_slug_and_urls_match_server_validation(self) -> None:
        event = _normalize_monitor_event(
            {
                "id": "web:chat:slug",
                "slug": "Chat_Bad Slug",
                "source": "chat",
                "kind": "page_change",
                "level": 2,
                "title": "Chat 页面更新",
                "summary": "摘要",
                "changed_at": "2026-11-14T13:00:00+08:00",
                "page_url": "https://chat.deepseek.com/a b",
                "archive_url": "https://chat.deepseek.com/ok",
                "payload": {},
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        assert event is not None
        # 服务端要求 slug 只含小写字母/数字/连字符；含空白的 URL 一律置空，
        # 不能让它在服务端被永久拒收后每轮重试。
        self.assertRegex(event["slug"], r"^[a-z0-9-]+$")
        self.assertEqual(event["page_url"], "")
        self.assertEqual(event["archive_url"], "https://chat.deepseek.com/ok")

    def test_web_event_tldr_wins_over_summary(self) -> None:
        event = _normalize_monitor_event(
            {
                "id": "web:chat:2",
                "slug": "web-chat-2",
                "source": "chat",
                "kind": "page_change",
                "level": 1,
                "title": "Chat 页面更新",
                "summary": "长摘要" * 200,
                "tldr": "Chat 页面新增了一个入口，对现有使用没有影响。",
                "changed_at": "2026-11-14T13:00:00+08:00",
                "page_url": "https://chat.deepseek.com/",
                "payload": {},
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["summary"], "Chat 页面新增了一个入口，对现有使用没有影响。")

    def test_web_event_builds_changes_detail_for_changes_page(self) -> None:
        event = _normalize_monitor_event(
            {
                "id": "web:chat:3",
                "slug": "web-chat-3",
                "source": "chat",
                "kind": "page_change",
                "level": 2,
                "title": "Chat 页面更新",
                "summary": "前端版本和正文都有变化",
                "changed_at": "2026-11-14T13:00:00+08:00",
                "page_url": "https://chat.deepseek.com/",
                "payload": {
                    "changes": [
                        {"kind": "commit", "old": "a1", "new": "b2"},
                        {"kind": "assets", "added": ["https://x/new.js"]},
                        {"kind": "text", "preview": "…旧 → …新"},
                    ]
                },
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        detail = event["changes_detail"]
        self.assertEqual(len(detail), 3)
        self.assertEqual(detail[0]["page"], "前端版本")
        self.assertEqual(detail[0]["bullets"], ["a1 → b2"])
        self.assertEqual(detail[1]["page"], "脚本资源")
        self.assertIn("新增：https://x/new.js", detail[1]["bullets"][0])
        self.assertEqual(detail[2]["page"], "页面正文")

    def test_web_event_prefers_raw_changes_detail_from_webwatch(self) -> None:
        event = _normalize_monitor_event(
            {
                "id": "web:chat:4",
                "slug": "web-chat-4",
                "source": "chat",
                "kind": "page_change",
                "level": 3,
                "title": "Chat 网页前端版本更新",
                "summary": "额度用完时，聊天界面会多一条提示",
                "changed_at": "2026-11-16T12:30:00+08:00",
                "page_url": "https://chat.deepseek.com/",
                "changes_detail": [
                    {
                        "page": "新增接口",
                        "level": 3,
                        "bullets": ["`/api/v0/chat/status`"],
                        "diff": "",
                    },
                    {
                        "page": "新增用户文案",
                        "level": 3,
                        "bullets": ["「使用额度已达上限，请额度恢复后再试」"],
                        "diff": "",
                    },
                ],
                "payload": {
                    "changes": [{"kind": "commit", "old": "a1", "new": "b2"}],
                    "frontend": {
                        "old_commit": "a1",
                        "new_commit": "b2",
                        "totals": {"manifest": 612, "added": 0, "removed": 0, "changed": 612},
                    },
                    "analysis": {
                        "headline": "额度用完时，聊天界面会多一条提示",
                        "tldr": "额度用完时会出现提示。",
                        "visible_changes": ["输入框上方会出现提示。"],
                        "impact": ["使用受限额度的用户会看到提示。"],
                        "action": "无需操作",
                    },
                },
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["changes_detail"][0]["page"], "新增接口")
        self.assertEqual(event["changes_detail"][1]["page"], "新增用户文案")
        self.assertEqual(event["payload"]["analysis"]["headline"], "额度用完时，聊天界面会多一条提示")
        # 技术核对区块依赖 payload.frontend，必须完整透传到站点。
        self.assertEqual(event["payload"]["frontend"]["new_commit"], "b2")
        self.assertEqual(event["payload"]["frontend"]["totals"]["manifest"], 612)

    def test_change_group_level_zero_is_not_replaced_by_event_level(self) -> None:
        event = _normalize_monitor_event(
            {
                "id": "web:chat:level-zero",
                "slug": "web-chat-level-zero",
                "source": "chat",
                "kind": "page_change",
                "level": 3,
                "title": "页面变化",
                "summary": "包含一个噪音分组",
                "changed_at": "2026-11-16T12:30:00+08:00",
                "page_url": "https://chat.deepseek.com/",
                "changes_detail": [
                    {"page": "噪音分组", "level": 0, "bullets": ["纯样式抖动"]}
                ],
                "payload": {},
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["changes_detail"][0]["level"], 0)

    def test_web_event_search_text_covers_detail_page_copy(self) -> None:
        """搜详情页里看到的文案必须命中：what_changed / bullets / preview（P2-50）。"""
        event = _normalize_monitor_event(
            {
                "id": "web:chat:search",
                "slug": "web-chat-search",
                "source": "chat",
                "kind": "page_change",
                "level": 2,
                "title": "Chat 页面更新",
                "summary": "一段列表摘要",
                "changed_at": "2026-11-16T12:30:00+08:00",
                "page_url": "https://chat.deepseek.com/",
                "payload": {
                    "what_changed": "当前对话已归档，入口挪到了侧栏",
                    "quality": "partial",
                    "level_decision": {
                        "final": 2,
                        "source": "guard",
                        "rule": 1,
                        "reason": "证据不完整，按值得看发布并标注待核实",
                    },
                    "changes": [
                        {"kind": "text", "preview": "预览里的隐藏文案"},
                    ],
                },
                "changes_detail": [
                    {
                        "page": "新增用户文案",
                        "level": 2,
                        "bullets": ["详情 bullet 里的归档提示"],
                        "diff": "",
                    }
                ],
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        assert event is not None
        search_text = event["search_text"]
        self.assertIn("当前对话已归档", search_text)
        self.assertIn("详情 bullet 里的归档提示", search_text)
        self.assertIn("预览里的隐藏文案", search_text)
        self.assertLessEqual(len(search_text), 20000)
        # 质量契约原样透传给站点，不在 watchsite 层丢弃。
        self.assertEqual(event["payload"]["quality"], "partial")
        self.assertEqual(event["payload"]["level_decision"]["final"], 2)

    def test_search_text_stays_within_limit_when_detail_text_is_huge(self) -> None:
        event = _normalize_monitor_event(
            {
                "id": "web:chat:huge-search",
                "slug": "web-chat-huge-search",
                "source": "chat",
                "kind": "page_change",
                "level": 2,
                "title": "Chat 页面更新",
                "summary": "摘要",
                "changed_at": "2026-11-16T12:30:00+08:00",
                "payload": {
                    "what_changed": "长文案" * 8000,
                    "changes": [{"kind": "text", "preview": "预览" * 8000}],
                },
                "changes_detail": [
                    {"page": "新增用户文案", "level": 2, "bullets": ["bullet" * 8000], "diff": ""},
                ],
            },
            int(FIXED_NOW.timestamp()),
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertLessEqual(len(event["search_text"]), 20000)

    def test_models_snapshot_reads_catalog_versions_and_fingerprints(self) -> None:
        state = {
            "models": {
                "catalog": ["deepseek-flash", "deepseek-v4-pro"],
                "catalog_checked_at": "2026-11-13T15:50:00Z",
                "versions_checked_at": "2026-11-13T15:50:00Z",
                "catalog_baseline_at": "2026-11-13T15:00:00Z",
                "catalog_first_seen": {
                    "deepseek-flash": "2026-11-13T15:00:00Z",
                    "deepseek-v4-pro": "2026-11-13T15:20:00Z",
                },
                "versions": {
                    "deepseek-flash": "DeepSeek-V4.1-Flash-1017",
                    "deepseek-v4-pro": "DeepSeek-V4-Pro-0813",
                },
                "fingerprints": {
                    "deepseek-flash": {
                        "value": "bc19ba97",
                        "readable": "fp_flash_1017",
                        "checked_at": "2026-11-13T15:50:00Z",
                    }
                },
                "last_catalog_change": {
                    "at": "2026-11-13T15:20:00Z",
                    "added": ["deepseek-v4-pro"],
                    "removed": [],
                },
            }
        }
        snapshot = _models_snapshot(state)
        self.assertTrue(snapshot["catalog_available"])
        self.assertEqual(snapshot["checked_at"], "2026-11-13T15:50:00Z")
        self.assertEqual(snapshot["catalog_checked_at"], "2026-11-13T15:50:00Z")
        self.assertEqual(snapshot["catalog_last_error"], "")
        self.assertEqual(snapshot["catalog_fail_streak"], 0)
        self.assertEqual(snapshot["catalog_incomplete_streak"], 0)
        self.assertEqual(len(snapshot["models"]), 2)
        flash = next(item for item in snapshot["models"] if item["id"] == "deepseek-flash")
        self.assertEqual(flash["version"], "DeepSeek-V4.1-Flash-1017")
        self.assertEqual(flash["fingerprint"], "fp_flash_1017")
        self.assertEqual(flash["first_seen"], "2026-11-13T15:00:00Z")
        self.assertTrue(flash["in_catalog"])
        self.assertEqual(snapshot["last_change"]["added"], ["deepseek-v4-pro"])

    def test_models_snapshot_exposes_catalog_failure_state(self) -> None:
        """P2-19：目录失败次数和脱敏错误要随快照上站，站点才能显示目录不新鲜。"""
        snapshot = _models_snapshot(
            {
                "models": {
                    "catalog": ["deepseek-flash"],
                    "catalog_checked_at": "2026-11-13T15:50:00Z",
                    "catalog_last_error": "ApiError: HTTP 401: unauthorized",
                    "catalog_fail_streak": "3",
                    "catalog_incomplete_streak": 2,
                }
            }
        )
        self.assertTrue(snapshot["catalog_available"])
        self.assertEqual(snapshot["catalog_checked_at"], "2026-11-13T15:50:00Z")
        self.assertEqual(snapshot["catalog_last_error"], "ApiError: HTTP 401: unauthorized")
        self.assertEqual(snapshot["catalog_fail_streak"], 3)
        self.assertEqual(snapshot["catalog_incomplete_streak"], 2)
        # 坏值不能把整轮采集打崩。
        bad = _models_snapshot(
            {
                "models": {
                    "catalog": ["deepseek-flash"],
                    "catalog_fail_streak": "not-a-number",
                    "catalog_incomplete_streak": None,
                }
            }
        )
        self.assertEqual(bad["catalog_fail_streak"], 0)
        self.assertEqual(bad["catalog_incomplete_streak"], 0)

    def test_models_snapshot_without_catalog_is_not_error(self) -> None:
        snapshot = _models_snapshot(
            {
                "models": {
                    "versions": {"deepseek-flash": "V1"},
                    "versions_checked_at": "2026-11-13T15:50:00Z",
                }
            }
        )
        self.assertFalse(snapshot["catalog_available"])
        self.assertEqual(len(snapshot["models"]), 1)
        self.assertFalse(snapshot["models"][0]["in_catalog"])
        self.assertTrue(snapshot["models"][0]["first_seen"])
        self.assertIsNone(snapshot["last_change"])

    def test_build_batch_includes_models_snapshot_with_catalog(self) -> None:
        root = temp_dir("collect-models-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "models": {
                        "catalog": ["deepseek-v4-pro"],
                        "catalog_checked_at": "2026-11-13T15:50:00Z",
                        "versions": {"deepseek-v4-pro": "DeepSeek-V4-Pro-0813"},
                        "versions_checked_at": "2026-11-13T15:50:00Z",
                    }
                }
            ),
            encoding="utf-8",
        )
        batch = build_batch(
            root / "missing.db", root, events_dir, state, now=FIXED_NOW
        )
        snapshot = batch["meta"]["models_snapshot"]
        self.assertTrue(snapshot["catalog_available"])
        self.assertEqual(snapshot["checked_at"], "2026-11-13T15:50:00Z")
        self.assertEqual(snapshot["models"][0]["id"], "deepseek-v4-pro")
        self.assertEqual(snapshot["models"][0]["version"], "DeepSeek-V4-Pro-0813")

    def test_build_batch_omits_empty_models_snapshot(self) -> None:
        root = temp_dir("collect-no-models-")
        self.addCleanup(cleanup, root)
        events_dir = root / "events"
        events_dir.mkdir()
        batch = build_batch(
            root / "missing.db", root, events_dir, root / "state.json", now=FIXED_NOW
        )
        self.assertNotIn("models_snapshot", batch["meta"])

    def test_bad_fail_streak_does_not_crash(self) -> None:
        now_epoch = int(FIXED_NOW.timestamp())
        state = {
            "sources": {
                "chat": {
                    "last_checked": "2026-11-13T15:50:00Z",
                    "last_success": "2026-11-13T15:50:00Z",
                    "fail_streak": "not-a-number",
                    "last_error": "",
                }
            }
        }
        row = _monitor_status(state, "chat", [], now_epoch)
        self.assertEqual(row["source"], "chat")
        self.assertEqual(row["status"], "ok")


if __name__ == "__main__":
    unittest.main()
