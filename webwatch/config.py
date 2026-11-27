"""监控目标与运行参数。

页面 / 资源抓取默认只允许 DeepSeek 官方域名；新增目标时必须同时加进 ALLOWED_HOSTS。
DSH 软件更新使用专用 DSH_ALLOWED_HOSTS（通过 safe_fetch 的按调用白名单），不扩大默认边界。
"""

from __future__ import annotations

import os
from pathlib import Path

SITE_NAME = "DeepSeek 观测站"
SITE_URL = "https://deepseek-watch.pages.dev"

# 只抓这些官方域名；任何重定向到别的域名都会被 safe_fetch 拒绝。
ALLOWED_HOSTS = (
    "chat.deepseek.com",
    "platform.deepseek.com",
    "www.deepseek.com",
    "deepseek.com",
    "fe-static.deepseek.com",
    "api-docs.deepseek.com",
    "api.deepseek.com",
)

# DSH（DeepSeek Harness）软件更新的官方分发源。
# 这些域名只在 webwatch/dsh_updates.py 里通过 safe_fetch(allowed_hosts=...) 使用，
# 不加入上面的 ALLOWED_HOSTS，避免扩大页面 / 资源抓取的安全边界。
DSH_GITHUB_API = "https://api.github.com"
DSH_GITHUB_REPO = "deepseek-ai/deepseek-harness"
DSH_NPM_REGISTRY = "https://registry.npmjs.org"
DSH_NPM_PACKAGE = "@deepseek-ai/dsh"
DSH_DESKTOP_FEED_BASE = "https://download.deepseek.com/dsh-desk/feeds/win-x64"
DSH_ALLOWED_HOSTS = (
    "api.github.com",
    "registry.npmjs.org",
    "download.deepseek.com",
)

TARGETS = {
    "chat": {
        "label": "Chat 网页",
        "url": "https://chat.deepseek.com/",
        "max_assets": 8,
        "source": "chat",
    },
    "platform": {
        "label": "Platform 控制台",
        "url": "https://platform.deepseek.com/",
        "max_assets": 8,
        "source": "platform",
        "banner_api": "https://platform.deepseek.com/api/v0/client/settings",
    },
    "harness": {
        "label": "Harness",
        "url": "https://www.deepseek.com/harness/",
        "max_assets": 0,
        "source": "harness",
    },
}

# 末尾斜杠不能省：官方文档站 2026-11-20 起，无斜杠的目录路由会回退到
# “Your First API Call” 页面，价格页表格因此解析为空；带斜杠才是价格页。
MODEL_PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
# 官方 API 地址只在 webwatch/fingerprint.py 内以 API_HOST 常量出现；
# 这里不再重复定义，避免让人误以为还有别的地方会调用付费接口。

# 已知的 system_fingerprint 明文（官方 2026-08-13 起返回的是它的 MD5）。
# 新值未收录时原样显示摘要，不猜测。
FP_PLAINTEXTS = (
    "fp_a18b46594c_prod0820_fp8_kvcache_20260402_fp",
    "fp_v4pro_20260812_prod0820_fp8_kvcache_20260402_fp",
    "fp_v41f_0908_prod0820_fp8_kvcache_20260402_fp",
    "fp_dsv41f_0910_prod0820_fp8_kvcache_20260402_fp",
    "fp_dsv41f_1017_prod0820_fp8_kvcache_20260402_fp",
)

DEFAULT_STATE_DIR = Path(os.getenv("WEBWATCH_STATE_DIR", "/var/lib/deepseek-webwatch"))
DEFAULT_EVENTS_DIR = Path(os.getenv("WEBWATCH_EVENTS_DIR", str(DEFAULT_STATE_DIR / "events")))

# 单次任务的安全阀：只保留最近 MAX_EVENTS_KEPT 条事件文件。
MAX_EVENTS_KEPT = 800

# 事件级别：0 噪音，1 轻微，2 值得看，3 重要。
LEVEL_LABELS = {0: "噪音", 1: "轻微", 2: "值得看", 3: "重要"}
