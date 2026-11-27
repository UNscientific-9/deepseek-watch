# watchcloud —— 只读站点（Cloudflare Pages + D1）

`watchcloud` 是 DeepSeek Watch 的站点层：Cloudflare Pages Functions 提供页面与 API，D1 存事件。采集器通过 `POST /internal/ingest` 写入，站点对外只读。

## 结构

```text
watchcloud/
  functions/[[path]].js    Pages Functions 入口
  src/
    constants.js           站点名、五个来源、级别
    app.js                 路由、页面、feed、sitemap、healthz
    db.js                  D1 查询与写入
    ingest.js              /internal/ingest 鉴权、校验、幂等写入
    render.js              服务端 HTML
    rss.js                 RSS 2.0 + XML 转义
  public/                  静态资源
  schema.sql               D1 建表
  tests/                   Node 内置 test
  wrangler.toml            Pages 项目与 D1 绑定
```

## 本地开发

```bash
cd watchcloud
npm install
cp .dev.vars.example .dev.vars       # 本地测试 token，不会提交
npm run db:init:local                # 建本地 D1 表
npm run dev                          # http://127.0.0.1:8788
```

没有数据时首页、`/status`、`/feed.xml` 都能正常打开，显示空状态。

## 部署

```bash
cd watchcloud
npx wrangler login
npx wrangler d1 create deepseek-watch-db
# 把输出的 database_id 填到 wrangler.toml
npm run db:init
npx wrangler pages project create deepseek-watch --production-branch main
npx wrangler pages secret put INGEST_TOKEN --project-name deepseek-watch
npm run deploy
```

更完整的说明见 [docs/DEPLOY.md](../docs/DEPLOY.md)。

## 数据接口

`POST /internal/ingest`，`Authorization: Bearer <INGEST_TOKEN>`，JSON：

```json
{
  "events": [],
  "statuses": []
}
```

- 最多 100 条事件 / 次，单次请求体上限 4 MB。
- 事件按 `id` 幂等 upsert。
- `source` 只允许 `docs|chat|platform|harness|fingerprint`；`level` 0–3。
- 成功返回 200，单条非法事件跳过并计入 `rejected_count`；请求级错误返回 400；无 token / 错 token 返回 401；请求体超限返回 413。

## 测试

```bash
npm test
```
