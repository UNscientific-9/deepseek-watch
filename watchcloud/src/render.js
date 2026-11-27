import {
  LEVELS,
  REPO_URL,
  SITE_NAME,
  SITE_NAME_EN,
  SITE_TAGLINE,
  SOURCES,
  SOURCE_ORDER,
  STATUS_LABELS,
  levelLabel,
  sourceLabel,
} from "./constants.js";
import { ASSET_VERSION, SITE_COMMIT, SITE_VERSION } from "./version.js";

const BEIJING = "Asia/Shanghai";
const QQ_GROUP_NUMBER = "721260245"; // 项目公开交流群；如需隐藏，改为空字符串

// Intl 格式器只构造一次：列表/详情页每次请求要格式化几十到几百个时间戳。
const DATE_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  timeZone: BEIJING,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const DAY_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: BEIJING,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const NUMBER_FORMATTER = new Intl.NumberFormat("zh-CN");

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function attr(value) {
  return esc(value);
}

export function headingText(value) {
  return String(value ?? "").trim().replace(/[，。]+$/u, "");
}

// 「内部调整」不是一个可靠结论：它只表示本轮没有抓到可读的界面变化，
// 既可能是内部调整，也可能是证据覆盖不足。对读者统一改成「未捕获到」，
// 避免把抓取能力的边界说成官方的实际状态。
const INTERNAL_ADJUSTMENT_TITLE = "本次为内部调整，未发现界面变化";

function isUncapturedChange(event) {
  const quality = String(event?.quality ?? "").trim();
  const title = String(event?.title ?? "").trim();
  return (
    quality === "internal" ||
    title === INTERNAL_ADJUSTMENT_TITLE ||
    title.startsWith("未捕获到用户可见变化")
  );
}

// 证据包里已经有具体资源/文件变化时，「internal」不等于「什么都没抓到」。
// 展示层据此保留事件自己的技术性标题与说明，而不是强行改回笼统的「未捕获」。
function technicalChangeDetail(event) {
  const frontend = event?.payload?.frontend;
  if (!frontend || typeof frontend !== "object" || Array.isArray(frontend)) return null;
  const totals = frontend.totals && typeof frontend.totals === "object" ? frontend.totals : {};
  const changed = Number(totals.changed || 0);
  const added = Number(totals.added || 0);
  const removed = Number(totals.removed || 0);
  const files = Array.isArray(frontend.changed_files) ? frontend.changed_files : [];
  const diffs = Array.isArray(frontend.file_diffs) ? frontend.file_diffs : [];
  if (!changed && !added && !removed && !files.length && !diffs.length) return null;
  return { totals, files, diffs };
}

function isTechnicalOnlyTitle(title) {
  return String(title ?? "").trim().startsWith("仅有技术变化");
}

// 历史事件（修复前生成）标题仍是「未捕获」，但 payload.frontend 里已经有
// main.js 字节变化。展示层据此补一条具体的技术标题，部署后旧事件也能看懂。
function technicalTitleFromEvidence(event) {
  // 只有「证据完整、确认没有用户文案」的 internal 才补技术标题；
  // partial 的未知部分还没核对，不能替它下「仅有技术变化」的结论。
  if (eventQuality(event) !== "internal") return "";
  const detail = technicalChangeDetail(event);
  if (!detail) return "";
  const frontend = event.payload.frontend;
  const js = frontend.main_js && typeof frontend.main_js === "object" ? frontend.main_js : {};
  const css = frontend.main_css && typeof frontend.main_css === "object" ? frontend.main_css : {};
  const oldJs = Number(js.old_size || 0);
  const newJs = Number(js.new_size || 0);
  if ((oldJs || newJs) && oldJs !== newJs) {
    return `仅有技术变化：main.js ${formatNumber(oldJs)} → ${formatNumber(newJs)} 字节`;
  }
  const oldCss = Number(css.old_size || 0);
  const newCss = Number(css.new_size || 0);
  if ((oldCss || newCss) && oldCss !== newCss) {
    return `仅有技术变化：main.css ${formatNumber(oldCss)} → ${formatNumber(newCss)} 字节`;
  }
  const names = detail.files
    .map((item) => String(item?.name || "").trim())
    .filter(Boolean);
  if (names.length) {
    return `仅有技术变化：${names.slice(0, 3).join("、")} 有内容变化`;
  }
  const changed = Number(detail.totals.changed || 0);
  const added = Number(detail.totals.added || 0);
  const removed = Number(detail.totals.removed || 0);
  if (changed || added || removed) {
    const parts = [];
    if (changed) parts.push(`内容变化 ${formatNumber(changed)}`);
    if (added) parts.push(`新增 ${formatNumber(added)}`);
    if (removed) parts.push(`移除 ${formatNumber(removed)}`);
    return `仅有技术变化：资源清单 ${formatNumber(detail.totals.manifest)} 个中有 ${parts.join("、")}`;
  }
  return "";
}

export function displayEventTitle(event) {
  if (!isUncapturedChange(event)) return event?.title;
  const title = String(event?.title ?? "").trim();
  if (isTechnicalOnlyTitle(title)) return title;
  return technicalTitleFromEvidence(event) || "未捕获到用户可见变化（可能为内部调整）";
}

export function displayEventSummary(event) {
  if (!isUncapturedChange(event)) return event?.summary;
  const summary = String(event?.summary ?? "").trim();
  if (
    summary &&
    (summary.startsWith("仅有技术变化") || summary.includes("仅检测到技术变化"))
  ) {
    return summary;
  }
  const derived = technicalTitleFromEvidence(event);
  if (derived) {
    return `仅检测到技术变化：${derived.replace(/^仅有技术变化：/, "")}；未提取到用户可见文案。`;
  }
  return "本轮没有抓到可读的界面变化；可能是内部调整，也可能是抓取覆盖不足。";
}

// 历史记录里有一批旧口径的 3 级（DSH 历史补录、只有版本号变化、明确无可见变化）。
// 数据库不改；页面、RSS、相邻记录统一按这个函数得到展示级别。条件与 db.js 的
// DISPLAY_LEVEL_SQL 保持一致，避免列表筛选和卡片文字打架。
export function displayEventLevel(event) {
  const level = Number(event?.level ?? 0);
  const quality = String(event?.quality ?? "").trim();
  const title = String(event?.title ?? "").trim();
  const summary = String(event?.summary ?? "").trim();
  const action = String(event?.action ?? "").trim();
  if (isUncapturedChange(event)) return 1;
  if (action.includes("历史版本补录")) return 1;
  if (title.includes("前端版本更新") && (!quality || quality === "unknown")) return 1;
  if (title.includes("图片预览与图表显示相关部分有调整")) return 2;
  if (title.includes("界面变化无法确认")) return 2;
  if ((!quality || quality === "unknown") && summary.includes("语言包未核对")) return 2;
  return Number.isFinite(level) ? level : 0;
}

const KIND_LABELS = {
  page_change: "页面变化",
  page_text: "正文变化",
  html: "页面结构变化",
  text: "文字变化",
  new_page: "新增页面",
  docwatch: "文档监控",
  update: "内容更新",
  commit: "仓库提交",
  assets: "前端资源",
  catalog: "模型目录",
  platform_banner: "平台公告",
  model_version: "模型版本",
  fingerprint: "指纹变化",
  dsh_release: "DSH 发版",
  dsh_commit: "DSH 提交",
  archive_entry: "存档条目",
};

export function kindLabel(kind) {
  const key = String(kind ?? "").trim();
  if (!key) return "信号";
  return KIND_LABELS[key] || "其它变化";
}

function dateParts(formatter, date) {
  return Object.fromEntries(
    formatter.formatToParts(date).map((part) => [part.type, part.value]),
  );
}

export function formatDate(ts, withTime = true) {
  if (!ts) return "—";
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const values = dateParts(DATE_TIME_FORMATTER, date);
  // 全站统一北京时间（UTC+8）的机器可读格式，不再混用点号 / ISO Z。
  return withTime
    ? `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}`
    : `${values.year}-${values.month}-${values.day}`;
}

export function formatDay(ts) {
  return formatDate(ts, false);
}

export function formatNumber(value) {
  return NUMBER_FORMATTER.format(Number(value) || 0);
}

export function dayKey(ts) {
  const values = dateParts(DAY_FORMATTER, new Date(Number(ts) * 1000));
  return `${values.year}-${values.month}-${values.day}`;
}

function svg(name, className = "") {
  const icons = {
    arrow: '<path d="M4 12h15M13 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="square"/>',
    rss: '<path d="M5 5a14 14 0 0 1 14 14M5 12a7 7 0 0 1 7 7" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="5.5" cy="18.5" r="2" fill="currentColor"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m16 16 5 5" fill="none" stroke="currentColor" stroke-width="1.6"/>',
    chat: '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H9l-5 4v-4.6A2.5 2.5 0 0 1 4 12.5Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="8.5" cy="9" r="1" fill="currentColor"/><circle cx="12" cy="9" r="1" fill="currentColor"/><circle cx="15.5" cy="9" r="1" fill="currentColor"/>',
    moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" fill="none" stroke="currentColor" stroke-width="1.5"/>',
    sun: '<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
    up: '<path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  };
  return `<svg class="${className}" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ""}</svg>`;
}

function navLink(href, label, active) {
  return `<a class="nav-link${active ? " is-active" : ""}" href="${attr(href)}"${active ? ' aria-current="page"' : ""}>${esc(label)}</a>`;
}

function statusDot(status) {
  return `<span class="status-dot status-${attr(status)}" aria-hidden="true"></span>`;
}

const DATA_STALE_SECONDS = 35 * 60;

function dataFreshness(statuses, nowTs = Math.floor(Date.now() / 1000)) {
  const newestTs = (statuses || []).reduce(
    (max, item) => Math.max(max, Number(item.lastCheckedTs) || 0),
    0,
  );
  if (!newestTs) return { stale: false, newestTs: 0, ageMinutes: 0 };
  const ageSeconds = Math.max(0, Number(nowTs) - newestTs);
  return {
    stale: ageSeconds > DATA_STALE_SECONDS,
    newestTs,
    ageMinutes: Math.floor(ageSeconds / 60),
  };
}

function statusStrip(statuses, freshness = dataFreshness(statuses)) {
  const map = new Map((statuses || []).map((item) => [item.source, item]));
  const stale = Boolean(freshness && freshness.stale);
  const overall = stale
    ? "error"
    : statuses.some((status) => status.status === "error")
      ? "error"
      : statuses.some((status) => status.status === "warn")
        ? "warn"
        : statuses.length
          ? "ok"
          : "unknown";
  const overallLabel = stale
    ? "STALE"
    : { ok: "LIVE", warn: "DELAY", error: "ERROR", unknown: "WAIT" }[overall];
  const label = stale
    ? `监控状态：数据已停更 ${freshness.ageMinutes} 分钟`
    : "监控状态";
  return `<div class="status-strip${stale ? " is-stale" : ""}" aria-label="${attr(label)}">
    <span class="live-mark"${stale ? ` title="已经 ${attr(freshness.ageMinutes)} 分钟没有收到新数据"` : ""}>${statusDot(overall)} ${overallLabel}</span>
    ${SOURCE_ORDER.map((source) => {
      const status = map.get(source) || { status: "unknown", lastCheckedTs: 0 };
      return `<a class="status-chip" href="/source/${source}" title="${attr(status.detail || "")}">
        ${statusDot(status.status)}
        <span>${esc(sourceLabel(source))}</span>
        <b>${esc(STATUS_LABELS[status.status] || "待采集")}</b>
      </a>`;
    }).join("")}
  </div>`;
}

function levelBadge(level) {
  const info = LEVELS[Number(level)] || LEVELS[0];
  return `<span class="level level-${Number(level)}"><span class="level-num">${Number(level)}</span>${esc(info.label)}</span>`;
}

// 质量闸门状态：只在列表 / 详情显示需要读者留意的状态。
// unknown / ok 是默认值，不打扰读者；contradiction / missing 虽然正常不会发布，
// 但 AI 关闭或旧数据仍可能带上站，必须有徽标，否则最严重的状态最不可见。
const QUALITY_LABELS = {
  partial: "待核实",
  repaired: "已按证据修正",
  internal: "仅技术变化",
  contradiction: "结论矛盾",
  missing: "证据缺失",
};
// 状态页质量分布需要覆盖全部取值，用更短的中文标签，避免一排徽标挤爆一行。
const QUALITY_DISTRIBUTION_LABELS = {
  ok: "可信",
  partial: "待核实",
  repaired: "已修正",
  internal: "仅技术",
  contradiction: "结论矛盾",
  missing: "证据缺失",
  unknown: "未标注",
};
const QUALITY_DISTRIBUTION_ORDER = [
  "ok",
  "partial",
  "repaired",
  "internal",
  "contradiction",
  "missing",
  "unknown",
];
const QUALITY_VALUES = new Set([
  "ok",
  "internal",
  "partial",
  "contradiction",
  "missing",
  "repaired",
  "unknown",
]);

function eventQuality(event) {
  const stored = String(event?.quality || "").trim();
  // 旧事件的列是 unknown，但 payload 里可能仍有质量结果；优先用白名单列值。
  const raw = QUALITY_VALUES.has(stored) && stored !== "unknown"
    ? stored
    : String(event?.payload?.quality || "").trim();
  return QUALITY_VALUES.has(raw) ? raw : "unknown";
}

function qualityBadge(quality) {
  const key = String(quality || "");
  const label = QUALITY_LABELS[key];
  if (!label) return "";
  return `<span class="quality-badge quality-${key}">${esc(label)}</span>`;
}

// 复核已完成且结论为 confirmed 时，不再显示“待核实”徽标：读者已经在标题下
// 看到“已复核”，再并排一个“待核实”会误以为复核没跑完。证据限制仍保留在
// 复核说明的“依据”里；只有 uncertain / failed 才继续显示待核实。
function qualityBadgeForEvent(event) {
  const quality = eventQuality(event);
  const recheck = event?.payload?.recheck;
  const state = String(recheck?.state || event?.recheckState || "").trim().toLowerCase();
  const verdict = String(recheck?.verdict || event?.recheckVerdict || "").trim().toLowerCase();
  if (quality === "partial" && state === "done" && verdict === "confirmed") return "";
  return qualityBadge(quality);
}

// 复核标记只用于已经做过「重评 / 自动复核」的事件；普通事件不打扰读者。
function formatReviewDate(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^\d+(?:\.\d+)?$/.test(raw)) return formatDate(Number(raw), true);
  const date = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (date) return `${date[1]}-${date[2]}-${date[3]}`;
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime())) {
    const values = dateParts(DATE_TIME_FORMATTER, parsed);
    return `${values.year}-${values.month}-${values.day}`;
  }
  return raw.slice(0, 20);
}

// 复核/重评依据可能带着历史回填时的内部口径（signals、docwatch 规则、级别维持 3），
// 在展示前统一改成读者能懂的说法；详细原文不再用 title 复制一遍，避免同一句出现两三次。
function plainReviewText(value) {
  return String(value ?? "")
    .replace(/signals/gi, "信号")
    .replace(/docwatch\s*规则/gi, "监控规则")
    .replace(/级别维持\s*(\d+)/g, "级别维持 $1 级")
    .replace(/页面\s*HTML\s*变化/gi, "页面正文变化")
    .trim();
}

function reviewNotice(event) {
  const payload = event?.payload && typeof event.payload === "object" ? event.payload : {};
  const recheck = payload.recheck && typeof payload.recheck === "object" && !Array.isArray(payload.recheck)
    ? payload.recheck
    : null;
  const regrade = payload.regrade && typeof payload.regrade === "object" && !Array.isArray(payload.regrade)
    ? payload.regrade
    : null;
  const hasRecheck = Boolean(recheck && Object.keys(recheck).length);
  const hasRegrade = Boolean(regrade && Object.keys(regrade).length);
  if (!hasRecheck && !hasRegrade) return "";

  const state = String(recheck?.state || "").trim().toLowerCase();
  let label = hasRegrade && hasRecheck ? "已重评 · 已复核" : hasRegrade ? "已重评" : "已复核";
  if (!hasRegrade && ["pending", "queued", "running"].includes(state)) label = "复核中";
  if (!hasRegrade && state === "failed") label = "复核未完成";

  const at = formatReviewDate(recheck?.at || regrade?.at || "");
  const details = [];
  if (hasRegrade) {
    const fromLevel = Number(regrade.from_level);
    const toLevel = Number(regrade.to_level);
    if (Number.isFinite(fromLevel) && Number.isFinite(toLevel) && fromLevel !== toLevel) {
      details.push(`原 ${fromLevel} 级 → ${toLevel} 级`);
    }
    const fromQuality = String(regrade.from_quality || "");
    const toQuality = String(regrade.to_quality || "");
    if (fromQuality && toQuality && fromQuality !== toQuality) {
      details.push(`可信度 ${QUALITY_DISTRIBUTION_LABELS[fromQuality] || fromQuality} → ${QUALITY_DISTRIBUTION_LABELS[toQuality] || toQuality}`);
    }
  }
  if (hasRecheck && recheck?.level_applied) {
    const fromLevel = Number(recheck.from_level);
    const toLevel = Number(recheck.to_level);
    if (Number.isFinite(fromLevel) && Number.isFinite(toLevel) && fromLevel !== toLevel) {
      details.push(`复核后 ${fromLevel} 级 → ${toLevel} 级`);
    }
  }
  if (recheck?.verdict === "uncertain") details.push("维持待核实");
  if (recheck?.verdict === "contradicted") details.push("已按证据修正");
  // 只展示复核/重评记录自己写的依据；没有就不硬回退到 level_decision.reason，
  // 否则复核区会和下方「判级依据」逐字重复，还会把内部术语顶到页面最上方。
  const rawBasis = String(recheck?.basis || regrade?.basis || "").trim();
  const basisText = plainReviewText(rawBasis);
  const basis = basisText ? `依据：${basisText.length > 120 ? `${basisText.slice(0, 120)}…` : basisText}` : "";
  const parts = [`<span class="recheck-label">${esc(label)}</span>`];
  if (at) parts.push(`<span class="mono">${esc(at)}</span>`);
  if (details.length) parts.push(`<span>${esc(details.join(" · "))}</span>`);
  if (basis) parts.push(`<span class="recheck-basis">${esc(basis)}</span>`);
  return `<p class="recheck-note">${parts.join(' <span class="recheck-sep" aria-hidden="true">·</span> ')}</p>`;
}

function formatLevelDecisionSource(source) {
  return {
    rule: "规则",
    ai: "模型建议",
    guard: "质量闸门",
    gate: "质量闸门",
    recheck: "发布后复核",
  }[String(source || "")] || "未注明";
}

// 判级依据：只展示最终级别、规则原始级别、判级来源和一句人话理由。
// 模型名、置信度、模型原始理由都不对外展示。
function renderLevelDecision(payload) {
  const decision = payload?.level_decision;
  const recheck = payload?.recheck && typeof payload.recheck === "object" && !Array.isArray(payload.recheck)
    ? payload.recheck
    : null;
  const recheckLevelValue = recheck?.level_applied ? Number(recheck?.to_level) : NaN;
  const recheckLevel = Number.isFinite(recheckLevelValue) ? recheckLevelValue : null;
  if ((!decision || typeof decision !== "object" || Array.isArray(decision)) && recheckLevel === null) return "";
  const finalValue = recheckLevel ?? decision?.final;
  const ruleValue = decision?.rule;
  const hasFinal = finalValue !== null && finalValue !== undefined && Number.isFinite(Number(finalValue));
  const hasRule = ruleValue !== null && ruleValue !== undefined && Number.isFinite(Number(ruleValue));
  const reason = plainReviewText(
    recheckLevel !== null
      ? recheck?.level_suggestion?.reason || recheck?.reason
      : decision?.reason,
  );
  if (!hasFinal && !hasRule && !reason) return "";
  const rows = [];
  if (hasFinal) rows.push(`<div><dt>最终级别</dt><dd>${levelBadge(Number(finalValue))}</dd></div>`);
  if (hasRule) rows.push(`<div><dt>规则原始级别</dt><dd>${Number(ruleValue)} 级</dd></div>`);
  rows.push(`<div><dt>判级来源</dt><dd>${esc(formatLevelDecisionSource(recheckLevel !== null ? "recheck" : decision?.source))}</dd></div>`);
  return `<details class="detail-section level-decision">
    <summary class="section-head"><h2 class="eyebrow">LEVEL DECISION / 判级依据</h2><span class="mono">WHY THIS LEVEL</span></summary>
    <dl class="brief-facts">${rows.join("")}</dl>
    ${reason ? `<p class="detail-copy">${esc(reason)}</p>` : ""}
  </details>`;
}

function detailLevelBlock(level, internal = false) {
  if (internal) {
    return `<div class="detail-level-block" title="这条记录没有抓到可读的界面变化，历史级别仅供参考">
            <span class="detail-level-num level-text-0">--</span>
            <span class="level level-internal">未定级</span>
            <span class="mono">UNRATED</span>
            <span class="detail-level-note">历史级别仅供参考</span>
          </div>`;
  }
  return `<div class="detail-level-block" title="级别由监控规则判定，用于决定是否推送，不等同于影响大小">
            <span class="detail-level-num level-text-${level}">${String(level).padStart(2, "0")}</span>
            ${levelBadge(level)}
            <span class="mono">${esc(LEVELS[level]?.short || "SIGNAL")}</span>
            <span class="detail-level-note">规则判定</span>
          </div>`;
}

function isoTimestamp(ts) {
  if (!ts) return "";
  const date = new Date(Number(ts) * 1000);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

function sitemapLastmod(ts) {
  const iso = isoTimestamp(ts);
  return iso ? iso.slice(0, 10) : "";
}

function jsonLdScript(data) {
  return JSON.stringify(data).replaceAll("<", "\\u003c");
}

function websiteJsonLd(siteUrl) {
  const data = {
    "@context": "https://schema.org",
    "@type": "WebSite",
    name: SITE_NAME,
    description: SITE_TAGLINE,
    inLanguage: "zh-CN",
  };
  if (siteUrl) data.url = siteUrl.replace(/\/$/, "");
  return data;
}

export function layout({
  title,
  description,
  path = "/",
  body,
  activeNav = "",
  siteUrl = "",
  noIndex = false,
  robots = "",
  type = "website",
  publishedTime = "",
  modifiedTime = "",
  jsonLd = null,
}) {
  const fullTitle = title ? `${title} · ${SITE_NAME}` : `${SITE_NAME} · ${SITE_TAGLINE}`;
  const canonical = siteUrl ? `${siteUrl.replace(/\/$/, "")}${path}` : "";
  const ogImage = siteUrl ? `${siteUrl.replace(/\/$/, "")}/og-image.png` : "";
  const metaDescription = description || SITE_TAGLINE;
  const structuredData = [
    websiteJsonLd(siteUrl),
    ...(Array.isArray(jsonLd) ? jsonLd : jsonLd ? [jsonLd] : []),
  ].filter(Boolean);
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${esc(fullTitle)}</title>
  <meta name="description" content="${attr(metaDescription)}">
  <meta name="robots" content="${attr(robots || (noIndex ? "noindex,nofollow" : "index,follow,max-image-preview:large"))}">
  ${canonical ? `<link rel="canonical" href="${attr(canonical)}">` : ""}
  <link rel="sitemap" type="application/xml" title="Sitemap" href="/sitemap.xml">
  <meta name="theme-color" content="#F4F1E9">
  <meta name="color-scheme" content="light dark">
  <meta property="og:type" content="${attr(type)}">
  <meta property="og:site_name" content="${attr(SITE_NAME)}">
  <meta property="og:locale" content="zh_CN">
  <meta property="og:title" content="${attr(fullTitle)}">
  <meta property="og:description" content="${attr(metaDescription)}">
  ${canonical ? `<meta property="og:url" content="${attr(canonical)}">` : ""}
  ${ogImage ? `<meta property="og:image" content="${attr(ogImage)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="DeepSeek 观测站：把每一次变化，变成可读的信号">` : ""}
  ${publishedTime ? `<meta property="article:published_time" content="${attr(publishedTime)}">` : ""}
  ${modifiedTime ? `<meta property="article:modified_time" content="${attr(modifiedTime)}">` : ""}
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="${attr(fullTitle)}">
  <meta name="twitter:description" content="${attr(metaDescription)}">
  ${ogImage ? `<meta name="twitter:image" content="${attr(ogImage)}">` : ""}
  <script src="/theme.js?v=${ASSET_VERSION}"></script>
  <link rel="icon" href="/favicon.svg?v=${ASSET_VERSION}" type="image/svg+xml">
  <link rel="alternate" type="application/rss+xml" title="${attr(SITE_NAME)} RSS" href="/feed.xml">
  <link rel="stylesheet" href="/site.css?v=${ASSET_VERSION}">
  <script src="/app.js?v=${ASSET_VERSION}" defer></script>
  ${structuredData.map((data) => `<script type="application/ld+json">${jsonLdScript(data)}</script>`).join("\n  ")}
</head>
<body>
  <a class="skip-link" href="#main">跳到正文</a>
  <header class="topbar">
    <div class="topbar-inner">
      <a class="brand" href="/" aria-label="${attr(SITE_NAME)} 首页">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="brand-name">${esc(SITE_NAME)}</span>
        <span class="brand-en">${esc(SITE_NAME_EN)}</span>
      </a>
      <nav class="main-nav" aria-label="主导航">
        ${navLink("/", "信号流", activeNav === "home")}
        ${navLink("/status", "监控状态", activeNav === "status")}
        ${navLink("/about", "关于", activeNav === "about")}
      </nav>
      <div class="top-actions">
        <button class="theme-toggle" type="button" data-theme-toggle aria-label="切换深色/浅色主题" title="切换深色/浅色主题">${svg("moon")}${svg("sun")}</button>
        ${QQ_GROUP_NUMBER ? `<button class="qq-button" type="button" data-copy="${attr(QQ_GROUP_NUMBER)}" data-copy-name="QQ群号" aria-label="QQ 群聊：复制群号 ${attr(QQ_GROUP_NUMBER)}">${svg("chat")}<span class="qq-tip" data-copy-tip>QQ 群聊 · 复制群号</span></button>` : ""}
        <a class="rss-button" href="/feed.xml" title="订阅全部信号">${svg("rss")}<span>RSS</span></a>
      </div>
    </div>
  </header>
  <div id="main" tabindex="-1">${body}</div>
  <footer class="footer">
    <div class="footer-inner">
      <div class="footer-brand">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <div>
          <strong>${esc(SITE_NAME)}</strong>
          <p>${esc(SITE_TAGLINE)}</p>
        </div>
      </div>
      <div class="footer-meta">
        <p><span class="mono">NO ACCOUNTS · NO COOKIES · NO TRACKERS</span></p>
        <p>只保存公开页面的变化与每日 RSS 拉取次数，不保存访问者身份。</p>
        <p>非官方项目，与 DeepSeek 及其关联公司无隶属或授权关系。</p>
        <p><a href="/subscribe">订阅方式</a> <span class="sep">/</span> <a href="/about">关于本站</a>${QQ_GROUP_NUMBER ? ` <span class="sep">/</span> <a href="/about#qq">QQ 交流群</a>` : ""} <span class="sep">/</span> <a href="${attr(REPO_URL)}" target="_blank" rel="noopener">GitHub 源码</a> <span class="sep">/</span> <a href="/status">监控状态</a></p>
        <p class="footer-version mono">v${esc(SITE_VERSION)}${SITE_COMMIT && SITE_COMMIT !== "dev" ? ` · ${esc(SITE_COMMIT)}` : ""}</p>
      </div>
    </div>
  </footer>
  <div id="copy-status" class="sr-only" role="status" aria-live="polite"></div>
</body>
</html>`;
}

function sidebar({ counts = {}, activeSource = "", activeLevel = "", q = "", basePath = "/" }) {
  const total = Object.values(counts).reduce((sum, value) => sum + Number(value || 0), 0);
  // 来源 / 级别 / 搜索词是同一组筛选条件：切换来源时也保留当前关键词和级别，
  // 否则用户在「搜关键词 → 点来源」这一步会静默丢掉另外两个条件。
  const sourceUrl = (source) => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (activeLevel !== "") params.set("level", activeLevel);
    const path = source ? `/source/${source}` : "/";
    const query = params.toString();
    return query ? `${path}?${query}` : path;
  };
  const filterUrl = (level) => {
    const params = new URLSearchParams();
    // 切换级别时保留当前搜索词，否则用户一点级别就把关键词静默清空了。
    if (q) params.set("q", q);
    if (level !== "") params.set("level", level);
    const query = params.toString();
    return query ? `${basePath}?${query}` : basePath;
  };
  return `<aside class="ledger" aria-label="筛选与来源">
    <div class="ledger-block">
      <p class="eyebrow">SECTIONS / 来源</p>
      <nav class="source-nav">
        <a class="${!activeSource ? "is-active" : ""}" href="${attr(sourceUrl(""))}"${!activeSource ? ' aria-current="page"' : ""}>全部信号 <b>${formatNumber(total)}</b></a>
        ${SOURCE_ORDER.map((source) => `<a class="${activeSource === source ? "is-active" : ""}" href="${attr(sourceUrl(source))}"${activeSource === source ? ' aria-current="page"' : ""}>
          <span>${esc(sourceLabel(source))}</span><b>${formatNumber(counts[source] || 0)}</b>
        </a>`).join("")}
      </nav>
    </div>
    <div class="ledger-block">
      <p class="eyebrow">LEVEL / 级别</p>
      <nav class="level-nav">
        <a class="${activeLevel === "" ? "is-active" : ""}" href="${attr(filterUrl(""))}"${activeLevel === "" ? ' aria-current="true"' : ""}>所有级别 <b>${formatNumber(total)}</b></a>
        ${[3, 2, 1, 0].map((level) => `<a class="${String(activeLevel) === String(level) ? "is-active" : ""}" href="${attr(filterUrl(String(level)))}"${String(activeLevel) === String(level) ? ' aria-current="true"' : ""}>
          ${levelBadge(level)} <b>${LEVELS[level].hint}</b>
        </a>`).join("")}
      </nav>
    </div>
    <div class="ledger-block">
      <p class="eyebrow">SEARCH / 搜索</p>
      <form class="search-form" action="${attr(basePath)}" method="get">
        ${activeLevel !== "" ? `<input type="hidden" name="level" value="${attr(activeLevel)}">` : ""}
        <label class="sr-only" for="q">搜索信号</label>
        <input id="q" name="q" type="search" value="${attr(q)}" placeholder="模型、页面、关键词…" autocomplete="off">
        <button type="submit" aria-label="搜索">${svg("search")}</button>
      </form>
    </div>
    <div class="ledger-block ledger-note">
      <p class="eyebrow">SOURCE NOTE</p>
      <p>这里只收录我自己监控产生的记录，不导入第三方归档历史。</p>
      <p class="mono">5 SOURCES / 5–30 MIN CYCLE</p>
    </div>
  </aside>`;
}

function signalCountText(event) {
  const count = formatNumber(event.signalCount);
  if (event.kind === "dsh_release") return `${count} 条说明`;
  if (event.kind === "dsh_commit") return `${count} 个提交`;
  return `${count} 处变化`;
}

function eventRow(event, index) {
  const date = formatDate(event.changedTs);
  const title = displayEventTitle(event);
  const summary = displayEventSummary(event);
  return `<article class="signal${isUncapturedChange(event) ? " signal-internal" : ""}">
    <div class="signal-rail" aria-hidden="true">
      <span class="signal-index">${String(index).padStart(2, "0")}</span>
      <span class="signal-dot level-${displayEventLevel(event)}"></span>
    </div>
    <div class="signal-body">
      <div class="signal-meta">
        <span class="mono source-tag">${esc(sourceLabel(event.source))}</span>
        <span class="meta-divider">/</span>
        <time datetime="${attr(new Date(event.changedTs * 1000).toISOString())}">${esc(date)}</time>
        ${event.signalCount > 1 ? `<span class="meta-divider">/</span><span>${signalCountText(event)}</span>` : ""}
      </div>
      <h3><a href="/e/${attr(event.slug)}">${esc(title)}</a></h3>
      ${summary ? `<p class="signal-summary">${esc(summary)}</p>` : ""}
    </div>
    <div class="signal-side">
      ${qualityBadgeForEvent(event)}
      ${isUncapturedChange(event) ? `<span class="level level-internal">未定级</span>` : levelBadge(displayEventLevel(event))}
      <a class="signal-arrow" href="/e/${attr(event.slug)}" aria-label="${attr(`打开详情：${title}`)}">${svg("arrow")}</a>
    </div>
  </article>`;
}

// 把「未捕获到变化」的记录从主信号流里拆出来，默认折叠成一条次级记录；
// 页面总数与分页不变，避免读者误以为这些也是已经确认的更新。
function renderSignalList(events, emptyHtml) {
  if (!events.length) return emptyHtml;
  const normal = events.filter((event) => !isUncapturedChange(event));
  const uncaptured = events.filter(isUncapturedChange);
  return `${normal.length ? normal.map(eventRow).join("") : `<p class="internal-empty">本页记录均为未捕获到用户可见变化的内部调整。</p>`}
    ${uncaptured.length ? `<details class="internal-events">
      <summary><span>未提取到用户可见文案的记录</span><b>${formatNumber(uncaptured.length)} 条</b></summary>
      <div class="internal-events-list">${uncaptured.map(eventRow).join("")}</div>
    </details>` : ""}`;
}

function emptyState({ title = "还没有信号", text = "监控已就绪。第一次采集之后，这里会出现可读的变化记录。", action = true } = {}) {
  return `<div class="empty-state">
    <span class="empty-mark" aria-hidden="true">∅</span>
    <h3>${esc(title)}</h3>
    <p>${esc(text)}</p>
    ${action ? `<a class="text-link" href="/about">它如何工作 ${svg("arrow")}</a>` : ""}
  </div>`;
}

function isoToTimestamp(value) {
  const ms = Date.parse(String(value || ""));
  return Number.isNaN(ms) ? 0 : Math.floor(ms / 1000);
}

function modelCatalogPanel(snapshot) {
  if (!snapshot) {
    return `<section class="catalog-panel" aria-labelledby="catalog-title">
      <div class="section-head">
        <div><p class="eyebrow">CURRENT MODELS / 当前模型目录</p><h2 id="catalog-title">官方 /models 现在有哪些模型</h2></div>
        <span class="mono">等待 /models 数据</span>
      </div>
      <p class="catalog-note">还没有模型目录快照。后台成功读取官方 /models 后，下一次推送会显示在这里。</p>
    </section>`;
  }
  const models = (Array.isArray(snapshot.models) ? snapshot.models : []).filter(
    (model) => model && typeof model === "object" && !Array.isArray(model),
  );
  const change = snapshot.last_change && typeof snapshot.last_change === "object" ? snapshot.last_change : null;
  const added = Array.isArray(change?.added) ? change.added.filter(Boolean) : [];
  const removed = Array.isArray(change?.removed) ? change.removed.filter(Boolean) : [];
  const changeTs = isoToTimestamp(change?.at);
  const nowTs = Math.floor(Date.now() / 1000);
  const changedRecently = changeTs > 0 && nowTs - changeTs < 7 * 86400;
  const changeParts = [];
  if (added.length) {
    changeParts.push(`新增 ${added.map((id) => `<code>${esc(id)}</code>`).join("、")}`);
  }
  if (removed.length) {
    changeParts.push(`下架 ${removed.map((id) => `<code>${esc(id)}</code>`).join("、")}`);
  }
  const changeText = changeParts.length
    ? `${esc(formatDate(changeTs))} · ${changeParts.join("；")}`
    : "无（与上次相同）";
  const checkedTs = isoToTimestamp(snapshot.checked_at);
  const checkedText = checkedTs ? formatDate(checkedTs) : "—";
  const rows = models.map((model) => {
    const firstSeenTs = isoToTimestamp(model.first_seen);
    const fingerprintTs = isoToTimestamp(model.fingerprint_checked_at) || checkedTs;
    const isNew = changedRecently && added.includes(model.id);
    return `<div class="catalog-row" role="row">
      <span class="catalog-id mono" role="cell" data-label="模型 API ID">${esc(model.id)}${isNew ? `<span class="catalog-new">NEW</span>` : ""}${model.in_catalog === false ? `<span class="catalog-tag">价格页</span>` : ""}</span>
      <span role="cell" data-label="价格页版本">${esc(model.version || "—")}</span>
      <span class="catalog-fp mono" role="cell" data-label="当前指纹" title="${attr(model.fingerprint || "")}">${esc(model.fingerprint || "—")}</span>
      <span class="mono" role="cell" data-label="首次记录">${esc(firstSeenTs ? formatDate(firstSeenTs) : "—")}</span>
      <span class="mono" role="cell" data-label="最近检查">${esc(fingerprintTs ? formatDate(fingerprintTs) : "—")}</span>
    </div>`;
  }).join("");
  return `<section class="catalog-panel" aria-labelledby="catalog-title">
    <div class="section-head">
      <div><p class="eyebrow">CURRENT MODELS / 当前模型目录</p><h2 id="catalog-title">官方 /models 现在有哪些模型</h2></div>
      <span class="mono">${formatNumber(models.length)} MODELS · 检查于 ${esc(checkedText)}</span>
    </div>
    <div class="catalog-summary">
      <p class="catalog-change${changeParts.length ? " is-changed" : ""}"><span>最近一次目录变化：</span>${changeText}</p>
      ${snapshot.catalog_available === false ? `<p class="catalog-note">这次快照没有 /models 目录，只显示价格页里的模型版本。</p>` : ""}
    </div>
    <div class="catalog-list" role="table" aria-label="当前模型目录">
      <div class="catalog-row catalog-head" role="row">
        <span role="columnheader">模型 API ID</span><span role="columnheader">价格页版本</span><span role="columnheader">当前指纹</span><span role="columnheader">首次记录</span><span role="columnheader">最近检查</span>
      </div>
      ${rows || `<p class="catalog-note">还没有模型数据，下一次巡检后出现。</p>`}
    </div>
    <p class="catalog-foot">目录来自官方 <code>GET /models</code>；新增或下架模型时会生成一条「模型目录」事件。<a href="/source/fingerprint">查看相关事件 ${svg("arrow")}</a></p>
  </section>`;
}

function filterChipUrl(basePath, params, drop, dropSource = false) {
  const query = new URLSearchParams(params);
  query.delete(drop);
  if (dropSource) query.delete("source");
  const text = query.toString();
  return text ? `${basePath}?${text}` : basePath;
}

function pagination({ total, limit, offset, basePath, params }) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const current = Math.floor(offset / limit) + 1;
  if (pages <= 1) return "";
  const linkFor = (page) => {
    const query = new URLSearchParams(params);
    if (page > 1) query.set("page", String(page));
    else query.delete("page");
    const text = query.toString();
    return text ? `${basePath}?${text}` : basePath;
  };
  const numbers = [];
  for (let page = 1; page <= pages; page += 1) {
    if (page === 1 || page === pages || Math.abs(page - current) <= 2) {
      numbers.push(`<a class="${page === current ? "is-current" : ""}" href="${attr(linkFor(page))}" ${page === current ? 'aria-current="page"' : ""}>${page}</a>`);
    } else if (numbers[numbers.length - 1] !== '<span class="page-gap">…</span>') {
      numbers.push('<span class="page-gap">…</span>');
    }
  }
  return `<nav class="pagination" aria-label="分页">
    ${current > 1 ? `<a class="page-prev" href="${attr(linkFor(current - 1))}">← 上一页</a>` : `<span class="page-prev is-disabled">← 上一页</span>`}
    <span class="page-numbers">${numbers.join("")}</span>
    ${current < pages ? `<a class="page-next" href="${attr(linkFor(current + 1))}">下一页 →</a>` : `<span class="page-next is-disabled">下一页 →</span>`}
  </nav>`;
}

export function renderHome({ events, total, totalAll, limit, offset, q, activeSource, activeLevel, statuses, counts, siteUrl, nowTs = Math.floor(Date.now() / 1000) }) {
  const latest = events[0];
  const freshness = dataFreshness(statuses, nowTs);
  const issue = latest ? dayKey(latest.changedTs) : dayKey(Math.floor(Date.now() / 1000));
  const params = {};
  if (q) params.q = q;
  if (activeLevel !== "") params.level = activeLevel;
  if (activeSource) params.source = activeSource;
  const filterBase = activeSource ? `/source/${activeSource}` : "/";
  // chip 只删除自己代表的筛选条件，保留其余条件。
  const chipUrl = (drop) => filterChipUrl(filterBase, params, drop, true);
  const filtered = Boolean(q || activeLevel !== "");
  const body = `<main class="home">
    <section class="hero">
      <div class="hero-top">
        <p class="eyebrow">INDEPENDENT WATCH / ISSUE ${esc(issue)}</p>
        <p class="hero-stamp mono">${esc(formatDate(Math.floor(Date.now() / 1000), false))} · BEIJING</p>
      </div>
      <div class="hero-grid">
        <div class="hero-copy">
          <h1>把每一次变化<br>变成可读的<span class="mark">信号</span></h1>
          <p class="hero-lead">持续观察 DeepSeek 的公开页面、模型指纹与接口文档。只记录发生了什么、可能影响什么，以及你什么时候该看一眼。</p>
          <div class="hero-actions">
            <a class="button-primary" href="#signals">浏览信号 ${svg("arrow")}</a>
            <a class="button-quiet" href="/subscribe">${svg("rss")} 订阅更新</a>
          </div>
        </div>
        <div class="hero-aside">
          <div class="hero-stat">
            <strong>${formatNumber(totalAll ?? total)}</strong>
            <span>条已记录信号</span>
          </div>
          <div class="hero-stat">
            <strong>05</strong>
            <span>个公开来源</span>
          </div>
          <div class="hero-stat">
            <strong>30<span class="unit">min</span></strong>
            <span>最长巡检间隔</span>
          </div>
          <p class="hero-note">没有账号、没有 Cookie、没有第三方统计。<br>你只需要一个 RSS 阅读器。</p>
        </div>
      </div>
      ${statusStrip(statuses, freshness)}
    </section>
    <div class="home-grid">
      ${sidebar({ counts, activeSource, activeLevel, q, basePath: filterBase })}
      <section class="feed" id="signals" aria-labelledby="feed-title">
        <div class="feed-head">
          <div>
            <p class="eyebrow">SIGNAL STREAM / 信号流</p>
            <h2 id="feed-title">${q ? `搜索「${esc(q)}」` : activeSource ? esc(sourceLabel(activeSource)) : "最新变化"}</h2>
          </div>
          <span class="feed-count mono">${formatNumber(total)} ENTRIES</span>
        </div>
        ${filtered ? `<div class="active-filters">
          ${q ? `<a href="${attr(chipUrl("q"))}">关键词：${esc(q)} ×</a>` : ""}
          ${activeLevel !== "" ? `<a href="${attr(chipUrl("level"))}">级别：${esc(levelLabel(activeLevel))} ×</a>` : ""}
        </div>` : ""}
        <div class="signal-list">
          ${renderSignalList(events, emptyState({
            title: filtered ? "没有匹配的筛选结果" : (totalAll ?? total) > 0 ? "这一页没有记录" : "还没有信号",
            text: filtered ? "当前条件下没有记录，换一个关键词或清除筛选。" : (totalAll ?? total) > 0 ? "页码可能已经过期，回到第一页看看。" : "监控已就绪。第一次采集之后，这里会出现可读的变化记录。",
            action: !filtered,
          }))}
        </div>
        ${pagination({ total, limit, offset, basePath: filterBase, params })}
      </section>
    </div>
  </main>`;
  return layout({
    title: "DeepSeek 更新与文档变化监控",
    description: "DeepSeek 观测站：持续记录 DeepSeek 公开页面、文档与模型指纹的变化，提供可读解读与 RSS 订阅。",
    path: "/",
    body,
    activeNav: "home",
    siteUrl,
  });
}

export function renderSource({ source, events, total, limit, offset, activeLevel, q = "", counts, siteUrl, modelsSnapshot = null }) {
  const info = SOURCES[source];
  const params = {};
  if (activeLevel !== "") params.level = activeLevel;
  if (q) params.q = q;
  const filterBase = `/source/${source}`;
  const chipUrl = (drop) => filterChipUrl(filterBase, params, drop);
  const filtered = Boolean(q || activeLevel !== "");
  const body = `<main class="page-main source-page">
    <header class="page-head">
      <p class="eyebrow">SOURCE / ${esc(info.short)}</p>
      <div class="page-head-grid">
        <div>
          <h1>${esc(info.label)}</h1>
          <p class="page-lead">${esc(info.description)}</p>
        </div>
        <div class="page-head-side">
          <span class="big-count">${formatNumber(counts[source] || 0)}</span>
          <span class="mono">RECORDED SIGNALS</span>
          <span class="mono">巡检：${esc(info.cadence)}</span>
        </div>
      </div>
    </header>
    ${source === "fingerprint" ? modelCatalogPanel(modelsSnapshot) : ""}
    <div class="home-grid">
      ${sidebar({ counts, activeSource: source, activeLevel, q, basePath: filterBase })}
      <section class="feed" aria-labelledby="feed-title">
        <div class="feed-head">
          <div>
            <p class="eyebrow">SIGNAL STREAM / ${esc(info.short)}</p>
            <h2 id="feed-title">${q ? `搜索「${esc(q)}」` : activeLevel !== "" ? esc(levelLabel(activeLevel)) : "全部记录"}</h2>
          </div>
          <span class="feed-count mono">${formatNumber(total)} ENTRIES</span>
        </div>
        ${filtered ? `<div class="active-filters">
          ${q ? `<a href="${attr(chipUrl("q"))}">关键词：${esc(q)} ×</a>` : ""}
          ${activeLevel !== "" ? `<a href="${attr(chipUrl("level"))}">级别：${esc(levelLabel(activeLevel))} ×</a>` : ""}
        </div>` : ""}
        <div class="signal-list">
          ${renderSignalList(events, emptyState({
            title: filtered ? "没有匹配的筛选结果" : (counts[source] || 0) > 0 ? "这一页没有记录" : "这个来源还没有信号",
            text: filtered ? "当前条件下没有记录，换一个关键词或清除筛选。" : (counts[source] || 0) > 0 ? "页码可能已经过期，回到第一页看看。" : "监控已就绪。第一次采集之后，这里会出现记录。",
            action: !filtered,
          }))}
        </div>
        ${pagination({ total, limit, offset, basePath: filterBase, params })}
      </section>
    </div>
  </main>`;
  return layout({
    title: info.label,
    description: info.description,
    path: `/source/${source}`,
    body,
    activeNav: "home",
    siteUrl,
  });
}

function normalizeLegacyDiff(raw) {
  const text = String(raw ?? "");
  const trimmed = text.trim();
  if (!trimmed.startsWith("[") || !trimmed.endsWith("]") || !/['"]/.test(trimmed)) return text;
  const body = trimmed.slice(1, -1).trim();
  if (!body) return "";
  const items = [];
  let current = "";
  let quote = "";
  let escaped = false;
  for (const ch of body) {
    if (escaped) {
      current += ch === "n" ? "\n" : ch === "t" ? "\t" : ch;
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (ch === quote) {
        quote = "";
        items.push(current);
        current = "";
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === "'" || ch === '"') quote = ch;
  }
  if (quote && current) items.push(current);
  return items.length ? items.join("\n") : text;
}

function formatBytes(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
}

function isRemovedLine(line) {
  return (line.startsWith("-") || line.startsWith("－")) && !line.startsWith("---");
}

function isAddedLine(line) {
  return (line.startsWith("+") || line.startsWith("＋")) && !line.startsWith("+++");
}

// 增删行数：用于折叠状态下直接显示「+N / -N」，不用展开就能看清改动规模。
function diffStats(raw) {
  let add = 0;
  let del = 0;
  for (const line of normalizeLegacyDiff(raw).split("\n")) {
    if (!line) continue;
    if (isAddedLine(line)) add += 1;
    else if (isRemovedLine(line)) del += 1;
  }
  return { add, del };
}

// 把 unified diff 转成左右两列：左列原文（删除），右列新内容（新增）。
// 连续的删除与新增按顺序一一配对，多出来的一侧留空——和代码 review 的
// side-by-side 习惯一致，比一行红一行绿上下排更好读。
// 两段文本像不像：用相邻字符对（bigram）的 Jaccard 系数，对中英文都有效，
// 也不需要引入 diff 库。用来判断「删除行」和「新增行」该不该配成一对。
function textGrams(text) {
  const set = new Set();
  const compact = String(text ?? "").replace(/\s+/g, "");
  for (let i = 0; i < compact.length - 1; i += 1) set.add(compact.slice(i, i + 2));
  if (!set.size && compact) set.add(compact);
  return set;
}

function jaccard(leftSet, rightSet) {
  if (!leftSet.size || !rightSet.size) return 0;
  let shared = 0;
  for (const gram of leftSet) if (rightSet.has(gram)) shared += 1;
  return shared / (leftSet.size + rightSet.size - shared);
}

// 相似度低于这个值就不配对：宁可两边各占一行，也不硬凑成「这行改成了那行」。
const PAIR_THRESHOLD = 0.34;
// 贪心配对是 O(删除行×新增行)。200×200 已经是 4 万次比较，超过就直接整块展示，
// 避免一个大改版把 /changes 页拖到 Workers CPU 超限。
const PAIR_BUDGET = 40_000;

function unpairedRows(removed, added) {
  return [
    { note: "改写幅度较大，未做逐行配对：左侧为原内容，右侧为新内容。" },
    ...removed.map((cell) => ({ left: cell, right: null })),
    ...added.map((cell) => ({ left: null, right: cell })),
  ];
}

// 把一批删除行和新增行配成左右两列。规则：
// 1. 只有一侧有内容 → 另一侧留空；
// 2. 数量相等 → 按位置配（最常见也最安全）；
// 3. 数量不等 → 按相似度贪心配，配不上的单独占一行；
// 4. 一对都配不上 → 退化成「先列删除、再列新增」，并标注未做逐行配对。
function pairDiffBlock(removed, added) {
  if (!removed.length || !added.length) {
    return [
      ...removed.map((cell) => ({ left: cell, right: null })),
      ...added.map((cell) => ({ left: null, right: cell })),
    ];
  }
  if (removed.length === added.length) {
    return removed.map((cell, index) => ({ left: cell, right: added[index] }));
  }
  if (removed.length * added.length > PAIR_BUDGET) {
    return unpairedRows(removed, added);
  }
  const usedAdded = new Array(added.length).fill(false);
  const addedGrams = added.map((cell) => textGrams(cell.text));
  const rows = [];
  let paired = 0;
  for (const cell of removed) {
    const cellGrams = textGrams(cell.text);
    let bestIndex = -1;
    let bestScore = 0;
    added.forEach((candidate, index) => {
      if (usedAdded[index]) return;
      const score = jaccard(cellGrams, addedGrams[index]);
      if (score > bestScore) {
        bestScore = score;
        bestIndex = index;
      }
    });
    if (bestIndex >= 0 && bestScore >= PAIR_THRESHOLD) {
      usedAdded[bestIndex] = true;
      paired += 1;
      rows.push({ left: cell, right: added[bestIndex] });
    } else {
      rows.push({ left: cell, right: null });
    }
  }
  added.forEach((cell, index) => {
    if (!usedAdded[index]) rows.push({ left: null, right: cell });
  });
  if (paired === 0) {
    return unpairedRows(removed, added);
  }
  return rows;
}

function splitDiffRows(raw) {
  const lines = normalizeLegacyDiff(raw).split("\n");
  const rows = [];
  let pendingDel = [];
  let pendingAdd = [];

  const flush = () => {
    if (!pendingDel.length && !pendingAdd.length) return;
    for (const row of pairDiffBlock(pendingDel, pendingAdd)) {
      rows.push(row.note ? { type: "note", text: row.note } : { type: "row", ...row });
    }
    pendingDel = [];
    pendingAdd = [];
  };

  for (const line of lines) {
    if (!line) continue;
    if (line.startsWith("---") || line.startsWith("+++")) continue;
    if (line.startsWith("@@")) {
      flush();
      rows.push({ type: "hunk", text: line });
      continue;
    }
    if (isRemovedLine(line)) {
      pendingDel.push({ text: line.slice(1), kind: "del" });
      continue;
    }
    if (isAddedLine(line)) {
      pendingAdd.push({ text: line.slice(1), kind: "add" });
      continue;
    }
    flush();
    const text = line.startsWith(" ") ? line.slice(1) : line;
    rows.push({ type: "row", left: { text, kind: "ctx" }, right: { text, kind: "ctx" } });
  }
  flush();
  return rows;
}

// 只把「真正变了的那一小段」包起来，整行仍然是原先的红/绿底色。
// 先求公共前缀和后缀，中间那段就是要高亮的改动。
function highlightChanged(text, counterpart) {
  const value = String(text ?? "");
  const other = String(counterpart ?? "");
  if (!value || !other || value === other) return esc(value);
  const limit = Math.min(value.length, other.length);
  let prefix = 0;
  while (prefix < limit && value[prefix] === other[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < limit - prefix &&
    value[value.length - 1 - suffix] === other[other.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  const head = value.slice(0, prefix);
  const middle = value.slice(prefix, value.length - suffix);
  const tail = suffix ? value.slice(value.length - suffix) : "";
  // 整行基本都变了就不叠高亮：底色已经说明问题，再压一层黄底会发脏
  if (!middle || middle.length / value.length > 0.7) return esc(value);
  return `${esc(head)}<mark class="diff-mark">${esc(middle)}</mark>${esc(tail)}`;
}

function renderSplitDiff(raw) {
  const rows = splitDiffRows(raw);
  if (!rows.length) return "";
  const cells = rows
    .map((row) => {
      if (row.type === "hunk") {
        return `<div class="diff-hunk mono">${esc(row.text)}</div>`;
      }
      if (row.type === "note") {
        return `<div class="diff-note">${esc(row.text)}</div>`;
      }
      const cell = (side, otherSide) => {
        if (!side) return `<div class="diff-cell diff-empty" aria-hidden="true"></div>`;
        const kind = side.kind === "add" ? "is-add" : side.kind === "del" ? "is-del" : "";
        const deep = side.kind === "add" || side.kind === "del";
        const body = deep && otherSide ? highlightChanged(side.text, otherSide.text) : esc(side.text);
        return `<div class="diff-cell${kind ? ` ${kind}` : ""}">${body || "&nbsp;"}</div>`;
      };
      return `<div class="diff-row">${cell(row.left, row.right)}${cell(row.right, row.left)}</div>`;
    })
    .join("");
  return `<div class="diff-split mono" role="table" aria-label="左右对照的改动详情">${cells}</div>`;
}

function diffStatBadge(raw) {
  const { add, del } = diffStats(raw);
  if (!add && !del) return "";
  const parts = [];
  if (add) parts.push(`<b class="diff-stat-add">+${add}</b>`);
  if (del) parts.push(`<b class="diff-stat-del">-${del}</b>`);
  return `<span class="diff-stat">${parts.join(" ")}</span>`;
}

function changeTags(change) {
  const tags = [];
  if (change.has_diff || change.hasDiff) tags.push("有对比");
  if (change.noise) tags.push("噪音");
  if (change.is_asset || change.isAsset) tags.push("资源");
  if (change.is_error || change.isError) tags.push("抓取错误");
  return tags
    .map(
      (tag) =>
        `<span class="change-kind${tag === "抓取错误" ? " is-error" : ""}">${esc(tag)}</span>`,
    )
    .join("");
}

// 详情页只渲染「目录」：每组一行（页面 + 首条 bullet + 标记），diff 正文统一进 /changes 页。
// 优先用 event_changes 里的真实分组（changes_detail）；旧事件没有时才回退到
// payload.changes 的 commit / assets 摘要。
// 首屏只放人话：这些分组 / 资源类变化属于技术核对，原始 bullet（commit、字节、
// i18n key、分片统计）只在 /changes 页展开，不进详情页正文。
const TECHNICAL_CHANGE_PAGES = new Set([
  "版本与构建",
  "语言包",
  "资源核对",
  // 旧版 watchsite 把 webwatch 摘要转成这些分组名。
  "前端版本",
  "脚本资源",
]);

function isTechnicalChange(change) {
  if (!change || typeof change !== "object") return false;
  if (change.is_asset || change.isAsset) return true;
  if (TECHNICAL_CHANGE_PAGES.has(String(change.page || "").trim())) return true;
  const kind = String(change.kind || "").trim();
  return kind === "commit" || kind === "assets";
}

function renderChangeIndex(payload, slug, detailChanges = null) {
  const detailed = Array.isArray(detailChanges)
    ? detailChanges.filter((change) => Array.isArray(change?.bullets) && change.bullets.some((bullet) => String(bullet || "").trim()))
    : [];
  const changes = detailed.length
    ? detailed
    : Array.isArray(payload?.changes)
      ? payload.changes
      : [];
  if (!changes.length) return "";
  // 没有 event_changes 行时（旧事件 / html-only）目录只是信息摘要，不做死链。
  const linkable = detailed.length > 0;
  return `<section class="detail-section">
    <div class="section-head"><h2 class="eyebrow">CHANGES / 变化摘要</h2><span class="mono">${changes.length} 条摘要</span></div>
    <div class="change-index">
      ${changes
        .map((change) => {
          const technical = isTechnicalChange(change);
          const firstBullet = technical
            ? ""
            : (Array.isArray(change.bullets) ? change.bullets : []).find(
                (bullet) => String(bullet || "").trim(),
              ) || String(change.preview || "").trim();
          // webwatch 的 payload.changes 用 kind 区分变化类型（commit / assets / text），没有 page。
          const label = change.page || (change.kind ? kindLabel(change.kind) : "未命名页面");
          const hasDiff = change.has_diff || change.hasDiff;
          const bytes = Number(change.diff_bytes || change.diffBytes || 0);
          const tag = linkable ? "a" : "div";
          const href = linkable ? ` href="${attr(`/e/${slug}/changes`)}"` : "";
          // 摘要来自 payload.changes，对比页来自 event_changes，两边下标不保证一一对应；
          // 逐行带 #change-N 会跳到错组，所以统一进对比页，不写锚点。
          return `<${tag} class="change-index-row"${href}>
          <span class="change-page mono">${esc(label)}</span>
          ${firstBullet ? `<span class="change-index-bullet">${esc(firstBullet)}</span>` : technical ? `<span class="change-index-bullet is-muted">${linkable ? "技术核对细节，见完整变化明细" : "技术细节已从摘要中省略"}</span>` : `<span class="change-index-bullet is-muted">没有文字摘要</span>`}
          <span class="change-index-tags">${changeTags(change)}${!technical && hasDiff && bytes ? `<span class="change-size mono">${esc(formatBytes(bytes))}</span>` : ""}</span>
        </${tag}>`;
        })
        .join("")}
    </div>
  </section>`;
}

function cleanTextList(value, limit, itemChars) {
  const items = Array.isArray(value) ? value : [];
  return items
    .map((item) => String(item ?? "").trim())
    .filter(Boolean)
    .slice(0, limit)
    .map((item) => item.slice(0, itemChars));
}

function textSnippet(value, limit) {
  const text = String(value ?? "").trim();
  // 按码点截断：直接 slice(0, limit) 会在 emoji 边界切出孤立代理，页面显示 �。
  const chars = Array.from(text);
  if (chars.length <= limit) return text;
  const head = chars.slice(0, limit).join("");
  const boundary = Math.max(
    head.lastIndexOf("。"),
    head.lastIndexOf("；"),
    head.lastIndexOf("，"),
    head.lastIndexOf(" "),
  );
  const cut = boundary >= Math.floor(limit * 0.55) ? head.slice(0, boundary) : head;
  return `${cut.trimEnd()}…`;
}

const QUOTE_PAIRS = {
  "「": "」",
  "『": "』",
  "“": "”",
  "‘": "’",
  "（": "）",
  "(": ")",
};

// 只在引号/括号外按给定字符切句。中文文档里经常出现「完全支持。」这种引号内句号，
// 旧实现会把它当断句，线上 docwatch-625 因此出现半句起头的段落。
function splitOutsideQuotes(value, isSeparator) {
  const text = String(value ?? "").trim();
  if (!text) return [];
  const parts = [];
  const stack = [];
  let buffer = "";
  for (const ch of text) {
    const expected = QUOTE_PAIRS[ch];
    if (expected) {
      stack.push(expected);
      buffer += ch;
      continue;
    }
    if (stack.length && ch === stack[stack.length - 1]) {
      stack.pop();
      buffer += ch;
      continue;
    }
    if (!stack.length && isSeparator(ch)) {
      // 句末标点保留在正文里；逗号/换行只负责切分，不进入结果。
      const keep = "。！？!?；;".includes(ch) ? ch : "";
      const part = `${buffer}${keep}`.trim();
      if (part) parts.push(part);
      buffer = "";
      continue;
    }
    buffer += ch;
  }
  const tail = buffer.trim();
  if (tail) parts.push(tail);
  return parts;
}

function cleanPart(part) {
  return String(part ?? "").trim().replace(/^[，,、：:\s]+/, "").replace(/[，,、：:\s]+$/, "");
}

function splitBriefPoints(value, limit = 4, itemChars = 220) {
  const text = String(value ?? "").trim();
  if (!text) return [];
  let parts = splitOutsideQuotes(text, (ch) => "。！？!?；;\n".includes(ch))
    .map(cleanPart)
    .filter(Boolean);
  // 一句话很长时再按逗号拆开，避免省流第一条就占满整屏。
  if (parts.length <= 1 && text.length > 70) {
    parts = splitOutsideQuotes(text, (ch) => "，,".includes(ch))
      .map(cleanPart)
      .filter(Boolean);
  }
  return parts.slice(0, limit).map((part) => textSnippet(part, itemChars));
}

function splitDetailParagraphs(value, limit = 10, itemChars = 900) {
  const text = String(value ?? "").trim();
  if (!text) return [];
  const blocks = text.split(/\n{2,}/).map((block) => block.trim()).filter(Boolean);
  const parts = [];
  for (const block of blocks) {
    let sentences = splitOutsideQuotes(block, (ch) => "。！？!?；;".includes(ch))
      .map(cleanPart)
      .filter(Boolean);
    // 单个超长句再按分号拆，让详细分析读起来是一颗颗要点而不是大段墙。
    if (sentences.length <= 1 && block.length > 260) {
      sentences = splitOutsideQuotes(block, (ch) => "；;".includes(ch))
        .map(cleanPart)
        .filter(Boolean);
    }
    parts.push(...sentences);
  }
  return parts.slice(0, limit).map((part) => textSnippet(part, itemChars));
}

// 纯否定句（如「价格与接口未变」）放在省流里会制造语义噪音：读者看到「重要」+「未变」，
// 不知道到底要不要管。只当它是唯一要点时保留，否则交给详细/影响段落承载。
const PURE_NEGATIVE_RE = /^[^。！？!?]{1,20}(?:未变|不变|没变|无变化|未受影响|不受影响|没有影响|无影响)[。！？!?]?$/;

function isPureNegativePoint(text) {
  return PURE_NEGATIVE_RE.test(String(text ?? "").trim());
}

function normalizeForCompare(text) {
  return String(text ?? "")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "");
}

function dedupeBriefAndSections(points, sections) {
  const filteredPoints = points.filter((point) => {
    if (points.length > 1 && isPureNegativePoint(point)) return false;
    return Boolean(String(point ?? "").trim());
  });
  const pointKeys = new Set(filteredPoints.map(normalizeForCompare).filter(Boolean));
  const cleanedSections = [];
  for (const section of sections) {
    const paragraphs = [];
    // 「影响」段落写「没有影响」是正常结论，不能当噪音删掉；只清理「变化内容」里的纯否定句。
    const isImpactSection = String(section.title || "").includes("影响");
    for (const paragraph of section.paragraphs) {
      const sentences = splitOutsideQuotes(paragraph, (ch) => "。！？!?；;".includes(ch))
        .map(cleanPart)
        .filter(Boolean);
      if (sentences.length <= 1) {
        if (!isImpactSection && isPureNegativePoint(paragraph)) continue;
        if (!pointKeys.has(normalizeForCompare(paragraph))) paragraphs.push(paragraph);
        continue;
      }
      const kept = sentences.filter((sentence) => !pointKeys.has(normalizeForCompare(sentence)));
      if (kept.length) paragraphs.push(kept.join(""));
    }
    if (paragraphs.length) cleanedSections.push({ ...section, paragraphs });
  }
  return { points: filteredPoints, sections: cleanedSections };
}

function splitLongParagraphs(paragraphs, maxChars = 180) {
  const out = [];
  for (const paragraph of paragraphs) {
    const text = String(paragraph ?? "").trim();
    if (!text) continue;
    if (text.length <= maxChars) {
      out.push(text);
      continue;
    }
    const sentences = splitOutsideQuotes(text, (ch) => "。！？!?；;".includes(ch))
      .map(cleanPart)
      .filter(Boolean);
    if (sentences.length <= 1) {
      out.push(text);
      continue;
    }
    let buffer = "";
    for (const sentence of sentences) {
      if (buffer && buffer.length + sentence.length > maxChars) {
        out.push(buffer);
        buffer = sentence;
      } else {
        buffer += sentence;
      }
    }
    if (buffer) out.push(buffer);
  }
  return out;
}

function briefPointsFor(event, analysis) {
  const structured = cleanTextList(analysis?.tldr_points, 7, 300);
  if (structured.length) return structured;
  const tldr = String(analysis?.tldr ?? "").trim();
  if (tldr) return splitBriefPoints(tldr, 3, 200);
  const overview = String(analysis?.overview ?? "").trim();
  if (overview) return splitBriefPoints(overview, 2, 120);
  const summary = String(event.summary ?? "").trim();
  if (summary) return splitBriefPoints(summary, 2, 120);
  const changed = String(event.payload?.what_changed ?? "").trim();
  return splitBriefPoints(changed || summary, 2, 120);
}

function legacySectionsFor(event, analysis, { preferEventCopy = false } = {}) {
  const sections = [];
  const visible = cleanTextList(analysis?.visible_changes, 8, 900);
  const changed = preferEventCopy ? "" : String(event.payload?.what_changed ?? "").trim();
  const whatChanged = visible.length
    ? visible
    : splitDetailParagraphs(changed || event.summary, 12, 900);
  if (whatChanged.length) {
    sections.push({ title: "变化内容", paragraphs: whatChanged });
  }
  const impactList = cleanTextList(analysis?.impact, 6, 900);
  const impact = impactList.length
    ? impactList
    : splitDetailParagraphs(event.impact, 6, 900);
  if (impact.length) {
    // 不用「为什么重要」：很多事件本身影响有限，这个标题会逼读者把「没影响」读成「很重要」。
    sections.push({ title: "影响", paragraphs: impact });
  }
  return sections;
}

function recheckRecord(event) {
  const payload = event?.payload && typeof event.payload === "object" ? event.payload : {};
  const value = payload.recheck;
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function recheckAppliedFields(event) {
  const recheck = recheckRecord(event);
  return new Set(
    (Array.isArray(recheck?.applied) ? recheck.applied : [])
      .map((field) => String(field || "").trim())
      .filter(Boolean),
  );
}

/**
 * 详情页统一的两版解读模型：
 * 1. 新判读有 tldr_points / sections 时直接用；
 * 2. 旧 docwatch / webwatch 判读用 tldr + visible_changes + impact 收敛；
 * 3. 完全没有判读的旧事件用 summary / what_changed / impact 兜底。
 * 4. 发布后复核改过 summary / impact / action 时，优先用事件的新字段，
 *    不再展示可能已经过时的旧 analysis。
 * 所有来源、所有历史事件都走同一套模板，不再只对 docs 新事件生效。
 */
function buildDetailBrief(event, analysis) {
  const correctedCopy = ["summary", "impact", "action"].some((field) =>
    recheckAppliedFields(event).has(field)
  );
  const sourceAnalysis = correctedCopy ? null : analysis;
  const structuredSections = (Array.isArray(sourceAnalysis?.sections) ? sourceAnalysis.sections : [])
    .map((section) => ({
      title: String(section?.title ?? "").trim().slice(0, 200),
      paragraphs: cleanTextList(section?.paragraphs, 12, 1000),
    }))
    .filter((section) => section.title && section.paragraphs.length)
    .slice(0, 20);
  const structuredPoints = cleanTextList(sourceAnalysis?.tldr_points, 7, 300);
  const overview = String(sourceAnalysis?.overview ?? "").trim().slice(0, 2000);
  // 有 tldr_points / overview 也算模型结构化字段：即使模型漏了 sections，也不要把有效省流丢掉；
  // 但 sections 为空时，详情仍要退回旧 analysis / 事件字段，不能凭空消失。
  const hasModelFields = structuredSections.length > 0 || structuredPoints.length > 0 || Boolean(overview);
  const points = briefPointsFor(event, sourceAnalysis);
  const rawSections = structuredSections.length
    ? structuredSections
    : legacySectionsFor(event, sourceAnalysis, { preferEventCopy: correctedCopy });
  const deduped = dedupeBriefAndSections(points, rawSections);
  const sections = deduped.sections;
  if (!deduped.points.length && !sections.length && !overview) return null;
  return {
    source: correctedCopy ? "recheck" : structuredSections.length ? "model" : sourceAnalysis ? "legacy_analysis" : "legacy_event",
    tldr_points: deduped.points,
    overview: hasModelFields ? overview : "",
    sections,
    action: String(
      correctedCopy
        ? event.action
        : sourceAnalysis?.action || event.action || event.payload?.action || "",
    ).trim().slice(0, 800),
    degraded: Boolean(sourceAnalysis?.degraded ?? event.payload?.degraded),
  };
}

/**
 * DSH 发版详情：按用户 2026-11-23 口径，不做省流，直接照搬官方更新日志。
 * 没有 release note 时只显示「已发布」，等后续巡检补上完整日志再用同一事件回填。
 */
function renderDshReleaseNotes(event) {
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  const note = payload.release_note && typeof payload.release_note === "object" ? payload.release_note : {};
  const rawBlocks = Array.isArray(note.blocks) ? note.blocks : [];
  let blocks = rawBlocks
    .filter((block) => block && typeof block === "object" && String(block.text ?? "").trim())
    .slice(0, 1000);
  if (!blocks.length) {
    // 升级前的旧事件只有 payload.notes（最多 20 条）；先按条目展示，等本轮
    // 巡检用完整 release_note 回填后自动变成完整更新日志。
    blocks = (Array.isArray(payload.notes) ? payload.notes : [])
      .filter((item) => String(item ?? "").trim())
      .slice(0, 1000)
      .map((text) => ({ type: "bullet", text: String(text).trim() }));
  }
  const version = String(payload.version ?? "").trim();
  if (!blocks.length) {
    return `<section class="detail-section detail-release-note">
      <div class="section-head"><h2 class="eyebrow">发布状态 / RELEASE</h2><span class="mono">RELEASED</span></div>
      <p class="detail-copy">DSH ${version ? `v${esc(version)} ` : ""}已发布。官方暂未提供更新说明，后续巡检会自动检查并补上。</p>
    </section>`;
  }
  const bulletCount = blocks.filter((block) => block.type === "bullet").length;
  const parts = [];
  let bullets = [];
  const flushBullets = () => {
    if (!bullets.length) return;
    parts.push(`<ul class="release-note-list">${bullets.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`);
    bullets = [];
  };
  for (const block of blocks) {
    const type = String(block.type || "paragraph");
    const text = String(block.text ?? "").trim();
    if (type === "heading") {
      flushBullets();
      parts.push(`<h3 class="release-note-heading">${esc(text)}</h3>`);
    } else if (type === "bullet") {
      bullets.push(text);
    } else {
      flushBullets();
      parts.push(`<p class="release-note-paragraph">${esc(text)}</p>`);
    }
  }
  flushBullets();
  return `<section class="detail-section detail-release-note">
    <div class="section-head"><h2 class="eyebrow">更新日志 / RELEASE NOTES</h2><span class="mono">${bulletCount} 条</span></div>
    <div class="release-note">${parts.join("")}</div>
  </section>`;
}

/**
 * 结构化两版解读：省流（蓝色圆点）+ 详细分析（按主题分组，段落前黑色圆点）。
 * 所有来源都通过 buildDetailBrief 归一成同一份数据。
 */
function renderBriefAndDetail(brief) {
  const points = cleanTextList(brief.tldr_points, 7, 300);
  const overview = String(brief.overview ?? "").trim().slice(0, 2000);
  const sections = (Array.isArray(brief.sections) ? brief.sections : [])
    .map((section) => ({
      title: String(section?.title ?? "").trim().slice(0, 200),
      paragraphs: splitLongParagraphs(cleanTextList(section?.paragraphs, 12, 1000)),
    }))
    .filter((section) => section.title && section.paragraphs.length)
    .slice(0, 20);
  if (!points.length && !sections.length && !overview) return "";
  const action = String(brief.action ?? "").trim().slice(0, 800) || "打开原始页面核对变化。";
  const isModel = brief.source === "model";
  const detailTitle = isModel ? "详细分析 / FULL ANALYSIS" : "变化明细 / DETAILS";
  const detailCount = sections.length
    ? `<span class="mono">${sections.length} ${isModel ? "个主题" : "组"}</span>`
    : "";
  const legacyNote = isModel ? "" : `<p class="detail-legacy-note">按原始记录整理，未做结构化解读。</p>`;
  return `${points.length ? `<section class="detail-brief">
    <div class="section-head"><h2 class="eyebrow">省流 / TL;DR</h2><span class="mono">${points.length} 条要点</span></div>
    <ul class="tldr-list">${points.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>
    ${brief.degraded ? `<p class="brief-caveat">本条结论依据有限，建议对照原文核实。</p>` : ""}
  </section>` : ""}
  ${sections.length || overview ? `<details class="detail-section detail-full" open>
    <summary class="section-head"><h2 class="eyebrow">${detailTitle}</h2>${detailCount}</summary>
    <div class="detail-full-inner">
      ${legacyNote}
      ${overview ? `<p class="detail-lead">${esc(overview)}</p>` : ""}
      ${sections.map((section) => `<section class="detail-group"><h3>${esc(section.title)}</h3>${section.paragraphs.map((paragraph) => `<p>${esc(paragraph)}</p>`).join("")}</section>`).join("")}
    </div>
  </details>` : ""}
  <section class="detail-section detail-action"><div class="section-head"><h2 class="eyebrow">建议 / ACTION</h2></div><p class="detail-copy">${esc(action)}</p></section>`;
}

function renderAnalysisEvidence(payload, evidenceLines) {
  const groups = (Array.isArray(payload?.changes) ? payload.changes : [])
    .map((group) => ({
      page: String(group?.page || "").trim(),
      bullets: (Array.isArray(group?.bullets) ? group.bullets : [])
        .map((bullet) => String(bullet || "").trim())
        .filter(Boolean)
        .slice(0, 8),
    }))
    .filter((group) => group.bullets.length);
  const lines = Array.isArray(evidenceLines) ? evidenceLines.filter(Boolean) : [];
  if (!groups.length && !lines.length) return "";
  const count = groups.length ? `${groups.length} GROUPS` : `${lines.length} ITEMS`;
  return `<details class="detail-section">
    <summary class="section-head"><h2 class="eyebrow">EVIDENCE / 依据</h2><span class="mono">${esc(count)}</span></summary>
    ${groups.length ? `<div class="brief-groups">${groups.map((group) => `<div class="brief-group">
      <h3 class="brief-group-title">${esc(group.page || "未命名页面")}</h3>
      <ul class="brief-list">${group.bullets.map((bullet) => `<li>${esc(bullet)}</li>`).join("")}</ul>
    </div>`).join("")}</div>` : ""}
    ${lines.length ? `<ol class="evidence-list">${lines.map((item) => `<li>${esc(item)}</li>`).join("")}</ol>` : ""}
  </details>`;
}

function sizeText(value) {
  const size = Number(value);
  return Number.isFinite(size) && size > 0 ? formatBytes(size) : "（无）";
}

function renderAnalysisTechnical(payload) {
  const frontend = payload?.frontend && typeof payload.frontend === "object" ? payload.frontend : null;
  if (!frontend) return "";
  const totals = frontend.totals && typeof frontend.totals === "object" ? frontend.totals : {};
  const build = frontend.new_build && typeof frontend.new_build === "object" ? frontend.new_build : {};
  const oldBuild = frontend.old_build && typeof frontend.old_build === "object" ? frontend.old_build : {};
  const rows = [];
  if (frontend.old_commit || frontend.new_commit) {
    rows.push(["版本", `${frontend.old_commit || "（无）"} → ${frontend.new_commit || "（无）"}`]);
  }
  if (oldBuild.appVersion || build.appVersion) {
    rows.push(["应用版本", `${oldBuild.appVersion || "（无）"} → ${build.appVersion || "（无）"}`]);
  }
  if (oldBuild.commit_datetime || build.commit_datetime) {
    rows.push(["构建时间", `${oldBuild.commit_datetime || "（无）"} → ${build.commit_datetime || "（无）"}`]);
  }
  const js = frontend.main_js && typeof frontend.main_js === "object" ? frontend.main_js : {};
  if (js.old_size || js.new_size) rows.push(["主包大小", `${sizeText(js.old_size)} → ${sizeText(js.new_size)}`]);
  const css = frontend.main_css && typeof frontend.main_css === "object" ? frontend.main_css : {};
  if (css.old_size || css.new_size) rows.push(["样式大小", `${sizeText(css.old_size)} → ${sizeText(css.new_size)}`]);
  if (Number(totals.manifest) > 0) {
    rows.push([
      "资源清单",
      `${formatNumber(totals.manifest)} 个（新增 ${formatNumber(totals.added)}，移除 ${formatNumber(totals.removed)}，内容变化 ${formatNumber(totals.changed)}）`,
    ]);
  }
  if (!rows.length) return "";
  return `<details class="detail-section">
    <summary class="section-head"><h2 class="eyebrow">TECHNICAL / 技术核对</h2><span class="mono">${esc(`${rows.length} ITEMS`)}</span></summary>
    <dl class="brief-facts">${rows.map(([term, value]) => `<div><dt>${esc(term)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>
  </details>`;
}

function internalNotice(event) {
  if (!isUncapturedChange(event)) return "";
  if (eventQuality(event) === "internal" && technicalChangeDetail(event)) {
    return `<p class="internal-note">这条记录只检测到资源层面的技术变化，没有提取到用户可见文案；具体文件与字节变化见下方「变化摘要 / 技术核对」。</p>`;
  }
  return `<p class="internal-note">这条记录没有抓到可读的界面变化；可能是内部调整，也可能是本轮抓取覆盖不足。历史级别与结论仅供参考。</p>`;
}

export function renderDetail({ event, previous, next, siteUrl, changes = [] }) {
  const evidence = Array.isArray(event.evidence) ? event.evidence : [];
  const titleText = displayEventTitle(event);
  const summaryText = displayEventSummary(event);
  const analysis = event.payload?.analysis && typeof event.payload.analysis === "object"
    && !Array.isArray(event.payload.analysis)
    ? event.payload.analysis
    : null;
  // DSH 发版按用户口径直接照搬官方更新日志，不生成「省流」；其余来源继续走
  // 统一的两版解读模板（新结构化判读、旧 web 判读、旧 docwatch 和规则兜底）。
  const isDshRelease = event.kind === "dsh_release";
  const detailBrief = isDshRelease ? null : buildDetailBrief(event, analysis);
  // 只有真正走结构化判读才隐藏旧 web 的 EVIDENCE / TECHNICAL；旧 analysis 仍保留它们。
  const structuredBrief = detailBrief?.source === "model";
  const recheckCorrected = detailBrief?.source === "recheck";
  const briefHtml = isDshRelease
    ? renderDshReleaseNotes(event)
    : detailBrief
      ? renderBriefAndDetail(detailBrief)
      : "";
  // 旧 web 判读还带依据分组 / 技术核对，保留在详情页下方；结构化 docs 用事件自带证据列表。
  // 复核修正过摘要时只隐藏可能过时的旧判读，技术核对直接来自 frontend 证据，仍然保留：
  // 它往往是有变化但无用户文案时唯一的具体文件/字节证据。
  const evidenceHtml = analysis && !structuredBrief && !recheckCorrected ? renderAnalysisEvidence(event.payload, evidence) : "";
  const technicalHtml = !structuredBrief && (analysis || technicalChangeDetail(event))
    ? renderAnalysisTechnical(event.payload)
    : "";
  const detailChanges = Array.isArray(changes) ? changes : [];
  const changesHtml = renderChangeIndex(event.payload, event.slug, detailChanges);
  const decisionHtml = renderLevelDecision(event.payload);
  const displayLevel = displayEventLevel(event);
  // 只有 event_changes 真有内容时才给「完整对比 / 变化摘要」入口，避免 html-only
  // 事件点进去是空页面（P2-49）。有 diff 才叫完整对比，否则是变化摘要（P2-48）。
  const hasChangeRows = detailChanges.some(
    (change) =>
      (Array.isArray(change?.bullets) && change.bullets.some((bullet) => String(bullet || "").trim())) ||
      String(change?.diff || "").trim(),
  );
  const hasDiffRows = detailChanges.some((change) => change?.hasDiff ?? change?.has_diff);
  const changesAction = hasChangeRows
    ? `<div class="detail-actions"><a class="button-primary" href="${attr(`/e/${event.slug}/changes`)}">${hasDiffRows ? "查看完整对比" : "查看全部变化摘要"} ${svg("arrow")}</a></div>`
    : "";
  const structureNote = event.kind === "html" && !hasChangeRows
    ? `<p class="muted detail-structure-note">本轮只检测到页面结构变化，没有可展示的文字对比。</p>`
    : "";
  // 省流句取 summary（新事件是 tldr）；详细解读优先用 payload.what_changed 全文。
  // 复核改过摘要时，旧 what_changed 可能已被证伪，必须让位给新摘要。
  const whatChanged = recheckCorrected
    ? String(event.summary || "").trim() || "没有额外说明。"
    : String(event.payload?.what_changed || "").trim() || event.summary || "没有额外说明。";
  const pageUrl = siteUrl ? `${siteUrl.replace(/\/$/, "")}/e/${event.slug}` : "";
  const publishedTime = isoTimestamp(event.changedTs);
  const modifiedTime = isoTimestamp(event.detectedTs || event.changedTs);
  const articleJsonLd = {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: titleText,
    description: summaryText || titleText,
    datePublished: publishedTime || undefined,
    dateModified: modifiedTime || undefined,
    mainEntityOfPage: pageUrl || undefined,
    author: { "@type": "Organization", name: SITE_NAME },
    publisher: { "@type": "Organization", name: SITE_NAME },
    inLanguage: "zh-CN",
  };
  const body = `<main class="page-main detail-page">
    <nav class="breadcrumb" aria-label="面包屑">
      <a href="/">信号流</a><span>/</span><a href="/source/${attr(event.source)}">${esc(sourceLabel(event.source))}</a><span>/</span><span>${esc(formatDate(event.changedTs, false))}</span>
    </nav>
    <article class="detail">
      <header class="detail-head">
        <div class="detail-kicker">
          <span class="mono">${esc(sourceLabel(event.source))} / ${esc(kindLabel(event.kind))}</span>
          <span class="detail-kicker-meta">${qualityBadgeForEvent(event)}<span class="mono">${esc(formatDate(event.changedTs))}</span></span>
        </div>
        <div class="detail-title-row">
          <div>
            <h1>${esc(headingText(titleText))}</h1>
            ${internalNotice(event)}
            ${!isDshRelease && summaryText && !briefHtml ? `<p class="detail-tldr">${esc(summaryText)}</p>` : ""}
            ${reviewNotice(event)}
          </div>
          ${detailLevelBlock(displayEventLevel(event), isUncapturedChange(event) && !technicalChangeDetail(event))}
        </div>
      </header>
      <div class="detail-body-grid">
        <div class="detail-content">
          ${briefHtml || `<section class="detail-section">
            <div class="section-head"><h2 class="eyebrow">WHAT CHANGED / 发生了什么</h2><span class="mono">SIGNAL ${esc(String(event.signalCount).padStart(2, "0"))}</span></div>
            <p class="detail-copy">${esc(whatChanged)}</p>
          </section>
          <div class="detail-notes">
            <section class="note-block">
              <h2 class="eyebrow">WHY IT MATTERS / 影响</h2>
              <p>${esc(event.impact || "暂无影响解读。")}</p>
            </section>
            <section class="note-block">
              <h2 class="eyebrow">WHAT TO DO / 建议</h2>
              <p>${esc(event.action || "打开原始页面核对变化。")}</p>
            </section>
          </div>`}
          ${decisionHtml}
          ${isDshRelease ? "" : changesHtml}
          ${structureNote}
          ${isDshRelease ? "" : (evidenceHtml || `<section class="detail-section">
            <div class="section-head"><h2 class="eyebrow">EVIDENCE / 证据</h2><span class="mono">${evidence.length} ITEMS</span></div>
            ${evidence.length ? `<ol class="evidence-list">${evidence.map((item) => `<li>${esc(item)}</li>`).join("")}</ol>` : `<p class="muted">这次没有单独的文本证据，页面差异见上方明细。</p>`}
          </section>`)}
          ${technicalHtml}
          ${isDshRelease ? "" : changesAction}
        </div>
        <aside class="detail-aside">
          <div class="aside-box">
            <h2 class="eyebrow">RECORD / 记录</h2>
            <dl>
              <div><dt>来源</dt><dd><a href="/source/${attr(event.source)}">${esc(sourceLabel(event.source))}</a></dd></div>
              <div><dt>记录时间</dt><dd>${esc(formatDate(event.changedTs))}</dd></div>
              <div><dt>发现时间</dt><dd>${esc(formatDate(event.detectedTs))}</dd></div>
              <div><dt>${event.kind === "dsh_release" ? "说明条数" : event.kind === "dsh_commit" ? "提交数" : "变化处"}</dt><dd>${formatNumber(event.signalCount)}</dd></div>
            </dl>
            ${event.pageUrl ? `<a class="button-primary full" href="${attr(event.pageUrl)}" target="_blank" rel="noopener noreferrer">打开原始页面 ${svg("arrow")}</a>` : ""}
            ${event.archiveUrl ? `<a class="button-quiet full" href="${attr(event.archiveUrl)}" target="_blank" rel="noopener noreferrer">查看归档快照</a>` : ""}
          </div>
          <div class="aside-box aside-note">
            <h2 class="eyebrow">READ THIS WAY</h2>
            <p>级别由规则决定，不由模型压低。重要变化会直接出现在 RSS 里。</p>
            <a class="text-link" href="/feed.xml">${svg("rss")} 订阅这个站</a>
          </div>
        </aside>
      </div>
      <nav class="prev-next" aria-label="相邻记录">
        ${previous ? `<a class="prev" href="/e/${attr(previous.slug)}"><span class="mono">← PREVIOUS</span><strong>${esc(previous.title)}</strong></a>` : "<span></span>"}
        ${next ? `<a class="next" href="/e/${attr(next.slug)}"><span class="mono">NEXT →</span><strong>${esc(next.title)}</strong></a>` : "<span></span>"}
      </nav>
    </article>
  </main>`;
  return layout({
    title: titleText,
    description: summaryText || titleText,
    path: `/e/${event.slug}`,
    body,
    activeNav: "home",
    siteUrl,
    type: "article",
    publishedTime,
    modifiedTime,
    jsonLd: articleJsonLd,
  });
}

function renderChangeGroup(change, index, open) {
  const bullets = Array.isArray(change.bullets) ? change.bullets.filter(Boolean) : [];
  const hasDiff = Boolean(change.hasDiff ?? change.has_diff);
  const rawDiff = hasDiff ? String(change.diff || "") : "";
  const diffHtml = hasDiff ? renderSplitDiff(rawDiff) : "";
  const note = String(change.note || "").trim();
  const bytes = Number(change.diffBytes ?? change.diff_bytes ?? 0);
  const stat = hasDiff ? diffStatBadge(rawDiff) : "";
  const tags = `${changeTags(change)}${hasDiff && bytes ? `<span class="change-size mono">${esc(formatBytes(bytes))}</span>` : ""}`;
  return `<details class="change change-group" id="change-${index}"${open ? " open" : ""}>
    <summary class="change-head">
      <span class="change-page mono">${esc(change.page || "未命名页面")}</span>
      <span class="change-meta">${tags ? `<span class="change-tags">${tags}</span>` : ""}${stat}</span>
    </summary>
    ${bullets.length ? `<ul>${bullets.map((bullet) => `<li>${esc(bullet)}</li>`).join("")}</ul>` : ""}
    ${diffHtml}
    ${note ? `<p class="change-note">${esc(note)}</p>` : ""}
    ${!bullets.length && !diffHtml && !note ? `<p class="muted change-group-empty">这一组没有可展示的文本，请打开原始页面核对。</p>` : ""}
  </details>`;
}

// 附属对比页：详情页只留目录，完整 diff 统一放这里。noindex,follow 避免稀释事件页。
export function renderEventChanges({ event, changes, siteUrl, showAll = false }) {
  const groups = Array.isArray(changes) ? changes : [];
  const withDiff = groups.filter((change) => (change.hasDiff ?? change.has_diff) && !change.noise).length;
  // 没有 diff 的 webwatch 事件（绝大多数）不能自称「左右对照」：标题和引导语
  // 改成「变化摘要」，也不再提供毫无意义的「展开全部」（P2-48）。
  const hasDiff = withDiff > 0;
  const modeLabel = hasDiff ? "完整对比" : "变化摘要";
  const toolbar = groups.length
    ? `<div class="changes-toolbar">
        <span class="mono">${groups.length} 组${hasDiff ? ` · ${withDiff} 组有实质对比` : "变化摘要"}</span>
        ${hasDiff ? `<a class="text-link" href="${attr(`/e/${event.slug}/changes${showAll ? "" : "?all=1"}`)}">${esc(showAll ? "全部收起" : `展开全部 ${groups.length} 组`)}</a>` : ""}
      </div>`
    : "";
  const disclaimer = hasDiff
    ? `<p class="changes-disclaimer">改前改后由采集到的文本差异重建，不是官方截图；准确内容以原始页面为准。</p>`
    : "";
  const lead = groups.length
    ? hasDiff
      ? `完整对比：共 ${groups.length} 组变化，其中 ${withDiff} 组有实质改动。默认全部折叠，点开任意一组看左右对照的改动；也可以直接「展开全部」。`
      : `变化摘要：共 ${groups.length} 组变化，默认全部折叠，点开任意一组查看具体变化。`
    : "该事件暂无对比明细。";
  const body = `<main class="page-main detail-page changes-page">
    <nav class="breadcrumb" aria-label="面包屑">
      <a href="/">信号流</a><span>/</span><a href="/e/${attr(event.slug)}">${esc(headingText(displayEventTitle(event)))}</a><span>/</span><span>${esc(modeLabel)}</span>
    </nav>    <article class="detail">
      <header class="detail-head">
        <div class="detail-kicker">
          <span class="mono">${esc(sourceLabel(event.source))} / ${hasDiff ? "COMPARE" : "SUMMARY"}</span>
          <span class="mono">${esc(formatDate(event.changedTs))}</span>
        </div>
        <div class="detail-title-row">
          <div>
            <h1>${esc(headingText(displayEventTitle(event)))}</h1>
            <p class="detail-lead">${esc(lead)}</p>
          </div>
          ${detailLevelBlock(displayEventLevel(event), isUncapturedChange(event) && !technicalChangeDetail(event))}
        </div>
      </header>
      ${toolbar}
      ${disclaimer}
      <div class="change-stack">
        ${groups.length
          ? groups
              .map((change, index) => renderChangeGroup(change, index, showAll))
              .join("")
          : `<div class="change-empty"><p>该事件暂无对比明细。</p><p class="muted">旧事件没有单独存储对比正文，可以返回事件页查看变化目录。</p></div>`}
      </div>
      <div class="changes-actions">
        <a class="button-primary" href="/e/${attr(event.slug)}">返回事件 ${svg("arrow")}</a>
      </div>
    </article>
  </main>`;
  return layout({
    title: `${headingText(displayEventTitle(event))} · ${modeLabel}`,
    description: `查看「${headingText(displayEventTitle(event))}」的${modeLabel}。`,
    path: `/e/${event.slug}/changes`,
    body,
    activeNav: "home",
    siteUrl,
    robots: "noindex,follow",
  });
}

// 状态页只给读者看“正不正常、多久前”，内部流水号、UTC ISO 时间和采集器
// 自述都属于运维信息，不再直接铺在页面上；异常时保留一句人话原因。
function friendlyStatusDetail(status) {
  const detail = String(status?.detail || "").trim();
  if (!detail) return "—";
  if (status?.status === "ok") return "—";
  if (/^最后成功[:：]/.test(detail)) return "—";
  return detail
    .replace(/#\d+/g, "")
    .replace(/\d{4}-\d{2}-\d{2}T[\d:.]+Z/g, (value) => {
      const ts = Date.parse(value);
      return Number.isNaN(ts) ? value : formatDate(Math.floor(ts / 1000));
    })
    .replace(/\s+/g, " ");
}

function statusTimeCell(ts, nowTs, srLabel) {
  if (!ts) return `<span class="mono" role="cell"><span class="sr-only">${srLabel}：</span>—</span>`;
  const age = relativeAgeText(nowTs - Number(ts));
  return `<span class="mono status-time" role="cell"><span class="sr-only">${srLabel}：</span>${esc(formatDate(ts))}<em>${esc(age)}</em></span>`;
}

function statusTable(statuses, nowTs = Math.floor(Date.now() / 1000)) {
  if (!statuses.length) {
    return emptyState({ title: "还没有状态数据", text: "第一次推送后，这里会显示每个来源的最后巡检时间。" });
  }
  const details = statuses.map((status) => friendlyStatusDetail(status));
  const showDetail = details.some((detail) => detail && detail !== "—");
  return `<div class="status-table" role="table" aria-label="各来源监控状态">
    <div class="status-row status-head" role="row">
      <span role="columnheader">来源</span><span role="columnheader">状态</span><span role="columnheader">最后成功</span><span role="columnheader">最后信号</span>${showDetail ? `<span role="columnheader">说明</span>` : ""}
    </div>
    ${statuses.map((status, index) => `<div class="status-row" role="row">
      <span class="status-source" role="cell"><a href="/source/${attr(status.source)}">${esc(status.label || sourceLabel(status.source))}</a></span>
      <span role="cell"><span class="sr-only">状态：</span>${statusDot(status.status)} ${esc(STATUS_LABELS[status.status] || "待采集")}</span>
      ${statusTimeCell(status.lastSuccessTs, nowTs, "最后成功")}
      ${statusTimeCell(status.lastEventTs, nowTs, "最后信号")}
      ${showDetail ? `<span class="status-detail" role="cell"><span class="sr-only">说明：</span>${esc(details[index])}</span>` : ""}
    </div>`).join("")}
  </div>`;
}

function statusHistoryPanel(statusHistory = {}) {
  const rows = SOURCE_ORDER.map((source) => {
    // 手机端只放得下 14 格；桌面也统一用 14 格，避免“标题写 30、实际看到 14”。
    const entries = Array.isArray(statusHistory[source]) ? statusHistory[source].slice(0, 14) : [];
    const padded = [
      ...Array(Math.max(0, 14 - entries.length)).fill(null),
      ...entries.slice().reverse(),
    ];
    let okCount = 0;
    let warnCount = 0;
    let emptyCount = 0;
    const cells = padded.map((item) => {
      if (!item) {
        emptyCount += 1;
        return `<span class="history-cell is-empty" aria-label="暂无记录"></span>`;
      }
      const isOk = item.status === "ok";
      const isAbnormal = item.status === "warn" || item.status === "error";
      if (isOk) okCount += 1;
      else if (isAbnormal) warnCount += 1;
      const state = isOk ? "正常" : isAbnormal ? "抓取异常" : "待采集";
      const cls = isOk ? "is-ok" : isAbnormal ? "is-warn" : "is-unknown";
      const label = `${formatDate(item.checkedTs)} · ${state}`;
      return `<span class="history-cell ${cls}" title="${attr(label)}" aria-label="${attr(label)}"></span>`;
    }).join("");
    const summary = `最近 14 次巡检：正常 ${okCount} 次，异常 ${warnCount} 次，暂无记录 ${emptyCount} 次`;
    return `<div class="history-row">
      <a class="history-source" href="/source/${attr(source)}">${esc(sourceLabel(source))}</a>
      <div class="history-cells" role="img" aria-label="${attr(`${sourceLabel(source)}，${summary}`)}">${cells}</div>
    </div>`;
  }).join("");
  return `<div class="status-history">
    <div class="history-legend">
      <span><i class="history-legend-mark is-ok"></i>正常</span>
      <span><i class="history-legend-mark is-warn"></i>抓取异常</span>
      <span><i class="history-legend-mark is-unknown"></i>暂无记录</span>
    </div>
    <div class="history-scroll" tabindex="0" role="group" aria-label="最近巡检记录">${rows}</div>
  </div>`;
}

function relativeAgeText(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return "刚刚";
  if (value < 3600) return `${Math.floor(value / 60)} 分钟前`;
  if (value < 86400) return `${Math.floor(value / 3600)} 小时前`;
  return `${Math.floor(value / 86400)} 天前`;
}

function pipelineSourceLabel(source) {
  const key = String(source || "").trim();
  if (key === "watchsite") return "推送器";
  if (key === "docwatch") return "文档采集";
  if (key === "webwatch") return "网页采集";
  return key ? sourceLabel(key) : "采集端";
}

// 「采集与推送」面板：面向读者的只保留最近推送、站点事件总数和来源计数；
// 质量分布、推送结果、采集异常是运维口径，收进「技术明细」折叠区。
function pipelinePanel(pushMeta, nowTs, totalEvents = null) {
  const meta = pushMeta && typeof pushMeta === "object" ? pushMeta : {};
  const lastPushTs = Number(meta.lastPushTs) > 0 ? Number(meta.lastPushTs) : 0;
  const eventCount =
    meta.eventCount === null || meta.eventCount === undefined || !Number.isFinite(Number(meta.eventCount))
      ? null
      : Number(meta.eventCount);
  const countValue = (raw) =>
    raw === null || raw === undefined || !Number.isFinite(Number(raw)) ? null : Number(raw);
  const newEventCount = countValue(meta.newEventCount);
  const pushedCount = countValue(meta.pushedCount);
  const rejectedCount = countValue(meta.rejectedCount);
  const deadLetterCount = countValue(meta.deadLetterCount);
  const backlogCount = countValue(meta.backlogCount);
  const sourceCounts =
    meta.sourceCounts && typeof meta.sourceCounts === "object" && !Array.isArray(meta.sourceCounts)
      ? meta.sourceCounts
      : null;
  const qualityCounts =
    meta.qualityCounts && typeof meta.qualityCounts === "object" && !Array.isArray(meta.qualityCounts)
      ? meta.qualityCounts
      : null;
  const collectionErrors = Array.isArray(meta.collectionErrors) ? meta.collectionErrors : null;
  const siteTotal = totalEvents === null || totalEvents === undefined ? eventCount : Number(totalEvents);

  const sourceItems = sourceCounts
    ? SOURCE_ORDER.map(
        (source) => `<li><span>${esc(sourceLabel(source))}</span><b>${formatNumber(sourceCounts[source] || 0)}</b></li>`,
      ).join("")
    : "";
  const qualityItems = qualityCounts
    ? QUALITY_DISTRIBUTION_ORDER.filter((key) => Number(qualityCounts[key] || 0) > 0)
        .map(
          (key) =>
            `<li><b>${formatNumber(qualityCounts[key])}</b><span>${esc(QUALITY_DISTRIBUTION_LABELS[key] || key)}</span></li>`,
        )
        .join("")
    : "";
  const pushResultItems = [
    ["本轮新增", newEventCount],
    ["成功推送", pushedCount],
    ["待重试（被拒收）", rejectedCount],
    ["死信（不再重推）", deadLetterCount],
    ["积压未推送", backlogCount],
  ]
    .map(([label, value]) => `<li><span>${label}</span><b>${value === null ? "暂无" : formatNumber(value)}</b></li>`)
    .join("");
  const errorItems = (collectionErrors || [])
    .map((item) => {
      const source = item && typeof item === "object" ? String(item.source || "") : "";
      const detail = item && typeof item === "object" ? String(item.detail || "") : String(item || "");
      if (!detail) return "";
      return `<li>${source ? `<b class="pipeline-error-source">${esc(pipelineSourceLabel(source))}</b>` : ""}<span>${esc(detail)}</span></li>`;
    })
    .filter(Boolean)
    .join("");
  return `<section class="status-panel pipeline-panel">
    <div class="section-head"><div><p class="eyebrow">PIPELINE / 采集与推送</p><h2>采集与推送</h2></div><span class="mono">OPS VIEW</span></div>
    <div class="pipeline-cards">
      <div class="pipeline-card">
        <span class="pipeline-label">最近推送</span>
        <strong>${lastPushTs ? esc(formatDate(lastPushTs)) : "暂无"}</strong>
        <span class="pipeline-note">${lastPushTs ? `${esc(relativeAgeText(nowTs - lastPushTs))}更新` : "还没有收到推送记录"}</span>
      </div>
      <div class="pipeline-card">
        <span class="pipeline-label">站点事件总数</span>
        <strong>${siteTotal === null || !Number.isFinite(siteTotal) ? "暂无" : formatNumber(siteTotal)}</strong>
        <span class="pipeline-note">当前已入库的全部信号</span>
      </div>
      <div class="pipeline-card">
        <span class="pipeline-label">本轮推送</span>
        <strong>${pushedCount === null && newEventCount === null ? "暂无" : `${pushedCount === null ? "—" : formatNumber(pushedCount)} / ${newEventCount === null ? "—" : formatNumber(newEventCount)}`}</strong>
        <span class="pipeline-note">成功入库 / 本轮新增</span>
      </div>
    </div>
    <div class="pipeline-grid">
      <div class="pipeline-block">
        <h3>各来源计数</h3>
        ${sourceItems ? `<ul class="pipeline-source-list">${sourceItems}</ul>` : `<p class="pipeline-empty">暂无</p>`}
      </div>
    </div>
    <details class="pipeline-technical">
      <summary><span>技术明细</span><b>质量分布 / 推送结果 / 采集异常</b></summary>
      <p class="pipeline-note">推送端最近一轮统计的事件总数：${eventCount === null ? "暂无" : formatNumber(eventCount)}</p>
      <div class="pipeline-grid">
        <div class="pipeline-block">
          <h3>质量分布</h3>
          ${qualityItems ? `<ul class="pipeline-quality-list">${qualityItems}</ul>` : `<p class="pipeline-empty">暂无</p>`}
        </div>
        <div class="pipeline-block">
          <h3>推送结果</h3>
          <ul class="pipeline-source-list">${pushResultItems}</ul>
        </div>
        <div class="pipeline-block">
          <h3>采集异常</h3>
          ${errorItems ? `<ul class="pipeline-errors-list">${errorItems}</ul>` : `<p class="pipeline-empty">最近一轮推送没有报告采集异常。</p>`}
        </div>
      </div>
    </details>
  </section>`;
}

// 近 N 天的日期桶（从早到晚），rssStats 和 eventTrend 共用。
function dayWindow(days, initial) {
  const byDay = new Map();
  for (let index = days - 1; index >= 0; index -= 1) {
    const date = new Date(Date.now() - index * 86400 * 1000);
    byDay.set(dayKey(Math.floor(date.getTime() / 1000)), initial());
  }
  return byDay;
}

function barWidth(value, max) {
  return Math.max(0, Math.min(100, Math.round((value / max) * 100 / 5) * 5));
}

function rssStats(daily, todayEvents, days = 7) {
  const byDay = dayWindow(days, () => ({ total: 0, sources: {} }));
  for (const row of daily) {
    if (!byDay.has(row.day)) continue;
    const entry = byDay.get(row.day);
    entry.total += row.requests;
    entry.sources[row.source] = (entry.sources[row.source] || 0) + row.requests;
  }
  const rows = [...byDay.entries()];
  const max = Math.max(1, ...rows.map(([, value]) => value.total));
  const total = rows.reduce((sum, [, value]) => sum + value.total, 0);
  const todayKey = dayKey(Math.floor(Date.now() / 1000));
  const today = byDay.get(todayKey)?.total || 0;
  const breakdown = rows.reduce((acc, [, value]) => {
    for (const [source, count] of Object.entries(value.sources)) acc[source] = (acc[source] || 0) + count;
    return acc;
  }, {});
  return `<section class="stats-section">
    <div class="section-head">
      <div><p class="eyebrow">RSS PULLS / 拉取统计</p><h2>订阅拉取记录（近似）</h2></div>
      <span class="stats-total"><strong>${formatNumber(total)}</strong><span class="mono">LAST ${days} DAYS</span></span>
    </div>
    <div class="stats-today">
      <div><strong>${formatNumber(today)}</strong><span>今天记录到的拉取</span></div>
      <div><strong>${formatNumber(todayEvents)}</strong><span>今天新增事件</span></div>
      <div><strong>${formatNumber(rows[rows.length - 2]?.[1].total || 0)}</strong><span>昨天记录到的拉取</span></div>
    </div>
    <p class="stats-note">只记录日期、订阅源和次数。没有 IP、User-Agent、Cookie，也没有读者身份。为了控制数据库写入，每个运行实例每天每个 feed 最多记 1 次，所以这里显示的是拉取下限，不是真实拉取次数，更不等于订阅人数。</p>
    <div class="bars">
      ${rows.map(([day, value]) => {
        const width = barWidth(value.total, max);
        return `<div class="bar-row">
        <span class="bar-day mono">${esc(day.slice(5))}</span>
        <span class="bar-track"><i class="bar-fill-${width}"></i></span>
        <span class="bar-value mono">${formatNumber(value.total)}</span>
      </div>`;
      }).join("")}
    </div>
    <p class="stats-foot mono">SOURCE BREAKDOWN · ${Object.entries(breakdown).map(([source, count]) => `${esc(source === "all" ? "全部 feed" : sourceLabel(source))} ${formatNumber(count)}`).join(" · ") || "暂无拉取"}</p>
  </section>`;
}

function eventTrend(eventDaily, days = 7) {
  const byDay = dayWindow(days, () => 0);
  for (const row of eventDaily) {
    if (byDay.has(row.day)) byDay.set(row.day, row.count);
  }
  const rows = [...byDay.entries()];
  const max = Math.max(1, ...rows.map(([, count]) => count));
  const total = rows.reduce((sum, [, count]) => sum + count, 0);
  return `<section class="status-panel compact event-trend">
    <div class="section-head"><div><p class="eyebrow">EVENTS / 新增事件</p><h2>近 7 天新增</h2></div><span class="mono">${formatNumber(total)} EVENTS</span></div>
    <div class="bars">
      ${rows.map(([day, count]) => {
        const width = barWidth(count, max);
        return `<div class="bar-row">
        <span class="bar-day mono">${esc(day.slice(5))}</span>
        <span class="bar-track is-event"><i class="bar-fill-${width}"></i></span>
        <span class="bar-value mono">${formatNumber(count)}</span>
      </div>`;
      }).join("")}
    </div>
  </section>`;
}

export function renderStatus({ statuses, statusHistory = {}, daily, eventDaily = [], totalEvents, counts, lastIngest, pushMeta = null, todayEvents = 0, siteUrl, nowTs = Math.floor(Date.now() / 1000) }) {
  const freshness = dataFreshness(statuses, nowTs);
  const lastPush = Number(pushMeta?.lastPushTs) > 0 ? Number(pushMeta.lastPushTs) : Number(lastIngest) || 0;
  const staleNotice = freshness.stale
    ? `<div class="status-stale" role="alert">
        <b>数据已停更</b>
        <span>最后一次收到推送是 ${esc(formatDate(freshness.newestTs))}，已经 ${formatNumber(freshness.ageMinutes)} 分钟没有新数据。下面的来源状态停留在那次推送，不能代表现在。</span>
      </div>`
    : "";
  const body = `<main class="page-main status-page">
    <header class="page-head">
      <p class="eyebrow">SYSTEM / 监控状态</p>
      <div class="page-head-grid">
        <div>
          <h1>这条观测线还在正常工作吗？</h1>
          <p class="page-lead">Chat / Platform / Harness 网页与文档约每 15 分钟、模型目录约每 5 分钟、模型指纹约每 30 分钟巡检一次。状态异常时，网站会保留最后一次成功的记录，不伪装成新鲜数据。</p>
        </div>
        <div class="page-head-side">
          <span class="big-count">${formatNumber(totalEvents)}</span>
          <span class="mono">TOTAL SIGNALS</span>
          <span class="mono">${lastPush ? `上次推送 ${formatDate(lastPush)}` : "等待第一次推送"}</span>
        </div>
      </div>
    </header>
    ${staleNotice}
    <div class="status-layout">
      <section class="status-panel history-panel">
        <div class="section-head"><div><p class="eyebrow">RECENT CHECKS</p><h2>最近 14 次巡检</h2></div><span class="mono">绿色正常 · 橙色异常 · 灰色无记录</span></div>
        ${statusHistoryPanel(statusHistory)}
      </section>
      <section class="status-panel">
        <div class="section-head"><div><p class="eyebrow">SOURCE HEALTH</p><h2>来源健康度</h2></div><span class="mono">${statuses.length} SOURCES</span></div>
        ${statusTable(statuses, nowTs)}
      </section>
      ${pipelinePanel(pushMeta, nowTs, totalEvents)}
      ${rssStats(daily, todayEvents)}
      ${eventTrend(eventDaily)}
      <section class="status-panel compact">
        <div class="section-head"><div><p class="eyebrow">COVERAGE</p><h2>记录分布</h2></div></div>
        <div class="coverage-list">
          ${SOURCE_ORDER.map((source) => `<a href="/source/${source}"><span>${esc(sourceLabel(source))}</span><b>${formatNumber(counts[source] || 0)}</b></a>`).join("")}
        </div>
      </section>
    </div>
  </main>`;
  return layout({
    title: "监控状态",
    description: "DeepSeek 观测站的来源健康度、采集与推送状态、巡检节奏与 RSS 拉取统计。",
    path: "/status",
    body,
    activeNav: "status",
    siteUrl,
  });
}

function feedLink(href, label, siteUrl) {
  // RSS 阅读器需要完整地址；页面展示和复制都补上站点域名。
  const full = siteUrl ? `${siteUrl.replace(/\/$/, "")}${href}` : href;
  return `<div class="feed-link">
    <a href="${attr(href)}"><span>${esc(label)}</span><code>${esc(full)}</code></a>
    <button type="button" class="copy-button" data-copy="${attr(full)}" data-copy-name="订阅地址" aria-label="${attr(`复制${label}的 RSS 地址`)}">复制</button>
  </div>`;
}

export function renderAbout({ siteUrl }) {
  const body = `<main class="page-main about-page">
    <header class="page-head about-head">
      <p class="eyebrow">ABOUT / 关于本站</p>
      <h1><span class="nowrap">一个一起看信号的</span><span class="mark">变化观测站</span></h1>
      <p class="page-lead">这里记录 DeepSeek 文档、网页和模型指纹的变化，整理成可读的中文信号，再通过 RSS 交给你。本站是非官方项目，与 DeepSeek 及其关联公司无隶属或授权关系。</p>
    </header>
    <div class="about-grid">
      <article class="about-copy">
        <section>
          <p class="eyebrow">01 / WHAT IT DOES</p>
          <h2>只记录「发生了什么」</h2>
          <p>后台巡检五个公开来源：官方文档、Chat 网页、Platform 控制台、Harness 与模型指纹。检测到变化时，按规则判断级别，生成简短的影响与建议，并保留原始页面链接和证据。</p>
        </section>
        <section>
          <p class="eyebrow">02 / HOW TO READ</p>
          <h2>级别是一个路标，不是结论</h2>
          <div class="level-explain">
            ${[3, 2, 1, 0].map((level) => `<div>${levelBadge(level)}<p>${esc({ 3: "可能会影响价格、能力或兼容性，建议尽快看。", 2: "值得花几分钟确认，可能要调整用法。", 1: "轻微变化，知道即可。", 0: "构建、缓存或纯前端抖动，通常可以忽略。" }[level])}</p></div>`).join("")}
          </div>
        </section>
        <section>
          <p class="eyebrow">03 / PRIVACY</p>
          <h2>没有账号，也就不需要信任一个账号系统</h2>
          <p>全站只读，不使用 Cookie、第三方统计或账号系统。RSS 是唯一的订阅方式；拉取统计只按日期和来源累加，不能定位到个人。</p>
        </section>
        <section>
          <p class="eyebrow">04 / RSS</p>
          <h2>把变化放进你的阅读器</h2>
          <p>把地址粘进任意 RSS 阅读器即可；默认只推送级别 1 及以上的记录。</p>
          <div class="feed-links">
            ${feedLink("/feed.xml", "全部信号", siteUrl)}
            ${SOURCE_ORDER.map((source) => feedLink(`/feed/${source}.xml`, sourceLabel(source), siteUrl)).join("")}
          </div>
        </section>
        ${QQ_GROUP_NUMBER ? `<section id="qq">
          <p class="eyebrow">05 / COMMUNITY</p>
          <h2>有问题，来群里说</h2>
          <p>站点没有评论区和账号。发现误报、想加一个监控页面，或者只是聊聊，都可以来 QQ 群。</p>
          <div class="feed-links qq-links">
            <div class="feed-link">
              <span class="qq-label"><span>QQ 交流群</span><code>${esc(QQ_GROUP_NUMBER)}</code></span>
              <button type="button" class="copy-button" data-copy="${attr(QQ_GROUP_NUMBER)}" data-copy-name="群号" aria-label="复制 QQ 群号">复制群号</button>
            </div>
          </div>
        </section>` : ""}
        <section>
          <p class="eyebrow">06 / OPEN SOURCE</p>
          <h2>代码在 GitHub</h2>
          <p>项目以 MIT 许可开源；发现问题、想补充监控页面，欢迎开 Issue 或提交 PR。</p>
          <p><a class="button-quiet" href="${attr(REPO_URL)}" target="_blank" rel="noopener">打开 GitHub 仓库 ${svg("arrow")}</a></p>
        </section>
      </article>
    </div>
  </main>`;
  return layout({
    title: "关于",
    description: "DeepSeek 观测站是什么、如何阅读级别、隐私与 RSS 说明。",
    path: "/about",
    body,
    activeNav: "about",
    siteUrl,
  });
}

export function renderSubscribe({ siteUrl }) {
  const body = `<main class="page-main subscribe-page">
    <header class="page-head">
      <p class="eyebrow">SUBSCRIBE / 订阅</p>
      <h1><span class="nowrap">把变化放进</span><span class="mark">你的阅读器</span></h1>
      <p class="page-lead">不需要账号，也没有邮件打扰。复制一个 RSS 地址，粘进任意阅读器即可；默认只推送级别 1 及以上的记录。</p>
    </header>
    <div class="about-grid">
      <article class="about-copy">
        <section>
          <p class="eyebrow">01 / ALL SIGNALS</p>
          <h2>订阅全部信号</h2>
          <p>一个地址覆盖文档、Chat、Platform、Harness 和模型指纹五个来源。</p>
          <div class="feed-links">${feedLink("/feed.xml", "全部信号", siteUrl)}</div>
        </section>
        <section>
          <p class="eyebrow">02 / BY SOURCE</p>
          <h2>只订阅你关心的来源</h2>
          <p>按来源拆分，适合只想跟某一类变化的读者。</p>
          <div class="feed-links">
            ${SOURCE_ORDER.map((source) => feedLink(`/feed/${source}.xml`, sourceLabel(source), siteUrl)).join("")}
          </div>
        </section>
        <section>
          <p class="eyebrow">03 / HOW TO</p>
          <h2>怎么用</h2>
          <p>复制上面的地址 → 打开 Feedly、Inoreader、NetNewsWire 或任意 RSS 阅读器 → 添加订阅。网页和 RSS 的内容一致，不需要注册。</p>
          <p>不想用 RSS？可以用 Blogtrottr、Feedrabbit 这类“RSS 转邮件”服务，把地址填进去即可；本站不保存你的邮箱。</p>
        </section>
        <section>
          <p class="eyebrow">04 / OPEN SOURCE</p>
          <h2>想自己部署？</h2>
          <p>站点和采集器都在 GitHub 上，MIT 许可，部署说明见仓库 <code>docs/DEPLOY.md</code>。</p>
          <p><a class="button-quiet" href="${attr(REPO_URL)}" target="_blank" rel="noopener">打开 GitHub 仓库 ${svg("arrow")}</a></p>
        </section>
      </article>
    </div>
  </main>`;
  return layout({
    title: "订阅",
    description: "订阅 DeepSeek 观测站：全部信号或按来源拆分的 RSS，无需账号。",
    path: "/subscribe",
    body,
    activeNav: "",
    siteUrl,
  });
}

export function renderNotFound({ siteUrl }) {
  return layout({
    title: "页面不存在",
    description: "这个页面不存在。",
    path: "/404",
    body: `<main class="not-found"><p class="eyebrow">404 / LOST SIGNAL</p><h1>这条信号<br>没有找到</h1><p>链接可能已经过期，或者这个页面从未存在。</p><a class="button-primary" href="/">回到信号流 ${svg("arrow")}</a></main>`,
    activeNav: "",
    siteUrl,
    noIndex: true,
  });
}

export function renderSitemap(events, siteUrl) {
  const base = siteUrl.replace(/\/$/, "");
  const latestLastmod = sitemapLastmod(events[0]?.changedTs);
  const urls = [
    { path: "/", lastmod: latestLastmod },
    { path: "/about", lastmod: latestLastmod },
    { path: "/subscribe", lastmod: latestLastmod },
    { path: "/status", lastmod: latestLastmod },
    { path: "/feed.xml", lastmod: latestLastmod },
    ...SOURCE_ORDER.map((source) => ({ path: `/source/${source}`, lastmod: latestLastmod })),
    ...SOURCE_ORDER.map((source) => ({ path: `/feed/${source}.xml`, lastmod: latestLastmod })),
    ...events.map((event) => ({ path: `/e/${event.slug}`, lastmod: sitemapLastmod(event.changedTs) })),
  ];
  return `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map(({ path, lastmod }) => `  <url><loc>${esc(base + path)}</loc>${lastmod ? `<lastmod>${lastmod}</lastmod>` : ""}</url>`).join("\n")}
</urlset>`;
}

export function renderRobots(siteUrl) {
  const base = siteUrl.replace(/\/$/, "");
  return `User-agent: *
Allow: /
Disallow: /internal/

Sitemap: ${base}/sitemap.xml
`;
}

export { levelBadge, statusDot };
