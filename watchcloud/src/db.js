import { SOURCE_ORDER } from "./constants.js";

// 历史事件里有一批旧口径的 3 级：DSH 历史版本补录、只有前端版本号变化、
// 以及明确“未发现可见变化”的记录。数据库里的 level 不改写；展示和筛选统一
// 通过这个 CASE 读出“展示级别”，避免旧记录继续冒充“重要”。
const DISPLAY_LEVEL_SQL = `CASE
  WHEN COALESCE(quality, '') = 'internal' THEN 1
  WHEN action LIKE '%历史版本补录%' THEN 1
  WHEN title = '本次为内部调整，未发现界面变化'
    OR title LIKE '未捕获到用户可见变化%' THEN 1
  WHEN title LIKE '%前端版本更新%'
    AND COALESCE(quality, '') IN ('', 'unknown') THEN 1
  WHEN title LIKE '%图片预览与图表显示相关部分有调整%' THEN 2
  WHEN title LIKE '%界面变化无法确认%' THEN 2
  WHEN COALESCE(quality, '') IN ('', 'unknown')
    AND summary LIKE '%语言包未核对%' THEN 2
  ELSE level
END`;

// 列表 / RSS / 相邻事件只需要展示字段；payload_json、evidence_json、search_text
// 是详情页才用的大字段（S-A5）。分开查能明显减小 D1 每行读取与 JSON.parse 开销。
const LIST_COLUMNS = `id, slug, source, kind, (${DISPLAY_LEVEL_SQL}) AS level, level_label, quality, title, summary,
  impact, action, signal_count, changed_ts, detected_ts, page_url, archive_url, updated_ts,
  json_extract(payload_json, '$.recheck.state') AS recheck_state,
  json_extract(payload_json, '$.recheck.verdict') AS recheck_verdict,
  json_extract(payload_json, '$.frontend.totals') AS frontend_totals_json,
  json_extract(payload_json, '$.frontend.main_js') AS frontend_main_js_json,
  json_extract(payload_json, '$.frontend.main_css') AS frontend_main_css_json`;
const DETAIL_COLUMNS = `${LIST_COLUMNS}, evidence_json, payload_json, search_text`;

function safeJson(value, fallback) {
  try {
    const parsed = JSON.parse(value || "");
    return parsed === null || parsed === undefined ? fallback : parsed;
  } catch {
    return fallback;
  }
}

export function rowToEvent(row) {
  if (!row) return null;
  const payload = safeJson(row.payload_json, {});
  // 列表查询不取 payload_json（省流量），但首页需要 frontend 的技术字段才能
  // 把「未捕获」历史记录升级为具体的文件/字节标题；这里只补最小的三块。
  if (
    !payload.frontend &&
    (row.frontend_totals_json || row.frontend_main_js_json || row.frontend_main_css_json)
  ) {
    payload.frontend = {
      totals: safeJson(row.frontend_totals_json, {}),
      main_js: safeJson(row.frontend_main_js_json, {}),
      main_css: safeJson(row.frontend_main_css_json, {}),
    };
  }
  return {
    id: String(row.id || ""),
    slug: String(row.slug || ""),
    source: String(row.source || ""),
    kind: String(row.kind || ""),
    level: Number(row.level || 0),
    levelLabel: String(row.level_label || ""),
    quality: String(row.quality || "unknown"),
    title: String(row.title || ""),
    summary: String(row.summary || ""),
    impact: String(row.impact || ""),
    action: String(row.action || ""),
    signalCount: Number(row.signal_count || 1),
    changedTs: Number(row.changed_ts || 0),
    detectedTs: Number(row.detected_ts || 0),
    pageUrl: String(row.page_url || ""),
    archiveUrl: String(row.archive_url || ""),
    evidence: safeJson(row.evidence_json, []),
    payload,
    recheckState: String(row.recheck_state || ""),
    recheckVerdict: String(row.recheck_verdict || ""),
    searchText: String(row.search_text || ""),
    updatedTs: Number(row.updated_ts || 0),
  };
}

function buildWhere({ source, level, q, minLevel }) {
  const clauses = [];
  const params = [];
  if (source) {
    clauses.push("source = ?");
    params.push(source);
  }
  if (level !== null && level !== undefined && level !== "") {
    clauses.push(`(${DISPLAY_LEVEL_SQL}) = ?`);
    params.push(Number(level));
  }
  if (minLevel !== null && minLevel !== undefined && minLevel !== "") {
    clauses.push(`(${DISPLAY_LEVEL_SQL}) >= ?`);
    params.push(Number(minLevel));
  }
  if (q) {
    clauses.push("instr(lower(search_text), lower(?)) > 0");
    params.push(String(q).slice(0, 120));
  }
  return { where: clauses.length ? `WHERE ${clauses.join(" AND ")}` : "", params };
}

export async function listEvents(db, options = {}) {
  const { source = "", level = null, minLevel = null, q = "", limit = 20, offset = 0, includePayload = false } = options;
  const { where, params } = buildWhere({ source, level, q, minLevel });
  const safeLimit = Math.max(1, Math.min(1000, Number(limit) || 20));
  const safeOffset = Math.max(0, Number(offset) || 0);
  // RSS 需要 DSH 发版的 payload.release_note；列表页仍只取轻量字段。
  const columns = includePayload ? DETAIL_COLUMNS : LIST_COLUMNS;
  const [countRow, result] = await Promise.all([
    db.prepare(`SELECT COUNT(*) AS total FROM events ${where}`).bind(...params).first(),
    db
      .prepare(
        `SELECT ${columns} FROM events ${where}
         ORDER BY changed_ts DESC, id DESC LIMIT ? OFFSET ?`,
      )
      .bind(...params, safeLimit, safeOffset)
      .all(),
  ]);
  return {
    items: (result.results || []).map(rowToEvent),
    total: Number(countRow?.total || 0),
    limit: safeLimit,
    offset: safeOffset,
  };
}

// sitemap 只需要 slug 和 changed_ts；走 listEvents 会把 payload/evidence/search_text
// 全部取回并逐行 JSON.parse，上千条事件时纯属浪费 Worker CPU 和内存。
export async function listSitemapEvents(db, limit = 5000) {
  const safeLimit = Math.max(1, Math.min(5000, Number(limit) || 5000));
  const result = await db
    .prepare(
      `SELECT slug, changed_ts FROM events
       ORDER BY changed_ts DESC, id DESC LIMIT ?`,
    )
    .bind(safeLimit)
    .all();
  return (result.results || []).map((row) => ({
    slug: String(row.slug || ""),
    changedTs: Number(row.changed_ts || 0),
  }));
}

// slug 归属批量查询：ingest 一次取回整批 slug 的占用情况，避免 50-100 个事件
// 各自一次串行点查。D1 / SQLite 的绑定变量有上限，按 90 一组分块。
const SLUG_QUERY_CHUNK = 90;

export async function getSlugOwners(db, slugs) {
  const owners = new Map();
  const unique = [...new Set((slugs || []).map((slug) => String(slug ?? "")).filter(Boolean))];
  for (let index = 0; index < unique.length; index += SLUG_QUERY_CHUNK) {
    const chunk = unique.slice(index, index + SLUG_QUERY_CHUNK);
    const placeholders = chunk.map(() => "?").join(", ");
    try {
      const result = await db
        .prepare(`SELECT id, slug FROM events WHERE slug IN (${placeholders})`)
        .bind(...chunk)
        .all();
      for (const row of result.results || []) {
        owners.set(String(row.slug || ""), String(row.id || ""));
      }
    } catch {
      // 查重失败按「没有占用」处理：唯一约束仍是最后兜底，不因一次只读查询异常让整批 500。
    }
  }
  return owners;
}

export async function getEventBySlug(db, slug) {
  const row = await db
    .prepare(`SELECT ${DETAIL_COLUMNS} FROM events WHERE slug = ? LIMIT 1`)
    .bind(String(slug || ""))
    .first();
  return rowToEvent(row);
}

function rowToChange(row) {
  return {
    idx: Number(row.idx || 0),
    page: String(row.page || ""),
    level: Number(row.level || 0),
    isAsset: Boolean(Number(row.is_asset || 0)),
    isError: Boolean(Number(row.is_error || 0)),
    noise: Boolean(Number(row.noise || 0)),
    hasDiff: Boolean(Number(row.has_diff || 0)),
    diffBytes: Number(row.diff_bytes || 0),
    bullets: safeJson(row.bullets_json, []),
    diff: String(row.diff || ""),
    note: String(row.note || ""),
  };
}

// 变化明细走单独的表：主事件 payload 只存目录，详情页不再背 diff 正文。
export async function getEventChanges(db, eventId) {
  const result = await db
    .prepare(
      `SELECT idx, page, level, is_asset, is_error, noise, has_diff, diff_bytes,
              bullets_json, diff, note
       FROM event_changes WHERE event_id = ? ORDER BY idx`,
    )
    .bind(String(eventId || ""))
    .all();
  return (result.results || []).map(rowToChange);
}

export async function getAdjacentEvents(db, event) {
  const previousRow = await db
    .prepare(
      `SELECT ${LIST_COLUMNS} FROM events
       WHERE changed_ts < ? OR (changed_ts = ? AND id < ?)
       ORDER BY changed_ts DESC, id DESC LIMIT 1`,
    )
    .bind(event.changedTs, event.changedTs, event.id)
    .first();
  const nextRow = await db
    .prepare(
      `SELECT ${LIST_COLUMNS} FROM events
       WHERE changed_ts > ? OR (changed_ts = ? AND id > ?)
       ORDER BY changed_ts ASC, id ASC LIMIT 1`,
    )
    .bind(event.changedTs, event.changedTs, event.id)
    .first();
  return {
    previous: rowToEvent(previousRow),
    next: rowToEvent(nextRow),
  };
}

export async function getStatuses(db) {
  const result = await db
    .prepare(
      `SELECT source, label, last_checked_ts, last_success_ts, last_event_ts,
              status, detail, updated_ts
       FROM source_status ORDER BY source`,
    )
    .all();
  const rows = (result.results || []).map((row) => ({
    source: String(row.source || ""),
    label: String(row.label || ""),
    lastCheckedTs: Number(row.last_checked_ts || 0),
    lastSuccessTs: Number(row.last_success_ts || 0),
    lastEventTs: Number(row.last_event_ts || 0),
    status: String(row.status || "unknown"),
    detail: String(row.detail || ""),
    updatedTs: Number(row.updated_ts || 0),
  }));
  const order = new Map(SOURCE_ORDER.map((source, index) => [source, index]));
  return rows.sort((a, b) => (order.get(a.source) ?? 99) - (order.get(b.source) ?? 99));
}

export async function getStatusHistory(db, perSource = 30) {
  const limit = Math.max(1, Math.min(30, Number(perSource) || 30));
  const results = await Promise.all(
    SOURCE_ORDER.map((source) =>
      db
        .prepare(
          `SELECT status, checked_ts, detail
           FROM status_history
           WHERE source = ? AND checked_ts > 0
           ORDER BY checked_ts DESC LIMIT ?`,
        )
        .bind(source, limit)
        .all()
        .catch(() => ({ results: [] })),
    ),
  );
  const history = {};
  SOURCE_ORDER.forEach((source, index) => {
    history[source] = (results[index].results || []).map((row) => ({
      status: String(row.status || "unknown"),
      checkedTs: Number(row.checked_ts || 0),
      detail: String(row.detail || ""),
    }));
  });
  return history;
}

export async function getSourceCounts(db) {
  const result = await db
    .prepare("SELECT source, COUNT(*) AS count FROM events GROUP BY source")
    .all();
  const counts = {};
  for (const source of SOURCE_ORDER) counts[source] = 0;
  for (const row of result.results || []) {
    counts[String(row.source)] = Number(row.count || 0);
  }
  return counts;
}

export async function getMeta(db, key) {
  const row = await db
    .prepare("SELECT value, updated_ts FROM meta WHERE key = ? LIMIT 1")
    .bind(key)
    .first();
  return row ? { value: String(row.value || ""), updatedTs: Number(row.updated_ts || 0) } : null;
}

// 状态页一次要读多个 meta 键：合成一条 IN 查询，避免 6 次串行点查。
export async function getMetaMany(db, keys) {
  const unique = [...new Set((keys || []).map((key) => String(key ?? "")).filter(Boolean))];
  if (!unique.length) return {};
  const placeholders = unique.map(() => "?").join(", ");
  const result = await db
    .prepare(`SELECT key, value, updated_ts FROM meta WHERE key IN (${placeholders})`)
    .bind(...unique)
    .all();
  const values = {};
  for (const row of result.results || []) {
    values[String(row.key || "")] = {
      value: String(row.value || ""),
      updatedTs: Number(row.updated_ts || 0),
    };
  }
  return values;
}

// 状态页「采集与推送」面板的质量分布：直接按事件列聚合，旧事件默认 unknown 也计入。
export async function getQualityCounts(db) {
  const result = await db
    .prepare("SELECT quality, COUNT(*) AS count FROM events GROUP BY quality")
    .all();
  const counts = {};
  for (const row of result.results || []) {
    counts[String(row.quality || "unknown")] = Number(row.count || 0);
  }
  return counts;
}

export async function getRssDaily(db, days = 14) {
  const result = await db
    .prepare(
      `SELECT day, source, requests FROM rss_daily
       ORDER BY day DESC, source ASC LIMIT ?`,
    )
    .bind(Math.max(1, Math.min(400, Number(days) * 12)))
    .all();
  return (result.results || []).map((row) => ({
    day: String(row.day || ""),
    source: String(row.source || ""),
    requests: Number(row.requests || 0),
  }));
}

export async function incrementRssDaily(db, day, source, nowTs) {
  return db
    .prepare(
      `INSERT INTO rss_daily (day, source, requests, updated_ts) VALUES (?, ?, 1, ?)
       ON CONFLICT(day, source) DO UPDATE SET
         requests = requests + 1,
         updated_ts = excluded.updated_ts`,
    )
    .bind(day, source, Number(nowTs))
    .run();
}

export async function getTotalEventCount(db) {
  const row = await db.prepare("SELECT COUNT(*) AS total FROM events").first();
  return Number(row?.total || 0);
}

export async function countEventsBetween(db, startTs, endTs) {
  const row = await db
    .prepare("SELECT COUNT(*) AS total FROM events WHERE changed_ts >= ? AND changed_ts < ?")
    .bind(Number(startTs) || 0, Number(endTs) || 0)
    .first();
  return Number(row?.total || 0);
}

export async function getEventDailyCounts(db, sinceTs) {
  const result = await db
    .prepare(
      `SELECT strftime('%Y-%m-%d', changed_ts, 'unixepoch', '+8 hours') AS day,
              COUNT(*) AS count
       FROM events WHERE changed_ts >= ?
       GROUP BY day ORDER BY day`,
    )
    .bind(Number(sinceTs) || 0)
    .all();
  return (result.results || []).map((row) => ({
    day: String(row.day || ""),
    count: Number(row.count || 0),
  }));
}
