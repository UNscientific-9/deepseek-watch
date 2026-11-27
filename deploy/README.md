# deploy —— Linux systemd 单元示例

本目录提供采集器、推送器与可选复核服务的 systemd 单元示例。所有服务只做出站 HTTPS，不新开公网端口。

## 文件

| 文件 | 用途 |
|---|---|
| `deepseek-webwatch.service` / `.timer` | 网页 / DSH 更新 / 模型目录 / 价格页 / 指纹巡检 |
| `deepseek-watch-push.service` / `.timer` | 把事件推送到站点 |
| `deepseek-recheck.service` / `.timer` | 可选：用本机 Claude Code 复核新前端事件（高级功能，按需启用） |
| `docwatch/deploy/docwatch-v2.service` / `.timer` | 文档站巡检 |

## 通用部署步骤

1. 创建目录与运行用户（示例）：

   ```bash
   sudo useradd --system --home /opt/deepseek-watch --shell /usr/sbin/nologin deepseek-watch
   sudo install -d -m 0755 /opt/deepseek-watch
   ```

2. 把仓库代码放到 `/opt/deepseek-watch`，按需调整 unit 中的 `User=`、`WorkingDirectory=`、`EnvironmentFile=`。

3. 准备环境文件（权限 600）：

   ```bash
   sudo install -m 0600 -o root -g root /dev/null /opt/deepseek-watch/env
   ```

   常用变量：

   ```bash
   DEEPSEEK_API_KEY=        # 可选：API 指纹 / 余额探测
   SMTP_USER=               # 可选：邮件通知
   SMTP_PASS=
   SMTP_HOST=
   INGEST_URL=https://<你的域名>/internal/ingest
   INGEST_TOKEN=<与 Cloudflare secret 相同>
   QQ_NOTIFY_ENABLED=0
   ```

4. 安装并启用 timer：

   ```bash
   sudo cp deploy/deepseek-webwatch.service deploy/deepseek-webwatch.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now deepseek-webwatch.timer
   ```

5. 首次运行建议先 dry-run：

   ```bash
   python -m webwatch.run --dry-run --no-api
   python -m watchsite.push --dry-run
   ```

## 验证

```bash
systemctl list-timers 'deepseek-*'
journalctl -u deepseek-webwatch.service -n 50 --no-pager
```

更完整的站点部署见 [docs/DEPLOY.md](../docs/DEPLOY.md)。
