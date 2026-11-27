import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const base = (process.env.SITE_URL || "https://deepseek-watch.pages.dev").replace(/\/$/, "");
const keyPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../public/indexnow-key.txt");
let key = (process.env.INDEXNOW_KEY || "").trim();
if (!key) {
  try {
    key = (await readFile(keyPath, "utf8")).trim();
  } catch {
    // 文件不存在时继续走下面的报错提示。
  }
}
const dryRun = process.argv.includes("--dry-run");

if (!/^[a-f0-9]{8,128}$/i.test(key)) {
  console.error("Set INDEXNOW_KEY (8-128 hex characters) or add public/indexnow-key.txt.");
  process.exit(1);
}

const host = new URL(base).host;
if (host === "localhost" || host === "127.0.0.1") {
  console.error("IndexNow only works against a public site; set SITE_URL to the production URL.");
  process.exit(1);
}

const response = await fetch(`${base}/sitemap.xml`);
if (!response.ok) {
  console.error(`Could not read sitemap: HTTP ${response.status}`);
  process.exit(1);
}

const xml = await response.text();
const urls = [...xml.matchAll(/<loc>(.*?)<\/loc>/g)].map((match) =>
  match[1].replaceAll("&amp;", "&").trim(),
);

if (!urls.length) {
  console.error("Sitemap contains no URLs.");
  process.exit(1);
}

const payload = {
  host,
  key,
  keyLocation: `${base}/indexnow-key.txt`,
  urlList: urls,
};

if (dryRun) {
  console.log(JSON.stringify({ ...payload, urlList: `${urls.length} URLs` }, null, 2));
  process.exit(0);
}

const result = await fetch("https://api.indexnow.org/indexnow", {
  method: "POST",
  headers: { "content-type": "application/json; charset=utf-8" },
  body: JSON.stringify(payload),
});

console.log(`IndexNow HTTP ${result.status}: ${urls.length} URL(s) submitted for ${host}`);
if (!result.ok) {
  console.error((await result.text()).slice(0, 1000));
  process.exit(1);
}
