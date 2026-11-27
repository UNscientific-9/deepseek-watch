"""新前端事件复核 worker。

用法：
    python -m watchsite.recheck --once          # systemd 使用
    python -m watchsite.recheck --dry-run       # 只扫描/打印，不启动 agent、不写事件
    python -m watchsite.recheck --enqueue <事件id>
    python -m watchsite.recheck --retry <事件id>
    python -m watchsite.recheck --revert <事件id>
    python -m watchsite.recheck --status
    python -m watchsite.recheck --init-baseline

触发：`deepseek-watch-push.service` 成功后 OnSuccess 启动本服务；5 分钟 timer 兜底。
worker 是 root，只做队列/校验/写事件/通知门控；agent 由 systemd-run 降权到 pi。
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import recheck_agent, recheck_apply, recheck_config as cfg, recheck_job, recheck_notify
from . import recheck_verdict

LOG = logging.getLogger("watchsite.recheck")
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: object, now: datetime) -> float | None:
    parsed = _parse_iso(value)
    return (now - parsed).total_seconds() if parsed else None


def _slug(value: object, fallback: str = "event") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (text or fallback)[:100].strip("-") or fallback


# 复核只看这些字段；payload.quality_state / level_decision / recheck 等内部记录
# 变化不应触发二次复核，否则审计回写会让 baseline 历史事件复活。
HASH_TOP_FIELDS = (
    "id", "source", "kind", "level", "level_label", "title", "summary", "impact",
    "action", "signal_count", "changed_at", "page_url", "archive_url", "evidence",
)
HASH_PAYLOAD_FIELDS = (
    "frontend", "changes", "commit_id", "level_rule", "what_changed",
    "degraded", "frontend_health",
)
HASH_FRONTEND_FIELDS = (
    "totals", "main_js", "main_css", "changed_files", "changed_code_snippets",
    "cjk_added", "api_added", "upper_added", "css_vars_added", "i18n",
    "css_rule_changes", "added_files", "removed_files", "snippets",
    "truncated", "baseline_missing", "old_commit", "new_commit", "old_build",
    "new_build", "main_js_url", "main_css_url",
)


def content_hash(event: dict) -> str:
    """事件复核内容的稳定哈希（排除会由审计/复核自己改写的内部字段）。

    frontend 只取复核真正会看的键，避免每轮把几百条事件的完整证据包
    重新 JSON 序列化；analysis 由审计可能回写，也不纳入，防止历史事件复活。
    """
    import hashlib

    if not isinstance(event, dict):
        return ""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    frontend = payload.get("frontend") if isinstance(payload.get("frontend"), dict) else {}
    material = {
        "top": {key: event.get(key) for key in HASH_TOP_FIELDS if key in event},
        "payload": {
            key: payload.get(key)
            for key in HASH_PAYLOAD_FIELDS
            if key in payload and key != "frontend"
        },
        "frontend": {
            key: frontend.get(key) for key in HASH_FRONTEND_FIELDS if key in frontend
        },
    }
    body = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def load_event(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_eligible(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    if str(event.get("source") or "") not in cfg.sources():
        return False
    if str(event.get("kind") or "") != "page_change":
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    frontend = payload.get("frontend")
    return isinstance(frontend, dict) and bool(frontend.get("totals"))


def _quality(event: dict) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(payload.get("quality") or "")


def _level(event: dict) -> int:
    try:
        return max(0, min(3, int(event.get("level") or 0)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------


class Queue:
    STATUSES = ("queued", "running", "done", "failed", "ignored")

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            # dry-run 不能碰生产队列：已存在时以只读方式打开，不存在时用内存库。
            if self.path.exists():
                uri = self.path.resolve().as_uri() + "?mode=ro"
                self.conn = sqlite3.connect(uri, uri=True, timeout=30)
                self.conn.row_factory = sqlite3.Row
                return
            self.conn = sqlite3.connect(":memory:", timeout=30)
            self.conn.row_factory = sqlite3.Row
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recheck_queue (
              event_id      TEXT PRIMARY KEY,
              event_path    TEXT NOT NULL,
              event_hash    TEXT NOT NULL,
              source        TEXT NOT NULL,
              quality       TEXT NOT NULL DEFAULT '',
              status        TEXT NOT NULL DEFAULT 'queued',
              attempts      INTEGER NOT NULL DEFAULT 0,
              enqueued_at   TEXT NOT NULL,
              started_at    TEXT,
              finished_at   TEXT,
              last_error    TEXT NOT NULL DEFAULT '',
              verdict_json  TEXT NOT NULL DEFAULT '',
              job_dir       TEXT NOT NULL DEFAULT '',
              level_before  INTEGER NOT NULL DEFAULT 0,
              quality_before TEXT NOT NULL DEFAULT '',
              updated_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_recheck_status
              ON recheck_queue(status, enqueued_at);
            CREATE TABLE IF NOT EXISTS recheck_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recheck_seen (
              event_id    TEXT PRIMARY KEY,
              event_hash  TEXT NOT NULL,
              event_path  TEXT NOT NULL,
              archived_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def get(self, event_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM recheck_queue WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def seen(self, event_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM recheck_seen WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def touch_seen(self, event_id: str, *, event_path: Path, event_hash_value: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO recheck_seen(event_id, event_hash, event_path, archived_at) "
            "VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
            "event_hash = excluded.event_hash, event_path = excluded.event_path, "
            "archived_at = excluded.archived_at",
            (event_id, event_hash_value, str(event_path), _iso(now)),
        )
        self.conn.commit()

    def forget_seen(self, event_id: str) -> None:
        self.conn.execute("DELETE FROM recheck_seen WHERE event_id = ?", (event_id,))
        self.conn.commit()

    def rows(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            cur = self.conn.execute(
                "SELECT * FROM recheck_queue WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM recheck_queue ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in cur.fetchall()]

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in self.STATUSES}
        for row in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM recheck_queue GROUP BY status"
        ).fetchall():
            counts[str(row["status"])] = int(row["n"])
        return counts

    def meta(self, key: str) -> str:
        row = self.conn.execute(
            "SELECT value FROM recheck_meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else ""

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO recheck_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def _write(
        self,
        event_id: str,
        *,
        status: str,
        now: datetime,
        **fields,
    ) -> None:
        values = {
            "status": status,
            "updated_at": _iso(now),
            **fields,
        }
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.conn.execute(
            f"UPDATE recheck_queue SET {assignments} WHERE event_id = ?",
            (*values.values(), event_id),
        )
        self.conn.commit()

    def insert(
        self,
        event_id: str,
        *,
        event_path: Path,
        event_hash_value: str,
        source: str,
        quality: str,
        status: str,
        now: datetime,
        level_before: int = 0,
        quality_before: str = "",
        attempts: int = 0,
        last_error: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO recheck_queue("
            "event_id, event_path, event_hash, source, quality, status, attempts, "
            "enqueued_at, started_at, finished_at, last_error, verdict_json, job_dir, "
            "level_before, quality_before, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                str(event_path),
                event_hash_value,
                source,
                quality,
                status,
                max(0, int(attempts)),
                _iso(now),
                None,
                None,
                last_error,
                "",
                "",
                int(level_before),
                quality_before,
                _iso(now),
            ),
        )
        self.conn.commit()

    def upsert_queued(
        self,
        event_id: str,
        *,
        event_path: Path,
        event_hash_value: str,
        source: str,
        quality: str,
        level_before: int,
        now: datetime,
        reset_attempts: bool = True,
        last_error: str = "",
    ) -> str:
        record = self.get(event_id)
        if record is None:
            self.insert(
                event_id,
                event_path=event_path,
                event_hash_value=event_hash_value,
                source=source,
                quality=quality,
                status="queued",
                now=now,
                level_before=level_before,
                quality_before=quality,
                last_error=last_error,
            )
            return "inserted"
        attempts = 0 if reset_attempts else int(record.get("attempts") or 0)
        self._write(
            event_id,
            status="queued",
            now=now,
            event_path=str(event_path),
            event_hash=event_hash_value,
            source=source,
            quality=quality,
            attempts=attempts,
            started_at=None,
            finished_at=None,
            last_error=last_error[:1000],
            verdict_json="",
            job_dir="",
            level_before=int(level_before),
            quality_before=quality,
        )
        return "requeued"

    def claim_next(self, now: datetime) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM recheck_queue WHERE status = 'queued' "
            "ORDER BY enqueued_at, rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        attempts = int(record.get("attempts") or 0) + 1
        self._write(
            str(record["event_id"]),
            status="running",
            now=now,
            attempts=attempts,
            started_at=_iso(now),
            last_error="",
            finished_at=None,
        )
        record["attempts"] = attempts
        record["status"] = "running"
        record["started_at"] = _iso(now)
        return record

    def mark_done(
        self,
        event_id: str,
        *,
        now: datetime,
        verdict_json: str = "",
        job_dir: str = "",
        last_error: str = "",
        event_hash_value: str | None = None,
        quality: str | None = None,
    ) -> None:
        fields = {
            "finished_at": _iso(now),
            "verdict_json": verdict_json[:8000],
            "job_dir": job_dir[:500],
            "last_error": last_error[:1000],
        }
        if event_hash_value is not None:
            fields["event_hash"] = event_hash_value
        if quality is not None:
            fields["quality"] = quality
        self._write(event_id, status="done", now=now, **fields)

    def mark_failed(
        self,
        event_id: str,
        *,
        now: datetime,
        error: str,
        job_dir: str = "",
    ) -> str:
        record = self.get(event_id)
        attempts = int(record.get("attempts") or 0) if record else 0
        status = "failed" if attempts >= cfg.max_attempts() else "queued"
        self._write(
            event_id,
            status=status,
            now=now,
            finished_at=_iso(now) if status == "failed" else None,
            last_error=str(error)[:1000],
            job_dir=job_dir[:500],
        )
        return status

    def mark_failed_terminal(
        self, event_id: str, *, now: datetime, error: str, job_dir: str = ""
    ) -> None:
        """不可重试的失败（例如沙箱不可用）直接落 failed。"""
        self._write(
            event_id,
            status="failed",
            now=now,
            finished_at=_iso(now),
            last_error=str(error)[:1000],
            job_dir=job_dir[:500],
        )

    def requeue(
        self,
        event_id: str,
        *,
        now: datetime,
        error: str = "",
        decrement_attempts: bool = False,
    ) -> None:
        fields = {
            "started_at": None,
            "finished_at": None,
            "last_error": str(error)[:1000],
        }
        if decrement_attempts:
            record = self.get(event_id)
            fields["attempts"] = max(0, int((record or {}).get("attempts") or 0) - 1)
        self._write(event_id, status="queued", now=now, **fields)

    def touch_path(self, event_id: str, path: Path, *, now: datetime) -> None:
        record = self.get(event_id)
        if not record:
            return
        self._write(
            event_id,
            status=str(record.get("status") or "queued"),
            now=now,
            event_path=str(path),
        )

    def mark_ignored(self, event_id: str, *, now: datetime, error: str = "") -> None:
        self._write(
            event_id,
            status="ignored",
            now=now,
            finished_at=_iso(now),
            last_error=str(error)[:1000],
        )

    def recover_stale(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=cfg.stale_running_seconds())
        recovered = 0
        for row in self.conn.execute(
            "SELECT * FROM recheck_queue WHERE status = 'running'"
        ).fetchall():
            started = _parse_iso(row["started_at"])
            if started is None or started <= cutoff:
                attempts = int(row["attempts"] or 0)
                status = "failed" if attempts >= cfg.max_attempts() else "queued"
                self._write(
                    str(row["event_id"]),
                    status=status,
                    now=now,
                    finished_at=_iso(now) if status == "failed" else None,
                    last_error="running 超过时限，已回收",
                )
                recovered += 1
        return recovered

    def cleanup(self, now: datetime) -> dict:
        done_cutoff = _iso(now - timedelta(days=cfg.queue_keep_days()))
        # 不能直接删掉 done/ignored：事件文件还在，下一次扫描会把它们当成
        # 新事件重跑，历史会复活。先写进 recheck_seen 墓碑表，再删队列行。
        archived_rows = self.conn.execute(
            "SELECT * FROM recheck_queue WHERE status IN ('done','ignored') "
            "AND updated_at < ?",
            (done_cutoff,),
        ).fetchall()
        for row in archived_rows:
            self.conn.execute(
                "INSERT INTO recheck_seen(event_id, event_hash, event_path, archived_at) "
                "VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                "event_hash = excluded.event_hash, event_path = excluded.event_path, "
                "archived_at = excluded.archived_at",
                (row["event_id"], row["event_hash"], row["event_path"], _iso(now)),
            )
        archived = len(archived_rows)
        deleted_old = self.conn.execute(
            "DELETE FROM recheck_queue WHERE status IN ('done','ignored') AND updated_at < ?",
            (done_cutoff,),
        ).rowcount
        failed_rows = self.conn.execute(
            "SELECT event_id, event_hash, event_path FROM recheck_queue WHERE status = 'failed' "
            "ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (cfg.failed_keep(),),
        ).fetchall()
        deleted_failed = 0
        for row in failed_rows:
            # 失败记录超限后也要写墓碑：事件文件还在，不能下一轮又当新事件重跑。
            self.conn.execute(
                "INSERT INTO recheck_seen(event_id, event_hash, event_path, archived_at) "
                "VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                "event_hash = excluded.event_hash, event_path = excluded.event_path, "
                "archived_at = excluded.archived_at",
                (row["event_id"], row["event_hash"], row["event_path"], _iso(now)),
            )
            deleted_failed += self.conn.execute(
                "DELETE FROM recheck_queue WHERE event_id = ?", (row["event_id"],)
            ).rowcount
        self.conn.commit()
        return {
            "done": max(0, deleted_old),
            "archived": archived,
            "failed": max(0, deleted_failed),
        }


# ---------------------------------------------------------------------------
# 扫描 / 入队
# ---------------------------------------------------------------------------


def _event_files(events_dir: Path) -> list[Path]:
    try:
        return sorted(
            (path for path in Path(events_dir).glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
        )
    except OSError:
        return []


def find_event_path(events_dir: Path, event_id: str) -> Path | None:
    target = str(event_id or "")
    if not target:
        return None
    for path in _event_files(events_dir):
        event = load_event(path)
        if not event:
            continue
        if str(event.get("id") or "") == target or str(event.get("slug") or "") == target:
            return path
    return None


def published_event_ids() -> set[str]:
    """读取 watchsite 推送状态，返回已经成功上站的事件 id。

    OnSuccess 只在推送成功后触发；timer 兜底时用它挡住「事件已生成但推送
    失败/还没推」的情况，避免复核跑在上站之前。
    """
    path = cfg.push_state_dir() / "push-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    published: set[str] = set()
    for key in ("sent_ids",):
        value = data.get(key)
        if isinstance(value, list):
            published.update(str(item) for item in value if str(item))
    versions = data.get("sent_versions")
    if isinstance(versions, dict):
        published.update(str(item) for item in versions if str(item))
    return published


def scan(
    queue: Queue,
    events_dir: Path,
    *,
    now: datetime | None = None,
    baseline: bool = True,
    published: set[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """扫描事件目录；首次运行把现有事件标 ignored，不跑历史。

    dry_run=True 时只读：不写队列、不写 meta，仅统计「本来会做什么」。
    """
    now = _now(now)
    stats = {
        "seen": 0,
        "eligible": 0,
        "enqueued": 0,
        "requeued": 0,
        "ignored": 0,
        "baseline": 0,
        "unpublished": 0,
    }
    first_run = baseline and not queue.meta("baseline_at")
    for path in _event_files(events_dir):
        stats["seen"] += 1
        event = load_event(path)
        if not event:
            continue
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        record = queue.get(event_id)
        if not is_eligible(event):
            if record and record.get("status") in ("queued", "running"):
                if not dry_run:
                    queue.mark_ignored(event_id, now=now, error="不再符合复核范围")
                stats["ignored"] += 1
            continue
        stats["eligible"] += 1
        hash_value = content_hash(event)
        if record is None:
            # 首次基线要把历史全部吃掉（无论是否已推送），之后只处理
            # 已经在 watchsite push-state 里成功上站的事件。
            if not first_run and published is not None and event_id not in published:
                stats["unpublished"] += 1
                continue
            seen = queue.seen(event_id)
            if seen:
                # 已归档的墓碑：事件文件还在，不能重新当成新事件。
                if str(seen.get("event_hash") or "") != hash_value and not dry_run:
                    queue.touch_seen(
                        event_id, event_path=path, event_hash_value=hash_value, now=now
                    )
                continue
            if first_run:
                stats["baseline"] += 1
                if not dry_run:
                    queue.insert(
                        event_id,
                        event_path=path,
                        event_hash_value=hash_value,
                        source=str(event.get("source") or ""),
                        quality=_quality(event),
                        status="ignored",
                        now=now,
                        level_before=_level(event),
                        quality_before=_quality(event),
                        last_error="首次部署基线，不跑历史事件",
                    )
            else:
                stats["enqueued"] += 1
                if not dry_run:
                    queue.insert(
                        event_id,
                        event_path=path,
                        event_hash_value=hash_value,
                        source=str(event.get("source") or ""),
                        quality=_quality(event),
                        status="queued",
                        now=now,
                        level_before=_level(event),
                        quality_before=_quality(event),
                    )
            continue
        if str(record.get("event_hash") or "") == hash_value:
            if str(record.get("event_path") or "") != str(path) and not dry_run:
                queue.touch_path(event_id, path, now=now)
            continue
        # 内容变了：同一事件被改写，旧结论必须丢弃。
        status = str(record.get("status") or "")
        if status == "running":
            # 单 worker 下 running 只可能是上次崩溃残留；recover_stale 已处理，
            # 这里再保守地重新排队，避免旧 verdict 覆盖新内容。
            stats["requeued"] += 1
            if not dry_run:
                queue.upsert_queued(
                    event_id,
                    event_path=path,
                    event_hash_value=hash_value,
                    source=str(event.get("source") or ""),
                    quality=_quality(event),
                    level_before=_level(event),
                    now=now,
                    reset_attempts=True,
                    last_error="事件在复核期间被改写，已重新入队",
                )
        elif status in ("done", "failed", "ignored", "queued"):
            stats["requeued"] += 1
            if not dry_run:
                queue.upsert_queued(
                    event_id,
                    event_path=path,
                    event_hash_value=hash_value,
                    source=str(event.get("source") or ""),
                    quality=_quality(event),
                    level_before=_level(event),
                    now=now,
                    reset_attempts=True,
                    last_error="事件内容变化，已重新入队",
                )
    if not dry_run:
        if first_run:
            queue.set_meta("baseline_at", _iso(now))
        queue.set_meta("last_scan_at", _iso(now))
        queue.set_meta("schema_version", str(SCHEMA_VERSION))
    return stats


def init_baseline(
    queue: Queue, events_dir: Path, *, now: datetime | None = None, dry_run: bool = False
) -> int:
    """显式把当前所有符合条件、且不在队列里的事件标 ignored。

    事件目录不存在时直接报错，避免在空目录上「成功」建出空基线。
    """
    now = _now(now)
    if not Path(events_dir).is_dir():
        raise FileNotFoundError(f"事件目录不存在：{events_dir}")
    added = 0
    for path in _event_files(events_dir):
        event = load_event(path)
        if not event or not is_eligible(event):
            continue
        event_id = str(event.get("id") or "")
        if not event_id or queue.get(event_id) or queue.seen(event_id):
            continue
        if not dry_run:
            queue.insert(
                event_id,
                event_path=path,
                event_hash_value=content_hash(event),
                source=str(event.get("source") or ""),
                quality=_quality(event),
                status="ignored",
                now=now,
                level_before=_level(event),
                quality_before=_quality(event),
                last_error="手工建立基线，不跑历史事件",
            )
        added += 1
    if not dry_run:
        queue.set_meta("baseline_at", _iso(now))
    return added


# ---------------------------------------------------------------------------
# 写事件（保留原属主/权限）
# ---------------------------------------------------------------------------


def write_event_preserving(path: Path, event: dict) -> None:
    """原子写事件，并保留原文件的属主/权限（webwatch DynamicUser 必须能读）。"""
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        info = path.stat()
        uid, gid, mode = info.st_uid, info.st_gid, info.st_mode & 0o777
    except OSError:
        uid = gid = None
        mode = 0o640
    tmp = path.with_name(f".{path.name}.recheck-{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode or 0o640)
        if os.name == "posix" and uid is not None and gid is not None:
            try:
                os.chown(tmp, uid, gid)
            except OSError as exc:
                LOG.error("保留事件属主失败 %s: %s", path, exc)
                raise
        os.replace(tmp, path)
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 单实例 / 互斥
# ---------------------------------------------------------------------------


def _acquire_lock(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _release_lock(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass


def other_agent_running() -> bool:
    """best-effort：探测本机是否已有 pi/Claude/opencode 任务在跑。"""
    if os.name != "posix":
        return False
    try:
        import subprocess

        result = subprocess.run(
            ["pgrep", "-af", r"/opt/agent/bin/agent|/opt/agent/bin/agent|opencode"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return False
    if result.returncode != 0:
        return False
    own = str(os.getpid())
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        if parts[0] == own:
            continue
        # worker 自己启动 systemd-run 时命令行也含 claude 路径；只在启动前调用，
        # 因此这里不需要排除子进程。
        return True
    return False


# ---------------------------------------------------------------------------
# job / agent / apply
# ---------------------------------------------------------------------------


def job_dir_for(event: dict, hash_value: str) -> Path:
    slug = _slug(event.get("slug") or event.get("id"))
    return cfg.jobs_dir() / f"{slug}-{hash_value[:8]}"


def _start_latency(event_path: Path, now: datetime) -> dict:
    """记录事件文件 mtime / 最近推送时间到复核启动的差值。"""
    try:
        mtime = Path(event_path).stat().st_mtime
    except OSError:
        mtime = 0
    push_ts = 0.0
    try:
        push_dir = Path(os.getenv("RECHECK_PUSH_STATE_DIR", "/var/lib/deepseek-watch-push"))
        for name in ("push-state.json", "state.json"):
            try:
                data = json.loads((push_dir / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            metrics = data.get("metrics") if isinstance(data, dict) else None
            parsed = _parse_iso((metrics or {}).get("last_push_ts"))
            if parsed:
                push_ts = parsed.timestamp()
                break
    except (OSError, ValueError):
        push_ts = 0.0
    start_ts = now.timestamp()
    return {
        "event_mtime_ts": int(mtime),
        "push_ts": int(push_ts),
        "event_mtime_age_seconds": round(start_ts - mtime, 1) if mtime else None,
        "push_age_seconds": round(start_ts - push_ts, 1) if push_ts else None,
    }


def _compact_verdict_for_queue(verdict: dict) -> str:
    return json.dumps(
        {
            "verdict": verdict.get("verdict"),
            "confidence": verdict.get("confidence"),
            "quality": verdict.get("quality"),
            "level_suggestion": verdict.get("level_suggestion"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def process_one(
    queue: Queue,
    runner,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """取一条 queued，建 job、跑 agent、校验、应用、写事件。"""
    now = _now(now)
    record = queue.claim_next(now)
    if record is None:
        return {"status": "empty"}
    event_id = str(record["event_id"])
    event_path = Path(record["event_path"])
    job_dir = Path(record["job_dir"] or job_dir_for({}, str(record["event_hash"])))
    result = {"status": "failed", "event_id": event_id, "job_dir": str(job_dir)}
    try:
        event = load_event(event_path)
        if not event:
            queue.mark_failed(event_id, now=now, error="事件文件不存在或无法解析")
            return {**result, "error": "event missing"}
        current_hash = content_hash(event)
        if current_hash != str(record["event_hash"]):
            queue.upsert_queued(
                event_id,
                event_path=event_path,
                event_hash_value=current_hash,
                source=str(event.get("source") or ""),
                quality=_quality(event),
                level_before=_level(event),
                now=now,
                reset_attempts=True,
                last_error="事件在复核前被改写，已重新入队",
            )
            return {**result, "status": "requeued", "error": "hash changed before job"}
        if dry_run:
            # 真正的只读空跑：不建 job、不写队列、不启动 agent。
            return {**result, "status": "dry_run"}
        job_dir = job_dir_for(event, current_hash)
        result["job_dir"] = str(job_dir)
        latency = _start_latency(event_path, now)
        LOG.info(
            "REVIEW_START event_id=%s source=%s level=%s quality=%s event_mtime_ts=%s "
            "push_ts=%s event_mtime_age_seconds=%s push_age_seconds=%s",
            event_id,
            event.get("source"),
            event.get("level"),
            _quality(event),
            latency["event_mtime_ts"],
            latency["push_ts"],
            latency["event_mtime_age_seconds"],
            latency["push_age_seconds"],
        )
        if latency["push_age_seconds"] is not None and latency["push_age_seconds"] > 60:
            LOG.warning("复核启动距最近一次推送 %.1f 秒，超过 60 秒目标", latency["push_age_seconds"])
        job_info = recheck_job.build_job(
            job_dir,
            event,
            events_dir=event_path.parent,
            vault_dir=cfg.vault_dir(),
        )
        recheck_agent.prepare_job_permissions(job_dir)
        result.update(
            {
                "job_bytes": job_info["bytes"],
                "job_digest": job_info["digest"],
                "vault_available": job_info["vault_available"],
            }
        )
        LOG.info(
            "JOB_BUILT event_id=%s job=%s bytes=%s digest=%s vault=%s",
            event_id,
            job_dir.name,
            job_info["bytes"],
            job_info["digest"][:16],
            job_info["vault_available"],
        )

        # 重试时 out/ 会保留上一次现场；必须删掉旧 verdict，否则 agent 没写
        # 新文件时会把上一轮的结论当成这一轮的。
        try:
            (job_dir / "out" / "verdict.json").unlink(missing_ok=True)
        except OSError as exc:
            LOG.warning("清理旧 verdict 失败：%s", exc)
        agent_lock = _acquire_lock(cfg.state_dir() / "agent.lock")
        if agent_lock is None:
            queue.requeue(
                event_id, now=now, error="另一个 agent 正在运行", decrement_attempts=True
            )
            return {**result, "status": "busy"}
        try:
            run_result = runner.run(job_dir, event, timeout=cfg.agent_timeout())
        finally:
            _release_lock(agent_lock)
        duration = int(run_result.get("duration_seconds") or 0)
        verdict = run_result.get("verdict")
        if verdict is None:
            verdict = recheck_verdict.load_verdict(
                job_dir / "out" / "verdict.json",
                event=event,
                allowed_hosts=cfg.allowed_hosts(),
            )
        else:
            # runner 可能直接返回 dict（测试/自定义）；统一再过一次校验。
            verdict = recheck_verdict.validate_verdict(
                verdict, event=event, allowed_hosts=cfg.allowed_hosts()
            )

        # 版本守卫：agent 跑完后事件可能被新一轮巡检改写。
        latest = load_event(event_path)
        latest_hash = content_hash(latest) if latest else ""
        if latest is None or latest_hash != current_hash:
            queue.upsert_queued(
                event_id,
                event_path=event_path,
                event_hash_value=latest_hash or current_hash,
                source=str((latest or event).get("source") or ""),
                quality=_quality(latest or event),
                level_before=_level(latest or event),
                now=now,
                reset_attempts=True,
                last_error="事件在复核期间被改写，旧 verdict 已丢弃",
            )
            return {**result, "status": "discarded", "error": "hash changed during recheck"}

        # 用最新事件对象合并复核结果：agent 运行期间 audit 可能已写入
        # analysis / quality / quality_state，不能拿启动前的旧快照整体写回。
        audit_quality_changed = _quality(latest) != _quality(event)
        apply_result = recheck_apply.apply_recheck(
            latest,
            verdict,
            apply_level=cfg.apply_level_enabled(),
            now=now,
            job=job_dir.name,
            duration_seconds=duration,
            attempts=int(record.get("attempts") or 1),
            skip_quality=audit_quality_changed,
        )
        notify_result = recheck_notify.notify_upgrade(latest, now_ts=int(now.timestamp()))
        recheck_notify.apply_notification_result(latest, notify_result, now_iso=_iso(now))
        write_event_preserving(event_path, latest)
        queue.mark_done(
            event_id,
            now=now,
            verdict_json=_compact_verdict_for_queue(verdict),
            job_dir=str(job_dir),
            last_error="；".join(notify_result.get("errors") or [])[:1000],
            event_hash_value=content_hash(latest),
            quality=_quality(latest),
        )
        LOG.info(
            "REVIEW_DONE event_id=%s verdict=%s quality=%s level=%s->%s applied=%s "
            "duration=%ss notify=%s",
            event_id,
            verdict.get("verdict"),
            verdict.get("quality"),
            apply_result["from"]["level"],
            apply_result["to"]["level"],
            ",".join(apply_result["applied"]) or "-",
            duration,
            ",".join(notify_result.get("sent") or []) or notify_result.get("skipped") or "-",
        )
        cleanup = cleanup_job_evidence(job_dir)
        if cleanup["removed_bytes"]:
            LOG.info(
                "JOB_EVIDENCE_CLEANED job=%s files=%s bytes=%s",
                job_dir.name,
                cleanup["removed_files"],
                cleanup["removed_bytes"],
            )
        return {
            **result,
            "status": "done",
            "verdict": verdict.get("verdict"),
            "applied": apply_result["applied"],
            "level_applied": apply_result["level_applied"],
            "notify": notify_result,
            "cleanup": cleanup,
        }
    except recheck_agent.SandboxUnavailable as exc:
        queue.mark_failed_terminal(event_id, now=now, error=f"沙箱不可用：{exc}", job_dir=str(job_dir))
        LOG.error("沙箱不可用，复核失败（不降级）：%s", exc)
        _alert("沙箱不可用", str(exc))
        return {**result, "status": "failed", "error": f"sandbox: {exc}"}
    except (recheck_agent.AgentTimeout, recheck_agent.AgentRunError, recheck_verdict.VerdictError) as exc:
        status = queue.mark_failed(event_id, now=now, error=str(exc), job_dir=str(job_dir))
        LOG.error("复核失败 attempts=%s status=%s: %s", record.get("attempts"), status, exc)
        return {**result, "status": status, "error": str(exc)}
    except (recheck_job.JobError, recheck_apply.ApplyError, OSError, ValueError) as exc:
        status = queue.mark_failed(event_id, now=now, error=f"{type(exc).__name__}: {exc}", job_dir=str(job_dir))
        LOG.exception("复核处理失败 status=%s", status)
        return {**result, "status": status, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        status = queue.mark_failed(event_id, now=now, error=f"{type(exc).__name__}: {exc}", job_dir=str(job_dir))
        LOG.exception("复核出现未预期错误 status=%s", status)
        return {**result, "status": status, "error": f"{type(exc).__name__}: {exc}"}


def _alert(key: str, message: str) -> None:
    """沙箱不可用/致命错误告警；冷却 6 小时，只有配置了邮件才发。"""
    if os.getenv("RECHECK_ALERT", "1").strip().lower() in {"0", "false", "off", "no"}:
        return
    try:
        from webwatch.mail import mail_enabled, send_text

        if not mail_enabled():
            return
        state_file = cfg.state_dir() / "alert-state.json"
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        last = float(data.get(key) or 0)
        if last and time.time() - last < 6 * 3600:
            return
        if send_text(f"[DeepSeek 观测站] 复核 {key}", message[:6000]):
            data[key] = time.time()
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("告警发送失败：%s", exc)


# ---------------------------------------------------------------------------
# 待通知重试
# ---------------------------------------------------------------------------


def notify_pending(
    events_dir: Path,
    *,
    now: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    now = _now(now)
    limit = cfg.max_notifications_per_run() if limit is None else max(0, int(limit))
    stats = {"scanned": 0, "pending": 0, "notified": 0, "errors": 0}
    if limit <= 0:
        return stats
    paths = sorted(
        _event_files(events_dir),
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
        reverse=True,
    )
    for path in paths:
        if stats["notified"] >= limit:
            break
        event = load_event(path)
        if not event:
            continue
        stats["scanned"] += 1
        if not recheck_notify.notification_pending(event):
            continue
        stats["pending"] += 1
        result = recheck_notify.notify_upgrade(
            event, now_ts=int(now.timestamp()), dry_run=dry_run
        )
        if result.get("errors"):
            stats["errors"] += 1
            LOG.warning("复核升级补通知失败 %s: %s", event.get("id"), result["errors"])
        if result.get("complete") and not dry_run:
            recheck_notify.apply_notification_result(event, result, now_iso=_iso(now))
            try:
                write_event_preserving(path, event)
                stats["notified"] += 1
                LOG.info("复核升级补通知完成 event_id=%s channels=%s", event.get("id"), result.get("sent"))
            except OSError as exc:
                stats["errors"] += 1
                LOG.error("补通知写事件失败 %s: %s", event.get("id"), exc)
    return stats


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


def _dir_bytes(path: Path) -> int:
    total = 0
    for item in Path(path).rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def cleanup_job_evidence(job_dir: Path | str) -> dict:
    """删除复核完成后不再需要的大块证据副本，保留结论和审计日志。

    只清 `evidence/files/`（复制进来的新旧文件）和 `evidence/changes.diff`
    （可能很大）；`BRIEF.md`、`event.json`、`verdict.json`、`out/RESULT.md`
    等仍保留，方便回看结论和排查问题。
    """
    base = Path(job_dir)
    stats = {"removed_files": 0, "removed_bytes": 0}
    if not base.is_dir():
        return stats
    targets = [base / "evidence" / "files", base / "evidence" / "changes.diff"]
    for target in targets:
        try:
            if target.is_dir() and not target.is_symlink():
                size = _dir_bytes(target)
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists() or target.is_symlink():
                size = target.stat().st_size
                target.unlink(missing_ok=True)
            else:
                continue
        except OSError as exc:  # 清理失败不能影响复核结论写回
            LOG.warning("清理复核证据失败 %s: %s", target, exc)
            continue
        stats["removed_files"] += 1
        stats["removed_bytes"] += size
    return stats


def cleanup_jobs(
    jobs_dir: Path,
    *,
    now: datetime | None = None,
    keep: int | None = None,
    max_bytes: int | None = None,
    keep_days: int | None = None,
) -> dict:
    now = _now(now)
    keep = cfg.max_jobs_kept() if keep is None else max(0, int(keep))
    max_bytes = cfg.max_jobs_bytes() if max_bytes is None else max(0, int(max_bytes))
    keep_days = cfg.job_keep_days() if keep_days is None else max(1, int(keep_days))
    stats = {"removed": 0, "bytes_before": 0, "bytes_after": 0}
    if not Path(jobs_dir).is_dir():
        return stats
    entries: list[tuple[float, Path]] = []
    for child in Path(jobs_dir).iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            entries.append((child.stat().st_mtime, child))
        except OSError:
            continue
    stats["bytes_before"] = sum(_dir_bytes(path) for _, path in entries)
    if max_bytes and stats["bytes_before"] > max_bytes:
        LOG.warning(
            "复核 job 占用 %.1f MB，超过上限 %.1f MB，将按时间清理",
            stats["bytes_before"] / 1024 / 1024,
            max_bytes / 1024 / 1024,
        )
    entries.sort(reverse=True)
    cutoff = now.timestamp() - keep_days * 86400
    keep_set: list[tuple[float, Path]] = []
    for index, (mtime, path) in enumerate(entries):
        if index >= keep or mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            stats["removed"] += 1
        else:
            keep_set.append((mtime, path))
    total = sum(_dir_bytes(path) for _, path in keep_set)
    for _, path in reversed(keep_set):
        if total <= max_bytes:
            break
        size = _dir_bytes(path)
        shutil.rmtree(path, ignore_errors=True)
        stats["removed"] += 1
        total -= size
    stats["bytes_after"] = max(0, total)
    return stats


def cleanup_all(queue: Queue, *, now: datetime | None = None) -> dict:
    now = _now(now)
    jobs = cleanup_jobs(cfg.jobs_dir(), now=now)
    queue_stats = queue.cleanup(now)
    return {"jobs": jobs, "queue": queue_stats}


# ---------------------------------------------------------------------------
# 状态 / 日志
# ---------------------------------------------------------------------------


def _write_state(**values) -> None:
    path = cfg.state_dir() / "state.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        logs = cfg.logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            logs / "recheck.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
    except OSError:
        pass


def status(queue: Queue) -> dict:
    state = {}
    try:
        data = json.loads((cfg.state_dir() / "state.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            state = data
    except (OSError, ValueError):
        pass
    return {
        "state_dir": str(cfg.state_dir()),
        "events_dir": str(cfg.events_dir()),
        "vault_dir": str(cfg.vault_dir()),
        "sources": list(cfg.sources()),
        "counts": queue.counts(),
        "baseline_at": queue.meta("baseline_at"),
        "last_scan_at": queue.meta("last_scan_at"),
        "state": state,
        "recent": queue.rows(limit=10),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_once(
    *,
    runner=None,
    now: datetime | None = None,
    dry_run: bool = False,
    scan_only: bool = False,
    notify_first: bool = True,
    agent_probe=None,
) -> dict:
    now = _now(now)
    events_dir = cfg.events_dir()
    stats = {
        "started_at": _iso(now),
        "events_dir": str(events_dir),
        "state_dir": str(cfg.state_dir()),
        "scan": {},
        "notify": {},
        "job": {},
    }
    if not events_dir.is_dir():
        LOG.error("事件目录不存在：%s", events_dir)
        if not dry_run:
            _write_state(last_error=f"事件目录不存在：{events_dir}", last_run=_iso(now))
        return {**stats, "error": "events dir missing"}
    queue = Queue(cfg.queue_db_path(), read_only=dry_run)
    try:
        recovered = 0 if dry_run else queue.recover_stale(now)
        if recovered:
            LOG.warning("回收卡死的 running 记录 %s 条", recovered)
        published = published_event_ids() if cfg.require_pushed() else None
        if published is not None and not published:
            if queue.meta("baseline_at"):
                # 基线之后 push-state 突然读不到，静默跳过会让复核永久停摆。
                message = (
                    "push-state 缺失/为空，无法确认哪些事件已经上站；"
                    "为避免误复核，本轮停止。请检查 watchsite 推送状态文件。"
                )
                LOG.error(message)
                if not dry_run:
                    _alert("push-state 缺失", message)
                    _write_state(last_error=message, last_run=_iso(now))
                return {**stats, "error": "push-state missing"}
            LOG.warning("首次基线阶段 push-state 为空，先建立基线")
        stats["scan"] = scan(queue, events_dir, now=now, published=published, dry_run=dry_run)
        LOG.info("SCAN %s", json.dumps(stats["scan"], ensure_ascii=False))
        if notify_first and not dry_run:
            stats["notify"] = notify_pending(events_dir, now=now)
        if scan_only:
            stats["job"] = {"status": "scan_only"}
        elif dry_run:
            stats["job"] = {"status": "dry_run"}
        else:
            probe = agent_probe or other_agent_running
            if probe():
                LOG.warning("检测到其它 pi/Claude/opencode 任务，本轮不启动 agent")
                stats["job"] = {"status": "busy"}
            else:
                runner = runner or recheck_agent.ClaudeRunner()
                stats["job"] = process_one(queue, runner, now=now)
        stats["counts"] = queue.counts()
        if not dry_run:
            stats["cleanup"] = cleanup_all(queue, now=now)
        if not dry_run:
            _write_state(
                last_run=_iso(now),
                last_scan_at=queue.meta("last_scan_at"),
                counts=stats["counts"],
                last_job=stats.get("job"),
                last_error="" if not stats.get("error") else stats["error"],
            )
        return stats
    finally:
        queue.close()


def _print_human(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 前端事件复核 worker")
    parser.add_argument("--once", action="store_true", help="单次执行（默认）")
    parser.add_argument("--dry-run", action="store_true", help="只读扫描，不建 job、不写队列、不启动 agent")
    parser.add_argument("--scan-only", action="store_true", help="只扫描入队，不处理")
    parser.add_argument("--enqueue", metavar="EVENT_ID", help="手工把一条事件加入队列")
    parser.add_argument("--retry", metavar="EVENT_ID", help="把 failed 事件重置为 queued")
    parser.add_argument("--revert", metavar="EVENT_ID", help="用 payload.recheck.from 回滚修正")
    parser.add_argument("--init-baseline", action="store_true", help="把当前事件标为 ignored 基线")
    parser.add_argument("--status", action="store_true", help="打印队列与最近状态")
    parser.add_argument("--fake-verdict", metavar="FILE", help="离线验证：用固定 verdict 代替 agent")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    now = _now()

    if args.dry_run and (args.enqueue or args.retry or args.revert):
        print("error: --dry-run 不能与 --enqueue/--retry/--revert 同用", file=sys.stderr)
        return 2

    if args.fake_verdict and os.getenv("RECHECK_ALLOW_FAKE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        print("error: --fake-verdict 仅供离线验证，需设置 RECHECK_ALLOW_FAKE=1", file=sys.stderr)
        return 2

    if args.status:
        queue = Queue(cfg.queue_db_path(), read_only=True)
        try:
            _print_human(status(queue))
        finally:
            queue.close()
        return 0

    events_dir = cfg.events_dir()
    if args.init_baseline and not events_dir.is_dir():
        print(f"error: 事件目录不存在：{events_dir}", file=sys.stderr)
        return 1
    queue = Queue(cfg.queue_db_path(), read_only=args.dry_run)
    try:
        if args.init_baseline:
            count = init_baseline(queue, events_dir, now=now, dry_run=args.dry_run)
            print(json.dumps({"baseline_added": count, "baseline_at": queue.meta("baseline_at")}, ensure_ascii=False))
            return 0
        if args.enqueue:
            path = find_event_path(events_dir, args.enqueue)
            if path is None:
                print(f"error: 找不到事件 {args.enqueue}", file=sys.stderr)
                return 1
            event = load_event(path)
            if not event or not is_eligible(event):
                print("error: 事件不符合复核范围（source/kind/payload.frontend）", file=sys.stderr)
                return 1
            queue.forget_seen(str(event.get("id")))
            action = queue.upsert_queued(
                str(event.get("id")),
                event_path=path,
                event_hash_value=content_hash(event),
                source=str(event.get("source") or ""),
                quality=_quality(event),
                level_before=_level(event),
                now=now,
                reset_attempts=True,
                last_error="手工入队",
            )
            print(json.dumps({"event_id": event.get("id"), "action": action}, ensure_ascii=False))
            return 0
        if args.retry:
            record = queue.get(args.retry)
            if not record:
                print(f"error: 队列里没有事件 {args.retry}", file=sys.stderr)
                return 1
            queue._write(args.retry, status="queued", now=now, attempts=0, last_error="手工重试")
            print(json.dumps({"event_id": args.retry, "status": "queued"}, ensure_ascii=False))
            return 0
        if args.revert:
            path = find_event_path(events_dir, args.revert)
            if path is None:
                print(f"error: 找不到事件 {args.revert}", file=sys.stderr)
                return 1
            event = load_event(path)
            if not event:
                print("error: 事件无法解析", file=sys.stderr)
                return 1
            try:
                result = recheck_apply.revert_recheck(event, now=now)
            except recheck_apply.ApplyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            write_event_preserving(path, event)
            record = queue.get(str(event.get("id")))
            if record:
                queue.mark_done(
                    str(event.get("id")),
                    now=now,
                    last_error="手工回滚",
                    event_hash_value=content_hash(event),
                    quality=_quality(event),
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    finally:
        queue.close()

    lock = _acquire_lock(cfg.state_dir() / "worker.lock")
    if lock is None:
        LOG.warning("另一个复核 worker 正在运行（或状态目录不可写），本轮跳过")
        return 0
    try:
        runner = None
        if args.fake_verdict:
            try:
                payload = json.loads(Path(args.fake_verdict).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"error: 读取 fake verdict 失败：{exc}", file=sys.stderr)
                return 2

            def _writer(job_dir: Path, event: dict):
                value = dict(payload)
                value["event_id"] = event.get("id")
                return value

            runner = recheck_agent.FakeRunner(_writer)
        result = run_once(
            runner=runner,
            now=now,
            dry_run=args.dry_run,
            scan_only=args.scan_only,
            # 离线 fake runner 不启动真实 agent，不需要让本机正在运行的
            # Claude Code 会话阻塞验证；生产路径仍然走 other_agent_running。
            agent_probe=(lambda: False) if args.fake_verdict else None,
        )
        _print_human(result)
        return 0 if not result.get("error") else 1
    finally:
        _release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
