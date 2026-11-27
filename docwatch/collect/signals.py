"""docwatch.collect.signals —— 快照存储 + 信号判定 + 证据包。

职责（CONTRACT §2 §3）：
- 把每一轮抓到的页面正文 / HTML 哈希 / 资源引用 / 资源内容 存进 SQLite（表名统一 `v2_` 前缀，
  可以和 v1 的 pages/changes 表同库共存），保留历史版本并限制磁盘占用
- `diff_snapshot(old, new)` 按 §3.1 产出 Signal 列表：page_html / page_text / asset_url /
  asset_content / new_page / gone_page / fetch_error
- 首次（库里没数据）是「基线模式」：全部写入，**不产生任何信号**
- 有信号时落一套证据包（§3.2）：manifest.json / BRIEF.md / pages/*.txt / assets/*.txt / diff/*.diff

对外主入口：`run_once(data_dir, ...) -> job dict`（字段见 §3.2，另附耗时/流量等运行信息）。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python docwatch/collect/signals.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docwatch.collect import asset as A
from docwatch.collect.fetch import (ACCEPT_ANY, ACCEPT_HTML, WORKERS, FetchError,
                                    Fetcher, ParseError, human_bytes)

try:  # v1 的正文抽取与段落 diff 已经调好了，直接复用；拿不到就走本地兜底
    from docwatch.watch_docs import diff_lines as _v1_diff_lines
    from docwatch.watch_docs import extract_page as _v1_extract_page
except Exception:  # noqa: BLE001 - 单独拷走 collect 目录时也能跑
    _v1_diff_lines = None
    _v1_extract_page = None

BEIJING = timezone(timedelta(hours=8))
DB_NAME = "docs.db"
JOBS_DIR = "jobs"

# 磁盘上限（任务书：资源快照总占用 ≤ 200 MB，超出按时间淘汰最旧）
ASSET_BLOB_CAP_BYTES = 200 * 1024 * 1024
HISTORY_CAP_BYTES = 60 * 1024 * 1024      # 历史正文 / 资源字符串版本
MAX_VERSIONS_PER_URL = 20                 # 同一个页面/资源最多留几版历史
MAX_ASSET_TEXT_BYTES = 200 * 1024         # 证据包里单个资源文本截断（§3.2）
MAX_EVIDENCE_TEXT_BYTES = 1024 * 1024     # 证据包里单个正文文本截断（页面）
JOBS_KEEP = 120                           # 证据包只留最近多少个作业目录

# BRIEF.md 的行预算（§3.2 要求 ≤ 200 行）
BRIEF_MAX_LINES = 200
BRIEF_PAGE_TABLE_ROWS = 20
BRIEF_PAGE_DETAILS = 6
BRIEF_DIFF_LINES = 24
BRIEF_STRINGS_PER_SIDE = 20
BRIEF_ASSET_DETAILS = 6

# 页面分级：给 Lead 的 rules.py 用（§7.5 追加要求）
KEY_PAGE_PATTERNS = ("/quick_start/pricing", "/quick_start/rate_limit",
                     "/quick_start/error_codes", "/quick_start/token_usage", "/updates")

# ================================================================ 小工具

def now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def page_class(url: str) -> str:
    """key（价格/限流/错误码/更新日志/api）/ regular（其余文档）/ marketing（官网首页）。"""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    if host.endswith("www.deepseek.com"):
        return "marketing"
    if "/zh-cn/api/" in path:
        return "key"
    if any(p in path for p in KEY_PAGE_PATTERNS):
        return "key"
    return "regular"


def slug_for(url: str) -> str:
    """URL → 证据包里的安全文件名。

    刻意用 `docwatch/toolbox.py` 的 `_slugish()` 同款规则（小写、非字母数字全变 `-`），
    这样 agent 跑 `toolbox diff <URL>` 时能用 `target in f.name` 直接命中证据文件。
    """
    raw = urllib.parse.unquote(url.split("#")[0])
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if len(s) > 110:  # 超长的 URL 截断补短哈希（toolbox 匹配不到时会退回历史版本）
        s = s[:96].rstrip("-") + "-" + sha256_hex(url)[:8]
    return s or ("x-" + sha256_hex(url)[:8])


def mask_asset_hashes(text: str) -> str:
    """把资源文件名里的内容哈希抹成 <H> —— 用来区分「站点重新构建」和「页面真改了」。"""
    return re.sub(r"([.\-_])[0-9a-f]{8,}(?=\.(?:js|mjs|css|png|jpe?g|webp|gif|svg|ico|woff2?|ttf))",
                  r"\1<H>", text)


def text_diff_ops(old_text: str, new_text: str) -> list[dict]:
    """正文段落级 diff：[{kind: change|add|del, before: [...], after: [...]}]。"""
    old_lines = old_text.split("\n") if old_text else []
    new_lines = new_text.split("\n") if new_text else []
    if _v1_diff_lines is not None:
        return _v1_diff_lines(old_lines, new_lines)
    ops: list[dict] = []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        kind = {"replace": "change", "delete": "del", "insert": "add"}[tag]
        ops.append({"kind": kind, "before": old_lines[i1:i2], "after": new_lines[j1:j2]})
    return ops


def diff_stats(ops: list[dict]) -> dict:
    added = sum(len(o["after"]) for o in ops)
    removed = sum(len(o["before"]) for o in ops)
    added_chars = sum(len("\n".join(o["after"])) for o in ops if o["after"])
    removed_chars = sum(len("\n".join(o["before"])) for o in ops if o["before"])
    return {"added_lines": added, "removed_lines": removed,
            "added_chars": added_chars, "removed_chars": removed_chars,
            "changed_chars": added_chars + removed_chars}


def _excerpt(lines: list[str], limit: int = 160) -> str:
    s = " / ".join(x.strip() for x in lines if x.strip())
    return s[:limit] + ("…" if len(s) > limit else "")


def _sig(kind: str, *, page: str = "", asset: str = "", detail: str = "",
         before: str = "", after: str = "", bytes_delta: int = 0, **extra) -> dict:
    """按 §3.1 拼一条 Signal；extra 是给规则/模型的补充字段（不影响契约字段）。"""
    sig = {
        "kind": kind,
        "page": page,
        "asset": asset,
        "detail": detail,
        "before": before,
        "after": after,
        "bytes_delta": int(bytes_delta),
    }
    sig.update(extra)
    return sig


# ================================================================ 存储层

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS v2_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS v2_pages (
    url            TEXT PRIMARY KEY,
    title          TEXT,
    etag           TEXT,
    last_modified  TEXT,
    html_sha256    TEXT,
    html_size      INTEGER,
    html_norm_sha256 TEXT,
    text_sha256    TEXT,
    text           TEXT,
    text_lines     INTEGER,
    refs_json      TEXT,
    ok             INTEGER DEFAULT 1,
    fail_streak    INTEGER DEFAULT 0,
    last_error     TEXT,
    active         INTEGER DEFAULT 1,
    first_seen     TEXT,
    updated_at     TEXT,
    last_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS v2_page_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT,
    ts         TEXT,
    html_sha256 TEXT,
    html_size  INTEGER,
    title      TEXT,
    text_sha256 TEXT,
    text       TEXT,
    refs_json  TEXT,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_v2_page_versions_url ON v2_page_versions(url, id);
CREATE TABLE IF NOT EXISTS v2_assets (
    url            TEXT PRIMARY KEY,
    kind           TEXT,
    sha256         TEXT,
    size           INTEGER,
    etag           TEXT,
    last_modified  TEXT,
    strings_json   TEXT,
    ok             INTEGER DEFAULT 1,
    fail_streak    INTEGER DEFAULT 0,
    last_error     TEXT,
    active         INTEGER DEFAULT 1,
    first_seen     TEXT,
    updated_at     TEXT,
    last_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS v2_asset_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT,
    ts         TEXT,
    sha256     TEXT,
    size       INTEGER,
    strings_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_v2_asset_versions_url ON v2_asset_versions(url, id);
CREATE TABLE IF NOT EXISTS v2_asset_blobs (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER,
    data       BLOB,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS v2_sitemap (
    url        TEXT PRIMARY KEY,
    first_seen TEXT,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS v2_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT,
    mode         TEXT,
    baseline     INTEGER DEFAULT 0,
    pages_total  INTEGER,
    pages_changed INTEGER,
    assets_total INTEGER,
    assets_changed INTEGER,
    n_signals    INTEGER,
    n_errors     INTEGER,
    duration_ms  INTEGER,
    wire_bytes   INTEGER,
    evidence_dir TEXT,
    job_json     TEXT,
    notified     INTEGER DEFAULT 0,
    -- 0 = 快照尚未提交，作业还不能补发；save_snapshots 成功后置 1。
    ready        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v2_signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    ord        INTEGER,
    kind       TEXT,
    page       TEXT,
    asset      TEXT,
    page_class TEXT,
    detail     TEXT,
    before     TEXT,
    after      TEXT,
    bytes_delta INTEGER,
    signal_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_v2_signals_job ON v2_signals(job_id, ord);
"""


class Store:
    """`<data-dir>/docs.db` 上的 v2_ 表读写。单线程使用（抓取并发在 fetch 层）。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / DB_NAME
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_V2)
        # 老库升级：notified 用于「通知补发」。旧行进库时默认 1（视为已通知，不补发历史），
        # 新作业由 insert_job 显式写 0。
        job_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(v2_jobs)")}
        if "notified" not in job_cols:
            self.conn.execute("ALTER TABLE v2_jobs ADD COLUMN notified INTEGER DEFAULT 1")
        if "ready" not in job_cols:
            # 旧库升级分两步：新列默认 0（新作业 insert_job 显式写 0），
            # 只把迁移前的老作业置 1（它们都已经提交过快照，不能重新补发）。
            last_id = int(self.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM v2_jobs").fetchone()[0] or 0)
            self.conn.execute("ALTER TABLE v2_jobs ADD COLUMN ready INTEGER DEFAULT 0")
            self.conn.execute("UPDATE v2_jobs SET ready = 1 WHERE id <= ?", (last_id,))
        self.conn.commit()

    # -- meta -----------------------------------------------------
    def meta_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM v2_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO v2_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    def meta_json(self, key: str, default=None):
        return _json_or(self.meta_get(key), default)

    def meta_set_json(self, key: str, value) -> None:
        self.meta_set(key, json.dumps(value, ensure_ascii=False))

    # -- 读取快照 -------------------------------------------------
    def load_snapshot(self) -> dict | None:
        """把上一轮的状态读成 snapshot dict；库里没数据返回 None（= 基线模式）。"""
        pages: dict[str, dict] = {}
        for r in self.conn.execute("SELECT * FROM v2_pages WHERE active = 1"):
            pages[r["url"]] = {
                "url": r["url"],
                "title": r["title"] or "",
                "etag": r["etag"] or "",
                "last_modified": r["last_modified"] or "",
                "html_sha256": r["html_sha256"] or "",
                "html_size": int(r["html_size"] or 0),
                "html_norm_sha256": r["html_norm_sha256"] or "",
                "text_sha256": r["text_sha256"] or "",
                "text": r["text"] or "",
                "text_lines": int(r["text_lines"] or 0),
                "refs": _json_or(r["refs_json"], {k: [] for k in A.KINDS}),
                "ok": bool(r["ok"]),
                "fail_streak": int(r["fail_streak"] or 0),
                "error": r["last_error"] or "",
                "updated_at": r["updated_at"] or "",
            }
        assets: dict[str, dict] = {}
        for r in self.conn.execute("SELECT * FROM v2_assets WHERE active = 1"):
            assets[r["url"]] = {
                "url": r["url"],
                "kind": r["kind"] or "",
                "sha256": r["sha256"] or "",
                "size": int(r["size"] or 0),
                "etag": r["etag"] or "",
                "last_modified": r["last_modified"] or "",
                "strings": _json_or(r["strings_json"], []),
                "ok": bool(r["ok"]),
                "fail_streak": int(r["fail_streak"] or 0),
                "error": r["last_error"] or "",
                "updated_at": r["updated_at"] or "",
            }
        if not pages and not assets:
            return None
        return {
            "ts": self.meta_get("last_run"),
            "pages": pages,
            "assets": assets,
            "page_list": self.meta_json("page_list", []),
            "sitemap": self.meta_json("sitemap", []),
        }

    def load_asset_text(self, sha256: str) -> str:
        """从内容寻址的 blob 里取某版本资源原文（证据包用）。"""
        if not sha256:
            return ""
        row = self.conn.execute("SELECT data FROM v2_asset_blobs WHERE sha256 = ?",
                                (sha256,)).fetchone()
        if not row or row["data"] is None:
            return ""
        return bytes(row["data"]).decode("utf-8", "replace")

    # -- 写入 -----------------------------------------------------
    def mark_job_ready(self, job_id: int) -> None:
        """单独把作业标成可补发（测试/修复用；正常路径由 save_snapshots 同事务置位）。"""
        self.conn.execute("UPDATE v2_jobs SET ready = 1 WHERE id = ?", (int(job_id),))
        self.conn.commit()

    def insert_job(self, job: dict) -> int:
        # 先把完整 job_json 落库，再补 id / evidence_dir。这样即使进程在写证据包时
        # 被硬中断，也不会留下「n_signals > 0 但 job_json 为空」的不可恢复作业。
        snapshot = {key: value for key, value in job.items() if key != "id"}
        cur = self.conn.execute(
            "INSERT INTO v2_jobs(created_at, mode, baseline, pages_total, pages_changed,"
            " assets_total, assets_changed, n_signals, n_errors, duration_ms, wire_bytes,"
            " evidence_dir, job_json, notified, ready) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
            (job["created_at"], job.get("mode", "high"), int(bool(job.get("baseline"))),
             job.get("pages_total", 0), job.get("pages_changed", 0),
             job.get("assets_total", 0), job.get("assets_changed", 0),
             len(job.get("signals") or []), job.get("n_errors", 0),
             job.get("duration_ms", 0), job.get("wire_bytes", 0),
             "", json.dumps(snapshot, ensure_ascii=False)))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_job(self, job: dict) -> None:
        self.conn.execute(
            "UPDATE v2_jobs SET pages_total=?, pages_changed=?, assets_total=?,"
            " assets_changed=?, n_signals=?, n_errors=?, duration_ms=?, wire_bytes=?,"
            " evidence_dir=?, job_json=? WHERE id=?",
            (job.get("pages_total", 0), job.get("pages_changed", 0),
             job.get("assets_total", 0), job.get("assets_changed", 0),
             len(job.get("signals") or []), job.get("n_errors", 0),
             job.get("duration_ms", 0), job.get("wire_bytes", 0),
             job.get("evidence_dir", ""),
             json.dumps(job, ensure_ascii=False), job["id"]))
        self.conn.commit()

    def insert_signals(self, job_id: int, signals: list[dict]) -> None:
        rows = []
        for i, s in enumerate(signals):
            rows.append((job_id, i, s["kind"], s.get("page", ""), s.get("asset", ""),
                         s.get("page_class", ""), s.get("detail", ""),
                         str(s.get("before", ""))[:4000], str(s.get("after", ""))[:4000],
                         int(s.get("bytes_delta") or 0),
                         json.dumps(s, ensure_ascii=False)))
        self.conn.executemany(
            "INSERT INTO v2_signals(job_id, ord, kind, page, asset, page_class, detail,"
            " before, after, bytes_delta, signal_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def _archive_page(self, url: str, ts: str, rec: dict, reason: str) -> bool:
        """归档一版页面正文（同一内容不重复记录）。"""
        last = self.conn.execute(
            "SELECT html_sha256, text_sha256 FROM v2_page_versions"
            " WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)).fetchone()
        if last and (last["html_sha256"] or "") == (rec.get("html_sha256") or "") \
                and (last["text_sha256"] or "") == (rec.get("text_sha256") or ""):
            return False
        self.conn.execute(
            "INSERT INTO v2_page_versions(url, ts, html_sha256, html_size, title, text_sha256,"
            " text, refs_json, reason) VALUES(?,?,?,?,?,?,?,?,?)",
            (url, ts, rec.get("html_sha256") or "", int(rec.get("html_size") or 0),
             rec.get("title") or "", rec.get("text_sha256") or "", rec.get("text") or "",
             json.dumps(rec.get("refs") or {k: [] for k in A.KINDS}, ensure_ascii=False),
             reason))
        return True

    def _archive_asset(self, url: str, ts: str, rec: dict) -> bool:
        """归档一版资源（sha256 + 字符串集合），同一内容不重复记录。"""
        last = self.conn.execute(
            "SELECT sha256 FROM v2_asset_versions WHERE url = ? ORDER BY id DESC LIMIT 1",
            (url,)).fetchone()
        if last and (last["sha256"] or "") == (rec.get("sha256") or ""):
            return False
        self.conn.execute(
            "INSERT INTO v2_asset_versions(url, ts, sha256, size, strings_json)"
            " VALUES(?,?,?,?,?)",
            (url, ts, rec.get("sha256") or "", int(rec.get("size") or 0),
             json.dumps(rec.get("strings") or [], ensure_ascii=False)))
        return True

    def save_snapshots(self, old: dict | None, new: dict,
                       bodies: dict[str, bytes], ready_job_id: int | None = None) -> dict:
        """写页面/资源/站点清单，并把被替换掉的旧版本归档。返回归档计数。

        `ready_job_id` 不为空时，在**同一个事务**里把该作业标成 ready=1：
        要么快照和 ready 一起落库，要么都不落，避免「快照前移但作业永远补发不了」的窗口。
        中途异常会整体回滚，绝不留下半个快照。
        """
        try:
            return self._save_snapshots(old, new, bodies, ready_job_id)
        except Exception:
            self.conn.rollback()
            raise

    def _save_snapshots(self, old: dict | None, new: dict,
                        bodies: dict[str, bytes], ready_job_id: int | None = None) -> dict:
        ts = new["ts"]
        counts = {"page_versions": 0, "asset_versions": 0, "blobs": 0}
        old_pages = (old or {}).get("pages", {})
        old_assets = (old or {}).get("assets", {})

        for url, rec in new["pages"].items():
            prev = old_pages.get(url)
            row = self.conn.execute("SELECT first_seen FROM v2_pages WHERE url = ?",
                                    (url,)).fetchone()
            first_seen = (row["first_seen"] if row and row["first_seen"] else ts)
            changed_at = (prev or {}).get("last_changed_at", "") or ""
            if prev and prev["html_sha256"] and (prev["html_sha256"] != rec["html_sha256"]
                                                 or prev["text_sha256"] != rec["text_sha256"]):
                changed_at = ts
                # 变更前后各存一版：toolbox 的 `diff <URL>` 靠历史表工作，只有一版比不出来
                if self._archive_page(url, prev.get("updated_at") or ts, prev, "changed-old"):
                    counts["page_versions"] += 1
                if self._archive_page(url, ts, rec, "changed-new"):
                    counts["page_versions"] += 1
            self.conn.execute(
                "INSERT INTO v2_pages(url, title, etag, last_modified, html_sha256, html_size,"
                " html_norm_sha256, text_sha256, text, text_lines, refs_json, ok, fail_streak,"
                " last_error, active, first_seen, updated_at, last_changed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)"
                " ON CONFLICT(url) DO UPDATE SET title=excluded.title, etag=excluded.etag,"
                " last_modified=excluded.last_modified, html_sha256=excluded.html_sha256,"
                " html_size=excluded.html_size, html_norm_sha256=excluded.html_norm_sha256,"
                " text_sha256=excluded.text_sha256, text=excluded.text,"
                " text_lines=excluded.text_lines, refs_json=excluded.refs_json, ok=excluded.ok,"
                " fail_streak=excluded.fail_streak, last_error=excluded.last_error,"
                " active=1, updated_at=excluded.updated_at,"
                " last_changed_at=COALESCE(NULLIF(excluded.last_changed_at,''),"
                " v2_pages.last_changed_at)",
                (url, rec["title"], rec["etag"], rec["last_modified"], rec["html_sha256"],
                 rec["html_size"], rec["html_norm_sha256"], rec["text_sha256"], rec["text"],
                 rec["text_lines"], json.dumps(rec["refs"], ensure_ascii=False),
                 int(rec["ok"]), rec["fail_streak"], rec["error"], first_seen or ts, ts,
                 changed_at))

        if not new.get("partial"):  # 局部跑（--limit/--only）时没抓到的页面不能算「下线」
            for url in [u for u in old_pages if u not in new["pages"]]:
                # 停用时清掉失败计数：重新启用后不应继承旧故障，否则会立刻误报。
                self.conn.execute(
                    "UPDATE v2_pages SET active = 0, fail_streak = 0, last_error = '' "
                    "WHERE url = ?", (url,))

        for url, rec in new["assets"].items():
            prev = old_assets.get(url)
            row = self.conn.execute("SELECT first_seen FROM v2_assets WHERE url = ?",
                                    (url,)).fetchone()
            first_seen = row["first_seen"] if row and row["first_seen"] else ts
            changed_at = ""
            sha = rec.get("sha256") or ""
            body = bodies.get(url)
            if body is not None and sha:
                if not self.conn.execute(
                        "SELECT 1 FROM v2_asset_blobs WHERE sha256 = ?", (sha,)).fetchone():
                    self.conn.execute(
                        "INSERT INTO v2_asset_blobs(sha256, size, data, created_at)"
                        " VALUES(?,?,?,?)", (sha, len(body), sqlite3.Binary(body), ts))
                    counts["blobs"] += 1
            if prev and prev["sha256"] and prev["sha256"] != sha:
                changed_at = ts
                if self._archive_asset(url, prev.get("updated_at") or ts, prev):
                    counts["asset_versions"] += 1
                if self._archive_asset(url, ts, rec):
                    counts["asset_versions"] += 1
            self.conn.execute(
                "INSERT INTO v2_assets(url, kind, sha256, size, etag, last_modified,"
                " strings_json, ok, fail_streak, last_error, active, first_seen, updated_at,"
                " last_changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)"
                " ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, sha256=excluded.sha256,"
                " size=excluded.size, etag=excluded.etag,"
                " last_modified=excluded.last_modified, strings_json=excluded.strings_json,"
                " ok=excluded.ok, fail_streak=excluded.fail_streak,"
                " last_error=excluded.last_error, active=1, updated_at=excluded.updated_at,"
                " last_changed_at=COALESCE(NULLIF(excluded.last_changed_at,''),"
                " v2_assets.last_changed_at)",
                (url, rec.get("kind", ""), sha, int(rec.get("size") or 0),
                 rec.get("etag", ""), rec.get("last_modified", ""),
                 json.dumps(rec.get("strings") or [], ensure_ascii=False),
                 int(rec.get("ok", True)), int(rec.get("fail_streak") or 0),
                 rec.get("error", ""), first_seen, ts, changed_at))
        if not new.get("partial"):
            for url in [u for u in old_assets if u not in new["assets"]]:
                self.conn.execute(
                    "UPDATE v2_assets SET active = 0, fail_streak = 0, last_error = '' "
                    "WHERE url = ?", (url,))

        for url in (new.get("sitemap") or []) if not new.get("partial") else []:
            self.conn.execute(
                "INSERT INTO v2_sitemap(url, first_seen, last_seen) VALUES(?,?,?)"
                " ON CONFLICT(url) DO UPDATE SET last_seen = excluded.last_seen",
                (url, ts, ts))
        if ready_job_id is not None:
            self.conn.execute("UPDATE v2_jobs SET ready = 1 WHERE id = ?",
                              (int(ready_job_id),))
        self.conn.commit()
        if not new.get("partial"):
            self.meta_set_json("page_list", new.get("page_list") or [])
            self.meta_set_json("sitemap", new.get("sitemap") or [])
        self.meta_set("last_run", ts)
        return counts

    # -- 清理 -----------------------------------------------------
    def enforce_caps(self) -> dict:
        """资源快照 ≤ 200 MB、历史版本 ≤ 60 MB，超出按时间淘汰最旧。"""
        out = {"blob_bytes": _sum_size(self.conn, "v2_asset_blobs", "size"),
               "history_bytes": _sum_size(self.conn, "v2_page_versions", "length(text)")
               + _sum_size(self.conn, "v2_asset_versions", "length(strings_json)"),
               "evicted_blobs": 0, "evicted_versions": 0}
        while out["blob_bytes"] > ASSET_BLOB_CAP_BYTES:
            row = self.conn.execute(
                "SELECT sha256, size FROM v2_asset_blobs ORDER BY created_at ASC LIMIT 1").fetchone()
            if not row:
                break
            self.conn.execute("DELETE FROM v2_asset_blobs WHERE sha256 = ?", (row["sha256"],))
            out["blob_bytes"] -= int(row["size"] or 0)
            out["evicted_blobs"] += 1
        while out["history_bytes"] > HISTORY_CAP_BYTES:
            row = self.conn.execute(
                "SELECT id, length(text) AS n FROM v2_page_versions ORDER BY id ASC LIMIT 1"
            ).fetchone()
            a_row = self.conn.execute(
                "SELECT id, length(strings_json) AS n FROM v2_asset_versions"
                " ORDER BY id ASC LIMIT 1").fetchone()
            if not row and not a_row:
                break
            if a_row and (not row or int(a_row["n"] or 0) >= int(row["n"] or 0)):
                self.conn.execute("DELETE FROM v2_asset_versions WHERE id = ?", (a_row["id"],))
                out["history_bytes"] -= int(a_row["n"] or 0)
            else:
                self.conn.execute("DELETE FROM v2_page_versions WHERE id = ?", (row["id"],))
                out["history_bytes"] -= int(row["n"] or 0)
            out["evicted_versions"] += 1
        for table in ("v2_page_versions", "v2_asset_versions"):
            self.conn.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM ("
                f" SELECT id, ROW_NUMBER() OVER (PARTITION BY url ORDER BY id DESC) AS rn"
                f" FROM {table}) WHERE rn > ?)", (MAX_VERSIONS_PER_URL,))
        self.conn.commit()
        return out

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()


def _sum_size(conn: sqlite3.Connection, table: str, expr: str) -> int:
    row = conn.execute(f"SELECT COALESCE(SUM({expr}), 0) FROM {table}").fetchone()
    return int(row[0] or 0)


def _json_or(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


# ================================================================ 采集

def _carry_page(old_rec: dict | None, url: str, error: FetchError) -> dict:
    """抓取失败：保留上一次的内容（否则下一轮会把「失败→恢复」误判成内容变化）。"""
    base = dict(old_rec) if old_rec else {
        "url": url, "title": "", "etag": "", "last_modified": "",
        "html_sha256": "", "html_size": 0, "html_norm_sha256": "",
        "text_sha256": "", "text": "", "text_lines": 0,
        "refs": {k: [] for k in A.KINDS},
    }
    base["ok"] = False
    base["fail_streak"] = int(base.get("fail_streak") or 0) + 1
    base["error"] = f"{error.kind}: {error.reason}"[:300]
    return base


def _carry_asset(old_rec: dict | None, url: str, kind: str, error: FetchError) -> dict:
    base = dict(old_rec) if old_rec else {
        "url": url, "kind": kind, "sha256": "", "size": 0, "etag": "",
        "last_modified": "", "strings": [],
    }
    base["ok"] = False
    base["fail_streak"] = int(base.get("fail_streak") or 0) + 1
    base["error"] = f"{error.kind}: {error.reason}"[:300]
    base["downloaded"] = False
    return base


def parse_page(url: str, html: str, final_url: str) -> dict:
    """HTML → 页面记录（标题 / 正文 / 引用 / 两种哈希）。"""
    if _v1_extract_page is not None:
        title, lines = _v1_extract_page(html)
    else:
        title, lines = "", A.text_lines_from_html(html)
    rsc = A.rsc_text(html)
    if len(rsc) >= 3:  # Next.js 页面（官网首页）正文在 RSC payload 里
        lines = rsc
        if not title:
            title = rsc[0]
    text = "\n".join(lines)
    refs = A.page_refs(html, final_url or url)
    body = html.encode("utf-8")
    return {
        "url": url,
        "title": title.strip(),
        "etag": "",
        "last_modified": "",
        "html_sha256": sha256_hex(body),
        "html_size": len(body),
        "html_norm_sha256": sha256_hex(mask_asset_hashes(html)),
        "text_sha256": sha256_hex(text),
        "text": text,
        "text_lines": len(lines),
        "refs": refs,
        "ok": True,
        "fail_streak": 0,
        "error": "",
    }


def _collect_pages(fx: Fetcher, page_list: list[str],
                   old: dict | None, workers: int) -> dict[str, dict]:
    old_pages = (old or {}).get("pages", {})
    reqs = []
    for url in page_list:
        o = old_pages.get(url)
        reqs.append({"url": url,
                     "etag": (o or {}).get("etag", ""),
                     "last_modified": (o or {}).get("last_modified", ""),
                     "accept": ACCEPT_HTML})
    results = fx.get_many(reqs, workers)
    pages: dict[str, dict] = {}
    for url, res in zip(page_list, results):
        o = old_pages.get(url)
        if isinstance(res, FetchError):
            pages[url] = _carry_page(o, url, res)
            continue
        if res.not_modified:
            if o is None:
                pages[url] = _carry_page(
                    None, url, FetchError(url, "服务器回 304 但本地没有快照"))
                continue
            rec = dict(o)
            rec["ok"] = True
            rec["fail_streak"] = 0
            rec["error"] = ""
            rec["revalidated"] = True
            pages[url] = rec
            continue
        rec = parse_page(url, res.text(), res.final_url)
        rec["etag"] = res.etag
        rec["last_modified"] = res.last_modified
        rec["revalidated"] = False
        pages[url] = rec
    return pages


def _collect_assets(fx: Fetcher, pages: dict[str, dict],
                    old: dict | None, workers: int) -> tuple[dict[str, dict], dict[str, bytes]]:
    """抓第一方 JS/CSS。304 只更新 ETag；200 才读内容算 sha256 + 字符串集合。"""
    old_assets = (old or {}).get("assets", {})
    targets: list[str] = []
    for rec in pages.values():
        # 页面这轮抓失败时用的是上一轮的 refs，资源还是要继续验证（否则会误报「资源消失」）
        for url in A.monitor_targets(rec.get("refs") or {}):
            if url not in targets:
                targets.append(url)
    targets.sort()
    reqs = []
    for url in targets:
        o = old_assets.get(url)
        reqs.append({"url": url, "etag": (o or {}).get("etag", ""),
                     "last_modified": (o or {}).get("last_modified", ""),
                     "accept": ACCEPT_ANY})
    results = fx.get_many(reqs, workers)
    assets: dict[str, dict] = {}
    bodies: dict[str, bytes] = {}
    kind_by_url = {u: ("css" if urllib.parse.urlsplit(u).path.lower().endswith(".css") else "js")
                   for u in targets}
    for url, res in zip(targets, results):
        o = old_assets.get(url)
        if isinstance(res, FetchError):
            assets[url] = _carry_asset(o, url, kind_by_url[url], res)
            continue
        if res.not_modified:
            if o is None:
                assets[url] = _carry_asset(
                    None, url, kind_by_url[url], FetchError(url, "服务器回 304 但本地没有快照"))
                continue
            rec = dict(o)
            rec["ok"] = True
            rec["fail_streak"] = 0
            rec["error"] = ""
            rec["downloaded"] = False
            assets[url] = rec
            continue
        text = res.text()
        assets[url] = {
            "url": url,
            "kind": kind_by_url[url],
            "sha256": res.sha256(),
            "size": res.size,
            "etag": res.etag,
            "last_modified": res.last_modified,
            "strings": A.js_strings(text),
            "ok": True,
            "fail_streak": 0,
            "error": "",
            "downloaded": True,
        }
        bodies[url] = res.body
    return assets, bodies


SITEMAP_INDEX_MAX_CHILDREN = 8   # sitemapindex 最多递归抓几个子 sitemap
SITEMAP_MIN_KEEP_RATIO = 0.8     # 新清单比旧清单少 20% 以上就认为解析异常，保留旧清单
SITEMAP_SHRINK_MIN_PAGES = 10    # 旧清单少于这个页数时不做「大幅收缩」判断（首次/小站）


def _sitemap_locs(fx: Fetcher, url: str, xml: str, depth: int = 0,
                  errors: list[str] | None = None) -> list[str]:
    """解析 sitemap；遇到 sitemapindex 就抓子 sitemap 并合并（最多两层、最多 8 个）。

    `errors` 收集非致命的抓取/解析问题，调用方会写进 job.sitemap_error 并纳入健康告警，
    不能只写 stderr 当没发生（P2-24）。
    """
    if not A.sitemap_is_index(xml):
        urls = A.parse_sitemap(xml)
        if not urls:
            raise ParseError(url, "sitemap 里没有 <loc>")
        return urls
    if depth >= 2:
        raise ParseError(url, "sitemapindex 嵌套超过 2 层")
    children = A.sitemap_children(xml)
    if not children:
        raise ParseError(url, "sitemapindex 里没有子 sitemap")
    if len(children) > SITEMAP_INDEX_MAX_CHILDREN:
        msg = (f"sitemapindex 有 {len(children)} 个子 sitemap，"
               f"只取前 {SITEMAP_INDEX_MAX_CHILDREN} 个")
        sys.stderr.write(f"[warn] {msg}\n")
        if errors is not None:
            errors.append(msg)
    root_netloc = urllib.parse.urlsplit(A.DOCS_ROOT).netloc
    out: list[str] = []
    skipped = 0
    for raw_child in children[:SITEMAP_INDEX_MAX_CHILDREN]:
        child = urllib.parse.urljoin(url, raw_child)
        parts = urllib.parse.urlsplit(child)
        if parts.netloc != root_netloc or not parts.path.lower().endswith(".xml"):
            skipped += 1
            continue
        try:
            resp = fx.get(child, accept="application/xml,text/xml,*/*")
            if resp.not_modified:   # 子 sitemap 没带条件请求，正常不会走到这里
                continue
            out.extend(_sitemap_locs(fx, child, resp.text(), depth + 1, errors))
        except FetchError as exc:
            msg = f"子 sitemap 抓取失败，已跳过：{child}（{exc}）"
            sys.stderr.write(f"[warn] {msg}\n")
            if errors is not None:
                errors.append(msg)
    if skipped:
        msg = f"sitemapindex 里有 {skipped} 个非本站/非 xml 地址，已跳过"
        sys.stderr.write(f"[warn] {msg}\n")
        if errors is not None:
            errors.append(msg)
    if not out:
        raise ParseError(url, "sitemapindex 的子 sitemap 都解析失败")
    return list(dict.fromkeys(out))


def _resolve_page_list(store: Store, fx: Fetcher, explicit: list[str] | None
                       ) -> tuple[list[str], list[str], bool, list[str]]:
    """返回 (监控页面清单, sitemap 原始清单, 是否全量, sitemap 非致命错误)。"""
    if explicit:
        return [A.canon(u) for u in explicit], store.meta_json("sitemap", []), False, []

    sitemap = store.meta_json("sitemap", [])
    old_sitemap = list(sitemap)
    sitemap_errors: list[str] = []
    etag, lm = store.meta_get("sitemap_etag"), store.meta_get("sitemap_lm")
    if not sitemap:  # 缓存里没有清单就别发条件请求，否则会拿到 304 却没内容
        etag, lm = "", ""
    try:
        resp = fx.get(A.DOCS_SITEMAP, etag, lm, accept="application/xml,text/xml,*/*")
        if not resp.not_modified:
            urls = _sitemap_locs(fx, A.DOCS_SITEMAP, resp.text(), errors=sitemap_errors)
            new_pages = A.zh_page_urls(urls)
            old_pages = A.zh_page_urls(old_sitemap)
            if not new_pages:
                raise ParseError(A.DOCS_SITEMAP, "sitemap 解析后没有本站页面")
            # 结构改版/被截断时，宁可保留旧清单并告警，也不要把所有真实页面判成「消失」。
            # 但真下线也可能一次少很多页，所以连续两轮看到同一份收缩清单就接受，避免永远卡住。
            shrunk = (len(old_pages) >= SITEMAP_SHRINK_MIN_PAGES
                      and len(new_pages) < len(old_pages) * SITEMAP_MIN_KEEP_RATIO)
            if shrunk:
                pending = store.meta_json("sitemap_pending", None)
                same = (isinstance(pending, dict)
                        and pending.get("urls") == sorted(urls))
                if not same:
                    store.meta_set_json("sitemap_pending", {"urls": sorted(urls)})
                    raise ParseError(
                        A.DOCS_SITEMAP,
                        f"页面清单异常收缩（{len(old_pages)} → {len(new_pages)} 页），"
                        f"先保留旧清单，下一轮确认")
                msg = (f"页面清单连续两轮收缩（{len(old_pages)} → {len(new_pages)} 页），"
                       f"确认接受新清单")
                sys.stderr.write(f"[warn] {msg}\n")
                sitemap_errors.append(msg)
            store.meta_set("sitemap_pending", "")
            sitemap = urls
            store.meta_set("sitemap_etag", resp.etag)
            store.meta_set("sitemap_lm", resp.last_modified)
            store.meta_set_json("sitemap", sitemap)   # 局部跑也要存，否则下次 304 没内容可用
    except FetchError as exc:
        if not sitemap:
            raise
        msg = f"sitemap 抓取失败，沿用缓存清单：{exc}"
        sys.stderr.write(f"[warn] {msg}\n")
        sitemap_errors.append(msg)

    pages = A.zh_page_urls(sitemap)
    if not pages:
        raise ParseError(A.DOCS_SITEMAP, "sitemap 解析后没有可用页面")
    for url in A.SITE_PAGES:
        if url not in pages:
            pages.append(A.canon(url))
    pages = sorted(dict.fromkeys(pages))
    return pages, sitemap, True, sitemap_errors


# ================================================================ 信号判定

def _ref_pages(pages: dict[str, dict], kinds: tuple[str, ...]) -> dict[str, set[str]]:
    """{资源 url: {引用它的页面 url, ...}}"""
    out: dict[str, set[str]] = {}
    for page_url, rec in pages.items():
        refs = rec.get("refs") or {}
        for kind in kinds:
            for url in refs.get(kind) or []:
                out.setdefault(url, set()).add(page_url)
    return out


def _pair_transitions(old_map: dict[str, set[str]],
                      new_map: dict[str, set[str]]) -> list[dict]:
    """把资源引用差异配成「改名 / 新增 / 消失」。

    改名靠文件名去掉哈希后的 stem 配对（main.0c15113f.js ↔ main.9ab31f02.js），
    配不上的就是一增一删。不做任何合并/丢弃：每条差异都会出现在结果里。
    """
    old_urls, new_urls = set(old_map), set(new_map)
    removed = sorted(old_urls - new_urls)
    added = sorted(new_urls - old_urls)
    pairs: list[dict] = []

    by_stem_old: dict[str, list[str]] = {}
    for url in removed:
        by_stem_old.setdefault(A.asset_stem(url), []).append(url)
    by_stem_new: dict[str, list[str]] = {}
    for url in added:
        by_stem_new.setdefault(A.asset_stem(url), []).append(url)

    for stem, olds in by_stem_old.items():
        news = by_stem_new.get(stem) or []
        for old_url, new_url in zip(olds, news):
            pairs.append({"action": "rename", "before": old_url, "after": new_url,
                          "stem": stem,
                          "pages": sorted(old_map.get(old_url, set()) | new_map.get(new_url, set()))})
            removed.remove(old_url)
            added.remove(new_url)
    for url in removed:
        pairs.append({"action": "removed", "before": url, "after": "", "stem": A.asset_stem(url),
                      "pages": sorted(old_map.get(url, set()))})
    for url in added:
        pairs.append({"action": "added", "before": "", "after": url, "stem": A.asset_stem(url),
                      "pages": sorted(new_map.get(url, set()))})
    return pairs


def _page_attr(pages: list[str]) -> str:
    """只有一个页面用到就记在这个页面上，多个页面共用就记成站点级（page 留空）。"""
    return pages[0] if len(pages) == 1 else ""


def _pages_hint(pages: list[str]) -> str:
    if not pages:
        return ""
    if len(pages) == 1:
        return f"，{pages[0]}"
    return f"，全站 {len(pages)} 页引用"


def diff_snapshot(old: dict, new: dict) -> list[dict]:
    """对比两轮快照，产出 §3.1 的 Signal 列表（不做任何合并 / 去重 / 限流）。"""
    sigs: list[dict] = []
    old_pages = old.get("pages", {})
    old_assets = old.get("assets", {})
    if new.get("partial"):
        # 局部跑（--limit/--only）只比对「这轮抓了、上一轮也有」的页面，
        # 否则没抓到的页面会被误判成资源消失/新增。
        shared = set(new["pages"]) & set(old_pages)
        old_pages = {u: r for u, r in old_pages.items() if u in shared}
        new = dict(new)
        new["pages"] = {u: r for u, r in new["pages"].items() if u in shared}

    # ---- 页面：HTML 哈希 / 正文
    for url in sorted(new["pages"]):
        n = new["pages"][url]
        o = old_pages.get(url)
        if o is None:
            continue  # 新页面由 new_page 信号负责
        if not n.get("ok") and o.get("ok"):
            sigs.append(_sig(
                "fetch_error", page=url,
                detail=f"页面抓取失败（第 {n.get('fail_streak', 1)} 次）：{n.get('error', '')}",
                before="正常", after=n.get("error", "")))
        if not n.get("ok") and o.get("ok") is False:
            continue  # 持续失败只在「突然失败」那一轮报，避免每轮重复刷屏
        html_changed = bool(o["html_sha256"] and n["html_sha256"]
                            and n["html_sha256"] != o["html_sha256"])
        text_changed = bool(o["text_sha256"] and n["text_sha256"]
                           and n["text_sha256"] != o["text_sha256"])
        if html_changed:
            refs_only = (n["html_norm_sha256"] == o["html_norm_sha256"]
                         and not text_changed)
            delta = int(n["html_size"]) - int(o["html_size"])
            if text_changed:
                nature = "HTML 与正文都变了"
            elif refs_only:
                nature = "差异只在资源文件名上（站点重新构建），正文无改动"
            else:
                nature = "正文无改动"
            sigs.append(_sig(
                "page_html", page=url,
                detail=(f"HTML 变了：{human_bytes(o['html_size'])} → "
                        f"{human_bytes(n['html_size'])}（{delta:+d} 字节），{nature}"),
                before=f"{o['html_sha256'][:12]} / {o['html_size']} 字节",
                after=f"{n['html_sha256'][:12]} / {n['html_size']} 字节",
                bytes_delta=delta,
                before_sha=o["html_sha256"][:16], after_sha=n["html_sha256"][:16],
                refs_only=refs_only))
        if text_changed:
            ops = text_diff_ops(o["text"], n["text"])
            st = diff_stats(ops)
            first_del = next((x for x in ops if x["before"]), {"before": []})
            first_add = next((x for x in ops if x["after"]), {"after": []})
            sigs.append(_sig(
                "page_text", page=url,
                detail=(f"正文变了：+{st['added_lines']} 行 / -{st['removed_lines']} 行"
                        f"（共 {n['text_lines']} 行，{st['changed_chars']} 字改动）"),
                before=_excerpt(first_del["before"]),
                after=_excerpt(first_add["after"]),
                bytes_delta=len(n["text"]) - len(o["text"]),
                added_lines=st["added_lines"], removed_lines=st["removed_lines"],
                text_changed_chars=st["changed_chars"],
                changed_chars=st["changed_chars"]))

    # ---- 前端资源：JS/CSS 改名 / 新增 / 消失
    old_js = _ref_pages(old_pages, ("js", "css"))
    new_js = _ref_pages(new["pages"], ("js", "css"))
    for tr in _pair_transitions(old_js, new_js):
        detail, b, a, delta = _asset_url_detail(tr, old_assets, new["assets"])
        sigs.append(_sig("asset_url", page=_page_attr(tr["pages"]),
                         asset=tr["after"] or tr["before"], detail=detail,
                         before=b, after=a, bytes_delta=delta,
                         action=tr["action"], pages=tr["pages"],
                         before_url=tr["before"], after_url=tr["after"]))

    # ---- 图片 / 其它引用：同样产信号，只在 detail 里注明类型（不下载图片内容）
    old_img = _ref_pages(old_pages, ("img",))
    new_img = _ref_pages(new["pages"], ("img",))
    for tr in _pair_transitions(old_img, new_img):
        detail, b, a = _ref_url_detail(tr, "图片")
        sigs.append(_sig("asset_url", page=_page_attr(tr["pages"]),
                         asset=tr["after"] or tr["before"], detail=detail,
                         before=b, after=a, bytes_delta=0,
                         action=tr["action"], pages=tr["pages"], ref_kind="img",
                         before_url=tr["before"], after_url=tr["after"]))

    old_other = _ref_pages(old_pages, ("other",))
    new_other = _ref_pages(new["pages"], ("other",))
    for tr in _pair_transitions(old_other, new_other):
        detail, b, a = _ref_url_detail(tr, "页面元信息引用")
        sigs.append(_sig("asset_url", page=_page_attr(tr["pages"]),
                         asset=tr["after"] or tr["before"], detail=detail,
                         before=b, after=a, bytes_delta=0,
                         action=tr["action"], pages=tr["pages"], ref_kind="other",
                         before_url=tr["before"], after_url=tr["after"]))

    # ---- 同名资源内容变化 / 资源抓取失败
    for url in sorted(new["assets"]):
        n = new["assets"][url]
        o = old_assets.get(url)
        if o is None:
            continue
        if not n.get("ok") and o.get("ok"):
            sigs.append(_sig(
                "fetch_error", asset=url,
                detail=f"资源抓取失败（第 {n.get('fail_streak', 1)} 次）：{n.get('error', '')}",
                before="正常", after=n.get("error", "")))
        if not n.get("ok") and o.get("ok") is False:
            continue
        if n.get("sha256") and o.get("sha256") and n["sha256"] != o["sha256"]:
            sigs.append(_sig(
                "asset_content", asset=url,
                detail=(f"{A.basename(url)} 内容变了：{human_bytes(o['size'])} → "
                        f"{human_bytes(n['size'])}"),
                before=f"{o['sha256'][:12]} / {o['size']} 字节",
                after=f"{n['sha256'][:12]} / {n['size']} 字节",
                bytes_delta=int(n["size"]) - int(o["size"]),
                before_sha=o["sha256"], after_sha=n["sha256"]))

    # ---- sitemap：新页面 / 下线页面
    if not new.get("partial"):
        old_list = old.get("page_list") or []
        new_list = new.get("page_list") or []
        old_set, new_set = set(old_list), set(new_list)
        for url in sorted(new_set - old_set):
            sigs.append(_sig(
                "new_page", page=url,
                detail=f"sitemap 里新出现一个页面（监控清单 {len(old_list)} → {len(new_list)} 页）",
                before="不存在", after=url))
        for url in sorted(old_set - new_set):
            sigs.append(_sig(
                "gone_page", page=url,
                detail=f"页面从 sitemap 消失（监控清单 {len(old_list)} → {len(new_list)} 页）",
                before=url, after="已下线"))
    return sigs


def _asset_url_detail(tr: dict, old_assets: dict, new_assets: dict) -> tuple:
    """资源 URL 变化 → (人话, before, after, bytes_delta)。"""
    old_url, new_url = tr["before"], tr["after"]
    old_rec = old_assets.get(old_url) or {}
    new_rec = new_assets.get(new_url) or {}
    hint = _pages_hint(tr["pages"])
    if tr["action"] == "rename":
        h_old, h_new = A.asset_hash(old_url), A.asset_hash(new_url)
        delta = int(new_rec.get("size") or 0) - (int(old_rec.get("size") or 0) or 0) \
            if new_rec.get("size") else 0
        detail = (f"{A.basename(new_url)} 文件名哈希变了"
                  f"（{h_old} → {h_new}）{hint}") if (
            h_old and h_new and A.is_content_hashed(old_url) and A.is_content_hashed(new_url)
        ) else (
            # 文件名里本来就没有内容哈希，只能说「改了地址」
            f"{A.basename(new_url)} 改了地址"
            f"（{A.basename(old_url)} → {A.basename(new_url)}）{hint}")
        if new_rec.get("size"):
            detail += f"，新文件 {human_bytes(new_rec['size'])}"
        return detail, old_url, new_url, delta
    if tr["action"] == "added":
        size = f"（{human_bytes(new_rec['size'])}）" if new_rec.get("size") else ""
        return (f"新增前端资源 {A.basename(new_url)}{size}{hint}",
                "", new_url, int(new_rec.get("size") or 0))
    return (f"前端资源消失：{A.basename(old_url)}"
            f"（原 {human_bytes(int(old_rec.get('size') or 0))}{hint}）",
            old_url, "", -int(old_rec.get("size") or 0))


def _ref_url_detail(tr: dict, label: str) -> tuple:
    old_url, new_url = tr["before"], tr["after"]
    hint = _pages_hint(tr["pages"])
    if tr["action"] == "rename":
        h_old, h_new = A.asset_hash(old_url), A.asset_hash(new_url)
        if h_old and h_new and A.asset_stem(old_url) == A.asset_stem(new_url):
            return (f"{label} {A.basename(new_url)} 换了文件名"
                    f"（{h_old} → {h_new}）{hint}", old_url, new_url)
        return (f"{label} {A.basename(old_url)} 换了地址 → {A.basename(new_url)}{hint}",
                old_url, new_url)
    if tr["action"] == "added":
        return f"{label}新增引用 {A.basename(new_url)}{hint}", "", new_url
    return f"{label}引用消失 {A.basename(old_url)}{hint}", old_url, ""


# ================================================================ 证据包

def _safe_write(path: Path, text: str, limit: int | None = None) -> int:
    """写证据文件，返回字节数。超长截断并留一行说明（§3.2 的 200 KB 上限）。"""
    data = text.encode("utf-8")
    if limit is not None and len(data) > limit:
        cut = data[:limit].decode("utf-8", "ignore")
        cut += (f"\n\n…（已截断：原文 {len(data)} 字节，本文件只保留前 {limit} 字节，"
                f"完整内容见资源地址）\n")
        data = cut.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def _unified(old_text: str, new_text: str, fromfile: str, tofile: str,
             n: int = 3) -> str:
    old_lines = old_text.split("\n") if old_text else []
    new_lines = new_text.split("\n") if new_text else []
    out = list(difflib.unified_diff(old_lines, new_lines, fromfile=fromfile,
                                    tofile=tofile, lineterm="", n=n))
    if not out:
        return "（没有差异）\n"
    return "\n".join(out) + "\n"


def _string_set_diff(old: list[str], new: list[str], fromfile: str, tofile: str) -> str:
    out = list(difflib.unified_diff(old, new, fromfile=fromfile, tofile=tofile, lineterm="", n=2))
    return ("\n".join(out) + "\n") if out else "（字符串集合没有差异）\n"


def _strings_delta(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    old_set, new_set = set(old or []), set(new or [])
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    return _rank_strings(added), _rank_strings(removed)


_PATHNOISE_RE = re.compile(r"[A-Za-z]\d|\d[A-Za-z]|\bl\d|M\d")


def _rank_strings(items: list[str]) -> list[str]:
    """简报里的字符串排序：中文文案 > 像人说的句子 > 路径/数值/标识符。

    压缩 JS 里大量是 CSS 值、SVG 路径、库内部标识符；把它们排到后面，
    前 20 条才是真正能看出「改了什么」的那几行。
    """

    def key(s: str) -> tuple:
        cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        has_space = " " in s
        noisy = bool(_PATHNOISE_RE.search(s)) or s.count("/") >= 2
        if cjk:
            return (0, 0, -cjk, s)
        if has_space and not noisy:
            return (1, 0, -len(s), s)
        if has_space:
            return (2, 0, len(s), s)
        return (3, 0, len(s), s)

    return sorted(items, key=key)


def _page_views(old: dict, new: dict) -> list[dict]:
    """有变化（HTML 或正文）的页面，附带给规则和简报用的细节。"""
    views: list[dict] = []
    old_pages = old.get("pages", {})
    for url in sorted(new["pages"]):
        n = new["pages"][url]
        o = old_pages.get(url)
        if o is None:
            # 新页面：old 侧为空，但仍要生成 view，否则 pages/<slug>.after.txt 不会落盘，
            # 规则判 3 级的重要事件在证据包里看不到正文。
            new_refs = {u for k in A.KINDS for u in (n.get("refs") or {}).get(k, [])}
            text = n.get("text") or ""
            views.append({
                "url": url, "slug": slug_for(url), "page_class": page_class(url),
                "title": n.get("title") or "",
                "html": {"changed": True, "before_sha": "", "after_sha": n["html_sha256"],
                         "before_size": 0, "after_size": int(n["html_size"]),
                         "refs_only": False},
                "text": {"changed": True, "before_lines": 0,
                         "after_lines": int(n.get("text_lines") or 0),
                         "text_changed_chars": len(text),
                         "added_lines": len([line for line in text.split("\n") if line.strip()]),
                         "removed_lines": 0, "before_text": "", "after_text": text, "ops": []},
                "refs_changed": [], "refs_added": sorted(new_refs), "refs_removed": [],
            })
            continue
        html_changed = bool(o["html_sha256"] and n["html_sha256"]
                            and n["html_sha256"] != o["html_sha256"])
        text_changed = bool(o["text_sha256"] and n["text_sha256"]
                           and n["text_sha256"] != o["text_sha256"])
        if not html_changed and not text_changed:
            continue
        ops = text_diff_ops(o["text"], n["text"]) if text_changed else []
        st = diff_stats(ops)
        renamed = []
        added_refs: list[str] = []
        removed_refs: list[str] = []
        old_refs = {u for k in A.KINDS for u in (o.get("refs") or {}).get(k, [])}
        new_refs = {u for k in A.KINDS for u in (n.get("refs") or {}).get(k, [])}
        if old_refs != new_refs:
            by_stem: dict[str, list[str]] = {}
            for u in sorted(old_refs - new_refs):
                by_stem.setdefault(A.asset_stem(u), []).append(u)
            for u in sorted(new_refs - old_refs):
                olds = by_stem.get(A.asset_stem(u)) or []
                if olds:
                    renamed.append({"before": olds.pop(0), "after": u})
                else:
                    added_refs.append(u)
            used = {r["before"] for r in renamed}
            removed_refs = [u for u in sorted(old_refs - new_refs) if u not in used]
        views.append({
            "url": url,
            "slug": slug_for(url),
            "page_class": page_class(url),
            "title": n.get("title") or "",
            "html": {
                "changed": html_changed,
                "before_sha": o["html_sha256"], "after_sha": n["html_sha256"],
                "before_size": int(o["html_size"]), "after_size": int(n["html_size"]),
                "refs_only": bool(html_changed and n["html_norm_sha256"] == o["html_norm_sha256"]
                                  and not text_changed),
            },
            "text": {
                "changed": text_changed,
                "before_lines": int(o.get("text_lines") or 0),
                "after_lines": int(n.get("text_lines") or 0),
                "text_changed_chars": st["changed_chars"],
                "added_lines": st["added_lines"], "removed_lines": st["removed_lines"],
                "before_text": o["text"], "after_text": n["text"],
                "ops": ops,
            },
            "refs_changed": renamed,
            "refs_added": added_refs,
            "refs_removed": removed_refs,
        })
    return views


def _asset_views(old: dict, new: dict, store: Store,
                 bodies: dict[str, bytes]) -> list[dict]:
    """资源层面的变化：改名 / 新增 / 消失 / 内容变，附带新旧文本与字符串增删。

    `bodies` 是本轮刚抓到的资源正文（新 blob 还没写进 v2_asset_blobs），
    生成证据必须优先用它，否则 after_text 恒为空（P2-23）。
    """
    views: list[dict] = []
    old_assets = old.get("assets", {})
    new_assets = new.get("assets", {})
    old_js = _ref_pages(old.get("pages", {}), ("js", "css"))
    new_js = _ref_pages(new.get("pages", {}), ("js", "css"))
    seen: set[str] = set()

    def build(action: str, before_url: str, after_url: str, pages: list[str]) -> None:
        key = after_url or before_url
        if key in seen:
            return
        seen.add(key)
        o_rec = old_assets.get(before_url) or {}
        n_rec = new_assets.get(after_url) or {}
        before_text = store.load_asset_text(o_rec.get("sha256", "")) if before_url else ""
        body = bodies.get(after_url) if after_url else None
        if body is not None:
            after_text = body.decode("utf-8", "replace")
        else:
            after_text = store.load_asset_text(n_rec.get("sha256", "")) if after_url else ""
        added, removed = _strings_delta(o_rec.get("strings") or [], n_rec.get("strings") or [])
        views.append({
            "slug": slug_for(key),
            "action": action,
            "before_url": before_url, "after_url": after_url,
            "before_sha": o_rec.get("sha256", ""), "after_sha": n_rec.get("sha256", ""),
            "before_size": int(o_rec.get("size") or 0),
            "after_size": int(n_rec.get("size") or 0),
            "pages": pages,
            "page_classes": sorted({page_class(p) for p in pages}),
            "before_text": before_text, "after_text": after_text,
            "strings_added": added, "strings_removed": removed,
        })

    for tr in _pair_transitions(old_js, new_js):
        build(tr["action"], tr["before"], tr["after"], tr["pages"])
    for url in sorted(new_assets):
        n = new_assets[url]
        o = old_assets.get(url)
        if o and n.get("sha256") and o.get("sha256") and n["sha256"] != o["sha256"]:
            build("content", url, url, sorted(new_js.get(url, set())))
    return views


class _Brief:
    """按行数预算攒 BRIEF.md —— 超了就在末尾注明截断，绝不超过 200 行。"""

    def __init__(self, limit: int = BRIEF_MAX_LINES) -> None:
        self.lines: list[str] = []
        self.limit = limit
        self.truncated = False

    def add(self, *texts: str) -> None:
        for t in texts:
            for line in str(t).split("\n"):
                if len(self.lines) >= self.limit:
                    self.truncated = True
                    return
                self.lines.append(line.rstrip())

    def text(self) -> str:
        if not self.truncated:
            return "\n".join(self.lines).rstrip() + "\n"
        # 提示行也算进 limit：内容行少留两行（一个空行 + 一行提示），
        # 保证输出不超过 BRIEF_MAX_LINES，不再出现「上限 200 实际 202 行」。
        out = list(self.lines[: max(0, self.limit - 2)])
        out.append("")
        out.append(f"…（BRIEF 已到 {self.limit} 行上限，剩余明细见 manifest.json 与 diff/ 目录）")
        return "\n".join(out).rstrip() + "\n"


def build_brief(job: dict, page_views: list[dict],
                asset_views: list[dict], errors: list[dict], run: dict) -> str:
    """人读简报：哪几页变了 / 变的是 HTML 还是资源 / 正文改了哪几行 / JS 字符串增删。"""
    b = _Brief()
    seq = iter("一二三四五六七八九十")

    def sec(title: str) -> str:
        return "## %s、%s" % (next(seq, "附"), title)

    n_signals = len(job.get("signals") or [])
    kind_count: dict[str, int] = {}
    for s in job.get("signals") or []:
        kind_count[s["kind"]] = kind_count.get(s["kind"], 0) + 1

    b.add(f"# 前端变化简报 · 作业 #{job['id']}", "")
    b.add(f"- 生成时间：{job['created_at']}（北京时间）")
    b.add(f"- 档位：{job.get('mode', 'high')}　"
          f"类型：{'首次基线（只建库，不报警）' if job.get('baseline') else '常规巡检'}")
    b.add(f"- 一句话：{_oneshot(job, page_views, asset_views, errors)}", "")

    b.add(sec("本轮数字"), "")
    b.add(f"- 页面：{job.get('pages_total', 0)} 页，"
          f"{job.get('pages_changed', 0)} 页有变化"
          f"（{run.get('pages_200', 0)} 个重新下载 / {run.get('pages_304', 0)} 个 304 未改动）")
    b.add(f"- 资源：{job.get('assets_total', 0)} 个 JS/CSS，"
          f"{job.get('assets_changed', 0)} 个地址/内容变了"
          f"（{run.get('assets_200', 0)} 个重新下载 / {run.get('assets_304', 0)} 个 304）")
    if job.get("refs_changed"):
        b.add(f"- 图片 / 页面元信息引用：{job['refs_changed']} 处变化（只比 URL/文件名，不下载图片）")
    if kind_count:
        kinds_txt = "、".join("%s %d" % (k, v) for k, v in sorted(kind_count.items()))
        b.add("- 信号：%d 条 —— %s" % (n_signals, kinds_txt))
    else:
        b.add("- 信号：0 条")
    b.add(f"- 运行：耗时 {run.get('duration_ms', 0) / 1000:.2f} s · "
          f"网络流量 {human_bytes(int(run.get('wire_bytes', 0)))}")
    b.add("")

    # ---- 哪几页变了
    b.add(sec("哪几页变了"), "")
    if page_views:
        b.add("| 页面 | 分级 | HTML | 正文 | 相关资源 |")
        b.add("| --- | --- | --- | --- | --- |")
        for v in page_views[:BRIEF_PAGE_TABLE_ROWS]:
            html_cell = "变了" if v["html"]["changed"] else "没变"
            if v["html"]["refs_only"]:
                html_cell = "仅资源文件名"
            text_cell = (f"变了 +{v['text']['added_lines']}/-{v['text']['removed_lines']} 行"
                         if v["text"]["changed"] else "无变化")
            bits = [A.basename(r["after"]) for r in v["refs_changed"][:3]]
            if v.get("refs_added"):
                bits.append("引用 +%d" % len(v["refs_added"]))
            if v.get("refs_removed"):
                bits.append("引用 -%d" % len(v["refs_removed"]))
            refs_cell = "、".join(bits) or "—"
            b.add(f"| {_page_label(v['url'])} | {v['page_class']} | {html_cell} | "
                  f"{text_cell} | {refs_cell} |")
        if len(page_views) > BRIEF_PAGE_TABLE_ROWS:
            b.add("", f"另有 {len(page_views) - BRIEF_PAGE_TABLE_ROWS} 页变化情况类似，"
                      "完整清单见 manifest.json 的 pages 字段。")
    else:
        b.add("（没有页面 HTML / 正文变化）")
    b.add("")

    # ---- 页面详情
    detail_views = [v for v in page_views if v["text"]["changed"]]
    if detail_views:
        b.add(sec("正文改动详情"), "")
        for i, v in enumerate(detail_views[:BRIEF_PAGE_DETAILS], 1):
            b.add(f"### {i}. {v['url']}")
            b.add(f"- 标题：{v['title'] or '（无）'}　分级：{v['page_class']}")
            if v["html"]["changed"]:
                b.add(f"- HTML：{v['html']['before_size']:,} → {v['html']['after_size']:,} 字节"
                      f"（{v['html']['after_size'] - v['html']['before_size']:+d}）")
            b.add(f"- 正文：{v['text']['before_lines']} → {v['text']['after_lines']} 行，"
                  f"改动 {v['text']['text_changed_chars']} 字"
                  f"（+{v['text']['added_lines']}/-{v['text']['removed_lines']} 行）")
            ref_bits = [f"{A.basename(r['before'])} → {A.basename(r['after'])}"
                        for r in v["refs_changed"][:3]]
            ref_bits += ["新增 " + A.basename(u) for u in v.get("refs_added", [])[:2]]
            ref_bits += ["消失 " + A.basename(u) for u in v.get("refs_removed", [])[:2]]
            if ref_bits:
                b.add("- 关联资源：" + "、".join(ref_bits))
            body = _render_ops(v["text"]["ops"], BRIEF_DIFF_LINES)
            if body:
                b.add("", "```diff", body, "```")
            b.add("")
        if len(detail_views) > BRIEF_PAGE_DETAILS:
            b.add(f"（另有 {len(detail_views) - BRIEF_PAGE_DETAILS} 页正文有改动，"
                  f"diff 见 diff/ 目录）", "")
    else:
        b.add(sec("正文改动详情"), "", "（正文没有变化）", "")

    # ---- 资源详情
    b.add(sec("前端资源变化"), "")
    if asset_views:
        for i, v in enumerate(asset_views[:BRIEF_ASSET_DETAILS], 1):
            label = {"rename": "改名", "added": "新增", "removed": "消失",
                     "content": "内容变化"}[v["action"]]
            b.add(f"### {i}. {label} · {A.basename(v['after_url'] or v['before_url'])}")
            if v["action"] == "rename":
                b.add(f"- 地址：{v['before_url']} → {v['after_url']}")
            elif v["action"] == "added":
                b.add(f"- 地址：{v['after_url']}")
            elif v["action"] == "removed":
                b.add(f"- 地址：{v['before_url']}（已消失）")
            else:
                b.add(f"- 地址：{v['after_url']}（同名内容变了）")
            if v["after_size"] or v["before_size"]:
                b.add(f"- 大小：{human_bytes(v['before_size'])} → {human_bytes(v['after_size'])}")
            if v["pages"]:
                b.add(f"- 引用页面：{len(v['pages'])} 页"
                      + (f"（{'、'.join(_page_label(p) for p in v['pages'][:3])}）"
                         if len(v["pages"]) <= 3 else ""))
            if v["strings_added"]:
                b.add(f"- 新增字符串（前 {min(len(v['strings_added']), BRIEF_STRINGS_PER_SIDE)} 条，"
                      f"共 {len(v['strings_added'])} 条）：")
                for s in v["strings_added"][:BRIEF_STRINGS_PER_SIDE]:
                    b.add(f"    - `{_shorten(s, 120)}`")
            if v["strings_removed"]:
                b.add(f"- 消失字符串（前 {min(len(v['strings_removed']), BRIEF_STRINGS_PER_SIDE)} 条，"
                      f"共 {len(v['strings_removed'])} 条）：")
                for s in v["strings_removed"][:BRIEF_STRINGS_PER_SIDE]:
                    b.add(f"    - `{_shorten(s, 120)}`")
            if not v["strings_added"] and not v["strings_removed"] and v["action"] == "content":
                b.add("- 字符串集合没有变化（可能只是压缩参数/注释变化）")
            b.add("")
        if len(asset_views) > BRIEF_ASSET_DETAILS:
            b.add(f"（另有 {len(asset_views) - BRIEF_ASSET_DETAILS} 个资源变化，"
                  f"明细见 manifest.json 与 assets/ 目录）", "")
    else:
        b.add("（没有 JS/CSS 变化）", "")

    # ---- 新页面 / 下线页面
    new_pages = [s for s in job.get("signals") or [] if s["kind"] == "new_page"]
    gone_pages = [s for s in job.get("signals") or [] if s["kind"] == "gone_page"]
    if new_pages or gone_pages:
        b.add(sec("新页面 / 下线页面"), "")
        for s in new_pages:
            b.add(f"- 新增：{s['page']}")
        for s in gone_pages:
            b.add(f"- 下线：{s['page']}")
        b.add("")

    # ---- 失败
    if errors:
        b.add(sec("抓取失败"), "")
        for e in errors[:12]:
            b.add(f"- {e['url']} —— {e['error']}")
        if len(errors) > 12:
            b.add(f"- …另有 {len(errors) - 12} 条失败，见 manifest.json 的 errors")
        b.add("")

    # ---- 说明
    b.add(sec("怎么读这份简报"), "")
    b.add("- `page_html` = HTML 字节哈希变了；`page_text` = 抽出的正文变了；"
          "`asset_url` = 资源地址/文件名变了；`asset_content` = 同名资源内容变了。")
    b.add("- JS 字符串是从压缩代码里抽的可读字面量（长度 ≥ 6、去掉路径和 URL），"
          "能看出「新功能/新文案/新模型名」。")
    b.add("- 机器可读明细：`manifest.json`；完整 diff：`diff/`；新旧正文：`pages/`；"
          "新旧资源文本：`assets/`。")
    b.add(f"- 本轮证据目录：{job.get('evidence_dir', '') or '（未生成）'}")
    return b.text()


def _oneshot(job: dict, page_views: list[dict], asset_views: list[dict],
             errors: list[dict]) -> str:
    """一句话结论（给第一屏，也方便邮件 headline 兜底）。"""
    if job.get("baseline"):
        return "首次基线：已把全部页面与资源写入快照，本轮不产生任何信号。"
    parts: list[str] = []
    text_pages = [v for v in page_views if v["text"]["changed"]]
    if text_pages:
        names = "、".join(_page_label(v["url"]) for v in text_pages[:3])
        more = f" 等 {len(text_pages)} 页" if len(text_pages) > 3 else ""
        parts.append(f"{names}{more} 正文有改动")
    html_only = [v for v in page_views if v["html"]["changed"] and not v["text"]["changed"]]
    if html_only:
        if all(v["html"]["refs_only"] for v in html_only):
            parts.append(f"{len(html_only)} 页 HTML 变化仅来自资源文件名（站点重新构建，正文无改动）")
        else:
            parts.append(f"{len(html_only)} 页 HTML 有变化但正文没变")
    if asset_views:
        action_cn = {"rename": "改名", "added": "新增", "removed": "消失", "content": "内容变"}
        kinds = {"rename": 0, "added": 0, "removed": 0, "content": 0}
        for v in asset_views:
            kinds[v["action"]] += 1
        bits = []
        for k, n in kinds.items():
            if n:
                bits.append("%d 个%s" % (n, action_cn[k]))
        parts.append("前端资源 " + "、".join(bits))
    new_pages = [s for s in (job.get("signals") or []) if s.get("kind") == "new_page"]
    if new_pages:
        names = "、".join(_page_label(s.get("page") or "") for s in new_pages[:3])
        parts.append(f"新增 {len(new_pages)} 个页面：{names}")
    gone_pages = [s for s in (job.get("signals") or []) if s.get("kind") == "gone_page"]
    if gone_pages:
        names = "、".join(_page_label(s.get("page") or "") for s in gone_pages[:3])
        parts.append(f"{len(gone_pages)} 个页面下线：{names}")
    ref_changes = [x for x in (job.get("signals") or [])
                   if x["kind"] == "asset_url" and x.get("ref_kind") in ("img", "other")]
    if ref_changes:
        imgs = sum(1 for x in ref_changes if x.get("ref_kind") == "img")
        others = len(ref_changes) - imgs
        bits = []
        if imgs:
            bits.append("%d 处图片引用" % imgs)
        if others:
            bits.append("%d 处页面元信息引用" % others)
        parts.append("、".join(bits) + "变化")
    if not parts:
        parts.append("没有检测到任何变化" if not errors else "没有内容变化，但有抓取失败")
    return "；".join(parts) + "。"


def _page_label(url: str) -> str:
    """URL → 简报里用的短标签（pricing / guides/thinking_mode 之类）。"""
    path = urllib.parse.urlsplit(url).path
    path = re.sub(r"^/zh-cn/", "", path).strip("/")
    return path or urllib.parse.urlsplit(url).hostname or url


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _render_ops(ops: list[dict], max_lines: int) -> str:
    """把段落 diff 渲染成 ```diff 块内容（每个 op 最多取几行）。"""
    total = sum(len(op["before"]) + len(op["after"]) for op in ops)
    out: list[str] = []
    for op in ops:
        for line in op["before"]:
            if len(out) >= max_lines:
                break
            out.append("- " + line)
        for line in op["after"]:
            if len(out) >= max_lines:
                break
            out.append("+ " + line)
        if len(out) >= max_lines:
            break
    if total > len(out):
        # 先记 total 再截断，提示的是「没显示的行数」，不会永远是 0。
        out = out[:max_lines]
        out.append(f"…（还有 {total - len(out)} 行改动未显示）")
    return "\n".join(out)


def write_evidence(job: dict, old: dict, new: dict, page_views: list[dict],
                   asset_views: list[dict], errors: list[dict], run: dict,
                   data_dir: Path) -> Path:
    """落 §3.2 的证据包，返回证据目录。"""
    ev = data_dir / JOBS_DIR / str(job["id"])
    if ev.exists():
        shutil.rmtree(ev)
    ev.mkdir(parents=True, exist_ok=True)
    job["evidence_dir"] = str(ev)  # 写 BRIEF 时就要引用
    files: dict[str, int] = {}

    # pages/<slug>.before.txt / .after.txt —— 只有正文真变了的页面才落（否则两份一样没信息量）
    page_files: dict[str, dict] = {}
    for v in page_views:
        if not v["text"]["changed"]:
            continue
        slug = v["slug"]
        before_bytes = _safe_write(ev / "pages" / f"{slug}.before.txt",
                                   v["text"]["before_text"], MAX_EVIDENCE_TEXT_BYTES)
        after_bytes = _safe_write(ev / "pages" / f"{slug}.after.txt",
                                  v["text"]["after_text"], MAX_EVIDENCE_TEXT_BYTES)
        files[f"pages/{slug}.before.txt"] = before_bytes
        files[f"pages/{slug}.after.txt"] = after_bytes
        diff_text = _unified(v["text"]["before_text"], v["text"]["after_text"],
                             f"{A.basename(v['url'])} (before)", f"{A.basename(v['url'])} (after)")
        files[f"diff/{slug}.diff"] = _safe_write(ev / "diff" / f"{slug}.diff", diff_text)
        page_files[slug] = {"before": f"pages/{slug}.before.txt",
                            "after": f"pages/{slug}.after.txt",
                            "diff": f"diff/{slug}.diff"}

    # assets/<slug>.before.txt / .after.txt + diff（字符串集合统一 diff）
    asset_files: dict[str, dict] = {}
    for v in asset_views:
        slug = v["slug"]
        entry: dict[str, str] = {}
        if v["before_text"]:
            files[f"assets/{slug}.before.txt"] = _safe_write(
                ev / "assets" / f"{slug}.before.txt", v["before_text"], MAX_ASSET_TEXT_BYTES)
            entry["before"] = f"assets/{slug}.before.txt"
        if v["after_text"]:
            files[f"assets/{slug}.after.txt"] = _safe_write(
                ev / "assets" / f"{slug}.after.txt", v["after_text"], MAX_ASSET_TEXT_BYTES)
            entry["after"] = f"assets/{slug}.after.txt"
        diff_text = _string_set_diff(
            (old.get("assets", {}).get(v["before_url"]) or {}).get("strings") or [],
            (new.get("assets", {}).get(v["after_url"]) or {}).get("strings") or [],
            f"{A.basename(v['before_url'] or v['after_url'])} 字符串集合 (before)",
            f"{A.basename(v['after_url'] or v['before_url'])} 字符串集合 (after)")
        files[f"diff/{slug}.diff"] = _safe_write(ev / "diff" / f"{slug}.diff", diff_text)
        entry["diff"] = f"diff/{slug}.diff"
        asset_files[slug] = entry

    # BRIEF.md
    brief = build_brief(job, page_views, asset_views, errors, run)
    files["BRIEF.md"] = _safe_write(ev / "BRIEF.md", brief)

    # manifest.json —— 给规则和 agent 的机器可读明细
    pages_manifest = []
    for v in page_views:
        pages_manifest.append({
            "url": v["url"],
            "slug": v["slug"],
            "page_class": v["page_class"],
            "title": v["title"],
            "html_changed": v["html"]["changed"],
            "html_refs_only": v["html"]["refs_only"],
            "html_before_sha": v["html"]["before_sha"],
            "html_after_sha": v["html"]["after_sha"],
            "html_before_size": v["html"]["before_size"],
            "html_after_size": v["html"]["after_size"],
            "text_changed": v["text"]["changed"],
            "text_changed_chars": v["text"]["text_changed_chars"],
            "text_added_lines": v["text"]["added_lines"],
            "text_removed_lines": v["text"]["removed_lines"],
            "text_before_lines": v["text"]["before_lines"],
            "text_after_lines": v["text"]["after_lines"],
            "refs_changed": v["refs_changed"],
            "refs_added": v.get("refs_added", []),
            "refs_removed": v.get("refs_removed", []),
            "evidence": page_files.get(v["slug"], {}),
        })
    assets_manifest = []
    for v in asset_views:
        assets_manifest.append({
            "action": v["action"],
            "before_url": v["before_url"],
            "after_url": v["after_url"],
            "slug": v["slug"],
            "before_sha": v["before_sha"],
            "after_sha": v["after_sha"],
            "before_size": v["before_size"],
            "after_size": v["after_size"],
            "affected_pages": v["pages"],
            "affected_page_classes": v["page_classes"],
            "strings_added": v["strings_added"],
            "strings_removed": v["strings_removed"],
            "evidence": asset_files.get(v["slug"], {}),
        })

    manifest = {
        # 顶层铺一份常用字段：docwatch/toolbox.py 直接读 manifest["id"/"created_at"/...]
        "id": job.get("id"), "created_at": job.get("created_at"),
        "mode": job.get("mode"), "baseline": job.get("baseline"),
        "pages_total": job.get("pages_total"), "pages_changed": job.get("pages_changed"),
        "assets_total": job.get("assets_total"), "assets_changed": job.get("assets_changed"),
        "refs_changed": job.get("refs_changed", 0), "n_errors": job.get("n_errors", 0),
        "duration_ms": job.get("duration_ms"), "wire_bytes": job.get("wire_bytes"),
        "evidence_dir": job.get("evidence_dir"),
        "job": job,
        "run": run,
        "brief": "BRIEF.md",
        "pages": pages_manifest,
        "assets": assets_manifest,
        "signals": job.get("signals") or [],
        "errors": errors,
        "sitemap": {
            "total": len(new.get("sitemap") or []),
            "new_pages": [s["page"] for s in job.get("signals") or [] if s["kind"] == "new_page"],
            "gone_pages": [s["page"] for s in job.get("signals") or [] if s["kind"] == "gone_page"],
            "error": job.get("sitemap_error") or "",
        },
        "evidence_files": dict(sorted(files.items())),
        "notes": [
            "level 由 docwatch/rules.py 判定；本文件只提供事实与证据。",
            "page_html 变化若 html_refs_only=true，说明差异只在资源文件名（站点重新构建）。",
            "text_changed_chars = 正文新增字符 + 删除字符（段落级 diff 统计）。",
            "asset_url 的 page 为空表示多个页面共用该资源；affected_pages 里有完整清单。",
        ],
    }
    for _ in range(2):  # 两遍：第二遍把 manifest.json 自己的大小也算进去
        manifest["evidence_files"] = dict(sorted(files.items()))
        blob = json.dumps(manifest, ensure_ascii=False, indent=1)
        files["manifest.json"] = len(blob.encode("utf-8"))
    manifest["evidence_files"] = dict(sorted(files.items()))
    blob = json.dumps(manifest, ensure_ascii=False, indent=1)
    files["manifest.json"] = _safe_write(ev / "manifest.json", blob)

    return ev


# ================================================================ 主流程

def run_once(data_dir: str | Path, *, pages: list[str] | None = None,
             mode: str | None = None, workers: int = WORKERS,
             fetcher: Fetcher | None = None, limit: int | None = None,
             write_evidence_package: bool = True,
             prune_jobs: bool = True) -> dict:
    """跑一轮采集 + 判定 + 落证据包，返回 job dict（CONTRACT §3.2）。

    参数：
    - data_dir：数据目录，库是 `<data-dir>/docs.db`，证据包在 `<data-dir>/jobs/<id>/`
    - pages：只跑指定页面（调试用；此时不做 new_page/gone_page 对比）
    - mode：档位（high/normal/quiet），只记录，不影响采集
    - fetcher：可注入（离线测试用 FakeFetcher）
    - limit：只跑前 N 页（调试用）
    """
    data_dir = Path(data_dir)
    store = Store(data_dir)
    fx = fetcher or Fetcher(workers=workers)
    started = time.monotonic()
    try:
        old = store.load_snapshot()
        # 只有「全量跑完一轮」才算建好基线；--limit/--only 这种调试跑不算
        baseline = old is None or not store.meta_get("baseline_done")
        run_mode = mode or store.meta_get("mode", "high") or "high"
        store.meta_set("mode", run_mode)

        page_list, sitemap, full, sitemap_errors = _resolve_page_list(store, fx, pages)
        if limit:
            page_list = page_list[:limit]
            full = False

        new: dict = {"ts": now_iso(), "pages": {}, "assets": {}, "page_list": page_list,
                     "sitemap": sitemap, "baseline": baseline, "partial": not full}
        new["pages"] = _collect_pages(fx, page_list, old, workers)
        assets, bodies = _collect_assets(fx, new["pages"], old, workers)
        new["assets"] = assets

        signals = [] if baseline else diff_snapshot(old, new)

        errors = [{"url": url, "error": rec.get("error", "")}
                  for url, rec in new["pages"].items() if not rec.get("ok")]
        errors += [{"url": url, "error": rec.get("error", "")}
                   for url, rec in new["assets"].items() if not rec.get("ok")]
        # sitemap 抓取/子 sitemap 失败也要进 job 的错误字段：否则新页面/下线页面
        # 可能整轮静默失效，而外面看起来一切正常（P2-24）。
        errors += [{"url": "sitemap", "error": msg} for msg in sitemap_errors]

        stats = dict(fx.stats)
        duration_ms = int((time.monotonic() - started) * 1000)
        run = {
            "duration_ms": duration_ms,
            "wire_bytes": stats.get("wire_bytes", 0),
            "body_bytes": stats.get("body_bytes", 0),
            "requests": stats.get("requests", 0),
            "retries": stats.get("retries", 0),
            "pages_200": sum(1 for r in new["pages"].values() if not r.get("revalidated") and r.get("ok")),
            "pages_304": sum(1 for r in new["pages"].values() if r.get("revalidated")),
            "assets_200": sum(1 for r in new["assets"].values() if r.get("downloaded")),
            "assets_304": sum(1 for r in new["assets"].values()
                              if r.get("ok") and not r.get("downloaded")),
            "downloaded_bytes": sum(len(b) for b in bodies.values()),
        }

        changed_pages = sorted({s["page"] for s in signals
                                if s["kind"] in ("page_html", "page_text") and s["page"]})
        job = {
            "id": 0,
            "created_at": new["ts"],
            "mode": run_mode,
            "baseline": baseline,
            "pages_total": len(page_list),
            "pages_changed": len(changed_pages),
            "assets_total": len(new["assets"]),
            "assets_changed": len({s["asset"] for s in signals
                                   if s["kind"] in ("asset_url", "asset_content")
                                   and s.get("ref_kind") not in ("img", "other")}),
            "refs_changed": sum(1 for s in signals if s["kind"] == "asset_url"
                                and s.get("ref_kind") in ("img", "other")),
            "n_errors": len(errors),
            "sitemap_error": "；".join(sitemap_errors),
            "sitemap_errors": sitemap_errors,
            "duration_ms": duration_ms,
            "wire_bytes": run["wire_bytes"],
            "signals": signals,
            "evidence_dir": "",
            "partial": not full,
        }
        job["id"] = store.insert_job(job)

        page_views: list[dict] = []
        asset_views: list[dict] = []
        if signals:
            page_views = _page_views(old, new) if old else []
            asset_views = _asset_views(old, new, store, bodies)
            if write_evidence_package:
                ev = write_evidence(job, old, new, page_views, asset_views, errors, run,
                                    data_dir)
                job["evidence_dir"] = str(ev)
                if prune_jobs:
                    _prune_jobs(data_dir)
        # 快照和 ready=1 必须同一个事务：如果这里被杀，作业保持 ready=0，
        # recover 不会补发，下一轮仍用旧快照重新 diff，不会丢变化也不会发两次。
        store.save_snapshots(old, new, bodies, ready_job_id=job["id"])
        if not new["partial"]:
            store.meta_set("baseline_done", "1")
        store.update_job(job)
        if signals:
            store.insert_signals(job["id"], signals)
        caps = store.enforce_caps()
        job["run"] = run
        job["caps"] = caps
        return job
    finally:
        if fetcher is None:
            fx.close()
        store.close()


def _prune_jobs(data_dir: Path, keep: int = JOBS_KEEP) -> None:
    """证据包只留最近 keep 个作业目录（只删自己生成的数字目录，不动别的）。"""
    jobs = data_dir / JOBS_DIR
    if not jobs.is_dir():
        return
    dirs = sorted((d for d in jobs.iterdir() if d.is_dir() and d.name.isdigit()),
                  key=lambda d: int(d.name))
    for d in dirs[:-keep] if len(dirs) > keep else []:
        try:
            shutil.rmtree(d)
        except OSError:
            pass


# ================================================================ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m docwatch.collect.signals",
        description="采集一轮（58 页 HTML + 24 个 JS/CSS + sitemap）并判定变化，有变化时落证据包")
    ap.add_argument("--data-dir", default="docwatch/data", help="数据目录（库 + jobs/<id>）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 页（调试）")
    ap.add_argument("--only", action="append", default=[], help="只跑指定 URL（可多次）")
    ap.add_argument("--mode", default="", help="档位 high/normal/quiet，仅记录")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--json", action="store_true", help="打印完整 job JSON")
    ap.add_argument("--no-evidence", action="store_true", help="不落证据包（只更新库）")
    args = ap.parse_args(argv)

    job = run_once(
        args.data_dir,
        pages=args.only or None,
        mode=args.mode or None,
        workers=args.workers,
        limit=args.limit or None,
        write_evidence_package=not args.no_evidence,
    )
    run = job.get("run") or {}
    if args.json:
        print(json.dumps(job, ensure_ascii=False, indent=1))
        return 0
    print(f"[job #{job['id']}] {'基线' if job['baseline'] else '巡检'} · "
          f"{job['pages_total']} 页 / {job['assets_total']} 资源 · "
          f"{job['pages_changed']} 页变化 · {len(job['signals'])} 条信号")
    print(f"  耗时 {job['duration_ms'] / 1000:.2f} s · 流量 {human_bytes(job['wire_bytes'])} · "
          f"请求 {run.get('requests', 0)}（304: {run.get('pages_304', 0)} 页 / "
          f"{run.get('assets_304', 0)} 资源）")
    for sig in job["signals"][:20]:
        print(f"  - [{sig['kind']}] {sig['detail']}")
    if len(job["signals"]) > 20:
        print(f"  …另有 {len(job['signals']) - 20} 条")
    if job.get("evidence_dir"):
        print(f"  证据包：{job['evidence_dir']}")
        brief = Path(job["evidence_dir"]) / "BRIEF.md"
        if brief.exists():
            lines = brief.read_text(encoding="utf-8").split("\n")
            print(f"  BRIEF.md：{len(lines)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
