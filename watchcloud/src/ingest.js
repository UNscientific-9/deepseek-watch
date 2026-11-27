import { isSource, levelLabel, SOURCE_ORDER } from "./constants.js";
import { getSlugOwners } from "./db.js";

const MAX_EVENTS = 100;
const MAX_STATUSES = 20;
const MAX_BODY_BYTES = 4_000_000;
const MAX_EVIDENCE = 20;
const MAX_EVIDENCE_CHARS = 1000;
const MAX_PAYLOAD_CHARS = 60_000;
const MAX_BATCH_STATEMENTS = 50;
// 单次请求的总 statement 上限：100 条事件 × 每组变化 1 条 INSERT 时，
// 最坏会到 4000+ 条，超过 D1/Worker 的查询数限制。这里在入口统一限流，
// 推送端会按同一个预算提前分块。
const MAX_TOTAL_STATEMENTS = 500;
const MAX_SLUG_ATTEMPTS = 20;
// 变化明细：单条 diff 上限 20,000 字符（D1 单语句 100KB 的安全余量），
// 每个事件最多 40 组（采集端目前上限 20，留余量）。
const MAX_CHANGE_GROUPS = 40;
const MAX_DIFF_CHARS = 20_000;
const MAX_CHANGE_BULLETS = 8;
const MAX_CHANGE_BULLET_CHARS = 500;
const MAX_CHANGE_NOTE_CHARS = 1000;
// 质量闸门状态（webwatch/quality.py 写入 payload.quality）。白名单外一律 unknown，
// 保证旧事件、非前端来源和异常值都能正常入库，不因新字段被拒。
const QUALITY_VALUES = new Set([
  "ok",
  "internal",
  "partial",
  "contradiction",
  "missing",
  "repaired",
]);
// 模型目录快照是整块 JSON 存入 meta；限制长度，避免异常 payload 长期占库。
const MAX_MODELS_SNAPSHOT_CHARS = 60_000;
const MAX_MODELS_IN_SNAPSHOT = 50;
// 推送端运维 meta（P2-52）：meta 表是 key/value TEXT，单条限制 8000 字符。
// 只接收白名单键；字段缺失或类型不对时跳过，不影响同批事件入库。
const MAX_PUSH_META_CHARS = 8000;
const MAX_PUSH_META_ERRORS = 40;
const MAX_PUSH_META_ERROR_CHARS = 1000;
const UTF8 = new TextEncoder();

function charLength(value) {
  return Array.from(String(value ?? "")).length;
}

function text(value, limit) {
  const raw = String(value ?? "").trim();
  if (raw.length <= limit) return raw;
  const chars = Array.from(raw);
  return chars.length <= limit ? raw : chars.slice(0, limit).join("");
}

function checkedText(value, limit) {
  const raw = String(value ?? "").trim();
  if (raw.length <= limit) return raw;
  return charLength(raw) > limit ? null : raw;
}

function httpUrl(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  return /^https?:\/\/[^\s]+$/i.test(raw) ? raw.slice(0, 800) : null;
}

function int(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : fallback;
}

function byteLength(value) {
  return UTF8.encode(String(value ?? "")).length;
}

// 单条 diff 截到 MAX_DIFF_CHARS 码点，末尾注明原文大小；截断备注本身也计入上限。
function truncateDiff(value, limit = MAX_DIFF_CHARS) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  const chars = Array.from(raw);
  if (chars.length <= limit) return raw;
  const marker = `\n…（已截断，原文 ${(byteLength(raw) / 1024).toFixed(1)} KB）`;
  const keep = Math.max(1, limit - Array.from(marker).length);
  return chars.slice(0, keep).join("") + marker;
}

function safeEqual(a, b) {
  const left = String(a || "");
  const right = String(b || "");
  let mismatch = left.length === right.length ? 0 : 1;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    mismatch |= (left.charCodeAt(index) || 0) ^ (right.charCodeAt(index) || 0);
  }
  return mismatch === 0;
}

function authOk(request, env) {
  const configured = env.INGEST_TOKEN || "";
  if (!configured) return false;
  const header = request.headers.get("authorization") || "";
  const match = header.match(/^Bearer\s+(.+)$/i);
  return Boolean(match && safeEqual(match[1], configured));
}

function shortHash(value) {
  let hash = 5381;
  const raw = String(value || "");
  for (let index = 0; index < raw.length; index += 1) {
    hash = ((hash * 33) ^ raw.charCodeAt(index)) >>> 0;
  }
  return hash.toString(36).slice(0, 8);
}

// 明细里的布尔标记只认真正的布尔 true 或数字 1。旧代码用 Boolean(raw.*)，
// 字符串 "false"/"0"/"no" 都会被当成 true，导致资源/错误/噪音标记反转（P3-05）。
function booleanFlag(value) {
  return value === true || value === 1;
}

function normalizeChange(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const bullets = Array.isArray(raw.bullets)
    ? raw.bullets
        .filter((item) => typeof item === "string" && item.trim())
        .slice(0, MAX_CHANGE_BULLETS)
        .map((item) => text(item, MAX_CHANGE_BULLET_CHARS))
    : [];
  const diff = truncateDiff(raw.diff);
  return {
    idx: 0,
    page: text(raw.page, 500),
    level: Math.max(0, Math.min(3, int(raw.level, 0))),
    isAsset: booleanFlag(raw.is_asset),
    isError: booleanFlag(raw.is_error),
    noise: booleanFlag(raw.noise),
    // has_diff / diff_bytes 由服务端从 diff 推导，不信任推送端自报。
    hasDiff: diff ? 1 : 0,
    diffBytes: byteLength(diff),
    bulletsJson: JSON.stringify(bullets),
    diff,
    note: text(raw.note, MAX_CHANGE_NOTE_CHARS),
  };
}

// 返回 null = 本次推送没带明细（保留库里旧记录）；返回数组 = 以本次为准（先删后插）。
function normalizeChangesDetail(raw) {
  if (!Array.isArray(raw)) return null;
  const changes = [];
  for (const item of raw) {
    if (changes.length >= MAX_CHANGE_GROUPS) break;
    const change = normalizeChange(item);
    if (!change) continue;
    change.idx = changes.length;
    changes.push(change);
  }
  return changes;
}

export function normalizeEventDetailed(raw, nowTs = Math.floor(Date.now() / 1000)) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { event: null, reason: "event must be an object" };
  }
  const id = checkedText(raw.id, 220);
  if (!id) return { event: null, reason: "invalid id" };
  const source = checkedText(raw.source, 32);
  if (!source || !isSource(source)) return { event: null, reason: "invalid source" };

  const rawSlug = checkedText(raw.slug, 140);
  if (!rawSlug) return { event: null, reason: "invalid slug" };
  const slug = rawSlug.toLowerCase();
  if (!/^[a-z0-9-]+$/.test(slug)) return { event: null, reason: "invalid slug" };

  const level = int(raw.level, -1);
  if (level < 0 || level > 3) return { event: null, reason: "invalid level" };

  // 展示字段按码点计数并截断，避免超长时整条事件被拒（P1-8）。
  const title = text(raw.title, 300);
  if (!title) return { event: null, reason: "invalid title" };

  const summary = text(raw.summary, 4000);
  const impact = text(raw.impact, 2000);
  const action = text(raw.action, 2000);

  const changedTs = int(raw.changed_ts, 0);
  // 未来时间必须拒绝：否则一个带未来 changed_ts 的事件会永久阻止后续正常更新。
  if (changedTs < 946_684_800 || changedTs > nowTs + 300) {
    return { event: null, reason: "invalid changed_ts" };
  }
  const detectedTsRaw = int(raw.detected_ts, nowTs) || nowTs;
  const detectedTs = Math.max(946_684_800, Math.min(detectedTsRaw, nowTs + 86400));

  const pageUrl = httpUrl(raw.page_url);
  const archiveUrl = httpUrl(raw.archive_url);
  if (pageUrl === null) return { event: null, reason: "invalid page_url" };
  if (archiveUrl === null) return { event: null, reason: "invalid archive_url" };

  const evidence = Array.isArray(raw.evidence)
    ? raw.evidence
        .filter((item) => typeof item === "string" && item.trim())
        .slice(0, MAX_EVIDENCE)
        .map((item) => text(item, MAX_EVIDENCE_CHARS))
    : [];

  if (!raw.payload || typeof raw.payload !== "object" || Array.isArray(raw.payload)) {
    return { event: null, reason: "invalid payload" };
  }
  const rawQuality = typeof raw.payload.quality === "string" ? raw.payload.quality.trim() : "";
  const quality = QUALITY_VALUES.has(rawQuality) ? rawQuality : "unknown";
  let payloadJson = "{}";
  try {
    payloadJson = JSON.stringify(raw.payload);
  } catch {
    return { event: null, reason: "invalid payload" };
  }
  if (payloadJson.length > MAX_PAYLOAD_CHARS && charLength(payloadJson) > MAX_PAYLOAD_CHARS) {
    return { event: null, reason: "payload too large" };
  }

  const searchText =
    text(raw.search_text, 20_000) ||
    text([title, summary, impact, action, evidence.join(" ")].join(" "), 20_000);
  const changesDetail = normalizeChangesDetail(raw.changes_detail);

  return {
    event: {
      id,
      slug,
      source,
      kind: text(raw.kind, 60) || "docwatch",
      level,
      levelLabel: text(raw.level_label, 20) || levelLabel(level),
      quality,
      title,
      summary,
      impact,
      action,
      signalCount: Math.max(1, int(raw.signal_count, 1)),
      changedTs,
      detectedTs,
      pageUrl,
      archiveUrl,
      evidenceJson: JSON.stringify(evidence),
      payloadJson,
      searchText,
      changesDetail,
    },
    reason: "",
  };
}

export function normalizeEvent(raw, nowTs = Math.floor(Date.now() / 1000)) {
  return normalizeEventDetailed(raw, nowTs).event;
}

export function normalizeStatus(raw) {
  if (!raw || typeof raw !== "object") return null;
  const source = text(raw.source, 32);
  if (!isSource(source)) return null;
  const status = ["ok", "warn", "error", "unknown"].includes(String(raw.status))
    ? String(raw.status)
    : "unknown";
  return {
    source,
    label: text(raw.label, 40) || source,
    lastCheckedTs: int(raw.last_checked_ts, 0) || null,
    lastSuccessTs: int(raw.last_success_ts, 0) || null,
    lastEventTs: int(raw.last_event_ts, 0) || null,
    status,
    detail: text(raw.detail, 500),
    updatedTs: int(raw.updated_ts, 0),
  };
}

// 快照由自己的推送器生成，服务端只做形状/体积兜底；逐字段清洗由
// watchsite._models_snapshot 和 render 负责，避免三层重复维护字段表。
function normalizeModelsSnapshot(raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== "object" || Array.isArray(raw)) return null;
  const checkedAt =
    text(raw.checked_at, 40) || text(raw.catalog_checked_at, 40);
  const models = Array.isArray(raw.models)
    ? raw.models.slice(0, MAX_MODELS_IN_SNAPSHOT)
    : [];
  // 空快照不能覆盖线上已有的完整目录。
  if (!checkedAt || !models.length) return null;
  const snapshot = {
    checked_at: checkedAt,
    catalog_available: Boolean(raw.catalog_available),
    models,
    last_change: raw.last_change === undefined ? null : raw.last_change,
  };
  // P2-19 / D2：模型目录失败信息要随快照一起上站，供状态页展示；
  // 老推送端没有这些字段时保持原样，不补默认值。
  const rawCatalogCheckedAt = text(raw.catalog_checked_at, 40);
  if (rawCatalogCheckedAt) snapshot.catalog_checked_at = rawCatalogCheckedAt;
  const rawCatalogError = raw.catalog_last_error;
  const catalogError =
    typeof rawCatalogError === "string" ? text(rawCatalogError, 500) : "";
  if (catalogError) snapshot.catalog_last_error = catalogError;
  const failStreak = int(raw.catalog_fail_streak, -1);
  if (failStreak >= 0) snapshot.catalog_fail_streak = failStreak;
  const incompleteStreak = int(raw.catalog_incomplete_streak, -1);
  if (incompleteStreak >= 0) snapshot.catalog_incomplete_streak = incompleteStreak;
  const value = JSON.stringify(snapshot);
  return charLength(value) <= MAX_MODELS_SNAPSHOT_CHARS ? value : null;
}

// 计数对象只保留白名单键，值必须是非负整数；整块无效时返回 null（不落库）。
function normalizeCounts(raw, allowed) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const counts = {};
  let hasValue = false;
  for (const key of allowed) {
    if (!Object.prototype.hasOwnProperty.call(raw, key)) continue;
    const value = int(raw[key], -1);
    if (value < 0) continue;
    counts[key] = value;
    hasValue = true;
  }
  return hasValue ? counts : null;
}

// 采集异常保留逐条完整说明（每条最多 1000 字符），列表过长时由 fitJsonMeta 从尾部裁掉。
function normalizeCollectionErrors(raw) {
  if (!Array.isArray(raw)) return null;
  const errors = [];
  for (const item of raw) {
    if (errors.length >= MAX_PUSH_META_ERRORS) break;
    if (typeof item === "string") {
      const detail = text(item, MAX_PUSH_META_ERROR_CHARS);
      if (detail) errors.push({ detail });
      continue;
    }
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const detail = text(item.detail ?? item.message ?? item.error, MAX_PUSH_META_ERROR_CHARS);
    if (!detail) continue;
    const source = text(item.source, 40);
    errors.push(source ? { source, detail } : { detail });
  }
  return errors;
}

// 把对象序列化成 meta 值；超过单条上限时，数组从尾部裁到能放下为止。
function fitJsonMeta(value, maxChars = MAX_PUSH_META_CHARS) {
  let json = JSON.stringify(value);
  if (charLength(json) <= maxChars) return json;
  if (!Array.isArray(value)) return null;
  const trimmed = value.slice();
  while (trimmed.length && charLength(JSON.stringify(trimmed)) > maxChars) trimmed.pop();
  json = JSON.stringify(trimmed);
  return charLength(json) <= maxChars ? json : null;
}

// 推送端运维 meta 白名单：值统一转成字符串/JSON 字符串，缺字段不报错。
function normalizePushMeta(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const values = {};
  const rawPushTs = raw.last_push_ts;
  const lastPush =
    typeof rawPushTs === "string" || typeof rawPushTs === "number" ? text(rawPushTs, 40) : "";
  if (lastPush) values.last_push_ts = lastPush;
  if (raw.event_count !== undefined && raw.event_count !== null) {
    const eventCount = int(raw.event_count, -1);
    if (eventCount >= 0) values.event_count = String(eventCount);
  }
  const sourceCounts = normalizeCounts(raw.source_counts, SOURCE_ORDER);
  if (sourceCounts) {
    const json = fitJsonMeta(sourceCounts);
    if (json) values.source_counts = json;
  }
  const collectionErrors = normalizeCollectionErrors(raw.collection_errors);
  if (collectionErrors) {
    const json = fitJsonMeta(collectionErrors);
    if (json) values.collection_errors = json;
  }
  const qualityCounts = normalizeCounts(raw.quality_counts, [...QUALITY_VALUES, "unknown"]);
  if (qualityCounts) {
    const json = fitJsonMeta(qualityCounts);
    if (json) values.quality_counts = json;
  }
  // D2：本轮推送结果计数（成功/被拒/死信/积压）。非负整数才收，缺字段保持「暂无」。
  for (const key of [
    "new_event_count",
    "pushed_count",
    "rejected_count",
    "dead_letter_count",
    "backlog_count",
  ]) {
    if (raw[key] === undefined || raw[key] === null) continue;
    const count = int(raw[key], -1);
    if (count >= 0) values[key] = String(count);
  }
  return values;
}

function metaStatement(env, key, value, nowTs) {
  return env.DB.prepare(
    `INSERT INTO meta (key, value, updated_ts) VALUES ('${String(key)}', ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts`,
  ).bind(String(value), nowTs);
}

// 冲突时最多尝试 20 个带 hash 的候选；候选集一次批量查回，避免逐候选串行点查。
function slugSuffixCandidates(event) {
  const stem = event.slug.slice(0, 120);
  const candidates = [];
  for (let attempt = 1; attempt <= MAX_SLUG_ATTEMPTS; attempt += 1) {
    candidates.push(`${stem}-${shortHash(`${event.id}#${attempt}`)}`);
  }
  return candidates;
}

async function assignSlugs(env, events) {
  // 每请求一次（正常情况）批量查回原始 slug 的占用，而不是每个事件点查一次。
  const owners = await getSlugOwners(env.DB, events.map((event) => event.slug));
  const usedSlugs = new Map();
  const unresolved = [];
  for (const event of events) {
    const usedBy = usedSlugs.get(event.slug);
    const owner = owners.get(event.slug);
    if ((!usedBy || usedBy === event.id) && (!owner || owner === event.id)) {
      usedSlugs.set(event.slug, event.id);
    } else {
      unresolved.push(event);
    }
  }
  if (!unresolved.length) return;

  // 只有真正冲突的事件才生成候选，并再一次批量查回候选占用；仍是常数条查询。
  const candidateOwners = await getSlugOwners(
    env.DB,
    unresolved.flatMap((event) => slugSuffixCandidates(event)),
  );
  for (const event of unresolved) {
    let chosen = "";
    for (const candidate of slugSuffixCandidates(event)) {
      const usedBy = usedSlugs.get(candidate);
      const owner = candidateOwners.get(candidate);
      if ((!usedBy || usedBy === event.id) && (!owner || owner === event.id)) {
        chosen = candidate;
        break;
      }
    }
    // 极端情况下 20 个候选全冲突：返回固定后缀，交给数据库唯一约束兜底。
    event.slug = chosen || `${event.slug.slice(0, 100)}-${shortHash(`${event.id}#fallback`)}`;
    usedSlugs.set(event.slug, event.id);
  }
}

function eventStatement(env, event, nowTs) {
  return env.DB.prepare(
    `INSERT INTO events (
       id, slug, source, kind, level, level_label, quality, title, summary, impact, action,
       signal_count, changed_ts, detected_ts, page_url, archive_url,
       evidence_json, payload_json, search_text, updated_ts
     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET
       source = excluded.source,
       kind = excluded.kind,
       level = excluded.level,
       level_label = excluded.level_label,
       quality = excluded.quality,
       title = excluded.title,
       summary = excluded.summary,
       impact = excluded.impact,
       action = excluded.action,
       signal_count = excluded.signal_count,
       changed_ts = excluded.changed_ts,
       -- 重推历史事件时保留首次发现时间；否则 sent_ids 丢失后的全量重放
       -- 会把所有旧事件的「发现时间」改成当天。
       detected_ts = MIN(events.detected_ts, excluded.detected_ts),
       page_url = excluded.page_url,
       archive_url = excluded.archive_url,
       evidence_json = excluded.evidence_json,
       payload_json = excluded.payload_json,
       search_text = excluded.search_text,
       updated_ts = excluded.updated_ts
     WHERE excluded.changed_ts >= events.changed_ts`,
  ).bind(
    event.id,
    event.slug,
    event.source,
    event.kind,
    event.level,
    event.levelLabel,
    event.quality,
    event.title,
    event.summary,
    event.impact,
    event.action,
    event.signalCount,
    event.changedTs,
    event.detectedTs,
    event.pageUrl,
    event.archiveUrl,
    event.evidenceJson,
    event.payloadJson,
    event.searchText,
    nowTs,
  );
}

// 按事件分组建语句：主行 + DELETE + 逐条 INSERT 必须整组落在同一个 batch，
// 避免 50 条边界把事件组切开，第二批失败时留下「主行 + 半个明细组」（P2-17）。
async function runStatements(env, eventGroups, statusStatements, metaStatements) {
  let batch = [];
  const flush = async () => {
    if (!batch.length) return;
    const current = batch;
    batch = [];
    await env.DB.batch(current);
  };
  for (const group of eventGroups) {
    if (group.length > MAX_BATCH_STATEMENTS) {
      throw new Error(`event statement group too large: ${group.length}`);
    }
    if (batch.length + group.length > MAX_BATCH_STATEMENTS) await flush();
    batch.push(...group);
  }
  await flush();
  // statuses / meta 不参与事件组拼批，避免事件事务和状态更新互相牵连。
  if (statusStatements.length) await env.DB.batch(statusStatements);
  if (metaStatements.length) await env.DB.batch(metaStatements);
}

// 明细语句紧跟在主事件语句之后：先 DELETE 再逐条 INSERT，重复推送不叠加。
// DELETE / INSERT 都带与主行一致的版本守卫：只有主行当前 changed_ts 与本事件相同
// 时才动明细。旧 changed_ts 重推时主行 UPDATE 被 WHERE 挡掉，这里同样跳过，
// 不会用旧证据覆盖新明细（P2-16）。
function eventChangesStatements(env, event, nowTs) {
  if (!event.changesDetail) return [];
  const statements = [
    env.DB.prepare(
      `DELETE FROM event_changes
       WHERE event_id = ?
         AND EXISTS (SELECT 1 FROM events WHERE id = ? AND changed_ts = ?)`,
    ).bind(event.id, event.id, event.changedTs),
  ];
  for (const change of event.changesDetail) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO event_changes
           (event_id, idx, page, level, is_asset, is_error, noise, has_diff, diff_bytes,
            bullets_json, diff, note, updated_ts)
         SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
         WHERE EXISTS (SELECT 1 FROM events WHERE id = ? AND changed_ts = ?)`,
      ).bind(
        event.id,
        change.idx,
        change.page,
        change.level,
        change.isAsset ? 1 : 0,
        change.isError ? 1 : 0,
        change.noise ? 1 : 0,
        change.hasDiff ? 1 : 0,
        change.diffBytes,
        change.bulletsJson,
        change.diff,
        change.note,
        nowTs,
        event.id,
        event.changedTs,
      ),
    );
  }
  return statements;
}

async function readBodyLimited(request, maxBytes) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let received = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      received += value.byteLength;
      if (received > maxBytes) {
        await reader.cancel().catch(() => {});
        return null;
      }
      chunks.push(value);
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // 忽略 release 失败
    }
  }
  const merged = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(merged);
}

export async function handleIngest(request, env) {
  if (!authOk(request, env)) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }
  const contentType = request.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    return json({ ok: false, error: "content-type must be application/json" }, 415);
  }
  const declaredLength = Number(request.headers.get("content-length") || 0);
  if (declaredLength > MAX_BODY_BYTES) {
    return json({ ok: false, error: "payload too large" }, 413);
  }

  const rawBody = await readBodyLimited(request, MAX_BODY_BYTES);
  if (rawBody === null) {
    return json({ ok: false, error: "payload too large" }, 413);
  }

  let body;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return json({ ok: false, error: "invalid json" }, 400);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return json({ ok: false, error: "invalid body" }, 400);
  }

  if (body.events !== undefined && !Array.isArray(body.events)) {
    return json({ ok: false, error: "events must be an array" }, 400);
  }
  if (body.statuses !== undefined && !Array.isArray(body.statuses)) {
    return json({ ok: false, error: "statuses must be an array" }, 400);
  }
  const rawEvents = Array.isArray(body.events) ? body.events : [];
  if (rawEvents.length > MAX_EVENTS) {
    return json({ ok: false, error: "too many events" }, 400);
  }
  const nowTs = Math.floor(Date.now() / 1000);
  const events = [];
  const rejected = [];
  rawEvents.forEach((item, index) => {
    const outcome = normalizeEventDetailed(item, nowTs);
    if (outcome.event) {
      events.push(outcome.event);
    } else {
      rejected.push({
        index,
        id: item && typeof item === "object" && typeof item.id === "string"
          ? text(item.id, 220)
          : "",
        reason: outcome.reason,
      });
    }
  });

  const rawStatuses = Array.isArray(body.statuses) ? body.statuses : [];
  if (rawStatuses.length > MAX_STATUSES) {
    return json({ ok: false, error: "too many statuses" }, 400);
  }
  const statuses = rawStatuses.map(normalizeStatus);
  if (statuses.some((status) => status === null)) {
    return json({ ok: false, error: "invalid status" }, 400);
  }

  if (
    body.meta !== undefined &&
    (!body.meta || typeof body.meta !== "object" || Array.isArray(body.meta))
  ) {
    return json({ ok: false, error: "meta must be an object" }, 400);
  }
  const rawModelsSnapshot = body.meta && body.meta.models_snapshot;
  const modelsSnapshotJson = normalizeModelsSnapshot(rawModelsSnapshot);
  if (rawModelsSnapshot !== undefined && modelsSnapshotJson === null) {
    // 快照是辅助展示数据，坏了不能连累同批事件全部被拒；下一轮推送会再带。
    console.warn("ignored invalid models_snapshot");
  }

  await assignSlugs(env, events);

  const eventGroups = [];
  for (const event of events) {
    eventGroups.push([
      eventStatement(env, event, nowTs),
      ...eventChangesStatements(env, event, nowTs),
    ]);
  }
  const statusStatements = [];
  if (statuses.length) {
    for (const status of statuses) {
      statusStatements.push(
        env.DB.prepare(
          `INSERT INTO source_status
             (source, label, last_checked_ts, last_success_ts, last_event_ts, status, detail, updated_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
             label = excluded.label,
             last_checked_ts = excluded.last_checked_ts,
             last_success_ts = excluded.last_success_ts,
             last_event_ts = excluded.last_event_ts,
             status = excluded.status,
             detail = excluded.detail,
             updated_ts = excluded.updated_ts`,
        ).bind(
          status.source,
          status.label,
          status.lastCheckedTs,
          status.lastSuccessTs,
          status.lastEventTs,
          status.status,
          status.detail,
          status.updatedTs || nowTs,
        ),
      );
      // 同一次实际巡检只记一条历史；推送器每 5 分钟重复状态时不会重复堆积。
      if (status.lastCheckedTs) {
        statusStatements.push(
          env.DB.prepare(
            `INSERT OR IGNORE INTO status_history
               (source, status, checked_ts, detail, created_ts)
             VALUES (?, ?, ?, ?, ?)`,
          ).bind(
            status.source,
            status.status,
            status.lastCheckedTs,
            status.detail,
            nowTs,
          ),
        );
      }
    }
  }
  const metaStatements = [];
  metaStatements.push(
    metaStatement(env, "last_ingest", JSON.stringify({ events: events.length, statuses: statuses.length }), nowTs),
  );
  metaStatements.push(metaStatement(env, "last_ingest_at", String(nowTs), nowTs));
  if (modelsSnapshotJson) {
    metaStatements.push(metaStatement(env, "models_snapshot", modelsSnapshotJson, nowTs));
  }
  const pushMeta = normalizePushMeta(body.meta);
  for (const [key, value] of Object.entries(pushMeta)) {
    metaStatements.push(metaStatement(env, key, value, nowTs));
  }

  const statementCount =
    eventGroups.reduce((sum, group) => sum + group.length, 0) +
    statusStatements.length +
    metaStatements.length;
  if (statementCount > MAX_TOTAL_STATEMENTS) {
    return json(
      {
        ok: false,
        error: "too many statements",
        statement_count: statementCount,
        max_statements: MAX_TOTAL_STATEMENTS,
      },
      400,
    );
  }

  try {
    await runStatements(env, eventGroups, statusStatements, metaStatements);
  } catch (error) {
    console.error("ingest failed", error);
    return json({ ok: false, error: "database error" }, 500);
  }
  return json({
    ok: true,
    event_count: events.length,
    rejected_count: rejected.length,
    rejected,
    status_count: statuses.length,
    events: events.length,
    statuses: statuses.length,
    models_snapshot: Boolean(modelsSnapshotJson),
    sources: SOURCE_ORDER.filter((source) => events.some((event) => event.source === source)),
  });
}

export function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
