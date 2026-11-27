import test from "node:test";
import assert from "node:assert/strict";

import { rowToEvent } from "../src/db.js";
import { normalizeEvent, normalizeStatus } from "../src/ingest.js";

const base = {
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

test("normalizes a valid event", () => {
  const event = normalizeEvent(base);
  assert.equal(event.id, base.id);
  assert.equal(event.slug, base.slug);
  assert.equal(event.level, 3);
  assert.equal(event.levelLabel, "重要");
  assert.equal(event.changedTs, base.changed_ts);
  assert.ok(event.searchText.includes("价格调整"));
});

test("rejects unknown source, bad level, bad slug and missing changed_ts", () => {
  assert.equal(normalizeEvent({ ...base, source: "unknown" }), null);
  assert.equal(normalizeEvent({ ...base, level: 9 }), null);
  assert.equal(normalizeEvent({ ...base, slug: "Bad Slug" }), null);
  assert.equal(normalizeEvent({ ...base, changed_ts: 0 }), null);
  assert.equal(normalizeEvent({ ...base, title: "" }), null);
  assert.equal(normalizeEvent({ ...base, page_url: "javascript:alert(1)" }), null);
});

test("truncates oversized display fields and rejects oversized payload", () => {
  const truncated = normalizeEvent({ ...base, title: "x".repeat(301) });
  assert.equal(truncated.title, "x".repeat(300));
  assert.equal(normalizeEvent({ ...base, payload: { blob: "x".repeat(60_001) } }), null);
});

test("normalizes status rows", () => {
  const status = normalizeStatus({
    source: "docs",
    label: "官方文档",
    last_checked_ts: 1763047866,
    last_success_ts: 1763047866,
    status: "ok",
    detail: "正常",
  });
  assert.equal(status.source, "docs");
  assert.equal(status.status, "ok");
  assert.equal(normalizeStatus({ source: "nope" }), null);
});

test("quality comes from payload.quality with a whitelist and defaults to unknown", () => {
  assert.equal(normalizeEvent({ ...base, payload: { quality: "partial" } }).quality, "partial");
  assert.equal(normalizeEvent({ ...base, payload: { quality: "repaired" } }).quality, "repaired");
  assert.equal(normalizeEvent({ ...base, payload: { quality: "internal" } }).quality, "internal");
  // 缺失、非法、非字符串都不能拒绝事件，只落 unknown。
  assert.equal(normalizeEvent({ ...base, payload: {} }).quality, "unknown");
  assert.equal(normalizeEvent({ ...base, payload: { quality: "bogus" } }).quality, "unknown");
  assert.equal(normalizeEvent({ ...base, payload: { quality: 42 } }).quality, "unknown");
});

test("rowToEvent exposes the quality column and defaults missing rows to unknown", () => {
  const event = rowToEvent({
    id: "docwatch:1",
    slug: "docwatch-1",
    source: "docs",
    kind: "docwatch",
    level: 2,
    level_label: "值得看",
    quality: "partial",
    title: "标题",
    changed_ts: 1763047866,
    detected_ts: 1763047866,
    updated_ts: 1763047866,
  });
  assert.equal(event.quality, "partial");
  assert.equal(rowToEvent({ id: "old", slug: "old" }).quality, "unknown");
});

test("rowToEvent rebuilds frontend summary from lightweight list columns", () => {
  const event = rowToEvent({
    id: "web:1",
    slug: "web-1",
    source: "chat",
    kind: "web",
    level: 1,
    level_label: "轻微",
    quality: "internal",
    title: "未捕获到用户可见变化（可能为内部调整）",
    changed_ts: 1763047866,
    detected_ts: 1763047866,
    updated_ts: 1763047866,
    frontend_totals_json: JSON.stringify({ manifest: 612, added: 0, removed: 0, changed: 1 }),
    frontend_main_js_json: JSON.stringify({ old_size: 1664072, new_size: 1664075 }),
    frontend_main_css_json: JSON.stringify({ old_size: 303321, new_size: 303321 }),
  });
  assert.deepEqual(event.payload.frontend.totals, { manifest: 612, added: 0, removed: 0, changed: 1 });
  assert.equal(event.payload.frontend.main_js.new_size, 1664075);
  // 详情查询有完整 payload 时以完整 payload 为准，轻量字段不能覆盖它。
  const detail = rowToEvent({
    id: "web:1",
    slug: "web-1",
    quality: "internal",
    payload_json: JSON.stringify({ frontend: { totals: { changed: 9 }, custom: 1 } }),
    frontend_totals_json: JSON.stringify({ changed: 1 }),
  });
  assert.equal(detail.payload.frontend.totals.changed, 9);
  assert.equal(detail.payload.frontend.custom, 1);
});
