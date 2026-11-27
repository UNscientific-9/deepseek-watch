# Security Policy / 安全策略

## 报告安全问题

请不要在公开 issue 中披露安全问题、密钥或可利用细节。请使用 GitHub 的 **Security → Report a vulnerability**（Security Advisory）私下报告，并附上：

- 受影响的版本或提交
- 复现步骤
- 影响范围
- 建议的修复方式（如果有）

我们会尽快确认并回复。

## 密钥与凭据

本仓库不包含任何真实密钥。以下内容永远不应提交到仓库：

- `.env`、`.env.*`、`apikey.env`、`watchcloud/.dev.vars`
- `*.key`、`*.pem`、`*.p12`、`*.pfx`
- SMTP 授权码、API Key、Cloudflare Token、QQ / NapCat Token
- 服务器 SSH 私钥、`auth.json`、浏览器 Cookie

如果你在本仓库历史或文件中发现疑似真实凭据，请立即通过 Security Advisory 联系我们，不要公开传播。

## 支持范围

安全修复会优先进入最新主分支。历史版本不单独维护。
