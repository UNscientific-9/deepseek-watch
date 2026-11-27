// 轻量级应用层限流：按 Cloudflare 传入的客户端 IP 做固定窗口计数。
//
// Pages Functions 没有内置的分布式限流，这里用 isolate 内存做第一道闸门：
// - 单 IP 每分钟最多 30 次 ingest、60 次 feed、120 次普通页面/healthz；
// - 最多保留 5000 个 key，超限先清理过期项，再淘汰最旧的 key，避免内存无限增长；
// - 只信任 Cloudflare 覆盖写入的 cf-connecting-ip，不信任客户端可伪造的 x-real-ip；
// - 真正的分布式攻击仍由 Cloudflare 边缘防护兜底，这层主要防脚本刷接口和写放大。

const LIMITS = {
  ingest: { max: 30, windowMs: 60_000 },
  feed: { max: 60, windowMs: 60_000 },
  page: { max: 120, windowMs: 60_000 },
  health: { max: 120, windowMs: 60_000 },
};

const MAX_BUCKETS = 5000;
const buckets = new Map();

function prune(now) {
  for (const [key, entry] of buckets) {
    if (!entry || entry.reset <= now) buckets.delete(key);
  }
  while (buckets.size >= MAX_BUCKETS) {
    const oldest = buckets.keys().next().value;
    if (oldest === undefined) break;
    buckets.delete(oldest);
  }
}

export function rateLimit(request, kind) {
  const config = LIMITS[kind] || LIMITS.page;
  const ip = (request.headers.get("cf-connecting-ip") || "unknown").trim().slice(0, 80);
  const key = `${kind}:${ip}`;
  const now = Date.now();
  let entry = buckets.get(key);
  if (!entry || entry.reset <= now) {
    prune(now);
    entry = { count: 0, reset: now + config.windowMs };
    buckets.set(key, entry);
  }
  entry.count += 1;
  if (entry.count > config.max) {
    return {
      limited: true,
      retryAfter: Math.max(1, Math.ceil((entry.reset - now) / 1000)),
    };
  }
  return { limited: false };
}
