import test from "node:test";
import assert from "node:assert/strict";

import { renderFeed } from "../src/rss.js";

const event = {
  id: "docwatch:491",
  slug: "docwatch-491",
  source: "docs",
  level: 3,
  title: "价格 <上调> & \"计费\" 变化",
  summary: "输出价 13.5 → 15 元",
  impact: "成本会变化",
  action: "检查预算",
  changedTs: Date.UTC(2026, 10, 13, 15, 31, 6) / 1000,
};

test("feed escapes XML and uses a stable guid", () => {
  const xml = renderFeed({ events: [event], siteUrl: "https://example.com" });
  assert.match(xml, /^<\?xml version="1.0" encoding="UTF-8"\?>/);
  assert.ok(xml.includes("<guid isPermaLink=\"false\">docwatch:491</guid>"));
  assert.ok(xml.includes("<title>[重要] 价格 &lt;上调&gt; &amp; &quot;计费&quot; 变化</title>"));
  assert.ok(!xml.includes("<上调>"));
  assert.ok(xml.includes("<pubDate>Fri, 13 Nov 2026 15:31:06 GMT</pubDate>"));
  assert.ok(xml.includes("https://example.com/feed.xml"));
});

test("feed titles carry the level prefix without changing level filtering", () => {
  const level1 = renderFeed({
    events: [{ ...event, id: "docs:1", slug: "docs-1", level: 1, title: "轻微变化" }],
    siteUrl: "https://example.com",
  });
  assert.ok(level1.includes("<title>[轻微] 轻微变化</title>"));
  const level3 = renderFeed({ events: [event], siteUrl: "https://example.com" });
  assert.ok(level3.includes("<title>[重要] 价格"));
});

test("feed does not present legacy no-change records as important", () => {
  const uncaptured = renderFeed({
    events: [
      {
        ...event,
        id: "web:platform:old",
        slug: "web-platform-old",
        source: "platform",
        quality: "internal",
        level: 3,
        title: "本次为内部调整，未发现界面变化",
        summary: "这次未发现用户能看到的界面或文案变化。",
        impact: "",
        action: "无需操作",
      },
    ],
    siteUrl: "https://example.com",
  });
  assert.ok(uncaptured.includes("<title>[轻微] 未捕获到用户可见变化（可能为内部调整）</title>"));
  assert.ok(!uncaptured.includes("[重要] 本次为内部调整"));

  const backfill = renderFeed({
    events: [
      {
        ...event,
        id: "dsh:release:old",
        slug: "dsh-release-old",
        source: "harness",
        kind: "dsh_release",
        level: 3,
        title: "DSH v0.1.0-rc.7 发布",
        summary: "官方更新日志共 1 条。",
        action: "历史版本补录，仅用于回看。",
      },
    ],
    siteUrl: "https://example.com",
  });
  assert.ok(backfill.includes("<title>[轻微] DSH v0.1.0-rc.7 发布</title>"));
  assert.ok(!backfill.includes("[重要] DSH v0.1.0-rc.7 发布"));
});

test("feed uses recheck-corrected public fields", () => {
  const corrected = {
    ...event,
    title: "复核修正后的标题",
    summary: "复核修正后的摘要",
    impact: "复核修正后的影响",
    action: "复核修正后的建议",
    payload: {
      recheck: {
        state: "done",
        applied: ["title", "summary", "impact", "action"],
        from: { title: "旧标题", summary: "旧摘要", impact: "旧影响", action: "旧建议" },
      },
    },
  };
  const xml = renderFeed({ events: [corrected], siteUrl: "https://example.com" });
  assert.ok(xml.includes("复核修正后的标题"));
  assert.ok(xml.includes("复核修正后的摘要"));
  assert.ok(xml.includes("影响：复核修正后的影响"));
  assert.ok(xml.includes("建议：复核修正后的建议"));
  assert.ok(!xml.includes("旧摘要"));
});

test("source feed uses its own self link and category", () => {
  const xml = renderFeed({ events: [event], source: "docs", siteUrl: "https://example.com" });
  assert.ok(xml.includes("https://example.com/feed/docs.xml"));
  assert.ok(xml.includes("https://example.com/source/docs"));
  assert.ok(xml.includes("<category>官方文档</category>"));
});

test("DSH release feed carries the full release note instead of a TL;DR", () => {
  const xml = renderFeed({
    events: [
      {
        ...event,
        id: "dsh:release:0.2.3-alpha.1",
        slug: "dsh-release-0-2-3-alpha-1",
        source: "harness",
        kind: "dsh_release",
        title: "DSH v0.2.3-alpha.1 发布",
        summary: "官方更新日志共 2 条。",
        impact: "",
        action: "打开 GitHub 发版说明查看完整变更。",
        payload: {
          release_note: {
            blocks: [
              { type: "heading", text: "新增功能" },
              { type: "bullet", text: "新增功能 A" },
              { type: "bullet", text: "修复问题 B" },
            ],
          },
        },
      },
    ],
    siteUrl: "https://example.com",
  });
  assert.ok(xml.includes("【新增功能】"));
  assert.ok(xml.includes("・新增功能 A"));
  assert.ok(xml.includes("・修复问题 B"));
  assert.ok(!xml.includes("## 新增功能"));
  assert.ok(!xml.includes("官方更新日志共 2 条。"));
});

test("empty feed is still valid RSS", () => {
  const xml = renderFeed({ events: [], siteUrl: "https://example.com" });
  assert.ok(xml.includes("<channel>"));
  assert.ok(xml.includes("<lastBuildDate>"));
  assert.ok(!xml.includes("<item>"));
});
