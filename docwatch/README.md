# docwatch —— 文档站变化监控

`docwatch` 盯官方文档站与官网首页的公开页面、静态资源与 sitemap，发现变化后按规则定级，并可选用模型生成「改了什么、有什么影响」的解读，最后通过邮件通知。

## 特点

- 纯规则决定级别与是否发送；模型只负责解释，不能压级或拦截通知。
- 夜间（23:00–07:00）静默，积压到早上合并发送。
- 只用 Python 标准库，无需安装第三方依赖。

## 结构

```text
docwatch/
  collect/       抓取、资源引用抽取、正文提取、sitemap、SQLite 快照、信号、证据包
  rules.py       判级（0–3）与发送决策
  wake.py        可选：调用本机模型网关生成解读
  toolbox.py     给模型使用的调查命令
  render/        邮件排版（HTML + 纯文本）
  run.py         入口：编排采集、判级、解读、通知
  deploy/        systemd 单元示例
  AGENT.md       运行时提示词模板
```

## 常用命令

```bash
# 只采集、判级、排版，不发信、不调模型（仍会联网抓取）
python -m docwatch.run --dry-run --data-dir .tmp/docwatch

# 规则自检与邮件排版自检
python -m docwatch.rules
python -m docwatch.render.test_render

# 采集层离线自检
python -m docwatch.collect.selftest --fake
```

## 配置

| 变量 | 用途 |
|---|---|
| `SMTP_USER` / `SMTP_PASS` / `SMTP_HOST` | 邮件通知 |
| `DOCWATCH_*` | 数据目录、发送档位等，见 `docwatch/run.py` 的参数说明 |
| `WEBWATCH_LLM_URL` / 模型名 | 可选的 OpenAI 兼容网关；不配置则只用规则文案 |

生产环境请用 `--data-dir` 指向独立的数据目录，避免误读写其他实例的数据。
