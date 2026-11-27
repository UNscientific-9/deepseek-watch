-- DeepSeek 观测站 · Cloudflare D1 schema
-- 只存公开的监控事件与来源状态，不存访问者 IP / User-Agent / Cookie。

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'docwatch',
  level INTEGER NOT NULL DEFAULT 0,
  level_label TEXT NOT NULL DEFAULT '噪音',
  -- 2026-11-18 新增：事件可信度（ok/internal/partial/contradiction/missing/repaired）。
  -- 旧事件 / 无质量闸门的来源统一为 unknown，不拒绝、不回溯。
  -- 线上 D1 已于 2026-11-18 执行；新环境按下面这条执行一次即可：
  --   ALTER TABLE events ADD COLUMN quality TEXT NOT NULL DEFAULT 'unknown';
  quality TEXT NOT NULL DEFAULT 'unknown',
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  impact TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  signal_count INTEGER NOT NULL DEFAULT 1,
  changed_ts INTEGER NOT NULL,
  detected_ts INTEGER NOT NULL,
  page_url TEXT NOT NULL DEFAULT '',
  archive_url TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  payload_json TEXT NOT NULL DEFAULT '{}',
  search_text TEXT NOT NULL DEFAULT '',
  updated_ts INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_changed ON events (changed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_source_changed ON events (source, changed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_level_changed ON events (level, changed_ts DESC);

CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  last_checked_ts INTEGER,
  last_success_ts INTEGER,
  last_event_ts INTEGER,
  status TEXT NOT NULL DEFAULT 'unknown',
  detail TEXT NOT NULL DEFAULT '',
  updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  checked_ts INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_ts INTEGER NOT NULL,
  PRIMARY KEY (source, checked_ts)
);

CREATE INDEX IF NOT EXISTS idx_status_history_source_checked
  ON status_history (source, checked_ts DESC);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_ts INTEGER NOT NULL
);

-- 每日 RSS 拉取次数：只记「哪天、哪个订阅源、被拉了几次」。
CREATE TABLE IF NOT EXISTS rss_daily (
  day TEXT NOT NULL,
  source TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (day, source)
);

CREATE INDEX IF NOT EXISTS idx_rss_daily_day ON rss_daily (day);

-- 事件的变化明细：把 diff 正文从 events.payload_json 里拆出来，单独按需读取。
-- 写入幂等：ingest 每次先 DELETE 该 event_id 再 INSERT，重复推送不叠加。
CREATE TABLE IF NOT EXISTS event_changes (
  event_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  page TEXT NOT NULL DEFAULT '',
  level INTEGER NOT NULL DEFAULT 0,
  is_asset INTEGER NOT NULL DEFAULT 0,
  is_error INTEGER NOT NULL DEFAULT 0,
  noise INTEGER NOT NULL DEFAULT 0,
  has_diff INTEGER NOT NULL DEFAULT 0,
  diff_bytes INTEGER NOT NULL DEFAULT 0,
  bullets_json TEXT NOT NULL DEFAULT '[]',
  diff TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (event_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_event_changes_event ON event_changes (event_id, idx);
