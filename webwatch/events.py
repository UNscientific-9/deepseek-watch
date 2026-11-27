"""事件构造、原子写入与清理。

事件格式为 schema_version=1，watchsite 可以直接读取。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import LEVEL_LABELS, MAX_EVENTS_KEPT


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def slugify(value: object, fallback: str = "event") -> str:
    # 第一处正则已把任意非字母数字串压成单个 "-"，连续横线不可能出现。
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (text or fallback)[:100].strip("-") or fallback


def build_event(
    *,
    event_id: str,
    slug: str,
    source: str,
    kind: str,
    level: int,
    title: str,
    summary: str,
    changed_at: str,
    page_url: str,
    archive_url: str = "",
    impact: str = "",
    action: str = "",
    signal_count: int = 1,
    evidence: list[str] | None = None,
    payload: dict | None = None,
) -> dict:
    level = max(0, min(3, int(level)))
    return {
        "schema_version": 1,
        "id": event_id[:220],
        "slug": slugify(slug, "web-event"),
        "source": source,
        "kind": kind,
        "level": level,
        "level_label": LEVEL_LABELS.get(level, "值得看"),
        "title": title[:300],
        "summary": summary[:4000],
        "impact": impact[:2000],
        "action": action[:2000],
        "signal_count": max(1, int(signal_count)),
        "changed_at": changed_at,
        "page_url": page_url,
        # 没有真实归档地址时留空：以前回退成 page_url，会让「查看归档快照」
        # 打开今天的实时页面，和事件描述对不上。
        "archive_url": archive_url,
        "evidence": [str(item)[:1000] for item in (evidence or [])][:20],
        "payload": payload if isinstance(payload, dict) else {},
        "generated_at": iso(now_utc()),
    }


def _fsync_directory(path: Path) -> None:
    """把目录项也刷盘，确保 rename 在掉电后仍可见（仅 POSIX）。"""
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_event(events_dir: Path, event: dict) -> Path:
    """原子写一条事件；临时文件 fsync 后再改名，重复运行幂等覆盖。

    先写盘并 fsync 是 P0-2 的要求：调用方只有在全部事件写成功后才能推进 state 基线。
    """
    events_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            # 0750 让推送用户经 POSIX ACL 只读；0700 会把 ACL mask 压成 ---。
            os.chmod(events_dir, 0o750)
        except OSError:
            pass
    slug = slugify(event.get("slug") or event.get("id"), "web-event")
    path = events_dir / f"{slug}.json"
    tmp = path.with_name(f"{slug}.json.tmp")
    replaced = False
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            try:
                # 0640 让推送用户经 POSIX ACL 只读；0600 会把 ACL mask 压成 ---。
                os.chmod(tmp, 0o640)
            except OSError:
                pass
        os.replace(tmp, path)
        replaced = True
        _fsync_directory(events_dir)
    finally:
        if not replaced:
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def prune_events(events_dir: Path, keep: int = MAX_EVENTS_KEPT) -> int:
    """按修改时间保留最近 keep 条，删掉更早的事件文件。返回删除数量。"""
    if keep <= 0 or not events_dir.is_dir():
        return 0
    files = sorted(
        (path for path in events_dir.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    removed = 0
    for path in files[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
