#!/usr/bin/env node
/**
 * 前端快照视觉还原器（本地工具，不连线上、不登录真实账号）
 *
 * 原理：保存下来的前端代码是完整的，缺的只是接口数据。本工具在本地起静态服务，
 * 用 Playwright 拦截 /api/v0/* 请求并返回场景定义里的假数据，让前端以为已登录，
 * 从而渲染出登录后的真实界面并截图。
 *
 * 用法：
 *   node shot.cjs --snapshot <快照目录> --scenario quota-dialog --out <输出目录>
 *   node shot.cjs --snapshot <快照目录> --scenario quota-dialog --all
 *
 * 快照目录要求：包含 page.html 或 index.html，以及它引用的资源（相对路径）。
 */

const http = require("http");
const fs = require("fs");
const path = require("path");

const SCENARIO_DIR = path.join(__dirname, "scenarios");

function loadPlaywright() {
  // 只走标准解析或显式 PLAYWRIGHT_MODULE，不写死本机用户目录。
  const candidates = [process.env.PLAYWRIGHT_MODULE].filter(Boolean);
  try {
    return require("playwright");
  } catch (_) {
    /* 继续找 */
  }
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (_) {
      /* 继续找 */
    }
  }
  throw new Error(
    "找不到 playwright 模块。请设置 PLAYWRIGHT_MODULE 指向已安装的 playwright 目录。"
  );
}

function findBrowser(playwright) {
  const candidates = [
    process.env.BROWSER_PATH,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return undefined; // 交给 playwright 用自带浏览器
}

function parseArgs(argv) {
  const out = { themes: ["light"], viewports: ["desktop"] };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    const next = argv[i + 1];
    if (key === "--snapshot") out.snapshot = next, i += 1;
    else if (key === "--scenario") out.scenario = next, i += 1;
    else if (key === "--out") out.out = next, i += 1;
    else if (key === "--theme") out.themes = [next], i += 1;
    else if (key === "--viewport") out.viewports = [next], i += 1;
    else if (key === "--cdp") out.cdp = next, i += 1;
    else if (key === "--all") {
      out.themes = ["light", "dark"];
      out.viewports = ["desktop", "mobile"];
    }
  }
  if (!out.snapshot || !out.scenario || !out.out) {
    console.error("用法: node shot.cjs --snapshot <dir> --scenario <name> --out <dir> [--all] [--cdp http://127.0.0.1:9222]");
    process.exit(2);
  }
  return out;
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".wasm": "application/wasm",
};

function serve(root) {
  const server = http.createServer((req, res) => {
    let urlPath = decodeURIComponent((req.url || "/").split("?")[0]);
    if (urlPath === "/") urlPath = "/page.html";
    const file = path.normalize(path.join(root, urlPath));
    if (!file.startsWith(path.normalize(root))) {
      res.writeHead(403);
      res.end("forbidden");
      return;
    }
    fs.readFile(file, (err, data) => {
      if (err) {
        res.writeHead(404);
        res.end("not found");
        return;
      }
      res.writeHead(200, {
        "content-type": MIME[path.extname(file).toLowerCase()] || "application/octet-stream",
        "access-control-allow-origin": "*",
        "cache-control": "no-store",
      });
      res.end(data);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

function materialize(value, now) {
  const text = JSON.stringify(value)
    .replace(/\{\{now\+(\d+)\}\}/g, (_, n) => String(now + Number(n)))
    .replace(/\{\{now\}\}/g, String(now));
  return JSON.parse(text);
}

function loadScenario(name) {
  const file = path.join(SCENARIO_DIR, `${name}.json`);
  const scenario = JSON.parse(fs.readFileSync(file, "utf8"));
  const now = Math.floor(Date.now() / 1000);
  const mocks = {};
  for (const [route, payload] of Object.entries(scenario.mocks || {})) {
    mocks[route] = materialize(payload, now);
  }
  return { ...scenario, mocks };
}

function viewportSize(name) {
  if (name === "mobile") return { width: 390, height: 844 };
  return { width: 1280, height: 900 };
}

// 前端启动会读 localStorage 里的远程模型配置；不注入的话模型列表为空，很多界面逻辑不渲染。
const REMOTE_MODEL_STORE = {
  schemaVersion: 2,
  entries: {
    model_configs: {
      id: 1,
      value: [
        {
          model_type: "default",
          name: "快速模式",
          description: "",
          welcome_msg: "使用快速模式开始对话",
          is_default: true,
          enabled: true,
          switchable: true,
          show_model_name_in_session: false,
          edit_quota: 10000,
          regenerate_quota: 10000,
          input_character_limit: 2621440,
          think_feature: {},
          search_feature: {},
          regenerate_options: null,
          file_feature: {
            token_limit: 890880,
            token_limit_with_thinking: 890880,
            max_input_file_count: 20,
            max_upload_file_size: 104857600,
            support_file_exts: ["pdf", "png", "jpg", "txt", "md"],
            conflict_with_search: false,
            vision: true,
            enable_thumbnail: true,
          },
          prompt_feature: [],
          tips: {},
          camera_mode: "photo_edit",
        },
      ],
    },
  },
  timestamp: Date.now(),
  remoteVersion: 103,
};

async function shoot({ browser, server, scenario, outDir, theme, viewport }) {
  const port = server.address().port;
  const context = await browser.newContext({
    viewport: viewportSize(viewport),
    colorScheme: theme,
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();

  await page.addInitScript(
    ({ modelStore }) => {
      const put = (key, value) =>
        localStorage.setItem(key, JSON.stringify({ value, __version: "0" }));
      put("userToken", "fake-token-demo");
      put("__appKit_userInfo", { id: "demo-user-1" });
      put("__appKit_@deepseek/chat_lastSessionValue", {
        userIsMuted: false,
        userMuteUntil: 0,
        loginMethod: "code",
        siderCollapsed: false,
      });
      localStorage.setItem("__ds_remote_feature_store_model", JSON.stringify(modelStore));
      // 用户级存储里的额度快照：前端用它判断“这个模型额度已耗尽”，缺了它弹窗/提示条不渲染。
      // 注意这个键保存的是裸对象，没有 {value, __version} 包装（与真实站点一致）。
      localStorage.setItem(
        "__appKit_@deepseek/chat_demo-user-1_userStorage",
        JSON.stringify({
          pinnedSessionsFolded: false,
          usageQuotaSnapshot: { generatedAt: Date.now(), exhaustedScopes: [null] },
        })
      );
    },
    { modelStore: REMOTE_MODEL_STORE }
  );

  await page.route("**/api/v0/**", async (route) => {
    const url = route.request().url();
    const routePath = url.split("?")[0].replace(/^https?:\/\/[^/]+/, "");
    if (process.env.VISUAL_DEBUG) console.log(`[api] ${routePath}`);
    const payload =
      scenario.mocks[routePath] || {
        code: 0,
        msg: "",
        data: { biz_code: 0, biz_msg: "", biz_data: {} },
      };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
  });

  const entry = scenario.entry || "page.html";
  await page.goto(`http://127.0.0.1:${port}/${entry}`, { waitUntil: "domcontentloaded" });
  if (scenario.clickText) {
    try {
      const target = page.getByText(scenario.clickText, { exact: false }).first();
      if (await target.count()) {
        await target.click({ timeout: 4000 });
        await page.waitForTimeout(scenario.clickWaitMs || 2000);
      }
    } catch (_) {
      /* 点击失败不致命，继续等目标文本 */
    }
  }
  if (scenario.waitForText) {
    try {
      await page.waitForFunction(
        (text) => document.documentElement && document.documentElement.innerHTML.includes(text),
        scenario.waitForText,
        { timeout: scenario.timeoutMs || 20000 }
      );
    } catch (error) {
      fs.mkdirSync(outDir, { recursive: true });
      const debugFile = path.join(outDir, `debug-${scenario.name}-${theme}-${viewport}.png`);
      await page.screenshot({ path: debugFile, fullPage: true }).catch(() => {});
      const state = await page
        .evaluate(() => ({
          text: (document.body && document.body.innerText || "").slice(0, 400),
          storageKeys: Object.keys(localStorage).filter((k) => k.includes("model") || k.includes("appKit")),
          modelStore: (localStorage.getItem("__ds_remote_feature_store_model") || "").slice(0, 200),
        }))
        .catch(() => null);
      throw new Error(
        `等待「${scenario.waitForText}」超时；当前地址 ${page.url()}；调试截图 ${debugFile}；状态 ${JSON.stringify(state)}`
      );
    }
  } else {
    await page.waitForTimeout(scenario.waitMs || 6000);
  }
  await page.waitForTimeout(scenario.settleMs || 800);

  fs.mkdirSync(outDir, { recursive: true });
  const file = path.join(outDir, `${scenario.name}-${theme}-${viewport}.png`);
  await page.screenshot({
    path: file,
    fullPage: scenario.fullPage !== false,
    animations: "disabled",
  });
  await context.close();
  return file;
}

async function main() {
  const args = parseArgs(process.argv);
  const snapshot = path.resolve(args.snapshot);
  const entryCandidates = ["page.html", "index.html"];
  if (!entryCandidates.some((name) => fs.existsSync(path.join(snapshot, name)))) {
    throw new Error(`快照目录里没有 page.html / index.html：${snapshot}`);
  }
  const scenario = loadScenario(args.scenario);
  if (!scenario.entry) {
    scenario.entry = fs.existsSync(path.join(snapshot, "page.html")) ? "page.html" : "index.html";
  }
  const { chromium } = loadPlaywright();
  let browser;
  if (args.cdp) {
    // 连接已经启动好的浏览器（适合沙箱环境或复用已有实例）
    browser = await chromium.connectOverCDP(args.cdp);
  } else {
    const executablePath = findBrowser();
    browser = await chromium.launch({ executablePath, headless: true });
  }
  const server = await serve(snapshot);
  const outputs = [];
  try {
    for (const theme of args.themes) {
      for (const viewport of args.viewports) {
        const file = await shoot({
          browser,
          server,
          scenario,
          outDir: path.resolve(args.out),
          theme,
          viewport,
        });
        outputs.push(file);
        console.log(file);
      }
    }
  } finally {
    if (!args.cdp) await browser.close();
    server.close();
  }
  console.log(JSON.stringify({ scenario: scenario.name, outputs }, null, 2));
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
