# 部署说明

本文只给通用步骤，所有域名、ID、路径都需要替换成你自己的环境。

## 一、Cloudflare Pages + D1（站点）

1. 安装依赖并登录：

   ```bash
   cd watchcloud
   npm install
   npx wrangler login
   ```

2. 创建 D1 数据库：

   ```bash
   npx wrangler d1 create deepseek-watch-db
   ```

   把输出的 `database_id` 填到 `watchcloud/wrangler.toml` 的 `REPLACE_WITH_YOUR_D1_ID`。

3. 初始化表结构：

   ```bash
   npm run db:init
   ```

4. 设置写入 token（自定义一个足够长的随机串）：

   ```bash
   npx wrangler pages secret put INGEST_TOKEN --project-name deepseek-watch
   ```

5. 部署：

   ```bash
   npm run deploy
   ```

6. 如果使用 IndexNow，生成自己的 key（8–128 位十六进制），设置 `INDEXNOW_KEY` 环境变量或放置 `public/indexnow-key.txt`，然后执行：

   ```bash
   npm run seo:ping -- --dry-run   # 自检
   npm run seo:ping
   ```

## 二、采集器（Linux + systemd）

参考 `deploy/` 下的 unit 文件。通用步骤：

1. 把代码放到 `/opt/deepseek-watch`（或任意目录），创建运行用户。
2. 准备环境文件（权限 600），示例变量：

   ```bash
   DEEPSEEK_API_KEY=
   SMTP_USER=
   SMTP_PASS=
   SMTP_HOST=
   WEBWATCH_LLM_URL=
   WEBWATCH_LLM_MODEL=
   ```

3. 按需修改 `deploy/*.service` 中的 `User=`、`WorkingDirectory=`、`EnvironmentFile=`。
4. 安装并启用 timer：

   ```bash
   sudo cp deploy/deepseek-webwatch.service deploy/deepseek-webwatch.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now deepseek-webwatch.timer
   ```

5. 首次运行建议先 dry-run：

   ```bash
   python -m webwatch.run --dry-run --no-api
   ```

## 三、推送器

在服务器环境文件中配置：

```bash
INGEST_URL=https://<你的域名>/internal/ingest
INGEST_TOKEN=<与 Cloudflare secret 相同>
```

然后启用 `deploy/deepseek-watch-push.timer`。

## 四、通知（可选）

- 邮件：配置 `SMTP_USER` / `SMTP_PASS` / `SMTP_HOST`。
- QQ：需要自建 NapCat / OneBot v11 WebSocket，配置 `QQ_NOTIFY_ENABLED=1`、`QQ_NOTIFY_GROUPS`、`QQ_NOTIFY_WS` 等，详见 `watchsite/qq.py` 顶部说明。

## 五、验证

```bash
curl -sS https://<你的域名>/healthz
curl -sS -o /dev/null -w '%{http_code}\n' https://<你的域名>/feed.xml
```

状态页 `/status` 可以看到来源健康度与上次推送时间。
