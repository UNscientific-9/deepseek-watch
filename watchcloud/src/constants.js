export const SITE_NAME = "DeepSeek 观测站";
export const SITE_NAME_EN = "DEEPSEEK SIGNAL / UNOFFICIAL";
export const SITE_TAGLINE = "把每一次变化，变成可读的信号。";
export const REPO_URL = "https://github.com/UNscientific-9/deepseek-watch";

export const SOURCES = {
  docs: {
    label: "官方文档",
    short: "DOCS",
    description: "api-docs.deepseek.com 与 www.deepseek.com 的正文、导航和前端资源。",
    cadence: "每 15 分钟",
  },
  chat: {
    label: "Chat 网页",
    short: "CHAT",
    description: "chat.deepseek.com 的公开页面快照，由自建网页监控抓取。",
    cadence: "每 15 分钟",
  },
  platform: {
    label: "Platform 控制台",
    short: "PLATFORM",
    description: "platform.deepseek.com 的公开页面、公告和构建指纹。",
    cadence: "每 15 分钟",
  },
  harness: {
    label: "Harness",
    short: "HARNESS",
    description: "DeepSeek Harness 公开页面快照，以及 GitHub release / master 提交 / NPM / 桌面端更新。",
    cadence: "页面与软件更新每 15 分钟",
  },
  fingerprint: {
    label: "模型指纹",
    short: "FINGERPRINT",
    description: "官方价格页的模型版本与 GET /models 当前模型目录；配置 API 后还会监控 system_fingerprint。",
    cadence: "模型目录每 5 分钟 · 指纹每 30 分钟",
  },
};

export const SOURCE_ORDER = ["docs", "chat", "platform", "harness", "fingerprint"];

export const LEVELS = {
  3: { label: "重要", short: "IMPORTANT", hint: "建议尽快看" },
  2: { label: "值得看", short: "WORTH A LOOK", hint: "可能要调整用法" },
  1: { label: "轻微", short: "MINOR", hint: "记录下来即可" },
  0: { label: "噪音", short: "NOISE", hint: "构建或缓存抖动" },
};

export const STATUS_LABELS = {
  ok: "正常",
  warn: "延迟",
  error: "异常",
  unknown: "待采集",
};

export function sourceLabel(source) {
  return SOURCES[source]?.label || source || "未知来源";
}

export function levelLabel(level) {
  return LEVELS[Number(level)]?.label || "噪音";
}

export function isSource(value) {
  return Object.prototype.hasOwnProperty.call(SOURCES, value);
}

export function isLevel(value) {
  return value === "0" || value === "1" || value === "2" || value === "3";
}
