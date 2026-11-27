import { SITE_NAME, levelLabel, sourceLabel } from "./constants.js";
import { displayEventLevel, displayEventSummary, displayEventTitle } from "./render.js";

function rfc822(ts) {
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return new Date().toUTCString();
  return date.toUTCString();
}

function clean(value) {
  // XML 1.0 不允许大部分控制字符；外部来源的摘要里偶尔会混进来。
  return String(value ?? "").replace(
    /[\u0000-\u0008\u000B\u000C\u000E-\u001F\uFFFE\uFFFF]/g,
    "",
  );
}

function xml(value) {
  return clean(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function dshReleaseNoteText(event) {
  if (event.kind !== "dsh_release") return "";
  const blocks = Array.isArray(event.payload?.release_note?.blocks)
    ? event.payload.release_note.blocks
    : [];
  const lines = [];
  for (const block of blocks.slice(0, 1000)) {
    const type = String(block?.type || "");
    const text = String(block?.text ?? "").trim();
    if (!text) continue;
    // RSS description 按纯文本处理：不把 Markdown 的 ## / - 原样丢给阅读器。
    if (type === "heading") lines.push(`【${text}】`);
    else if (type === "bullet") lines.push(`・${text}`);
    else lines.push(text);
  }
  return lines.join("\n");
}

export function renderFeed({ events, source = "", siteUrl, siteName = SITE_NAME }) {
  const base = siteUrl.replace(/\/$/, "");
  const isSourceFeed = Boolean(source);
  const title = isSourceFeed ? `${siteName} · ${sourceLabel(source)}` : `${siteName} · 全部信号`;
  const link = isSourceFeed ? `${base}/source/${source}` : base;
  const self = isSourceFeed ? `${base}/feed/${source}.xml` : `${base}/feed.xml`;
  const description = isSourceFeed
    ? `DeepSeek 观测站：${sourceLabel(source)}的公开变化记录。`
    : "DeepSeek 观测站：DeepSeek 公开页面、文档与模型指纹的可读变化记录。";
  const lastBuild = events.length ? rfc822(events[0].changedTs) : new Date().toUTCString();
  const items = events.map((event) => {
    const url = `${base}/e/${encodeURIComponent(event.slug)}`;
    const title = displayEventTitle(event) || event.title;
    // DSH 发版不做省流：RSS 也直接带完整更新日志，读者不必为了看说明再点开。
    // 有完整日志时不再追加「官方更新日志共 N 条」这句重复计数。
    const noteText = dshReleaseNoteText(event);
    const summary = [
      noteText || displayEventSummary(event),
      event.impact ? `影响：${event.impact}` : "",
      event.action ? `建议：${event.action}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    return `    <item>
      <title>${xml(`[${levelLabel(displayEventLevel(event))}] ${title}`)}</title>
      <link>${xml(url)}</link>
      <guid isPermaLink="false">${xml(event.id)}</guid>
      <pubDate>${rfc822(event.changedTs)}</pubDate>
      <category>${xml(sourceLabel(event.source))}</category>
      <description>${xml(summary || title)}</description>
    </item>`;
  }).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>${xml(title)}</title>
    <link>${xml(link)}</link>
    <atom:link href="${xml(self)}" rel="self" type="application/rss+xml" />
    <description>${xml(description)}</description>
    <language>zh-cn</language>
    <lastBuildDate>${lastBuild}</lastBuildDate>
    <ttl>60</ttl>
    <docs>https://www.rssboard.org/rss-specification</docs>
    <generator>DeepSeek Signal</generator>
${items}
  </channel>
</rss>
`;
}
