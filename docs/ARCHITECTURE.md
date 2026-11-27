# 架构说明

本项目由三部分组成：采集器、推送器、只读站点。

```text
┌─────────────────────────── Linux 服务器 / 任意常驻主机 ───────────────────────────┐
│                                                                                   │
│  docwatch（文档站 + 官网首页）                                                     │
│      │ 事件 JSON + 状态                                                            │
│      ▼                                                                            │
│  webwatch（网页 / DSH 更新 / 模型目录 / 价格页 / 指纹）                             │
│      │ 事件 JSON + 状态                                                            │
│      ▼                                                                            │
│  watchsite.push ──按事件 id 去重、分批──► POST /internal/ingest                     │
│                                                                                   │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │ HTTPS + Bearer Token
                                        ▼
┌────────────────────────────── Cloudflare Pages + Functions ───────────────────────┐
│  /internal/ingest  → 校验 → D1 upsert                                              │
│  /、/e/:id、/status、/about、/subscribe、/feed.xml、/feed/:source.xml、/sitemap.xml、/healthz │
└───────────────────────────────────────────────────────────────────────────────────┘
```

## 1. 采集器

### docwatch

- 目标：官方文档站与官网首页的公开页面。
- 流程：抓取 HTML / JS / CSS / sitemap → 归一化 → 规则定级 → 可选模型解读 → 写事件。
- 级别由 `docwatch/rules.py` 决定；模型只能写解读，不能压级或拦截通知。
- 数据目录默认由 `--data-dir` 指定，生产环境不要使用代码默认值。

### webwatch

- 目标：Chat / Platform / Harness 页面、DSH 软件更新、模型目录、价格页版本、可选 API 指纹。
- 安全抓取：所有页面请求必须经过 `webwatch/safe_fetch.py`，只允许官方域名、HTTPS、拒绝内网地址和越权重定向。
- 前端版本仓库保存在状态目录下的 `bundle-vault/`，用于对比历史版本。
- 输出事件到 `WEBWATCH_EVENTS_DIR`，状态写到 `WEBWATCH_STATE_DIR`。

## 2. 事件契约

来源枚举（硬契约）：

```text
docs | chat | platform | harness | fingerprint
```

级别：

```text
0 噪音 | 1 轻微 | 2 值得看 | 3 重要
```

来源状态：`ok | warn | error | unknown`。

事件字段使用 snake_case 推送到服务端，服务端内部转 camelCase。字段上限、请求体 4 MB、每批服务端最多 100 条，详见 `watchcloud/src/ingest.js`。

## 3. 推送器

`watchsite.push` 只读采集器产生的事件与状态：

- 按事件 id 去重，`sent_ids` 最多保留 5000 个。
- 每批最多 50 条，单请求不超过 3.5 MB，超出按 UTF-8 字节自动分块。
- 服务端返回 `rejected_count > 0` 时，只把 accepted id 标记为已发送；被拒事件下一轮重试。
- 可选邮件通知与 QQ 播报；失败只记日志，不影响网站推送。

## 4. 站点

`watchcloud/` 是 Cloudflare Pages 项目：

- `functions/[[path]].js` 是入口，`src/` 下按职责拆分。
- D1 表：`events`、`source_status`、`status_history`、`meta`，见 `schema.sql`。
- 唯一写入口：`POST /internal/ingest`，需要 `INGEST_TOKEN`。
- 页面只读，无账号、无 Cookie、无第三方统计。
- RSS / sitemap 由 Functions 动态生成。

## 5. 安全与隐私

- 所有密钥通过环境变量注入，不写入仓库。
- 抓取只允许官方公开域名，拒绝内网地址。
- 日志与事件写入前经过脱敏：`access_token`、`api_key`、Cookie、Authorization 等不会落盘。
- 站点不保存访问者身份；RSS 拉取统计只按日期与来源累加。
- 本项目为非官方项目，与 DeepSeek 无隶属关系。
