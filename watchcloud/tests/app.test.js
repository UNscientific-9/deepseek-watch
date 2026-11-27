import test from "node:test";
import assert from "node:assert/strict";

import { onRequest } from "../src/app.js";

const SITE = "https://deepseek-watch.pages.dev";
let requestSeq = 0;

// 最小 D1 mock：只实现 /healthz 与页面渲染会用到的 prepare/bind/first/all。
// statusReadError=true 时让 source_status / GROUP BY source 查询抛错，模拟 D1 局部读失败。
function fakeDb({
  eventCount = 0,
  lastIngestAt = null,
  statusReadError = false,
  statusRows = [],
  eventRow = null,
  eventRows = [],
  changesRows = [],
  qualityRows = [],
  metaValues = {},
} = {}) {
  const queries = [];
  function firstResult(sql, args = []) {
    if (/FROM meta/i.test(sql)) {
      const key = String(args[0] || "");
      if (Object.prototype.hasOwnProperty.call(metaValues, key)) {
        return { value: String(metaValues[key]), updated_ts: 1 };
      }
      if (lastIngestAt === null) return null;
      return { value: String(lastIngestAt), updated_ts: Number(lastIngestAt) };
    }
    if (/COUNT\(\*\) AS total FROM events/i.test(sql)) return { total: eventCount };
    if (/FROM events WHERE slug/i.test(sql)) return eventRow;
    return null;
  }

  function allResult(sql, args = []) {
    if (/FROM event_changes/i.test(sql)) return { results: changesRows };
    if (/FROM source_status/i.test(sql)) return { results: statusRows };
    if (/FROM meta/i.test(sql) && /key IN/i.test(sql)) {
      return {
        results: args
          .map((key) => String(key))
          .filter((key) => Object.prototype.hasOwnProperty.call(metaValues, key))
          .map((key) => ({ key, value: String(metaValues[key]), updated_ts: 1 })),
      };
    }
    if (/GROUP BY quality/i.test(sql)) return { results: qualityRows };
    if (/FROM events/i.test(sql)) return { results: eventRows };
    return { results: [] };
  }

  return {
    queries,
    prepare(sql) {
      queries.push(sql);
      if (statusReadError && /(FROM source_status|GROUP BY source)/i.test(sql)) {
        throw new Error("D1 read failed");
      }
      return {
        bind(...args) {
          return {
            first: async () => firstResult(sql, args),
            all: async () => allResult(sql, args),
            run: async () => ({ success: true }),
          };
        },
        first: async () => firstResult(sql),
        all: async () => allResult(sql),
        run: async () => ({ success: true }),
      };
    },
  };
}

function failingDb() {
  return {
    prepare() {
      throw new Error("D1 unavailable");
    },
  };
}

function makeRequest(path, method = "GET") {
  requestSeq += 1;
  return new Request(`${SITE}${path}`, {
    method,
    headers: { "cf-connecting-ip": `app-test-${process.pid}-${requestSeq}` },
  });
}

async function call(path, env, method = "GET") {
  return onRequest({
    request: makeRequest(path, method),
    env,
    waitUntil() {},
  });
}

// 错误路径测试会触发 app.js 的 console.error；这里临时静音，避免测试输出被预期堆栈淹没。
async function quiet(callback) {
  const original = console.error;
  console.error = () => {};
  try {
    return await callback();
  } finally {
    console.error = original;
  }
}

test("healthz returns 200 with lastIngestAt when ingest is recent and token is configured", async () => {
  const lastIngestTs = Math.floor(Date.now() / 1000) - 60;
  const response = await call("/healthz", {
    DB: fakeDb({ eventCount: 7, lastIngestAt: lastIngestTs }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.ok, true);
  assert.equal(data.service, "deepseek-watch");
  assert.equal(data.lastIngestAt, new Date(lastIngestTs * 1000).toISOString());
  assert.ok(data.ageSeconds >= 60 && data.ageSeconds <= 65, `ageSeconds=${data.ageSeconds}`);
  assert.equal(data.eventCount, 7);
  assert.equal(data.ingestTokenConfigured, undefined);
  assert.equal(data.error, undefined);
});

test("healthz returns 503 with lastIngestAt when last ingest is older than 15 minutes", async () => {
  const lastIngestTs = Math.floor(Date.now() / 1000) - 16 * 60;
  const response = await call("/healthz", {
    DB: fakeDb({ eventCount: 7, lastIngestAt: lastIngestTs }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.ok, false);
  assert.equal(data.error, "ingest_stale");
  assert.ok(Array.isArray(data.problems) && data.problems.includes("ingest_stale"));
  assert.equal(data.lastIngestAt, new Date(lastIngestTs * 1000).toISOString());
  assert.ok(data.ageSeconds >= 960, `ageSeconds=${data.ageSeconds}`);
});

test("healthz returns 503 when INGEST_TOKEN is not configured", async () => {
  const lastIngestTs = Math.floor(Date.now() / 1000) - 60;
  const response = await call("/healthz", {
    DB: fakeDb({ eventCount: 1, lastIngestAt: lastIngestTs }),
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.ok, false);
  assert.equal(data.error, "ingest_token_missing");
  assert.equal(data.ingestTokenConfigured, undefined);
  assert.equal(data.lastIngestAt, new Date(lastIngestTs * 1000).toISOString());
});

test("healthz returns 503 when last_ingest_at has never been written", async () => {
  const response = await call("/healthz", {
    DB: fakeDb({ eventCount: 0, lastIngestAt: null }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.ok, false);
  assert.equal(data.error, "ingest_missing");
  assert.equal(data.lastIngestAt, null);
});

test("healthz returns 503 when the database cannot be read", async () => {
  const response = await quiet(() =>
    call("/healthz", {
      DB: failingDb(),
      INGEST_TOKEN: "test-token",
    }),
  );
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.ok, false);
  assert.equal(data.error, "database_unavailable");
});

test("healthz returns 503 when every source is in error even though ingest is fresh", async () => {
  const now = Math.floor(Date.now() / 1000);
  const response = await call("/healthz", {
    DB: fakeDb({
      eventCount: 7,
      lastIngestAt: now - 60,
      statusRows: [
        { source: "docs", status: "error", last_checked_ts: now - 60, detail: "读取失败" },
        { source: "chat", status: "error", last_checked_ts: now - 60, detail: "抓取失败" },
      ],
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.error, "sources_all_error");
  assert.equal(data.sources.length, 2);
});

test("healthz returns 503 when source status has not been refreshed for 90 minutes", async () => {
  const now = Math.floor(Date.now() / 1000);
  const response = await call("/healthz", {
    DB: fakeDb({
      eventCount: 7,
      lastIngestAt: now - 60,
      statusRows: [
        { source: "docs", status: "ok", last_checked_ts: now - 100 * 60, detail: "" },
        { source: "chat", status: "ok", last_checked_ts: now - 100 * 60, detail: "" },
      ],
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.error, "sources_stale");
});

test("healthz stays 200 when only some sources are in error", async () => {
  const now = Math.floor(Date.now() / 1000);
  const response = await call("/healthz", {
    DB: fakeDb({
      eventCount: 7,
      lastIngestAt: now - 60,
      statusRows: [
        { source: "docs", status: "error", last_checked_ts: now - 60, detail: "部分失败" },
        { source: "chat", status: "ok", last_checked_ts: now - 60, detail: "" },
      ],
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.ok, true);
  assert.deepEqual(data.sources.map((item) => item.source), ["docs", "chat"]);
});

test("healthz exposes catalog freshness and flags a stale model catalog", async () => {
  const now = Math.floor(Date.now() / 1000);
  const checkedAt = new Date((now - 3 * 86400) * 1000).toISOString();
  const response = await call("/healthz", {
    DB: fakeDb({
      eventCount: 7,
      lastIngestAt: now - 60,
      metaValues: {
        models_snapshot: JSON.stringify({
          checked_at: checkedAt,
          catalog_available: true,
          models: [{ id: "deepseek-v4-pro" }],
          catalog_fail_streak: 4,
        }),
      },
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.catalogCheckedAt, checkedAt);
  assert.ok(data.catalogAgeSeconds >= 3 * 86400 - 5, `catalogAgeSeconds=${data.catalogAgeSeconds}`);
  assert.equal(data.catalogStale, true);
  assert.equal(data.catalogFailStreak, 4);
  assert.ok(data.problems.includes("catalog_stale"));
});

test("healthz flags repeated catalog failures even when the last snapshot is fresh", async () => {
  const now = Math.floor(Date.now() / 1000);
  const checkedAt = new Date((now - 10 * 60) * 1000).toISOString();
  const response = await call("/healthz", {
    DB: fakeDb({
      eventCount: 7,
      lastIngestAt: now - 60,
      metaValues: {
        models_snapshot: JSON.stringify({
          checked_at: checkedAt,
          models: [{ id: "deepseek-v4-pro" }],
          catalog_fail_streak: 3,
        }),
      },
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 503);
  const data = await response.json();
  assert.equal(data.catalogStale, false);
  assert.equal(data.catalogFailStreak, 3);
  assert.equal(data.error, "catalog_failing");
});

test("healthz keeps a fresh model catalog out of the problem list", async () => {
  const now = Math.floor(Date.now() / 1000);
  const checkedAt = new Date((now - 10 * 60) * 1000).toISOString();
  const response = await call("/healthz", {
    DB: fakeDb({
      eventCount: 7,
      lastIngestAt: now - 60,
      metaValues: {
        models_snapshot: JSON.stringify({ checked_at: checkedAt, models: [{ id: "deepseek-v4-pro" }] }),
      },
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.catalogStale, false);
  assert.ok(data.catalogAgeSeconds >= 590 && data.catalogAgeSeconds <= 610);
  assert.equal(data.error, undefined);
});

test("healthz reports null catalog fields when no snapshot has been pushed yet", async () => {
  const now = Math.floor(Date.now() / 1000);
  const response = await call("/healthz", {
    DB: fakeDb({ eventCount: 0, lastIngestAt: now - 60 }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.catalogCheckedAt, null);
  assert.equal(data.catalogAgeSeconds, null);
  assert.equal(data.catalogStale, false);
  assert.equal(data.catalogFailStreak, null);
});

test("out-of-range page redirects to the last available page", async () => {
  const response = await call("/?page=9999", {
    DB: fakeDb({ eventCount: 25 }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 302);
  const location = new URL(response.headers.get("location"));
  assert.equal(location.searchParams.get("page"), "2");
});

test("page 2 with an empty result set redirects back to page 1", async () => {
  const response = await call("/?q=nothing&page=2", {
    DB: fakeDb({ eventCount: 0 }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 302);
  const location = new URL(response.headers.get("location"));
  assert.equal(location.searchParams.get("page"), null);
  assert.equal(location.searchParams.get("q"), "nothing");
});

test("status page returns 503 when common data cannot be read", async () => {
  const response = await quiet(() =>
    call("/status", {
      DB: fakeDb({ statusReadError: true }),
      INGEST_TOKEN: "test-token",
    }),
  );
  // 不能把 D1 读失败伪装成 200 的「正常但没数据」。
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const body = await response.text();
  assert.match(body, /状态数据暂时不可用/);
  assert.doesNotMatch(body, /WAIT/);
});

test("status page renders the capture and push panel from meta", async () => {
  const now = Math.floor(Date.now() / 1000);
  const checkedAt = new Date((now - 2 * 86400) * 1000).toISOString();
  const response = await call("/status", {
    DB: fakeDb({
      eventCount: 3,
      metaValues: {
        last_ingest_at: String(now - 300),
        last_push_ts: new Date((now - 300) * 1000).toISOString(),
        event_count: "128",
        new_event_count: "12",
        pushed_count: "10",
        rejected_count: "2",
        dead_letter_count: "1",
        backlog_count: "4",
        source_counts: JSON.stringify({ docs: 100, chat: 20, platform: 5, harness: 2, fingerprint: 1 }),
        collection_errors: JSON.stringify([{ source: "watchsite", detail: "事件目录不存在：D:/events" }]),
        models_snapshot: JSON.stringify({
          checked_at: checkedAt,
          catalog_available: true,
          models: [{ id: "deepseek-v4-pro" }],
          catalog_fail_streak: 2,
          catalog_incomplete_streak: 1,
          catalog_last_error: "目录检查返回 401",
        }),
      },
      qualityRows: [
        { quality: "ok", count: 90 },
        { quality: "partial", count: 12 },
        { quality: "unknown", count: 26 },
      ],
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /采集与推送/);
  assert.match(html, /上次推送/);
  assert.match(html, /128/);
  assert.match(html, /本轮推送/);
  assert.match(html, /本轮新增/);
  assert.match(html, /成功推送/);
  assert.match(html, /待重试（被拒收）/);
  assert.match(html, /死信（不再重推）/);
  assert.match(html, /积压未推送/);
  assert.match(html, /事件目录不存在：D:\/events/);
  assert.match(html, /待核实/);
  // 模型目录的「最近检查」卡片已移除：只显示检查时间没有信息量。
  assert.doesNotMatch(html, /pipeline-label">模型目录/);
});

test("status page shows 暂无 instead of failing when push meta is missing", async () => {
  const response = await call("/status", {
    DB: fakeDb({ eventCount: 3 }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /采集与推送/);
  assert.ok((html.match(/暂无/g) || []).length >= 3);
  assert.match(html, /还没有收到推送记录/);
  assert.doesNotMatch(html, /pipeline-label">模型目录/);
});

test("fingerprint source page renders the stored models snapshot", async () => {
  const snapshot = {
    checked_at: "2026-11-16T10:50:00Z",
    catalog_available: true,
    models: [
      {
        id: "deepseek-flash",
        version: "DeepSeek-V4.1-Flash-1017",
        fingerprint: "bc19ba97",
        fingerprint_checked_at: "2026-11-16T10:50:00Z",
        first_seen: "2026-11-16T10:50:00Z",
        in_catalog: true,
      },
    ],
    last_change: null,
  };
  const response = await call("/source/fingerprint", {
    DB: fakeDb({ metaValues: { models_snapshot: JSON.stringify(snapshot) } }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /当前模型目录/);
  assert.match(html, /deepseek-flash/);
  assert.match(html, /DeepSeek-V4\.1-Flash-1017/);
});

test("home page shows an explicit ERROR state when common data cannot be read", async () => {
  const response = await quiet(() =>
    call("/", {
      DB: fakeDb({ statusReadError: true }),
      INGEST_TOKEN: "test-token",
    }),
  );
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /ERROR/);
  assert.match(html, /状态数据读取失败（D1）/);
  assert.doesNotMatch(html, /WAIT/);
});

const eventRow = {
  id: "docwatch:491",
  slug: "docwatch-491",
  source: "docs",
  kind: "docwatch",
  level: 3,
  level_label: "重要",
  title: "价格调整",
  summary: "输出价 13.5 → 15",
  impact: "成本上升",
  action: "检查预算",
  signal_count: 1,
  changed_ts: 1763047866,
  detected_ts: 1763047866,
  page_url: "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
  archive_url: "",
  evidence_json: "[]",
  payload_json: '{"changes":[{"page":"pricing","bullets":["a"],"has_diff":true}]}',
  search_text: "价格",
  updated_ts: 1763047866,
};

test("changes route renders stored diffs with noindex,follow", async () => {
  const response = await call("/e/docwatch-491/changes", {
    DB: fakeDb({
      eventRow,
      changesRows: [
        {
          idx: 0,
          page: "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
          level: 3,
          is_asset: 0,
          is_error: 0,
          noise: 0,
          has_diff: 1,
          diff_bytes: 12,
          bullets_json: '["输出价 13.5 → 15"]',
          diff: "－13.5\n＋15",
          note: "",
        },
      ],
    }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<meta name="robots" content="noindex,follow">/);
  // 左右对照：左原文 13.5（删除），右新内容 15（新增）；变了的那一小段行内高亮
  assert.match(html, /class="diff-cell is-del"/);
  assert.match(html, /class="diff-cell is-add"/);
  assert.match(html, /class="diff-mark"/);
  assert.match(html, /13\.5/);
  // 默认折叠，增删行数在折叠行上就能看到
  assert.doesNotMatch(html, /id="change-0" open/);
  assert.match(html, /<b class="diff-stat-add">\+1<\/b>/);
  assert.match(html, /<b class="diff-stat-del">-1<\/b>/);
});

test("changes route returns 404 for an unknown slug", async () => {
  const response = await call("/e/no-such-event/changes", {
    DB: fakeDb(),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 404);
});

test("changes route shows the empty state when an old event has no stored rows", async () => {
  const response = await call("/e/docwatch-491/changes", {
    DB: fakeDb({ eventRow, changesRows: [] }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /该事件暂无对比明细。/);
});

test("feed.xml renders stored events as RSS items", async () => {
  const db = fakeDb({ eventCount: 1, eventRows: [eventRow] });
  const response = await call("/feed.xml", {
    DB: db,
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /<rss version="2\.0"/);
  assert.match(body, /<title>\[重要\] 价格调整<\/title>/);
  assert.match(body, /<guid isPermaLink="false">docwatch:491<\/guid>/);
  // RSS 要带 DSH 完整更新日志，查询必须包含 payload_json（LIST_COLUMNS 没有它）。
  assert.ok(db.queries.some((sql) => /payload_json/.test(sql)));
});

test("feed.xml includes the full DSH release note from payload", async () => {
  const dshRow = {
    ...eventRow,
    id: "dsh:release:0.2.3-alpha.1",
    slug: "dsh-release-0-2-3-alpha-1",
    source: "harness",
    kind: "dsh_release",
    title: "DSH v0.2.3-alpha.1 发布",
    summary: "官方更新日志共 2 条。",
    payload_json: JSON.stringify({
      release_note: {
        blocks: [
          { type: "heading", text: "新增功能" },
          { type: "bullet", text: "新增功能 A" },
          { type: "bullet", text: "修复问题 B" },
        ],
      },
    }),
  };
  const response = await call("/feed.xml", {
    DB: fakeDb({ eventCount: 1, eventRows: [dshRow] }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /【新增功能】/);
  assert.doesNotMatch(body, /官方更新日志共 2 条。/);
  assert.match(body, /・新增功能 A/);
  assert.match(body, /・修复问题 B/);
});

test("sitemap.xml includes event slugs from the lightweight query", async () => {
  const response = await call("/sitemap.xml", {
    DB: fakeDb({ eventRows: [eventRow] }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.match(body, /<urlset/);
  assert.match(body, /<loc>https:\/\/deepseek-watch\.pages\.dev\/e\/docwatch-491<\/loc>/);
});

test("sitemap.xml returns 503 instead of an empty 200 when D1 fails", async () => {
  const response = await quiet(() =>
    call("/sitemap.xml", {
      DB: failingDb(),
      INGEST_TOKEN: "test-token",
    }),
  );
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("status page returns 503 instead of a misleading empty page when D1 fails", async () => {
  const response = await quiet(() =>
    call("/status", {
      DB: failingDb(),
      INGEST_TOKEN: "test-token",
    }),
  );
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("retry-after"), "60");
});

test("catch-all 503 page is not publicly cached and tells clients to retry", async () => {
  const response = await quiet(() =>
    call("/", {
      DB: failingDb(),
      INGEST_TOKEN: "test-token",
    }),
  );
  assert.equal(response.status, 503);
  // D1 瞬断恢复后不能被 public 缓存继续挡着，否则用户一直看到 SERVICE PAUSED。
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.ok(response.headers.get("retry-after"));
});

test("404 pages are not publicly cached", async () => {
  for (const path of ["/nope", "/e/no-such-event"]) {
    const response = await call(path, { DB: fakeDb(), INGEST_TOKEN: "test-token" });
    assert.equal(response.status, 404, path);
    // 刚出现的事件链接不能被旧 404 的 60 秒公开缓存挡住。
    assert.equal(response.headers.get("cache-control"), "no-store", path);
  }
});

test("successful pages keep the short public cache policy", async () => {
  const response = await call("/about", { DB: fakeDb(), INGEST_TOKEN: "test-token" });
  assert.equal(response.status, 200);
  assert.match(response.headers.get("cache-control"), /public, max-age=60/);
});

function statusQueries(db) {
  return db.queries.filter((sql) => /FROM source_status|GROUP BY source/i.test(sql));
}

test("detail, changes, about and 404 routes no longer issue status queries", async () => {
  const paths = ["/e/docwatch-491", "/e/docwatch-491/changes", "/about", "/e/no-such-event", "/nope"];
  for (const path of paths) {
    const db = fakeDb({ eventRow, changesRows: [] });
    const response = await call(path, { DB: db, INGEST_TOKEN: "test-token" });
    assert.ok([200, 404].includes(response.status), `${path} -> ${response.status}`);
    assert.equal(statusQueries(db).length, 0, `${path} should not read source_status / counts`);
  }
});

test("home still reads status data for the live status strip", async () => {
  const db = fakeDb({ eventCount: 0 });
  const response = await call("/", { DB: db, INGEST_TOKEN: "test-token" });
  assert.equal(response.status, 200);
  assert.ok(statusQueries(db).length >= 1);
});

test("home renders the quality badge from the events column", async () => {
  const db = fakeDb({
    eventCount: 1,
    eventRows: [{ ...eventRow, quality: "partial" }],
  });
  const response = await call("/", { DB: db, INGEST_TOKEN: "test-token" });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /quality-badge quality-partial">待核实/);
});

test("detail route exposes the quality column and level decision from payload", async () => {
  const row = {
    ...eventRow,
    quality: "repaired",
    payload_json: JSON.stringify({
      level_decision: { final: 2, source: "gate", rule: 1, reason: "证据与结论不一致，已按证据修正" },
    }),
  };
  const response = await call("/e/docwatch-491", {
    DB: fakeDb({ eventRow: row }),
    INGEST_TOKEN: "test-token",
  });
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /已按证据修正/);
  assert.match(html, /判级依据/);
  assert.match(html, /证据与结论不一致，已按证据修正/);
});
