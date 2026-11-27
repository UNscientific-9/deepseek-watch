# frontend-visual（本地工具，不参与线上链路）

把保存下来的 DeepSeek 前端快照在本地静态服务里还原成可截图页面：Playwright 拦截
`/api/v0/*` 并返回 `scenarios/*.json` 里的假数据，不需要登录真实账号，也不连线上接口。

## 依赖

- Node.js
- Playwright（本机已安装即可；不在项目里新增依赖）

找 Playwright 的顺序：先 `require("playwright")`，再读 `PLAYWRIGHT_MODULE` 指向的目录。
浏览器优先用 `BROWSER_PATH`，否则尝试本机 Edge / Chrome。

## 用法

```bash
node tools/frontend-visual/shot.cjs \
  --snapshot <快照目录> \
  --scenario quota-dialog \
  --out .tmp/frontend-visual
```

`--snapshot` 目录需要包含 `page.html` 或 `index.html` 及其引用的相对路径资源。
`--all` 会跑 `scenarios/` 下全部场景。

## 边界

- 只读本地快照，不登录、不调用真实接口、不写线上数据。
- 场景文件是手工构造的假数据，不含账号、token 或真实用户信息。
- 输出截图属于一次性验证产物，放 `.tmp/`，不要提交。
