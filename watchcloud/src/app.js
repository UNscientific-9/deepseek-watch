import { SOURCE_ORDER, isLevel, isSource, sourceLabel } from "./constants.js";
import {
  getAdjacentEvents,
  getEventBySlug,
  getEventChanges,
  getMeta,
  getMetaMany,
  getQualityCounts,
  getRssDaily,
  getSourceCounts,
  getStatuses,
  getStatusHistory,
  getTotalEventCount,
  incrementRssDaily,
  listEvents,
  listSitemapEvents,
  countEventsBetween,
  getEventDailyCounts,
} from "./db.js";
import { handleIngest, json } from "./ingest.js";
import { rateLimit } from "./rate-limit.js";
import {
  dayKey,
  layout,
  renderAbout,
  renderDetail,
  renderEventChanges,
  renderHome,
  renderNotFound,
  renderRobots,
  renderSitemap,
  renderSource,
  renderStatus,
  renderSubscribe,
} from "./render.js";
import { renderFeed } from "./rss.js";

const SECURITY_HEADERS = {
  "content-security-policy":
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "permissions-policy": "camera=(), microphone=(), geolocation=()",
  "cross-origin-opener-policy": "same-origin",
  "strict-transport-security": "max-age=31536000; includeSubDomains",
};

function secure(response) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
    if (!headers.has(name)) headers.set(name, value);
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function html(body, status = 200) {
  return secure(
    new Response(body, {
      status,
      headers: {
        "content-type": "text/html; charset=utf-8",
        // 200 页面缓存 60 秒；404 不缓存，避免刚出现的事件链接被旧 404 挡住；
        // 5xx 一律 no-store，D1 瞬断恢复后用户 / 爬虫不能继续看到缓存的错误页（P2-18）。
        "cache-control":
          status === 200 ? "public, max-age=60, stale-while-revalidate=300" : "no-store",
        ...(status === 503 ? { "retry-after": "60" } : {}),
      },
    }),
  );
}

function text(body, contentType, status = 200, cacheControl = "no-store", extraHeaders = {}) {
  return secure(
    new Response(body, {
      status,
      headers: {
        "content-type": contentType,
        "cache-control": cacheControl,
        ...extraHeaders,
      },
    }),
  );
}

function hashString(value) {
  let hash = 5381;
  const raw = String(value || "");
  for (let index = 0; index < raw.length; index += 1) {
    hash = ((hash * 33) ^ raw.charCodeAt(index)) >>> 0;
  }
  return hash.toString(36);
}

function siteUrl(request, env) {
  const configured = String(env.SITE_URL || "").trim();
  if (configured) return configured.replace(/\/$/, "");
  return new URL(request.url).origin;
}

// 状态数据读取失败时，返回显式的 error 标记和一份“全部来源异常”的占位状态。
// 不能悄悄退化成空数组：空数组在状态条上显示成 WAIT（待采集），看起来像“还没开始”，
// 实际是数据库读失败，会掩盖整条监控链路已经断掉。
function errorStatuses() {
  return SOURCE_ORDER.map((source) => ({
    source,
    label: sourceLabel(source),
    lastCheckedTs: 0,
    lastSuccessTs: 0,
    lastEventTs: 0,
    status: "error",
    detail: "状态数据读取失败（D1）",
    updatedTs: 0,
  }));
}

async function commonData(env) {
  try {
    const [statuses, counts] = await Promise.all([getStatuses(env.DB), getSourceCounts(env.DB)]);
    return { statuses, counts, error: false };
  } catch (error) {
    console.error("common data failed", error);
    return { statuses: errorStatuses(), counts: {}, error: true };
  }
}

function totalFromCounts(counts) {
  return Object.values(counts || {}).reduce((sum, value) => sum + Number(value || 0), 0);
}

function parsePage(url) {
  const page = Math.max(1, Math.min(9999, Number.parseInt(url.searchParams.get("page") || "1", 10) || 1));
  return page;
}

// 页码越界时回到最后一页，避免用户停在「还没有信号」的空页和 page=9998 的上一页链接上。
function clampPage(url, page, total, limit) {
  const maxPage = Math.max(1, Math.ceil(Number(total || 0) / limit));
  if (page <= maxPage) return null;
  const target = new URL(url);
  if (maxPage > 1) target.searchParams.set("page", String(maxPage));
  else target.searchParams.delete("page");
  return secure(
    new Response(null, {
      status: 302,
      headers: { location: target.toString(), "cache-control": "no-store" },
    }),
  );
}

function qValue(url) {
  return (url.searchParams.get("q") || "").trim().slice(0, 80);
}

function levelValue(url) {
  const raw = url.searchParams.get("level");
  return isLevel(raw) ? raw : "";
}

// 推送端 meta 里的时间可能是 ISO 字符串或 epoch 秒；统一转成秒，坏值返回 null。
function parseTimestamp(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  const numeric = Number(raw);
  if (Number.isFinite(numeric) && numeric > 0) return Math.trunc(numeric);
  const millis = Date.parse(raw);
  return Number.isFinite(millis) ? Math.floor(millis / 1000) : null;
}

function metaNumber(entry) {
  const raw = String(entry?.value ?? "").trim();
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? Math.trunc(value) : null;
}

function metaJson(entry, expected) {
  const raw = String(entry?.value ?? "").trim();
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (expected === "array") return Array.isArray(parsed) ? parsed : null;
    if (expected === "object") {
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    }
    return parsed;
  } catch {
    return null;
  }
}

// 状态页「采集与推送」面板的数据都来自 meta；任何一项读不到都返回 null，由模板显示「暂无」。
function pushPanelData(metaValues, qualityByDb) {
  const meta = metaValues || {};
  const qualityFromDb = qualityByDb && Object.keys(qualityByDb).length ? qualityByDb : null;
  const qualityFromPushRaw = metaJson(meta.quality_counts, "object");
  const qualityFromPush =
    qualityFromPushRaw && Object.keys(qualityFromPushRaw).length ? qualityFromPushRaw : null;
  const sourceCountsRaw = metaJson(meta.source_counts, "object");
  const sourceCounts =
    sourceCountsRaw && Object.keys(sourceCountsRaw).length ? sourceCountsRaw : null;
  const snapshot = metaJson(meta.models_snapshot, "object");
  const checkedTs = snapshot ? parseTimestamp(snapshot.checked_at) : null;
  const failStreak = Number(snapshot?.catalog_fail_streak);
  const incompleteStreak = Number(snapshot?.catalog_incomplete_streak);
  const lastError = String(snapshot?.catalog_last_error || "").trim().slice(0, 200);
  return {
    lastPushTs: parseTimestamp(meta.last_push_ts?.value) ?? parseTimestamp(meta.last_ingest_at?.value),
    eventCount: metaNumber(meta.event_count),
    newEventCount: metaNumber(meta.new_event_count),
    pushedCount: metaNumber(meta.pushed_count),
    rejectedCount: metaNumber(meta.rejected_count),
    deadLetterCount: metaNumber(meta.dead_letter_count),
    backlogCount: metaNumber(meta.backlog_count),
    sourceCounts,
    collectionErrors: metaJson(meta.collection_errors, "array"),
    qualityCounts: qualityFromDb || qualityFromPush,
    catalog:
      checkedTs === null
        ? null
        : {
            checkedTs,
            failStreak: Number.isFinite(failStreak) && failStreak >= 0 ? Math.trunc(failStreak) : null,
            incompleteStreak:
              Number.isFinite(incompleteStreak) && incompleteStreak >= 0
                ? Math.trunc(incompleteStreak)
                : null,
            lastError,
          },
  };
}

async function home(request, env, url) {
  const q = qValue(url);
  const activeLevel = levelValue(url);
  const page = parsePage(url);
  const limit = 20;
  const [{ items, total }, common] = await Promise.all([
    listEvents(env.DB, { q, level: activeLevel || null, limit, offset: (page - 1) * limit }),
    commonData(env),
  ]);
  const totalAll = totalFromCounts(common.counts);
  const nowTs = Math.floor(Date.now() / 1000);
  const redirected = clampPage(url, page, total, limit);
  if (redirected) return redirected;
  return html(
    renderHome({
      events: items,
      total,
      totalAll,
      nowTs,
      limit,
      offset: (page - 1) * limit,
      q,
      activeSource: "",
      activeLevel,
      statuses: common.statuses,
      counts: common.counts,
      siteUrl: siteUrl(request, env),
    }),
  );
}

async function sourcePage(request, env, url, source) {
  const activeLevel = levelValue(url);
  const q = qValue(url);
  const page = parsePage(url);
  const limit = 20;
  const [{ items, total }, common, modelsMeta] = await Promise.all([
    listEvents(env.DB, { source, q, level: activeLevel || null, limit, offset: (page - 1) * limit }),
    commonData(env),
    source === "fingerprint"
      ? getMeta(env.DB, "models_snapshot").catch(() => null)
      : Promise.resolve(null),
  ]);
  const redirected = clampPage(url, page, total, limit);
  if (redirected) return redirected;
  let modelsSnapshot = null;
  if (modelsMeta && modelsMeta.value) {
    try {
      const parsed = JSON.parse(modelsMeta.value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        modelsSnapshot = parsed;
      }
    } catch {
      // 快照损坏时页面仍然要能打开；下面会显示“暂时没有快照”。
    }
  }
  return html(
    renderSource({
      source,
      events: items,
      total,
      limit,
      offset: (page - 1) * limit,
      activeLevel,
      q,
      counts: common.counts,
      siteUrl: siteUrl(request, env),
      modelsSnapshot,
    }),
  );
}

async function detailPage(request, env, url, slug) {
  const event = await getEventBySlug(env.DB, slug);
  if (!event) return notFound(request, env);
  // 详情页不再读 source_status / source_counts：模板用不到（S-A1，每请求少两次 D1 查询）。
  const [{ previous, next }, changes] = await Promise.all([
    getAdjacentEvents(env.DB, event),
    getEventChanges(env.DB, event.id),
  ]);
  return html(
    renderDetail({
      event,
      previous,
      next,
      changes,
      siteUrl: siteUrl(request, env),
    }),
  );
}

async function changesPage(request, env, url, slug) {
  const event = await getEventBySlug(env.DB, slug);
  if (!event) return notFound(request, env);
  const changes = await getEventChanges(env.DB, event.id);
  return html(
    renderEventChanges({
      event,
      changes,
      siteUrl: siteUrl(request, env),
      showAll: url.searchParams.get("all") === "1",
    }),
  );
}

async function statusPage(request, env) {
  const nowTs = Math.floor(Date.now() / 1000);
  const todayStart = nowTs - ((nowTs + 8 * 3600) % 86400);
  const weekStart = todayStart - 6 * 86400;
  const [common, daily, eventDaily, metaValues, qualityByDb, todayEvents, statusHistory] = await Promise.all([
    commonData(env),
    getRssDaily(env.DB).catch(() => []),
    getEventDailyCounts(env.DB, weekStart).catch(() => []),
    getMetaMany(env.DB, [
      "last_ingest_at",
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
      "models_snapshot",
    ]).catch(() => ({})),
    getQualityCounts(env.DB).catch(() => ({})),
    countEventsBetween(env.DB, todayStart, todayStart + 86400).catch(() => 0),
    getStatusHistory(env.DB).catch(() => ({})),
  ]);
  if (common.error) {
    // 状态页的全部内容都来自 D1；读不到时不能伪装成「正常但没数据」的 200。
    return text(
      "状态数据暂时不可用，请稍后重试。",
      "text/plain; charset=utf-8",
      503,
      "no-store",
      { "retry-after": "60" },
    );
  }
  const totalEvents = totalFromCounts(common.counts);
  const lastIngest = parseTimestamp(metaValues.last_ingest_at?.value);
  return html(
    renderStatus({
      statuses: common.statuses,
      statusHistory,
      daily,
      eventDaily,
      totalEvents,
      counts: common.counts,
      lastIngest: lastIngest || 0,
      pushMeta: pushPanelData(metaValues, qualityByDb),
      todayEvents,
      nowTs,
      siteUrl: siteUrl(request, env),
    }),
  );
}

async function aboutPage(request, env) {
  return html(renderAbout({ siteUrl: siteUrl(request, env) }));
}

const RSS_RECORDED = new Set();

async function feed(request, env, context, source = "") {
  const base = siteUrl(request, env);
  // RSS 要带 DSH 发版的完整 release note，必须把 payload 一起取回；
  // 列表页仍走轻量字段，不受影响。
  const { items } = await listEvents(env.DB, {
    source,
    minLevel: 1,
    limit: 100,
    offset: 0,
    includePayload: true,
  });
  const nowTs = Math.floor(Date.now() / 1000);
  // 每个 isolate、每天、每个 feed 只记一次，避免每个 RSS 请求都写 D1（写放大/刷库）。
  const rssKey = `${dayKey(nowTs)}:${source || "all"}`;
  if (!RSS_RECORDED.has(rssKey)) {
    RSS_RECORDED.add(rssKey);
    const record = incrementRssDaily(env.DB, dayKey(nowTs), source || "all", nowTs).catch((error) => {
      console.error("rss stats failed", error);
    });
    // wrangler pages dev 在响应结束后会立即回收 isolate，waitUntil 里的 D1 写入不稳定；
    // 本地开发（CF_PAGES_BRANCH=local）直接 await，生产环境仍用 waitUntil，不阻塞阅读器。
    const localDev = String(env.CF_PAGES_BRANCH || "") === "local";
    if (context && typeof context.waitUntil === "function" && !localDev) {
      context.waitUntil(record);
    } else {
      await record;
    }
  }
  const body = renderFeed({ events: items, source, siteUrl: base });
  const etag = `W/"${hashString(body)}"`;
  if (request.headers.get("if-none-match") === etag) {
    return secure(
      new Response(null, {
        status: 304,
        headers: {
          "cache-control": "no-store, max-age=0",
          etag,
        },
      }),
    );
  }
  return text(
    body,
    "application/rss+xml; charset=utf-8",
    200,
    "no-store, max-age=0",
    { etag },
  );
}

async function sitemap(request, env) {
  const base = siteUrl(request, env);
  let items;
  try {
    // 轻量查询只取 slug / changed_ts；5000 条约几百 KB，不会把 payload 全读回来。
    items = await listSitemapEvents(env.DB, 5000);
  } catch (error) {
    // 读失败时不能返回只含固定页面的 200 空图：爬虫 / IndexNow 会当成
    // 「站点没有内容」并缓存一小时。503 + no-store 让调用方稍后重试。
    console.error("sitemap failed", error);
    return text(
      "Sitemap temporarily unavailable",
      "text/plain; charset=utf-8",
      503,
      "no-store",
      { "retry-after": "300" },
    );
  }
  return text(renderSitemap(items, base), "application/xml; charset=utf-8", 200, "public, max-age=3600");
}

const INGEST_STALE_SECONDS = 15 * 60;
// 指纹 30 分钟一轮，页面 15 分钟一轮；90 分钟还没更新就算采集端停摆，
// 但推送器自己每 5 分钟仍会刷新 last_ingest_at，所以必须单独看 source_status。
const SOURCE_STALE_SECONDS = 90 * 60;
// 模型目录名义上每 5 分钟检查一次；超过 24 小时没刷新就认为站点展示的目录已经过期（P2-19）。
const CATALOG_STALE_SECONDS = 24 * 60 * 60;

async function health(env) {
  const now = Date.now();
  const nowTs = Math.floor(now / 1000);
  const tokenConfigured = Boolean(String(env.INGEST_TOKEN || "").trim());
  const problems = [];
  let eventCount = 0;
  let lastIngestTs = 0;
  let statuses = [];
  let catalogAgeSeconds = null;
  let catalogStale = false;
  let catalogCheckedAt = null;
  let catalogFailStreak = null;

  try {
    const [count, meta, catalogMeta, rows] = await Promise.all([
      getTotalEventCount(env.DB),
      getMeta(env.DB, "last_ingest_at"),
      getMeta(env.DB, "models_snapshot"),
      getStatuses(env.DB),
    ]);
    eventCount = count;
    lastIngestTs = Number.parseInt(String(meta?.value ?? ""), 10) || 0;
    statuses = Array.isArray(rows) ? rows : [];
    try {
      const snapshot = JSON.parse(String(catalogMeta?.value || ""));
      const checkedTs = parseTimestamp(snapshot?.checked_at);
      if (checkedTs !== null) {
        catalogCheckedAt = new Date(checkedTs * 1000).toISOString();
        catalogAgeSeconds = Math.max(0, nowTs - checkedTs);
        catalogStale = catalogAgeSeconds > CATALOG_STALE_SECONDS;
      }
      const failStreak = Number(snapshot?.catalog_fail_streak);
      if (Number.isFinite(failStreak) && failStreak >= 0) catalogFailStreak = Math.trunc(failStreak);
    } catch {
      // 快照损坏按「暂无目录信息」处理，不额外制造 503。
    }
  } catch (error) {
    console.error("health check failed", error);
    problems.push("database_unavailable");
  }

  const ageSeconds = lastIngestTs > 0 ? Math.max(0, nowTs - lastIngestTs) : null;
  if (!problems.length) {
    if (!lastIngestTs) problems.push("ingest_missing");
    else if (ageSeconds > INGEST_STALE_SECONDS) problems.push("ingest_stale");
  }

  // 采集端全挂时 watchsite 仍会推送空事件 + error 状态并刷新 last_ingest_at。
  // 这里补上来源健康度：全部 error，或所有来源都超过 90 分钟未更新，都算不健康。
  const checkedTs = statuses.map((item) => Number(item.lastCheckedTs) || 0).filter(Boolean);
  const newestSourceTs = checkedTs.length ? Math.max(...checkedTs) : 0;
  const sourcesReport = statuses.map((item) => ({
    source: item.source,
    status: item.status,
    lastCheckedAt:
      Number(item.lastCheckedTs) > 0
        ? new Date(Number(item.lastCheckedTs) * 1000).toISOString()
        : null,
    lastCheckedTs: Number(item.lastCheckedTs) || 0,
    detail: item.detail,
  }));
  if (!problems.length && statuses.length) {
    if (statuses.every((item) => item.status === "error")) {
      problems.push("sources_all_error");
    } else if (newestSourceTs && nowTs - newestSourceTs > SOURCE_STALE_SECONDS) {
      problems.push("sources_stale");
    }
  }
  // 目录陈旧单独报：推送链路可能正常，但站点展示的模型目录已经是旧快照（P2-19）。
  if (!problems.length && catalogStale) problems.push("catalog_stale");
  // 连续多次检查失败也要进健康检查，避免「上次成功还新鲜」掩盖目录接口已经挂掉（P2-19）。
  if (!problems.length && catalogFailStreak !== null && catalogFailStreak >= 3) {
    problems.push("catalog_failing");
  }
  if (!tokenConfigured) problems.push("ingest_token_missing");

  return json(
    {
      ok: problems.length === 0,
      service: "deepseek-watch",
      time: new Date(now).toISOString(),
      // 推送端每次成功 ingest 都会写 meta.last_ingest_at（epoch 秒），这里转成 ISO 便于告警系统直接读取。
      lastIngestAt: lastIngestTs > 0 ? new Date(lastIngestTs * 1000).toISOString() : null,
      ageSeconds,
      eventCount,
      catalogCheckedAt,
      catalogAgeSeconds,
      catalogStale,
      catalogFailStreak,
      sources: sourcesReport,
      ...(problems.length ? { error: problems[0], problems } : {}),
    },
    problems.length ? 503 : 200,
  );
}

function methodNotAllowed() {
  return text("Method Not Allowed", "text/plain; charset=utf-8", 405);
}

export async function onRequest(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method.toUpperCase();

  try {
    // 先做应用层限流，恶意请求不进入 D1；ingest 单独用更严格的桶。
    const rateKind =
      path === "/internal/ingest"
        ? "ingest"
        : path === "/healthz"
          ? "health"
          : path.startsWith("/feed")
            ? "feed"
            : "page";
    const limit = rateLimit(request, rateKind);
    if (limit.limited) {
      return text("Too Many Requests", "text/plain; charset=utf-8", 429, "no-store", {
        "retry-after": String(limit.retryAfter),
      });
    }

    if (path === "/internal/ingest") {
      if (method !== "POST") return methodNotAllowed();
      return secure(await handleIngest(request, env));
    }
    if (method !== "GET" && method !== "HEAD") return methodNotAllowed();

    if (path === "/healthz") return secure(await health(env));
    if (path === "/") return await home(request, env, url);
    if (path === "/about") return await aboutPage(request, env);
    if (path === "/subscribe") return html(renderSubscribe({ siteUrl: siteUrl(request, env) }));
    if (path === "/status") return await statusPage(request, env);
    if (path === "/feed.xml") return await feed(request, env, context);
    if (path === "/robots.txt") {
      return text(renderRobots(siteUrl(request, env)), "text/plain; charset=utf-8", 200, "public, max-age=3600");
    }
    if (path === "/sitemap.xml") return await sitemap(request, env);

    const feedMatch = path.match(/^\/feed\/([a-z-]+)\.xml$/);
    if (feedMatch) {
      if (!isSource(feedMatch[1])) return notFound(request, env);
      return await feed(request, env, context, feedMatch[1]);
    }
    const sourceMatch = path.match(/^\/source\/([a-z-]+)$/);
    if (sourceMatch) {
      if (!isSource(sourceMatch[1])) return notFound(request, env);
      return await sourcePage(request, env, url, sourceMatch[1]);
    }
    const changesMatch = path.match(/^\/e\/([a-z0-9-]+)\/changes$/);
    if (changesMatch) return await changesPage(request, env, url, changesMatch[1]);
    const eventMatch = path.match(/^\/e\/([a-z0-9-]+)$/);
    if (eventMatch) return await detailPage(request, env, url, eventMatch[1]);
    return notFound(request, env);
  } catch (error) {
    console.error("request failed", error);
    // 错误页不需要状态数据：layout 已不接收 statuses，避免故障时再打两次 D1。
    return html(
      layout({
        title: "暂时不可用",
        description: "站点暂时不可用。",
        path,
        body: `<main class="not-found"><p class="eyebrow">503 / SERVICE PAUSED</p><h1>这条观测线<br>暂时安静。</h1><p>后端没有正常响应。请稍后再试。</p><a class="button-primary" href="/">重新连接</a></main>`,
        activeNav: "",
        siteUrl: siteUrl(request, env),
        noIndex: true,
      }),
      503,
    );
  }
}

async function notFound(request, env) {
  return html(renderNotFound({ siteUrl: siteUrl(request, env) }), 404);
}
