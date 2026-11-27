# Contributing / 参与贡献

感谢你愿意参与。请先阅读以下约定。

## 开发环境

- Python 3.12+
- Node.js 22+（仅 `watchcloud`）
- 不新增第三方运行时依赖：Python 只用标准库；`watchcloud` 只依赖 `wrangler` 作为开发工具。

## 本地验证

```bash
python -m compileall -q watchsite webwatch docwatch deploy
python -m unittest discover -s watchsite/tests -v
python -m unittest discover -s webwatch/tests -t . -v
python -m unittest discover -s deploy/tests -v
python -m docwatch.tests.test_docwatch_fixes -v
cd watchcloud && npm test
```

测试不得调用付费 API、不得发真实邮件：使用 `--dry-run`、`--no-api` 或假的 D1 实现。

## 提交约定

- 一个提交只做一件事，提交信息用简洁的中文或英文描述。
- 不要提交任何凭据、日志、运行状态、数据库文件、截图或 `node_modules`。
- 修改接口字段时，请同步更新数据流涉及的采集、校验、存储、展示与文档。
- 新增行为请补对应测试；改动前端资源后请按 `watchcloud` 的版本规范更新版本号。

## Pull Request

- 说明改了什么、为什么改、如何验证。
- 保持改动范围小而清晰；大改动请先开 issue 讨论。
- 提交前确认 `git status` 中没有无关文件。
