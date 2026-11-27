import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

import { handleIngest } from "../src/ingest.js";
import { listEvents } from "../src/db.js";

const baseEvent = {
  id: "docwatch:491",
  slug: "docwatch-491",
  source: "docs",
  kind: "docwatch",
  level: 3,
  level_label: "重要",
  title: "价格调整",
  summary: "输出价从 13.5 调到 15",
  impact: "成本上升",
  action: "检查预算",
  signal_count: 2,
  changed_ts: 1763047866,
  detected_ts: 1763047866,
  page_url: "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
  archive_url: "",
  evidence: ["价格行有变化"],
  payload: { changes: [] },
};

function fakeDb(existingSlugs = {}, { failBatchAt = 0 } = {}) {
  const statements = [];
  const batches = [];
  return {
    statements,
    batches,
    DB: {
      prepare(sql) {
        return {
          bind(...args) {
            const statement = { sql, args };
            statements.push(statement);
            const matchingSlugRows = () =>
              args
                .filter((slug) => existingSlugs[slug])
                .map((slug) => ({ id: existingSlugs[slug], slug }));
            return {
              ...statement,
              first: async () => {
                if (sql.includes("FROM events WHERE slug IN")) {
                  return matchingSlugRows()[0] || null;
                }
                if (sql.includes("FROM events WHERE slug")) {
                  const id = existingSlugs[args[0]];
                  return id ? { id } : null;
                }
                return null;
              },
              all: async () => {
                if (sql.includes("FROM events WHERE slug IN")) {
                  return { results: matchingSlugRows() };
                }
                return { results: [] };
              },
            };
          },
        };
      },
      async batch(items) {
        assert.ok(Array.isArray(items));
        batches.push(items);
        if (failBatchAt && batches.length === failBatchAt) {
          throw new Error("batch failed");
        }
      },
    },
  };
}

function request(body) {
  return new Request("https://deepseek-watch.pages.dev/internal/ingest", {
    method: "POST",
    headers: {
      authorization: "Bearer test-token",
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

// 预期失败路径会触发 console.error；静音避免测试输出被堆栈淹没。
async function quiet(callback) {
  const original = console.error;
  console.error = () => {};
  try {
    return await callback();
  } finally {
    console.error = original;
  }
}

// 真实 SQLite（node:sqlite）+ 仓库 schema，验证 SQL 语义（版本守卫、batch 事务）。
// D1 的 batch 是单事务，这里用 BEGIN/COMMIT 模拟；failBatchAt 用于模拟第 N 批失败。
function sqliteDb({ failBatchAt = 0 } = {}) {
  const database = new DatabaseSync(":memory:");
  database.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  const batches = [];
  return {
    database,
    batches,
    DB: {
      prepare(sql) {
        return {
          bind(...args) {
            return {
              ...{ sql, args },
              first: async () => {
                const row = database.prepare(sql).get(...args);
                return row === undefined ? null : row;
              },
              all: async () => ({ results: database.prepare(sql).all(...args) }),
              run: async () => database.prepare(sql).run(...args),
            };
          },
        };
      },
      async batch(items) {
        batches.push(items);
        if (failBatchAt && batches.length === failBatchAt) {
          throw new Error("batch failed");
        }
        database.exec("BEGIN");
        try {
          for (const item of items) database.prepare(item.sql).run(...item.args);
          database.exec("COMMIT");
        } catch (error) {
          database.exec("ROLLBACK");
          throw error;
        }
      },
    },
  };
}

function eventRows(db) {
  return db.statements.filter(
    (item) =>
      item.sql.includes("INSERT INTO events") &&
      typeof item.args[0] === "string" &&
      item.args[0].startsWith("docwatch:"),
  );
}

test("keeps valid events when one event is invalid", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [baseEvent, { ...baseEvent, id: "bad", slug: "Bad Slug" }],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.ok, true);
  assert.equal(data.event_count, 1);
  assert.equal(data.rejected_count, 1);
  assert.deepEqual(data.rejected, [
    { index: 1, id: "bad", reason: "invalid slug" },
  ]);
  assert.ok(db.statements.some((item) => item.args[0] === "docwatch:491"));
});

test("still rejects a malformed request body", async () => {
  const response = await handleIngest(
    request({ events: "not-an-array" }),
    { INGEST_TOKEN: "test-token", DB: fakeDb().DB },
  );
  assert.equal(response.status, 400);
});

test("counts text length in Unicode code points and truncates display fields", async () => {
  const db = fakeDb();
  const emojiTitle = "😀".repeat(150); // 150 码点 / 300 个 UTF-16 码元
  const response = await handleIngest(
    request({
      events: [{ ...baseEvent, title: emojiTitle, summary: "x".repeat(4001) }],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  assert.equal(data.rejected_count, 0);
  const row = eventRows(db)[0];
  assert.equal(row.args[7], emojiTitle);
  assert.equal(row.args[8], "x".repeat(4000));
});

test("accepts payload counted in code points instead of UTF-16 units", async () => {
  const db = fakeDb();
  // 30000 个 emoji 的 JSON 在 UTF-16 下约 60,011 码元，但只有约 30,011 个码点。
  const response = await handleIngest(
    request({
      events: [{ ...baseEvent, payload: { blob: "😀".repeat(30000) } }],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  assert.equal(data.rejected_count, 0);
});

test("renames duplicate slugs inside one batch", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [
        { ...baseEvent, id: "docwatch:dup-a", slug: "docwatch-dup" },
        { ...baseEvent, id: "docwatch:dup-b", slug: "docwatch-dup" },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 2);
  assert.equal(data.rejected_count, 0);
  const slugs = eventRows(db).map((item) => item.args[1]);
  assert.equal(slugs[0], "docwatch-dup");
  assert.notEqual(slugs[1], "docwatch-dup");
  assert.match(slugs[1], /^docwatch-dup-[a-z0-9]{1,8}$/);
});

test("stores the models snapshot in D1 meta", async () => {
  const db = fakeDb();
  const snapshot = {
    checked_at: "2026-11-16T10:50:00Z",
    catalog_checked_at: "2026-11-16T10:50:00Z",
    catalog_available: true,
    models: [
      {
        id: "deepseek-v4-pro",
        version: "DeepSeek-V4-Pro-0813",
        fingerprint: "fp_v4pro_20260812",
        fingerprint_checked_at: "2026-11-16T10:50:00Z",
        first_seen: "2026-11-16T10:00:00Z",
        in_catalog: true,
      },
    ],
    last_change: { at: "2026-11-16T10:50:00Z", added: ["deepseek-v4-pro"], removed: [] },
    catalog_last_error: "目录检查返回 401",
    catalog_fail_streak: 3,
    catalog_incomplete_streak: 1,
  };
  const response = await handleIngest(
    request({ events: [], statuses: [], meta: { models_snapshot: snapshot } }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.models_snapshot, true);
  const row = db.statements.find((item) => item.sql.includes("'models_snapshot'"));
  assert.ok(row);
  const stored = JSON.parse(row.args[0]);
  assert.equal(stored.models[0].id, "deepseek-v4-pro");
  assert.equal(stored.last_change.added[0], "deepseek-v4-pro");
  // P2-19 / D2：目录失败信息必须原样保留，不能在 ingest 层被丢掉。
  assert.equal(stored.catalog_last_error, "目录检查返回 401");
  assert.equal(stored.catalog_fail_streak, 3);
  assert.equal(stored.catalog_incomplete_streak, 1);
  assert.equal(stored.catalog_checked_at, "2026-11-16T10:50:00Z");
});

test("ignores an invalid models snapshot without rejecting events", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [baseEvent],
      statuses: [],
      meta: { models_snapshot: "not-an-object" },
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  assert.equal(data.models_snapshot, false);
  assert.equal(db.statements.some((item) => item.sql.includes("'models_snapshot'")), false);
});

function metaRow(db, key) {
  return db.statements.find(
    (item) => item.sql.includes("INSERT INTO meta") && item.sql.includes(`'${key}'`),
  );
}

test("persists push-side capture meta into D1 meta", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [baseEvent],
      statuses: [],
      meta: {
        last_push_ts: "2026-11-18T10:50:00+08:00",
        event_count: 128,
        new_event_count: 12,
        pushed_count: 10,
        rejected_count: 2,
        dead_letter_count: 1,
        backlog_count: 3,
        source_counts: { docs: 100, chat: 20, platform: 5, harness: 2, fingerprint: 1, bogus: 9 },
        collection_errors: [{ source: "watchsite", detail: "事件目录不存在：D:/events" }],
        quality_counts: { ok: 90, partial: 12, unknown: 26, bogus: 3 },
      },
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);

  assert.equal(metaRow(db, "last_push_ts").args[0], "2026-11-18T10:50:00+08:00");
  assert.equal(metaRow(db, "event_count").args[0], "128");
  assert.equal(metaRow(db, "new_event_count").args[0], "12");
  assert.equal(metaRow(db, "pushed_count").args[0], "10");
  assert.equal(metaRow(db, "rejected_count").args[0], "2");
  assert.equal(metaRow(db, "dead_letter_count").args[0], "1");
  assert.equal(metaRow(db, "backlog_count").args[0], "3");
  const sourceCounts = JSON.parse(metaRow(db, "source_counts").args[0]);
  assert.deepEqual(sourceCounts, { docs: 100, chat: 20, platform: 5, harness: 2, fingerprint: 1 });
  const errors = JSON.parse(metaRow(db, "collection_errors").args[0]);
  assert.deepEqual(errors, [{ source: "watchsite", detail: "事件目录不存在：D:/events" }]);
  const qualityCounts = JSON.parse(metaRow(db, "quality_counts").args[0]);
  assert.deepEqual(qualityCounts, { ok: 90, partial: 12, unknown: 26 });
});

test("limits each push-side meta value and trims long collection error lists", async () => {
  const db = fakeDb();
  const errors = Array.from({ length: 60 }, (_, index) => ({
    source: "watchsite",
    detail: `第 ${index} 条采集异常：${"很长的错误说明".repeat(120)}`,
  }));
  const response = await handleIngest(
    request({ events: [], statuses: [], meta: { collection_errors: errors } }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const stored = metaRow(db, "collection_errors").args[0];
  assert.ok(Array.from(stored).length <= 8000, `stored length=${Array.from(stored).length}`);
  const parsed = JSON.parse(stored);
  assert.ok(parsed.length > 0 && parsed.length < 60);
  assert.ok(parsed.length <= 40);
  assert.match(parsed[0].detail, /第 0 条采集异常/);
});

test("malformed push-side meta is ignored without rejecting the batch", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [baseEvent],
      statuses: [],
      meta: {
        last_push_ts: { nested: true },
        event_count: "not-a-number",
        new_event_count: -2,
        pushed_count: "bad",
        rejected_count: { count: 1 },
        dead_letter_count: undefined,
        backlog_count: "x",
        source_counts: ["docs"],
        collection_errors: "broken",
        quality_counts: 42,
      },
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  assert.equal(data.rejected_count, 0);
  for (const key of [
    "last_push_ts",
    "event_count",
    "new_event_count",
    "pushed_count",
    "rejected_count",
    "dead_letter_count",
    "backlog_count",
    "source_counts",
    "collection_errors",
    "quality_counts",
  ]) {
    assert.equal(db.statements.some((item) => item.sql.includes(`'${key}'`)), false, key);
  }
});

test("push-side meta lands in the real meta table as text", async () => {
  const db = sqliteDb();
  const response = await handleIngest(
    request({
      events: [],
      statuses: [],
      meta: {
        last_push_ts: "2026-11-18T10:50:00+08:00",
        event_count: 12,
        new_event_count: 5,
        pushed_count: 4,
        rejected_count: 1,
        dead_letter_count: 0,
        backlog_count: 1,
        source_counts: { docs: 10, chat: 2 },
        quality_counts: { ok: 10, unknown: 2 },
      },
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const rows = db.database
    .prepare("SELECT key, value FROM meta WHERE key IN ('last_push_ts', 'event_count', 'new_event_count', 'pushed_count', 'rejected_count', 'dead_letter_count', 'backlog_count', 'source_counts', 'quality_counts') ORDER BY key")
    .all();
  const values = Object.fromEntries(rows.map((row) => [row.key, row.value]));
  assert.equal(values.last_push_ts, "2026-11-18T10:50:00+08:00");
  assert.equal(values.event_count, "12");
  assert.equal(values.new_event_count, "5");
  assert.equal(values.pushed_count, "4");
  assert.equal(values.rejected_count, "1");
  assert.equal(values.dead_letter_count, "0");
  assert.equal(values.backlog_count, "1");
  assert.deepEqual(JSON.parse(values.source_counts), { docs: 10, chat: 2 });
  assert.deepEqual(JSON.parse(values.quality_counts), { ok: 10, unknown: 2 });
});

test("keeps trying suffixes when the database also has a slug", async () => {
  const db = fakeDb({ "docwatch-491": "existing-event" });
  const response = await handleIngest(
    request({ events: [baseEvent], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  const row = eventRows(db)[0];
  assert.notEqual(row.args[1], "docwatch-491");
  assert.match(row.args[1], /^docwatch-491-[a-z0-9]{1,8}$/);
});

// 与 ingest.js 的 shortHash 保持一致，用于预测冲突候选。
function shortHash(value) {
  let hash = 5381;
  const raw = String(value || "");
  for (let index = 0; index < raw.length; index += 1) {
    hash = ((hash * 33) ^ raw.charCodeAt(index)) >>> 0;
  }
  return hash.toString(36).slice(0, 8);
}

test("retries the next suffix when two candidate slugs collide", async () => {
  const first = `docwatch-491-${shortHash("docwatch:491#1")}`;
  const second = `docwatch-491-${shortHash("docwatch:491#2")}`;
  const db = fakeDb({
    "docwatch-491": "existing-event",
    [first]: "taken-1",
    [second]: "taken-2",
  });
  const response = await handleIngest(
    request({ events: [baseEvent], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  const row = eventRows(db)[0];
  assert.equal(row.args[1], `docwatch-491-${shortHash("docwatch:491#3")}`);
  // 第一次查原始 slug，第二次批量查 20 个候选；不再逐候选点查。
  const slugQueries = db.statements.filter((item) => item.sql.includes("FROM events WHERE slug IN"));
  assert.equal(slugQueries.length, 2);
});

test("falls back to a fixed suffix when all 20 candidates are taken", async () => {
  const existing = { "docwatch-491": "existing-event" };
  for (let attempt = 1; attempt <= 20; attempt += 1) {
    existing[`docwatch-491-${shortHash(`docwatch:491#${attempt}`)}`] = `taken-${attempt}`;
  }
  const db = fakeDb(existing);
  const response = await handleIngest(
    request({ events: [baseEvent], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  // 20 个候选全冲突时返回固定后缀，由数据库唯一约束做最后兜底。
  assert.equal(eventRows(db)[0].args[1], `docwatch-491-${shortHash("docwatch:491#fallback")}`);
});

test("resolves all slugs with one bulk query instead of serial point lookups", async () => {
  const events = Array.from({ length: 60 }, (_, index) => ({
    ...baseEvent,
    id: `docwatch:bulk-${index}`,
    slug: `docwatch-bulk-${index}`,
  }));
  const db = fakeDb();
  const response = await handleIngest(
    request({ events, statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 60);
  const slugQueries = db.statements.filter((item) => item.sql.includes("FROM events WHERE slug IN"));
  assert.equal(slugQueries.length, 1);
  assert.equal(slugQueries[0].args.length, 60);
  // 旧的逐条点查（WHERE slug = ?）不应再出现。
  assert.equal(
    db.statements.filter((item) => /FROM events WHERE slug = \?/i.test(item.sql)).length,
    0,
  );
});

function changeInserts(db) {
  return db.statements.filter((item) => item.sql.includes("INSERT INTO event_changes"));
}

function changeDeletes(db) {
  return db.statements.filter((item) => item.sql.includes("DELETE FROM event_changes"));
}

test("writes changes_detail into event_changes after deleting the old rows", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [
        {
          ...baseEvent,
          changes_detail: [
            {
              page: "https://example.com/pricing",
              level: 3,
              is_asset: false,
              is_error: false,
              noise: false,
              bullets: ["输出价 13.5 → 15", ""],
              diff: "－13.5\n＋15",
              note: "价格页",
            },
            { page: "https://example.com/main.js", noise: true, diff: "" },
          ],
        },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 1);
  assert.equal(data.rejected_count, 0);

  const deletes = changeDeletes(db);
  assert.equal(deletes.length, 1);
  // 版本守卫参数：event_id、主行 id、本事件 changed_ts。
  assert.deepEqual(deletes[0].args, ["docwatch:491", "docwatch:491", baseEvent.changed_ts]);
  const inserts = changeInserts(db);
  assert.equal(inserts.length, 2);

  // 语句顺序：先 INSERT 主事件，再 DELETE 旧明细，最后逐条 INSERT 新明细。
  const order = db.statements.map((item) =>
    item.sql.includes("INSERT INTO event_changes")
      ? "insert-change"
      : item.sql.includes("DELETE FROM event_changes")
        ? "delete-change"
        : item.sql.includes("INSERT INTO events")
          ? "insert-event"
          : "other",
  );
  assert.ok(order.indexOf("insert-event") < order.indexOf("delete-change"));
  assert.ok(order.indexOf("delete-change") < order.indexOf("insert-change"));

  const first = inserts[0].args;
  assert.equal(first[0], "docwatch:491");
  assert.equal(first[1], 0);
  assert.equal(first[2], "https://example.com/pricing");
  assert.equal(first[3], 3);
  assert.equal(first[7], 1); // has_diff
  assert.equal(first[8], Buffer.byteLength("－13.5\n＋15", "utf8"));
  assert.deepEqual(JSON.parse(first[9]), ["输出价 13.5 → 15"]);
  assert.equal(first[10], "－13.5\n＋15");
  assert.equal(first[11], "价格页");
  assert.ok(Number.isFinite(first[12]));

  const second = inserts[1].args;
  assert.equal(second[1], 1);
  assert.equal(second[6], 1); // noise
  assert.equal(second[7], 0); // has_diff
  assert.equal(second[8], 0);
  assert.equal(second[10], "");
});

test("re-pushing the same event does not accumulate change rows", async () => {
  const db = fakeDb();
  const body = {
    events: [
      {
        ...baseEvent,
        changes_detail: [{ page: "https://example.com/a", diff: "－a\n＋b" }],
      },
    ],
    statuses: [],
    meta: {},
  };
  for (let round = 0; round < 2; round += 1) {
    const response = await handleIngest(request(body), { INGEST_TOKEN: "test-token", DB: db.DB });
    assert.equal(response.status, 200);
  }
  // 每次推送都是「先删后插」：两轮各 1 条 DELETE、各 1 条 INSERT，不会叠加成 2 条。
  assert.equal(changeDeletes(db).length, 2);
  assert.equal(changeInserts(db).length, 2);
});

test("does not treat string false / 0 / no as true in change flags", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [
        {
          ...baseEvent,
          changes_detail: [
            { page: "https://example.com/string", is_asset: "false", is_error: "0", noise: "no" },
            { page: "https://example.com/typed", is_asset: true, is_error: 1, noise: true },
            { page: "https://example.com/numeric", is_asset: 0, is_error: 1, noise: 0 },
          ],
        },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const flags = changeInserts(db).map((item) => [item.args[4], item.args[5], item.args[6]]);
  assert.deepEqual(flags, [
    [0, 0, 0],
    [1, 1, 1],
    [0, 1, 0],
  ]);
});

test("an old changed_ts cannot overwrite newer event_changes (SQLite)", async () => {
  const sqlite = sqliteDb();
  const newer = {
    ...baseEvent,
    title: "NEW TITLE",
    changed_ts: baseEvent.changed_ts + 600,
    detected_ts: baseEvent.changed_ts + 600,
    changes_detail: [{ page: "https://example.com/new", diff: "＋new" }],
  };
  let response = await handleIngest(
    request({ events: [newer], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: sqlite.DB },
  );
  assert.equal(response.status, 200);

  response = await handleIngest(
    request({
      events: [
        {
          ...baseEvent,
          title: "STALE TITLE",
          changes_detail: [{ page: "https://example.com/stale", diff: "＋stale" }],
        },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: sqlite.DB },
  );
  assert.equal(response.status, 200);

  const row = sqlite.database
    .prepare("SELECT title, changed_ts FROM events WHERE id = ?")
    .get(baseEvent.id);
  assert.equal(row.title, "NEW TITLE");
  assert.equal(Number(row.changed_ts), newer.changed_ts);
  const changes = sqlite.database
    .prepare("SELECT page, diff FROM event_changes WHERE event_id = ? ORDER BY idx")
    .all(baseEvent.id);
  assert.deepEqual(
    changes.map((item) => [item.page, item.diff]),
    [["https://example.com/new", "＋new"]],
  );
});

test("a same-version re-push still refreshes event_changes (SQLite)", async () => {
  const sqlite = sqliteDb();
  const body = (page, diff) => ({
    events: [{ ...baseEvent, changes_detail: [{ page, diff }] }],
    statuses: [],
    meta: {},
  });
  let response = await handleIngest(request(body("https://example.com/a", "＋a")), {
    INGEST_TOKEN: "test-token",
    DB: sqlite.DB,
  });
  assert.equal(response.status, 200);
  response = await handleIngest(request(body("https://example.com/b", "＋b")), {
    INGEST_TOKEN: "test-token",
    DB: sqlite.DB,
  });
  assert.equal(response.status, 200);
  const changes = sqlite.database
    .prepare("SELECT page, diff FROM event_changes WHERE event_id = ? ORDER BY idx")
    .all(baseEvent.id);
  assert.deepEqual(
    changes.map((item) => [item.page, item.diff]),
    [["https://example.com/b", "＋b"]],
  );
});

test("event groups never span a 50-statement batch boundary", async () => {
  const events = Array.from({ length: 3 }, (_, index) => ({
    ...baseEvent,
    id: `docwatch:group-${index}`,
    slug: `docwatch-group-${index}`,
    changes_detail: Array.from({ length: 20 }, (_, change) => ({
      page: `https://example.com/${change}`,
      diff: `＋${change}`,
    })),
  }));
  const db = fakeDb({}, { failBatchAt: 2 });
  const response = await quiet(() =>
    handleIngest(request({ events, statuses: [], meta: {} }), {
      INGEST_TOKEN: "test-token",
      DB: db.DB,
    }),
  );
  assert.equal(response.status, 500);
  assert.equal(db.batches.length, 2, "第二批失败后不应再提交状态 / meta 批次");
  const firstBatch = db.batches[0];
  // 3 个事件各 22 条语句（主行 1 + DELETE 1 + INSERT 20），
  // 旧实现会把第一批切成 50 条，留下半个事件组；新实现第一批应是 44 条完整两组。
  assert.equal(firstBatch.length, 44);
  for (const event of events.slice(0, 2)) {
    const group = firstBatch.filter((item) => item.args[0] === event.id);
    assert.equal(group.length, 22, `${event.id} 在第一批里必须是完整的一组`);
    assert.equal(group.filter((item) => item.sql.includes("INSERT INTO events")).length, 1);
    assert.equal(group.filter((item) => item.sql.includes("DELETE FROM event_changes")).length, 1);
    assert.equal(group.filter((item) => item.sql.includes("INSERT INTO event_changes")).length, 20);
  }
  // 第三个事件整体不在第一批，不能出现「主行先落库、明细没落」的半组。
  assert.equal(
    firstBatch.some((item) => item.args[0] === events[2].id),
    false,
  );
});

test("a failed second batch leaves complete event groups only (SQLite)", async () => {
  const sqlite = sqliteDb({ failBatchAt: 2 });
  const events = Array.from({ length: 3 }, (_, index) => ({
    ...baseEvent,
    id: `docwatch:sqlite-group-${index}`,
    slug: `docwatch-sqlite-group-${index}`,
    changes_detail: Array.from({ length: 20 }, (_, change) => ({
      page: `https://example.com/${change}`,
      diff: `＋${change}`,
    })),
  }));
  const response = await quiet(() =>
    handleIngest(request({ events, statuses: [], meta: {} }), {
      INGEST_TOKEN: "test-token",
      DB: sqlite.DB,
    }),
  );
  assert.equal(response.status, 500);
  const rows = sqlite.database.prepare("SELECT id FROM events ORDER BY id").all();
  assert.deepEqual(
    rows.map((item) => item.id),
    ["docwatch:sqlite-group-0", "docwatch:sqlite-group-1"],
  );
  for (const id of ["docwatch:sqlite-group-0", "docwatch:sqlite-group-1"]) {
    const count = sqlite.database
      .prepare("SELECT COUNT(*) AS total FROM event_changes WHERE event_id = ?")
      .get(id);
    assert.equal(Number(count.total), 20, `${id} 的明细必须完整`);
  }
  const third = sqlite.database
    .prepare("SELECT COUNT(*) AS total FROM event_changes WHERE event_id = ?")
    .get("docwatch:sqlite-group-2");
  assert.equal(Number(third.total), 0);
});

test("truncates an oversized diff to 20,000 code points with a marker", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [
        {
          ...baseEvent,
          changes_detail: [
            { page: "https://example.com/huge", diff: `－旧\n＋${"新".repeat(30_000)}` },
          ],
        },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const diff = changeInserts(db)[0].args[10];
  assert.equal(Array.from(diff).length, 20_000);
  assert.match(diff, /…（已截断，原文 [0-9.]+ KB）$/);
  assert.ok(diff.startsWith("－旧\n＋新"));
});

test("rejects a request that would exceed the total statement budget", async () => {
  let batchCalls = 0;
  const statements = [];
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          const statement = { sql, args };
          statements.push(statement);
          return {
            ...statement,
            first: async () => null,
            all: async () => ({ results: [] }),
          };
        },
      };
    },
    async batch() {
      batchCalls += 1;
    },
  };
  const events = Array.from({ length: 100 }, (_, index) => ({
    ...baseEvent,
    id: `docwatch:heavy-${index}`,
    slug: `docwatch-heavy-${index}`,
    changes_detail: Array.from({ length: 40 }, (_, change) => ({
      page: `https://example.com/${change}`,
      bullets: [`变化 ${change}`],
    })),
  }));
  const response = await handleIngest(
    request({ events, statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db },
  );
  assert.equal(response.status, 400);
  const data = await response.json();
  assert.equal(data.error, "too many statements");
  assert.equal(batchCalls, 0);
});

test("rejects future changed_ts and keeps slug stable on conflict", async () => {
  const future = Math.floor(Date.now() / 1000) + 3600;
  const db = fakeDb();
  let response = await handleIngest(
    request({ events: [{ ...baseEvent, changed_ts: future }], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const rejected = await response.json();
  assert.equal(rejected.rejected_count, 1);
  assert.equal(rejected.rejected[0].reason, "invalid changed_ts");

  const db2 = fakeDb();
  response = await handleIngest(
    request({ events: [baseEvent], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db2.DB },
  );
  assert.equal(response.status, 200);
  const row = eventRows(db2)[0];
  assert.ok(!row.sql.includes("slug = excluded.slug"));
  assert.ok(row.sql.includes("WHERE excluded.changed_ts >= events.changed_ts"));
});

test("ignores a non-array changes_detail and drops invalid groups", async () => {
  const db = fakeDb();
  let response = await handleIngest(
    request({ events: [{ ...baseEvent, changes_detail: "not-an-array" }], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  // 字段缺失 / 非法：不动已有明细（没有 DELETE）。
  assert.equal(changeDeletes(db).length, 0);

  const db2 = fakeDb();
  response = await handleIngest(
    request({
      events: [
        {
          ...baseEvent,
          changes_detail: [null, 42, "x", { page: "https://example.com/ok", diff: "＋ok" }],
        },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db2.DB },
  );
  assert.equal(response.status, 200);
  assert.equal(changeDeletes(db2).length, 1);
  const inserts = changeInserts(db2);
  assert.equal(inserts.length, 1);
  assert.equal(inserts[0].args[2], "https://example.com/ok");
  assert.equal(inserts[0].args[1], 0);
});

function rawRequest(body, { token = "test-token", contentType = "application/json" } = {}) {
  const headers = { "content-type": contentType };
  if (token !== null) headers.authorization = `Bearer ${token}`;
  return new Request("https://deepseek-watch.pages.dev/internal/ingest", {
    method: "POST",
    headers,
    body,
  });
}

test("rejects missing or wrong bearer token", async () => {
  for (const token of [null, "wrong-token"]) {
    const response = await handleIngest(
      rawRequest(JSON.stringify({ events: [] }), { token }),
      { INGEST_TOKEN: "test-token", DB: fakeDb().DB },
    );
    assert.equal(response.status, 401);
    const data = await response.json();
    assert.equal(data.error, "unauthorized");
  }
});

test("rejects an unconfigured server token instead of allowing writes", async () => {
  const response = await handleIngest(
    rawRequest(JSON.stringify({ events: [] }), { token: "" }),
    { INGEST_TOKEN: "", DB: fakeDb().DB },
  );
  assert.equal(response.status, 401);
});

test("rejects non-JSON content types", async () => {
  const response = await handleIngest(
    rawRequest(JSON.stringify({ events: [] }), { contentType: "text/plain" }),
    { INGEST_TOKEN: "test-token", DB: fakeDb().DB },
  );
  assert.equal(response.status, 415);
});

test("rejects a body over 4 MB before parsing", async () => {
  const response = await handleIngest(
    rawRequest("x".repeat(4_000_001)),
    { INGEST_TOKEN: "test-token", DB: fakeDb().DB },
  );
  assert.equal(response.status, 413);
  const data = await response.json();
  assert.equal(data.error, "payload too large");
});

test("re-ingest preserves the first detected_ts instead of overwriting it", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({ events: [baseEvent], statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const sql = eventRows(db)[0].sql;
  // 全量重放时不能把历史事件的「发现时间」改成当天。
  assert.match(
    sql,
    /detected_ts = MIN\(events\.detected_ts, excluded\.detected_ts\)/,
  );
  assert.doesNotMatch(sql, /detected_ts = excluded\.detected_ts/);
});

test("rejects more than 100 events per request", async () => {
  const events = Array.from({ length: 101 }, (_, index) => ({
    ...baseEvent,
    id: `docwatch:${index}`,
    slug: `docwatch-${index}`,
  }));
  const response = await handleIngest(
    request({ events, statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: fakeDb().DB },
  );
  assert.equal(response.status, 400);
  const data = await response.json();
  assert.equal(data.error, "too many events");
});

test("stores whitelisted quality in the events column and defaults old events to unknown", async () => {
  const db = fakeDb();
  const response = await handleIngest(
    request({
      events: [
        { ...baseEvent, id: "docwatch:q-partial", slug: "docwatch-q-partial", payload: { quality: "partial" } },
        { ...baseEvent, id: "docwatch:q-bad", slug: "docwatch-q-bad", payload: { quality: "not-a-status" } },
        { ...baseEvent, id: "docwatch:q-missing", slug: "docwatch-q-missing", payload: {} },
      ],
      statuses: [],
      meta: {},
    }),
    { INGEST_TOKEN: "test-token", DB: db.DB },
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.event_count, 3);
  const rows = eventRows(db);
  assert.equal(rows.length, 3);
  // quality 列紧跟 level_label（下标 6），INSERT / ON CONFLICT 都要带上。
  assert.equal(rows[0].args[6], "partial");
  assert.equal(rows[1].args[6], "unknown");
  assert.equal(rows[2].args[6], "unknown");
  assert.match(rows[0].sql, /level_label, quality, title/);
  assert.match(rows[0].sql, /quality = excluded\.quality/);
});

test("legacy display level is corrected in filters without changing stored level", async () => {
  const sqlite = sqliteDb();
  const events = [
    {
      ...baseEvent,
      id: "web:harness:old",
      slug: "web-harness-old",
      source: "harness",
      kind: "dsh_release",
      title: "DSH v0.1.0-rc.7 发布",
      action: "历史版本补录，仅用于回看。",
    },
    {
      ...baseEvent,
      id: "web:platform:version",
      slug: "web-platform-version",
      source: "platform",
      kind: "page_change",
      title: "Platform 控制台前端版本更新",
      action: "打开页面确认变化内容。",
    },
    {
      ...baseEvent,
      id: "web:platform:uncaptured",
      slug: "web-platform-uncaptured",
      source: "platform",
      kind: "page_change",
      title: "本次为内部调整，未发现界面变化",
      action: "无需操作",
    },
    {
      ...baseEvent,
      id: "docwatch:real-important",
      slug: "docwatch-real-important",
      title: "价格调整",
    },
  ];
  const response = await handleIngest(
    request({ events, statuses: [], meta: {} }),
    { INGEST_TOKEN: "test-token", DB: sqlite.DB },
  );
  assert.equal(response.status, 200);

  const level3 = await listEvents(sqlite.DB, { level: 3 });
  assert.deepEqual(level3.items.map((item) => item.id), ["docwatch:real-important"]);
  const level1 = await listEvents(sqlite.DB, { level: 1 });
  assert.deepEqual(
    new Set(level1.items.map((item) => item.id)),
    new Set(["web:harness:old", "web:platform:version", "web:platform:uncaptured"]),
  );
  const stored = sqlite.database
    .prepare("SELECT level FROM events WHERE id = ?")
    .get("web:harness:old");
  assert.equal(stored.level, 3, "展示级别不能改写数据库里的历史 level");
});
