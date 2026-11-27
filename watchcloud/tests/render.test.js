import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { displayEventLevel, displayEventSummary, displayEventTitle, kindLabel, renderAbout, renderDetail, renderEventChanges, renderHome, renderSitemap, renderSource, renderStatus } from "../src/render.js";

const baseStatusArgs = {
  statuses: [],
  daily: [],
  eventDaily: [],
  totalEvents: 0,
  counts: {},
  lastIngest: 0,
  todayEvents: 0,
  siteUrl: "https://deepseek-watch.pages.dev",
};

test("kindLabel names DSH release and commit events", () => {
  assert.equal(kindLabel("dsh_release"), "DSH 发版");
  assert.equal(kindLabel("dsh_commit"), "DSH 提交");
});

test("DSH release detail copies the complete release note without a TL;DR", () => {
  const html = renderDetail({
    event: {
      id: "dsh:release:0.2.3-alpha.2",
      slug: "dsh-release-0-2-3-alpha-2",
      source: "harness",
      kind: "dsh_release",
      level: 3,
      levelLabel: "重要",
      quality: "unknown",
      title: "DSH 新版本 v0.2.3-alpha.2",
      summary: "官方更新日志共 3 条。",
      impact: "DeepSeek Harness（DSH）发布了新版本。",
      action: "打开 GitHub 发版说明查看完整变更。",
      signalCount: 3,
      changedTs: 1763047866,
      detectedTs: 1763047866,
      pageUrl: "https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.2",
      archiveUrl: "",
      evidence: ["新增功能 A", "修复问题 B"],
      payload: {
        version: "0.2.3-alpha.2",
        tag: "dsh-v0.2.3-alpha.2",
        notes: ["新增功能 A", "新增功能 B", "修复问题 C"],
        release_note: {
          blocks: [
            { type: "heading", text: "新增功能" },
            { type: "bullet", text: "新增功能 A" },
            { type: "bullet", text: "新增功能 B" },
            { type: "heading", text: "问题修复" },
            { type: "bullet", text: "修复问题 C" },
          ],
        },
        signals: ["npm", "github_release", "desktop:stable"],
        backfill: false,
        what_changed: "## 新增功能\n- 新增功能 A\n- 新增功能 B\n## 问题修复\n- 修复问题 C",
      },
    },
    previous: null,
    next: null,
    changes: [
      {
        idx: 0,
        page: "Release dsh-v0.2.3-alpha.2",
        level: 3,
        isAsset: false,
        isError: false,
        noise: false,
        hasDiff: false,
        diffBytes: 0,
        bullets: ["新增功能 A", "修复问题 B"],
        diff: "",
        note: "https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.2",
      },
    ],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /Harness \/ DSH 发版/);
  assert.match(html, /v0\.2\.3-alpha\.2/);
  assert.match(html, /重要/);
  assert.match(html, /说明条数/);
  assert.match(html, /更新日志 \/ RELEASE NOTES/);
  assert.match(html, /3 条/);
  assert.match(html, /新增功能 A/);
  assert.match(html, /新增功能 B/);
  assert.match(html, /修复问题 C/);
  assert.match(html, /<h3 class="release-note-heading">新增功能<\/h3>/);
  assert.doesNotMatch(html, /省流 \/ TL;DR/);
  assert.doesNotMatch(html, /变化明细 \/ DETAILS/);
  assert.doesNotMatch(html, /查看全部变化摘要/);
});

test("DSH release without a release note only says it is released", () => {
  const html = renderDetail({
    event: {
      id: "dsh:release:0.2.3-alpha.3",
      slug: "dsh-release-0-2-3-alpha-3",
      source: "harness",
      kind: "dsh_release",
      level: 3,
      levelLabel: "重要",
      quality: "unknown",
      title: "DSH v0.2.3-alpha.3 发布",
      summary: "",
      impact: "",
      action: "打开 GitHub 发版说明查看完整变更。",
      signalCount: 1,
      changedTs: 1763047866,
      detectedTs: 1763047866,
      pageUrl: "https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.3",
      archiveUrl: "",
      evidence: [],
      payload: { version: "0.2.3-alpha.3", notes: [], signals: ["github_release"], backfill: false },
    },
    previous: null,
    next: null,
    changes: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /发布状态 \/ RELEASE/);
  assert.match(html, /已发布/);
  assert.doesNotMatch(html, /省流 \/ TL;DR/);
  assert.doesNotMatch(html, /没有额外说明/);
  assert.doesNotMatch(html, /更新日志 \/ RELEASE NOTES/);
});

test("status history renders 14 cells per source and marks abnormal checks orange", () => {
  const html = renderStatus({
    ...baseStatusArgs,
    statusHistory: {
      chat: [
        { status: "ok", checkedTs: 1763047866, detail: "" },
        { status: "warn", checkedTs: 1763051466, detail: "抓取超时" },
      ],
    },
  });
  assert.match(html, /最近 14 次巡检/);
  assert.match(html, /history-cell is-warn/);
  assert.match(html, /history-cell is-ok/);
  assert.equal((html.match(/history-cell /g) || []).length, 70);
});

test("status page and home switch to STALE when pushes stop", () => {
  const lastCheckedTs = 1763047866;
  const nowTs = lastCheckedTs + 36 * 60;
  const statuses = [{ source: "chat", status: "ok", lastCheckedTs, lastSuccessTs: lastCheckedTs, detail: "" }];

  const statusHtml = renderStatus({ ...baseStatusArgs, statuses, nowTs });
  assert.match(statusHtml, /数据已停更/);
  assert.match(statusHtml, /已经 36 分钟没有新数据/);

  const homeHtml = renderHome({
    events: [],
    total: 0,
    totalAll: 0,
    limit: 20,
    offset: 0,
    q: "",
    activeSource: "",
    activeLevel: "",
    statuses,
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
    nowTs,
  });
  assert.match(homeHtml, /STALE/);
  assert.match(homeHtml, /status-strip is-stale/);
});

test("level filter and pagination keep the active search keyword", () => {
  const event = {
    id: "chat:1",
    slug: "chat-1",
    source: "chat",
    kind: "page_change",
    level: 2,
    levelLabel: "值得看",
    title: "额度提示变化",
    summary: "提示文案更新",
    impact: "",
    action: "",
    signalCount: 1,
    changedTs: 1763047866,
    detectedTs: 1763047866,
    pageUrl: "https://chat.deepseek.com",
    archiveUrl: "",
    evidence: [],
    payload: {},
    searchText: "",
  };
  const html = renderHome({
    events: [event],
    total: 45,
    totalAll: 45,
    limit: 20,
    offset: 0,
    q: "pricing",
    activeSource: "",
    activeLevel: "3",
    statuses: [],
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  // 切换级别时必须保留 q；翻页链接也必须保留 q + level。
  assert.match(html, /href="\/\?q=pricing&amp;level=3"/);
  assert.match(html, /href="\/\?q=pricing&amp;level=3&amp;page=2"/);
  // 每条记录的箭头链接要有可区分的无障碍名称。
  assert.match(html, /aria-label="打开详情：额度提示变化"/);
});

test("source navigation keeps the active keyword and level", () => {
  const html = renderSource({
    source: "harness",
    events: [],
    total: 0,
    limit: 20,
    offset: 0,
    activeLevel: "3",
    q: "dsh",
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  // 切换来源时不能静默丢掉另外两个筛选条件。
  assert.match(html, /href="\/source\/docs\?q=dsh&amp;level=3"/);
  assert.match(html, /href="\/\?q=dsh&amp;level=3"/);
  // 当前来源本身也保留同样的条件。
  assert.match(html, /href="\/source\/harness\?q=dsh&amp;level=3"/);
});

test("fingerprint source page renders the current model catalog", () => {
  const html = renderSource({
    source: "fingerprint",
    events: [],
    total: 0,
    limit: 20,
    offset: 0,
    activeLevel: "",
    q: "",
    statuses: [],
    counts: { fingerprint: 0 },
    siteUrl: "https://deepseek-watch.pages.dev",
    modelsSnapshot: {
      checked_at: "2026-11-16T10:50:00Z",
      catalog_available: true,
      models: [
        {
          id: "deepseek-v4-pro",
          version: "DeepSeek-V4-Pro-0813",
          fingerprint: "d400d062347a13495558bb956f2a0a0c",
          fingerprint_checked_at: "2026-11-16T10:50:00Z",
          first_seen: "2026-11-16T10:50:00Z",
          in_catalog: true,
        },
      ],
      last_change: {
        at: new Date(Date.now() - 3600 * 1000).toISOString(),
        added: ["deepseek-v4-pro"],
        removed: [],
      },
    },
  });
  assert.match(html, /当前模型目录/);
  assert.match(html, /deepseek-v4-pro/);
  assert.match(html, /DeepSeek-V4-Pro-0813/);
  assert.match(html, /NEW/);
  assert.match(html, /最近一次目录变化/);
  assert.match(html, /目录来自官方/);
  assert.match(html, /模型目录每 5 分钟/);
  assert.doesNotMatch(html, /不消耗 token/);
});

test("other source pages do not render the model catalog", () => {
  const html = renderSource({
    source: "chat",
    events: [],
    total: 0,
    limit: 20,
    offset: 0,
    activeLevel: "",
    q: "",
    statuses: [],
    counts: { chat: 0 },
    siteUrl: "https://deepseek-watch.pages.dev",
    modelsSnapshot: {
      catalog_available: true,
      models: [{ id: "deepseek-v4-pro" }],
    },
  });
  assert.doesNotMatch(html, /当前模型目录/);
});

test("about page no longer renders the colophon sidebar", () => {
  const html = renderAbout({ statuses: [], siteUrl: "https://deepseek-watch.pages.dev" });
  assert.doesNotMatch(html, /COLOPHON/);
  assert.match(html, /这里记录 DeepSeek 文档、网页和模型指纹的变化/);
  assert.match(html, /一个一起看信号的/);
  assert.match(html, /id="qq"/);
});

test("layout includes the theme toggle and early theme script", () => {
  const html = renderAbout({ statuses: [], siteUrl: "https://deepseek-watch.pages.dev" });
  assert.match(html, /data-theme-toggle/);
  assert.match(html, /src="\/theme\.js\?v=/);
  assert.match(html, /name="color-scheme"/);
});

test("header shows the public QQ group entry", () => {
  const html = renderAbout({ statuses: [], siteUrl: "https://deepseek-watch.pages.dev" });
  assert.match(html, /class="qq-button"/);
  assert.match(html, /QQ 群聊 · 复制群号/);
  assert.match(html, /721260245/);
});

test("footer shows site version and commit for上线核对", () => {
  const html = renderAbout({ statuses: [], siteUrl: "https://deepseek-watch.pages.dev" });
  assert.match(html, /class="footer-version mono"/);
  assert.match(html, /v\d+\.\d+\.\d+/);
  // 部署前由 scripts/gen-version.mjs 写入；本地开发时是 dev，不上 commit
  assert.match(html, /v0\.\d+\.\d+/);
});

test("home title contains search-friendly keywords", () => {
  const html = renderHome({
    events: [],
    total: 0,
    totalAll: 0,
    limit: 20,
    offset: 0,
    q: "",
    activeSource: "",
    activeLevel: "",
    statuses: [],
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /<title>DeepSeek 更新与文档变化监控 · DeepSeek 观测站<\/title>/);
});

test("layout includes social metadata and website structured data", () => {
  const html = renderAbout({ statuses: [], siteUrl: "https://deepseek-watch.pages.dev" });
  assert.match(html, /property="og:title"/);
  assert.match(html, /property="og:description"/);
  assert.match(html, /name="twitter:card" content="summary_large_image"/);
  assert.match(html, /property="og:image" content="https:\/\/deepseek-watch\.pages\.dev\/og-image\.png"/);
  assert.match(html, /name="twitter:image"/);
  assert.match(html, /rel="sitemap"/);
  assert.match(html, /"@type":"WebSite"/);
});

test("detail page includes article metadata and structured data", () => {
  const html = renderDetail({
    event: {
      slug: "docwatch-1",
      source: "docs",
      kind: "update",
      level: 2,
      title: "测试标题",
      summary: "测试摘要",
      impact: "影响说明",
      action: "建议动作",
      signalCount: 1,
      changedTs: 1763047866,
      detectedTs: 1763051466,
      payload: { model: "example-model-a" },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(html, /解读模型/);
  assert.doesNotMatch(html, /example-provider-ai\/example-model-a/);
  assert.match(html, /property="og:type" content="article"/);
  assert.match(html, /property="article:published_time"/);
  assert.match(html, /"@type":"Article"/);
  assert.match(html, /"headline":"测试标题"/);
});

test("detail page shows the tldr first and never renders diff bodies", () => {
  const html = renderDetail({
    event: {
      slug: "docwatch-tldr",
      source: "docs",
      kind: "docwatch",
      level: 3,
      title: "价格调整",
      summary: "价格页输出价从 13.5 涨到 15 元/百万 tokens，输入价没变。",
      impact: "成本 +11%",
      action: "检查预算",
      signalCount: 1,
      changedTs: 1763047866,
      payload: {
        what_changed: "价格表输出价 13.50 → 15.00 元（高峰），输入价 4 元未动。",
        model_note: "规则判 3 级没问题",
        changes: [
          {
            page: "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            level: 3,
            noise: false,
            has_diff: true,
            diff_bytes: 120,
            bullets: ["输出价 13.5 → 15"],
            // 旧事件可能仍带着 diff：详情页必须不展示。
            diff: "－旧内容\n＋新内容",
            note: "价格页",
          },
        ],
      },
      evidence: ["evidence 1"],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /价格页输出价从 13\.5 涨到 15 元\/百万 tokens，输入价没变/);
  assert.match(html, /变化明细 \/ DETAILS/);
  assert.match(html, /按原始记录整理，未做结构化解读/);
  assert.match(html, /价格表输出价 13\.50 → 15\.00 元（高峰），输入价 4 元未动。/);
  assert.doesNotMatch(html, /class="detail-tldr"/);
  // 没有 event_changes 行时不给对比入口，避免点进去是空页面（P2-49）。
  assert.doesNotMatch(html, /查看完整对比/);
  // 摘要来自 payload.changes，对比页来自 event_changes，不能逐行生成 #change-N。
  assert.doesNotMatch(html, /changes#change-\d/);
  assert.doesNotMatch(html, /<pre class="diff">/);
  assert.doesNotMatch(html, /－旧内容/);
  assert.doesNotMatch(html, /＋新内容/);
});

test("structured docs analysis renders two-tier brief and hides model diagnostics", () => {
  const html = renderDetail({
    event: {
      slug: "docwatch-structured",
      source: "docs",
      kind: "docwatch",
      level: 3,
      title: "4 个接口文档删去联网搜索「计费」措辞，站点重建",
      summary: "create-chat-completion 与 create-completion 页：联网工具返回说明由「计费的搜索与抓取次数」改为「搜索与抓取的次数」",
      action: "无需操作",
      signalCount: 82,
      changedTs: 1763047866,
      payload: {
        what_changed: "旧字段仍在，供回退使用。",
        model_note: "EACCES / BRIEF.md / manifest.json 这些内部诊断不能出现。",
        analysis: {
          headline: "服务端搜索抓取改免费，直调 Search/Fetch 仍按次计费",
          tldr: "价格页和服务端搜索计费口径调整。",
          tldr_points: ["服务端搜索与抓取不再单独计费。", "直接调用 Search / Fetch API 仍按成功请求收费。"],
          overview: "本轮 11 个页面围绕计费口径调整。",
          sections: [
            {
              title: "价格页：计价项改名",
              paragraphs: ["价格页把两个计价项改成 Search API 与 Fetch API，图片搜索独立计价行删除。"],
            },
            {
              title: "接入指南：一免一收",
              paragraphs: ["Claude Code 的 Web Search 不再计费。", "Codex Alpha Search 继续按成功请求收费。"],
            },
          ],
          action: "无需操作；直接调用 API 的用户核对账单。",
          degraded: false,
        },
      },
      evidence: ["价格页 diff"],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /服务端搜索与抓取不再单独计费/);
  assert.match(html, /详细分析 \/ FULL ANALYSIS/);
  assert.match(html, /价格页：计价项改名/);
  assert.match(html, /接入指南：一免一收/);
  assert.match(html, /建议 \/ ACTION/);
  assert.match(html, /class="detail-group"/);
  assert.doesNotMatch(html, /MODEL NOTE/);
  assert.doesNotMatch(html, /EACCES/);
  assert.doesNotMatch(html, /BRIEF\.md/);
  assert.doesNotMatch(html, /WHAT CHANGED \/ 发生了什么/);
  assert.doesNotMatch(html, /class="detail-tldr"/);
  assert.match(html, /<details class="detail-section detail-full" open>/);
});

test("structured analysis degraded state shows a reader-facing caveat", () => {
  const html = renderDetail({
    event: {
      slug: "docwatch-structured-degraded",
      source: "docs",
      kind: "docwatch",
      level: 2,
      title: "测试",
      summary: "测试摘要",
      signalCount: 1,
      changedTs: 1763047866,
      payload: {
        analysis: {
          headline: "测试",
          tldr: "测试",
          tldr_points: ["测试要点。"],
          overview: "",
          sections: [{ title: "测试主题", paragraphs: ["测试段落。"] }],
          action: "无需操作",
          degraded: true,
        },
      },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /本条结论依据有限，建议对照原文核实/);
});

test("structured analysis escapes section text", () => {
  const html = renderDetail({
    event: {
      slug: "docwatch-structured-xss",
      source: "docs",
      kind: "docwatch",
      level: 2,
      title: "测试",
      summary: "测试",
      signalCount: 1,
      changedTs: 1763047866,
      payload: {
        analysis: {
          tldr_points: ["<img src=x onerror=alert(1)>"],
          sections: [{ title: "<script>alert(2)</script>", paragraphs: ["<b>段落</b>"] }],
          action: "<b>点击</b>",
        },
      },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /&lt;img src=x/);
  assert.match(html, /&lt;script&gt;alert/);
});

test("detail page prefers real change groups over raw commit/assets summary", () => {
  const html = renderDetail({
    event: {
      slug: "web-platform-test",
      source: "platform",
      kind: "page_change",
      level: 3,
      title: "平台控制台更新",
      summary: "测试摘要",
      impact: "影响",
      action: "无需操作",
      signalCount: 2,
      changedTs: 1763047866,
      payload: {
        changes: [
          { kind: "commit", old: "0c1d152c", new: "21066fbf" },
          { kind: "assets", added: ["https://x/main.new.js"], removed: ["https://x/main.old.js"], changed: [] },
        ],
      },
      evidence: ["前端版本：0c1d152c → 21066fbf"],
    },
    changes: [
      { page: "版本与构建", bullets: ["commit：0c1d152c → 21066fbf"], isAsset: false },
      { page: "语言包", bullets: ["核对到 1 个语言包分片"], isAsset: false },
      { page: "资源核对", bullets: ["资源清单 323 个：新增 1，移除 1，内容变化 1"], isAsset: true },
    ],
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /3 条摘要/);
  assert.match(html, /版本与构建/);
  assert.match(html, /语言包/);
  assert.match(html, /资源核对/);
  assert.doesNotMatch(html, /没有文字摘要/);
  // 没有 diff 的 event_changes 只能叫「变化摘要」，不能承诺完整对比（P2-48）。
  assert.match(html, /查看全部变化摘要/);
  assert.doesNotMatch(html, /查看完整对比/);
});

test("detail page shows only TL;DR when summary and detail are the same sentence", () => {
  const html = renderDetail({
    event: {
      slug: "docwatch-old",
      source: "docs",
      kind: "docwatch",
      level: 1,
      title: "旧事件",
      summary: "这是旧的省流回退内容",
      signalCount: 1,
      changedTs: 1763047866,
      payload: {},
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /这是旧的省流回退内容/);
  // 省流和唯一一段详情逐字相同：不再让读者读两遍，也不渲染空的 DETAILS。
  assert.doesNotMatch(html, /变化明细 \/ DETAILS/);
  assert.doesNotMatch(html, /详细分析 \/ FULL ANALYSIS/);
  assert.doesNotMatch(html, /class="detail-tldr"/);
  // 没有目录就不渲染底部按钮。
  assert.doesNotMatch(html, /查看完整对比/);
});

test("changes page renders diffs, expands substantive groups and is noindex,follow", () => {
  const html = renderEventChanges({
    event: {
      slug: "docwatch-changes",
      source: "docs",
      kind: "docwatch",
      level: 3,
      title: "价格调整",
      changedTs: 1763047866,
    },
    changes: [
      {
        idx: 0,
        page: "https://example.com/pricing",
        level: 3,
        noise: false,
        hasDiff: true,
        diffBytes: 2048,
        bullets: ["输出价变化"],
        diff: "－旧内容\n＋新内容",
        note: "价格页",
      },
      {
        idx: 1,
        page: "https://example.com/assets/main.js",
        level: 0,
        noise: true,
        hasDiff: true,
        diffBytes: 300,
        bullets: [],
        diff: "－hash a\n＋hash b",
        note: "",
      },
    ],
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /<meta name="robots" content="noindex,follow">/);
  // 默认全部折叠：一进来不展开，用户自己点开
  assert.match(html, /<details class="change change-group" id="change-0">/);
  assert.match(html, /<details class="change change-group" id="change-1">/);
  assert.doesNotMatch(html, /id="change-0" open/);
  assert.doesNotMatch(html, /id="change-1" open/);
  // 折叠行就带增删行数，绿加红减，不用展开就能看到规模
  assert.match(html, /<b class="diff-stat-add">\+1<\/b>/);
  assert.match(html, /<b class="diff-stat-del">-1<\/b>/);
  // 标签、大小与增删行数分组渲染，右对齐的统计不随左侧网址长度漂移
  assert.match(html, /<span class="change-meta"><span class="change-tags"><span class="change-kind">有对比<\/span><span class="change-size mono">2\.0 KB<\/span><\/span><span class="diff-stat">/);
  // 左右对照：左原文（删除）右新内容（新增），行内只高亮真正变了的那一小段
  assert.match(html, /<div class="diff-cell is-del"><mark class="diff-mark">旧<\/mark>内容<\/div>/);
  assert.match(html, /<div class="diff-cell is-add"><mark class="diff-mark">新<\/mark>内容<\/div>/);
  // 改动是重建出来的，页面要有声明
  assert.match(html, /改前改后由采集到的文本差异重建/);
  assert.match(html, /2 组 · 1 组有实质对比/);
  assert.match(html, /展开全部 2 组/);
  assert.match(html, /2\.0 KB/);
  assert.match(html, /价格页/);
});

test("changes page with all=1 expands noise groups and flips the toggle", () => {
  const html = renderEventChanges({
    event: {
      slug: "docwatch-all",
      source: "docs",
      kind: "docwatch",
      level: 0,
      title: "重建",
      changedTs: 1763047866,
    },
    changes: [
      { idx: 0, page: "a", level: 0, noise: false, hasDiff: true, diff: "＋x", note: "" },
    ],
    siteUrl: "https://deepseek-watch.pages.dev",
    showAll: true,
  });
  assert.match(html, /id="change-0" open/);
  assert.match(html, /全部收起/);
  assert.match(html, /href="\/e\/docwatch-all\/changes"/);
});

test("changes page shows a friendly empty state", () => {
  const html = renderEventChanges({
    event: {
      slug: "docwatch-empty",
      source: "docs",
      kind: "docwatch",
      level: 2,
      title: "没有明细",
      changedTs: 1763047866,
    },
    changes: [],
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /该事件暂无对比明细。/);
  assert.match(html, /href="\/e\/docwatch-empty"/);
  assert.doesNotMatch(html, /change-stack"><details/);
});

test("legacy python-list diff renders as side-by-side cells on the changes page", () => {
  const html = renderEventChanges({
    event: {
      slug: "docwatch-legacy",
      source: "docs",
      kind: "update",
      level: 2,
      title: "旧事件",
      changedTs: 1763047866,
    },
    changes: [
      {
        idx: 0,
        page: "https://example.com",
        level: 2,
        noise: false,
        hasDiff: true,
        bullets: [],
        diff: "['－旧内容', '＋新内容']",
        note: "",
      },
    ],
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /<div class="diff-cell is-del"><mark class="diff-mark">旧<\/mark>内容<\/div>/);
  assert.match(html, /<div class="diff-cell is-add"><mark class="diff-mark">新<\/mark>内容<\/div>/);
  assert.doesNotMatch(html, /\[&#39;－旧内容/);
});

test("split diff keeps unmatched lines on one side and flags wholesale rewrites", () => {
  const html = renderEventChanges({
    event: {
      slug: "docwatch-mixed",
      source: "docs",
      kind: "docwatch",
      level: 2,
      title: "增减不配对",
      changedTs: 1763047866,
    },
    changes: [
      {
        idx: 0,
        page: "https://example.com/page",
        level: 2,
        noise: false,
        hasDiff: true,
        bullets: [],
        diff: "－输出价 13.50 元\n＋输出价 15.00 元\n＋工具调用费 5 元/千次",
        note: "",
      },
      {
        idx: 1,
        page: "https://example.com/rewrite",
        level: 2,
        noise: false,
        hasDiff: true,
        bullets: [],
        diff: "－完全不同的旧内容\n＋另起炉灶的新写法",
        note: "",
      },
    ],
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  // 第一组：前两行按相似度配上，第三行只在新内容一侧，左侧留空
  assert.match(html, /<div class="diff-cell diff-empty" aria-hidden="true"><\/div><div class="diff-cell is-add">/);
  assert.match(html, /<b class="diff-stat-add">\+2<\/b>/);
  assert.match(html, /<b class="diff-stat-del">-1<\/b>/);
  // 第二组：两边差太多，配不上就不硬配，明确标注未做逐行配对
  assert.match(html, /class="diff-note"/);
  assert.match(html, /未做逐行配对/);
});

test("sitemap includes lastmod for event pages", () => {
  const xml = renderSitemap(
    [{ slug: "docwatch-1", changedTs: 1763047866 }],
    "https://deepseek-watch.pages.dev",
  );
  assert.match(xml, /<loc>https:\/\/deepseek-watch\.pages\.dev\/e\/docwatch-1<\/loc><lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/);
});

test("detail directory labels webwatch change groups instead of '未命名页面'", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-1",
      source: "chat",
      kind: "page_change",
      level: 2,
      title: "Chat 页面更新",
      summary: "Chat 页面正文有变化",
      signalCount: 1,
      changedTs: 1763047866,
      payload: { changes: [{ kind: "text", preview: "新增了一个入口按钮" }] },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /文字变化/);
  assert.match(html, /新增了一个入口按钮/);
  assert.doesNotMatch(html, /未命名页面/);
});

test("detail renders the user-readable analysis brief before technical evidence", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-2",
      source: "chat",
      kind: "page_change",
      level: 3,
      title: "额度用完时，聊天界面会多一条提示",
      summary: "额度用完时会出现提示。",
      signalCount: 2,
      changedTs: 1763047866,
      payload: {
        changes: [{ kind: "assets", preview: "资源变化" }],
        analysis: {
          headline: "额度用完时，聊天界面会多一条提示",
          tldr: "额度用完时会出现提示。",
          visible_changes: ["输入框上方会出现「使用额度已达上限，请额度恢复后再试」。"],
          impact: ["使用受限额度的用户会看到提示。"],
          action: "无需操作",
          model: "example-model-b",
          confidence: "medium",
          degraded: false,
        },
      },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /变化明细 \/ DETAILS/);
  assert.match(html, /变化内容/);
  assert.match(html, /输入框上方会出现/);
  assert.match(html, /<h3>影响<\/h3>/);
  assert.match(html, /使用受限额度的用户会看到提示/);
  assert.match(html, /建议 \/ ACTION/);
  // 模型名和置信度不展示给读者。
  assert.doesNotMatch(html, /example-provider-ai\/example-model-b/);
  assert.doesNotMatch(html, /置信度/);
  assert.doesNotMatch(html, /analysis-meta/);
  assert.doesNotMatch(html, /UPDATE BRIEF/);
});

test("analysis events fold evidence and technical check into detail page", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-3",
      source: "chat",
      kind: "page_change",
      level: 3,
      title: "额度用完时，聊天界面会多一条提示",
      summary: "额度用完时会出现提示。",
      signalCount: 2,
      changedTs: 1763047866,
      payload: {
        changes: [
          { page: "新增接口", bullets: ["`/api/v0/chat/status`"] },
          { page: "新增用户文案", bullets: ["「使用额度已达上限，请额度恢复后再试」"] },
        ],
        frontend: {
          old_commit: "4d89b69a",
          new_commit: "85af6250",
          old_build: { appVersion: "2.7.1" },
          new_build: { appVersion: "2.8.0", commit_datetime: "2026/11/13 15:17:43" },
          main_js: { old_size: 1634000, new_size: 1657849 },
          main_css: { old_size: 300030, new_size: 303171 },
          totals: { manifest: 612, added: 0, removed: 0, changed: 612 },
        },
        analysis: {
          headline: "额度用完时，聊天界面会多一条提示",
          tldr: "额度用完时会出现提示。",
          visible_changes: ["输入框上方会出现「使用额度已达上限，请额度恢复后再试」。"],
          impact: ["使用受限额度的用户会看到提示。"],
          action: "无需操作",
          model: "example-model-b",
          confidence: "medium",
          degraded: false,
        },
      },
      evidence: ["前端版本：4d89b69a → 85af6250"],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /<details class="detail-section">/);
  assert.match(html, /<summary class="section-head">/);
  assert.match(html, /EVIDENCE \/ 依据/);
  assert.match(html, /TECHNICAL \/ 技术核对/);
  assert.match(html, /brief-list/);
  assert.match(html, /新增接口/);
  assert.match(html, /2\.7\.1/);
  assert.match(html, /2\.8\.0/);
  assert.match(html, /主包大小/);
  assert.match(html, /资源清单/);
  assert.doesNotMatch(html, /EVIDENCE \/ 证据/);
});

test("events without analysis still use the new detail layout", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-legacy",
      source: "chat",
      kind: "page_change",
      level: 2,
      title: "Chat 网页正文更新",
      summary: "页面正文有变化。",
      impact: "可能对应文案调整。",
      action: "打开页面确认。",
      signalCount: 1,
      changedTs: 1763047866,
      payload: { changes: [{ kind: "text", preview: "文字变化" }] },
      evidence: ["脚本资源：内容变化 2 个"],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(html, /UPDATE BRIEF/);
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /变化明细 \/ DETAILS/);
  // summary 和唯一的变化句相同，已去重；影响段仍保留。
  assert.match(html, /<h3>影响<\/h3>/);
  assert.match(html, /建议 \/ ACTION/);
  assert.match(html, /打开页面确认/);
  assert.match(html, /EVIDENCE \/ 证据/);
});

test("empty analysis lists fall back to event fields in the two-tier layout", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-empty",
      source: "chat",
      kind: "page_change",
      level: 2,
      title: "Chat 网页更新",
      summary: "页面有变化。",
      signalCount: 1,
      changedTs: 1763047866,
      payload: { analysis: { headline: "提示", tldr: "提示", visible_changes: [], impact: [] } },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /页面有变化/);
  assert.match(html, /变化明细 \/ DETAILS/);
  assert.match(html, /变化内容/);
  assert.doesNotMatch(html, /UPDATE BRIEF/);
});

test("analysis text is escaped before rendering", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-xss",
      source: "chat",
      kind: "page_change",
      level: 2,
      title: "<script>alert(1)</script>",
      summary: "<b>tldr</b>",
      signalCount: 1,
      changedTs: 1763047866,
      payload: {
        analysis: {
          headline: "<script>alert(1)</script>",
          tldr: "<b>tldr</b>",
          visible_changes: ["<img src=x onerror=alert(1)>"],
          impact: ["<script>alert(2)</script>"],
          action: "<b>click</b>",
          model_note: "<i>note</i>",
        },
      },
      evidence: ["<svg onload=alert(3)>"],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /&lt;img src=x/);
  assert.match(html, /&lt;script&gt;alert/);
});

test("degraded analysis states that no model interpretation was generated", () => {
  const html = renderDetail({
    event: {
      slug: "web-chat-degraded",
      source: "chat",
      kind: "page_change",
      level: 2,
      title: "Chat 网页前端版本更新",
      summary: "检测到前端更新；模型解读未生成。",
      signalCount: 1,
      changedTs: 1763047866,
      payload: {
        analysis: {
          headline: "Chat 网页前端版本更新",
          tldr: "检测到前端更新；模型解读未生成。",
          visible_changes: ["界面上新增了一条提示：「使用额度已达上限，请额度恢复后再试」"],
          impact: ["具体影响无法确认，建议打开原始页面核对。"],
          action: "如需确认，请打开原始页面。",
          model: "规则兜底",
          confidence: "low",
          degraded: true,
        },
      },
      evidence: [],
    },
    previous: null,
    next: null,
    statuses: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /本条结论依据有限，建议对照原文核实/);
  assert.match(html, /省流 \/ TL;DR/);
  assert.match(html, /变化明细 \/ DETAILS/);
});

function eventFixture(overrides = {}) {
  return {
    id: "web:1",
    slug: "web-1",
    source: "chat",
    kind: "page_change",
    level: 2,
    levelLabel: "值得看",
    title: "Chat 页面更新",
    summary: "页面有变化。",
    impact: "",
    action: "",
    signalCount: 1,
    changedTs: 1763047866,
    detectedTs: 1763047866,
    evidence: [],
    payload: {},
    ...overrides,
  };
}

test("home cards show quality badges only for partial, repaired and internal", () => {
  const html = renderHome({
    events: [
      eventFixture({ id: "web:partial", slug: "web-partial", quality: "partial" }),
      eventFixture({ id: "web:repaired", slug: "web-repaired", quality: "repaired" }),
      eventFixture({ id: "web:internal", slug: "web-internal", quality: "internal" }),
      eventFixture({ id: "web:ok", slug: "web-ok", quality: "ok" }),
      eventFixture({ id: "web:unknown", slug: "web-unknown", quality: "unknown" }),
    ],
    total: 5,
    totalAll: 5,
    limit: 20,
    offset: 0,
    q: "",
    activeSource: "",
    activeLevel: "",
    statuses: [],
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /quality-badge quality-partial">待核实/);
  assert.match(html, /quality-badge quality-repaired">已按证据修正/);
  assert.match(html, /quality-badge quality-internal">仅技术变化/);
  assert.equal((html.match(/quality-badge /g) || []).length, 3);
});

test("confirmed recheck hides the stale partial quality badge", () => {
  const event = eventFixture({
    quality: "partial",
    payload: {
      quality: "partial",
      recheck: {
        state: "done",
        verdict: "confirmed",
        basis: "已核对，但关键文件有截断",
      },
    },
  });
  const html = renderDetail({
    event,
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /已复核/);
  assert.doesNotMatch(html, /quality-badge quality-partial/);
});

test("legacy display levels are corrected without changing the database value", () => {
  assert.equal(
    displayEventLevel({ level: 3, kind: "dsh_release", action: "历史版本补录，仅用于回看。" }),
    1,
  );
  assert.equal(
    displayEventLevel({ level: 3, quality: "unknown", title: "Platform 控制台前端版本更新" }),
    1,
  );
  assert.equal(
    displayEventLevel({ level: 3, quality: "unknown", title: "平台控制台更新，界面变化无法确认" }),
    2,
  );
  assert.equal(
    displayEventLevel({
      level: 3,
      quality: "ok",
      title: "Chat 网页更新：图片预览与图表显示相关部分有调整",
    }),
    2,
  );
  assert.equal(
    displayEventLevel({ level: 3, quality: "unknown", title: "本次为内部调整，未发现界面变化" }),
    1,
  );
  assert.equal(displayEventLevel({ level: 3, quality: "ok", title: "价格调整" }), 3);
});

test("internal adjustment events are relabelled and grouped below the main feed", () => {
  const html = renderHome({
    events: [
      eventFixture({ id: "web:normal", slug: "web-normal", title: "正常更新" }),
      eventFixture({
        id: "web:internal",
        slug: "web-internal",
        quality: "internal",
        title: "本次为内部调整，未发现界面变化",
        summary: "本次没有提取到明确的用户可见变化。",
      }),
    ],
    total: 2,
    totalAll: 2,
    limit: 20,
    offset: 0,
    q: "",
    activeSource: "",
    activeLevel: "",
    statuses: [],
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /internal-events/);
  assert.match(html, /未捕获到用户可见变化（可能为内部调整）/);
  assert.match(html, /quality-badge quality-internal">仅技术变化/);
  assert.doesNotMatch(html, /本次为内部调整，未发现界面变化/);
});

test("legacy uncaptured titles are upgraded from frontend byte evidence", () => {
  const event = eventFixture({
    quality: "internal",
    level: 1,
    title: "未捕获到用户可见变化（可能为内部调整）",
    summary: "本次没有提取到明确的用户可见变化。",
    payload: {
      frontend: {
        totals: { manifest: 612, added: 0, removed: 0, changed: 1 },
        main_js: { old_size: 1664072, new_size: 1664075 },
      },
    },
  });
  assert.equal(
    displayEventTitle(event),
    "仅有技术变化：main.js 1,664,072 → 1,664,075 字节",
  );
  assert.match(displayEventSummary(event), /仅检测到技术变化：main\.js/);
  assert.match(displayEventSummary(event), /未提取到用户可见文案/);

  const partial = eventFixture({
    quality: "partial",
    title: "未捕获到用户可见变化（可能为内部调整）",
    payload: {
      frontend: { totals: { manifest: 323, added: 1, removed: 1, changed: 0 } },
    },
  });
  assert.equal(
    displayEventTitle(partial),
    "未捕获到用户可见变化（可能为内部调整）",
  );
});

test("technical-only internal events keep the concrete file/byte title", () => {
  const title = "仅有技术变化：main.js：1,664,072 → 1,664,075 字节（+3）";
  const summary = "仅检测到技术变化：main.js：1,664,072 → 1,664,075 字节（+3）；未提取到用户可见文案。";
  const event = eventFixture({
    quality: "internal",
    level: 1,
    title,
    summary,
    impact: "对日常使用没有可见影响，本次只有资源层面的技术变化。",
    action: "如需核对，可查看下方变化明细里的文件与字节变化。",
    payload: {
      frontend: {
        totals: { manifest: 612, added: 0, removed: 0, changed: 1 },
        main_js: { old_size: 1664072, new_size: 1664075 },
        changed_files: [{ key: "js:entry", name: "main.js", kind: "js", role: "entry" }],
      },
    },
  });
  assert.equal(displayEventTitle(event), title);
  assert.equal(displayEventSummary(event), summary);
  assert.equal(displayEventLevel(event), 1);
  const html = renderDetail({
    event,
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /仅有技术变化：main\.js/);
  assert.match(html, /只检测到资源层面的技术变化/);
  assert.match(html, /TECHNICAL \/ 技术核对/);
  assert.match(html, /quality-badge quality-internal">仅技术变化/);
  assert.doesNotMatch(html, /这条记录没有抓到可读的界面变化/);
});

test("recheck-corrected events still show the raw technical evidence", () => {
  const event = eventFixture({
    quality: "ok",
    level: 2,
    title: "登录设备管理页有小幅调整",
    summary: "登录设备管理相关代码有调整，具体可见效果尚待核对。",
    payload: {
      frontend: {
        totals: { manifest: 612, added: 0, removed: 0, changed: 1 },
        main_js: { old_size: 1666669, new_size: 1666571 },
      },
      analysis: {
        headline: "旧判读标题",
        tldr: "旧判读摘要",
        visible_changes: ["旧判读内容"],
        impact: [],
        action: "无需操作",
      },
      recheck: {
        state: "done",
        verdict: "confirmed",
        applied: ["summary", "impact", "action"],
      },
    },
  });
  const html = renderDetail({
    event,
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /TECHNICAL \/ 技术核对/);
  assert.doesNotMatch(html, /旧判读内容/);
});

test("detail page shows the quality badge and a level decision block without model details", () => {
  const html = renderDetail({
    event: eventFixture({
      quality: "partial",
      payload: {
        quality: "partial",
        level_decision: {
          final: 2,
          source: "guard",
          rule: 1,
          ai: 2,
          reason: "证据不完整，按值得看发布并标注待核实",
          ai_reason: "模型内部推理",
        },
        analysis: {
          visible_changes: ["界面上多了一条提示"],
          impact: [],
          action: "",
          model: "example-model-a",
          confidence: "high",
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /quality-badge quality-partial">待核实/);
  assert.match(html, /LEVEL DECISION \/ 判级依据/);
  assert.match(html, /证据不完整，按值得看发布并标注待核实/);
  assert.match(html, /最终级别/);
  assert.match(html, /规则原始级别/);
  assert.match(html, /判级来源/);
  assert.match(html, /质量闸门/);
  // 模型名、置信度、模型原始理由都不展示。
  assert.doesNotMatch(html, /example-provider-ai\/example-model-a/);
  assert.doesNotMatch(html, /置信度/);
  assert.doesNotMatch(html, /模型内部推理/);
});

test("detail page shows a small reviewed note for regraded events", () => {
  const html = renderDetail({
    event: eventFixture({
      quality: "ok",
      payload: {
        quality: "ok",
        level_decision: {
          final: 2,
          source: "rule",
          rule: 3,
          reason: "按新标准复核：局部可见调整，级别由 3 调整为 2。",
        },
        regrade: {
          at: "2026-11-18",
          basis: "按新标准复核：局部可见调整，级别由 3 调整为 2。",
          from_level: 3,
          to_level: 2,
          from_quality: "unknown",
          to_quality: "ok",
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /class="recheck-note"/);
  assert.match(html, /已重评/);
  assert.doesNotMatch(html, /已复核/);
  assert.match(html, /2026-11-18/);
  assert.match(html, /原 3 级 → 2 级/);
  assert.match(html, /依据：按新标准复核/);
});

test("detail page omits the reviewed note for ordinary events", () => {
  const html = renderDetail({
    event: eventFixture({ payload: {} }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(html, /class="recheck-note"/);
});

test("detail change index hides technical first bullets but the changes page keeps them", () => {
  const event = eventFixture({
    slug: "web-technical",
    title: "Chat 前端发版",
    payload: {},
  });
  const changes = [
    {
      idx: 0,
      page: "版本与构建",
      level: 2,
      isAsset: false,
      bullets: ["commit：0c1d152c → 21066fbf", "版本：2.7.1 → 2.8.0"],
    },
    {
      idx: 1,
      page: "新增用户文案",
      level: 2,
      isAsset: false,
      bullets: ["「使用额度已达上限，请额度恢复后再试」"],
    },
    {
      idx: 2,
      page: "资源核对",
      level: 2,
      isAsset: true,
      bullets: ["资源清单 323 个：新增 1，移除 1，内容变化 1", "main.js：1,634,000 → 1,657,849 字节"],
    },
  ];
  const detailHtml = renderDetail({
    event,
    previous: null,
    next: null,
    changes,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(detailHtml, /版本与构建/);
  assert.match(detailHtml, /「使用额度已达上限，请额度恢复后再试」/);
  assert.doesNotMatch(detailHtml, /commit：/);
  assert.doesNotMatch(detailHtml, /0c1d152c/);
  assert.doesNotMatch(detailHtml, /资源清单 323/);
  assert.doesNotMatch(detailHtml, /字节/);

  const changesHtml = renderEventChanges({
    event,
    changes,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  // 原始技术 bullet 仍保留在 /changes 页，供需要的人核对。
  assert.match(changesHtml, /commit：0c1d152c → 21066fbf/);
  assert.match(changesHtml, /资源清单 323 个/);
});

test("changes page without diffs speaks of a summary and hides the compare promises", () => {
  const html = renderEventChanges({
    event: eventFixture({ slug: "web-summary", title: "Chat 文案更新" }),
    changes: [
      {
        idx: 0,
        page: "新增用户文案",
        level: 2,
        noise: false,
        hasDiff: false,
        bullets: ["「当前对话已归档」"],
        diff: "",
        note: "",
      },
    ],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /变化摘要/);
  assert.doesNotMatch(html, /左右对照/);
  assert.doesNotMatch(html, /展开全部/);
  assert.doesNotMatch(html, /改前改后由采集到的文本差异重建/);
  assert.match(html, /「当前对话已归档」/);
});

test("html-only detail page has no dead compare link and explains the structural change", () => {
  const html = renderDetail({
    event: eventFixture({
      slug: "web-html-only",
      kind: "html",
      title: "Chat 页面结构变化",
      payload: { changes: [{ kind: "html", preview: "页面结构有变化" }] },
    }),
    previous: null,
    next: null,
    changes: [],
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /页面结构变化/);
  assert.match(html, /本轮只检测到页面结构变化/);
  assert.doesNotMatch(html, /查看完整对比/);
  assert.doesNotMatch(html, /查看全部变化摘要/);
});

test("status page renders the capture and push panel without truncating error details", () => {
  const longDetail = `采集异常详情：${"原始错误信息".repeat(40)}`;
  const html = renderStatus({
    ...baseStatusArgs,
    statuses: [
      {
        source: "chat",
        status: "warn",
        lastCheckedTs: 1763047866,
        lastSuccessTs: 1763047866,
        lastEventTs: 0,
        detail: longDetail,
      },
    ],
    pushMeta: {
      lastPushTs: 1763047866,
      eventCount: 128,
      newEventCount: 12,
      pushedCount: 10,
      rejectedCount: 2,
      deadLetterCount: 1,
      backlogCount: 3,
      sourceCounts: { docs: 100, chat: 20, platform: 5, harness: 2, fingerprint: 1 },
      collectionErrors: [{ source: "watchsite", detail: longDetail }],
      qualityCounts: { ok: 90, partial: 12, unknown: 26 },
    },
    nowTs: 1763134266,
  });
  assert.match(html, /采集与推送/);
  assert.match(html, /最近推送/);
  assert.match(html, /站点事件总数/);
  assert.match(html, /本轮推送/);
  assert.match(html, /各来源计数/);
  assert.match(html, /质量分布/);
  assert.match(html, /推送结果/);
  assert.match(html, /本轮新增/);
  assert.match(html, /成功推送/);
  assert.match(html, /待重试（被拒收）/);
  assert.match(html, /死信（不再重推）/);
  assert.match(html, /积压未推送/);
  assert.match(html, /采集异常/);
  // 明细在 <details> 里完整输出，不截断、不省略。
  assert.ok(html.includes(longDetail));
  // 只报告「什么时候检查过模型目录」的卡片没有信息量，已移除。
  assert.doesNotMatch(html, /pipeline-label">模型目录/);
  assert.match(html, /待核实/);
  assert.match(html, /推送器/);
});

test("status page shows 暂无 in every empty push panel slot", () => {
  const html = renderStatus({ ...baseStatusArgs, pushMeta: null });
  assert.match(html, /采集与推送/);
  assert.match(html, /还没有收到推送记录/);
  assert.match(html, /站点事件总数/);
  assert.match(html, /当前已入库的全部信号/);
  assert.match(html, /推送结果/);
  assert.ok((html.match(/暂无/g) || []).length >= 8);
});

test("legacy sentence splitting does not break inside Chinese quotes", () => {
  const html = renderDetail({
    event: eventFixture({
      title: "停止词补充上限",
      summary: "停止词补充 512 字节上限。",
      payload: {
        what_changed:
          "使用 Anthropic API 页把「完全支持」改成「完全支持。最多 16 个非空字符串，每个字符串不能超过 512 字节」。其余 66 页只是前端重建。",
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(html, /<p>最多 16 个非空字符串/);
  assert.match(html, /完全支持。最多 16 个非空字符串/);
});

test("pure negative TL;DR points are dropped and duplicate detail sentences removed", () => {
  const html = renderDetail({
    event: eventFixture({
      title: "停止词补充上限",
      summary: "停止词补充 512 字节上限。",
      payload: {
        analysis: {
          tldr_points: ["停止词补了 512 字节上限。", "价格与接口未变"],
          visible_changes: ["价格与接口未变", "使用超过 512 字节停止词的人需要检查。"],
          impact: [],
          action: "",
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  // 纯否定句不再作为省流点，也不在详情里重复出现。
  assert.doesNotMatch(html, /价格与接口未变/);
  assert.match(html, /停止词补了 512 字节上限/);
  assert.match(html, /使用超过 512 字节停止词的人需要检查/);
  // action 为空时给可执行的默认建议，不留空。
  assert.match(html, /打开原始页面核对变化/);
});

test("empty blocks are not rendered and quality badges cover contradiction/missing", () => {
  const detail = renderDetail({
    event: eventFixture({
      title: "只有影响的事件",
      summary: "",
      impact: "对普通用户没有影响。",
      action: "",
      payload: {},
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.doesNotMatch(detail, /0 条要点/);
  assert.doesNotMatch(detail, /0 个主题/);
  assert.match(detail, /<h3>影响<\/h3>/);

  const home = renderHome({
    events: [
      eventFixture({ id: "web:contradiction", slug: "web-contradiction", quality: "contradiction" }),
      eventFixture({ id: "web:missing", slug: "web-missing", quality: "missing" }),
    ],
    total: 2,
    totalAll: 2,
    limit: 20,
    offset: 0,
    q: "",
    activeSource: "",
    activeLevel: "",
    statuses: [],
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(home, /quality-badge quality-contradiction">结论矛盾/);
  assert.match(home, /quality-badge quality-missing">证据缺失/);
});

test("recheck-corrected summary replaces stale analysis and what_changed", () => {
  const html = renderDetail({
    event: eventFixture({
      title: "复核修正后的标题",
      summary: "复核修正后的摘要：登录入口出现调整。",
      impact: "所有需要登录的用户都会看到新的入口。",
      action: "按页面提示登录即可",
      payload: {
        what_changed: "旧判读：这里原本写着一段已经被证伪的变化。",
        recheck: {
          state: "done",
          verdict: "contradicted",
          applied: ["title", "summary", "impact", "action"],
          from: { title: "旧标题", summary: "旧摘要" },
        },
        analysis: {
          tldr: "旧判读的省流句。",
          visible_changes: ["旧判读的可见变化。"],
          impact: ["旧判读的影响。"],
          action: "旧判读的建议。",
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /复核修正后的摘要/);
  assert.match(html, /所有需要登录的用户都会看到新的入口/);
  assert.match(html, /按页面提示登录即可/);
  assert.doesNotMatch(html, /旧判读的省流句/);
  assert.doesNotMatch(html, /旧判读的可见变化/);
  assert.doesNotMatch(html, /已经被证伪的变化/);
});

test("home list uses recheck-corrected title and summary", () => {
  const html = renderHome({
    events: [
      eventFixture({
        title: "复核修正后的标题",
        summary: "复核修正后的摘要。",
        payload: {
          recheck: {
            state: "done",
            applied: ["title", "summary"],
            from: { title: "旧标题", summary: "旧摘要" },
          },
        },
      }),
    ],
    total: 1,
    totalAll: 1,
    limit: 20,
    offset: 0,
    q: "",
    activeSource: "",
    activeLevel: "",
    statuses: [],
    counts: {},
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /复核修正后的标题/);
  assert.match(html, /复核修正后的摘要/);
  assert.doesNotMatch(html, /旧摘要/);
});

test("recheck level upgrade is shown in review note and level decision", () => {
  const html = renderDetail({
    event: eventFixture({
      level: 3,
      payload: {
        level_decision: {
          final: 2,
          source: "rule",
          rule: 2,
          reason: "规则判为局部更新",
        },
        recheck: {
          state: "done",
          verdict: "contradicted",
          applied: ["level", "title"],
          level_applied: true,
          from_level: 2,
          to_level: 3,
          basis: "登录流程属于核心使用路径",
          level_suggestion: {
            suggested: 3,
            reason: "登录入口调整影响核心使用路径",
          },
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /复核后 2 级 → 3 级/);
  assert.match(html, /登录入口调整影响核心使用路径/);
  assert.match(html, /发布后复核/);
  assert.match(html, /level-text-3/);
});

test("review note uses reader language and separates spans", () => {
  const html = renderDetail({
    event: eventFixture({
      payload: {
        recheck: {
          at: "2026-11-18",
          basis: "按 2026-11 新标准用当前 docwatch 规则重算历史 signals：级别维持 3",
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  assert.match(html, /已复核/);
  assert.match(html, /监控规则/);
  assert.match(html, /信号/);
  assert.match(html, /级别维持 3 级/);
  assert.doesNotMatch(html, /signals/);
  assert.doesNotMatch(html, /docwatch 规则/);
  // 多个 span 之间有可复制/可朗读的分隔符。
  assert.match(html, /class="recheck-sep" aria-hidden="true">·<\/span>/);
});

test("long structured paragraphs are split at sentence boundaries", () => {
  const sentenceA = "第一句说清楚改前是什么。";
  const sentenceB = "第二句说清楚改后是什么以及对谁有影响。";
  const html = renderDetail({
    event: eventFixture({
      title: "长段落",
      summary: "长段落测试。",
      payload: {
        analysis: {
          tldr_points: ["测试要点。"],
          sections: [{ title: "主题", paragraphs: [sentenceA.repeat(6) + sentenceB.repeat(6)] }],
        },
      },
    }),
    previous: null,
    next: null,
    siteUrl: "https://deepseek-watch.pages.dev",
  });
  const group = (html.match(/<section class="detail-group">[\s\S]*?<\/section>/) || [""])[0];
  assert.ok((group.match(/<p>/g) || []).length >= 2, "长段落应按句拆成多段");
});

test("copy buttons declare what they copy", () => {
  const html = renderAbout({ statuses: [], siteUrl: "https://deepseek-watch.pages.dev" });
  assert.match(html, /data-copy-name="订阅地址"/);
  assert.match(html, /data-copy-name="群号"/);
});

test("header QQ button keeps its icon and shows a copy tooltip", () => {
  const css = readFileSync(new URL("../public/site.css", import.meta.url), "utf8");
  const block = (css.match(/\.qq-button \{\r?\n  position: relative;[\s\S]*?\n\}/) || [""])[0];
  assert.ok(block, "standalone .qq-button rule should exist");
  assert.match(block, /width:\s*36px/);
  assert.match(css, /\.qq-button:hover \.qq-tip/);
});

test("detail styles wrap long tokens and provide print rules", () => {
  const css = readFileSync(new URL("../public/site.css", import.meta.url), "utf8");
  const wrapBlock = (css.match(/\.feed-head h2,[\s\S]*?\n\}/) || [""])[0];
  assert.match(wrapBlock, /\.detail-group p/);
  assert.match(wrapBlock, /\.detail-group h3/);
  assert.match(wrapBlock, /\.tldr-list li/);
  assert.match(css, /@media print/);
});

test("status detail column wraps long errors instead of ellipsizing them", () => {
  const css = readFileSync(new URL("../public/site.css", import.meta.url), "utf8");
  const block = (css.match(/\.status-detail \{[^}]*\}/) || [""])[0];
  assert.ok(block, "base .status-detail rule should exist");
  assert.doesNotMatch(block, /text-overflow:\s*ellipsis/);
  assert.doesNotMatch(block, /white-space:\s*nowrap/);
  assert.match(block, /overflow-wrap:\s*anywhere/);
});
