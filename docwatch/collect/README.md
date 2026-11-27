# docwatch.collect —— 前端资源级采集与信号判定

把监控从「正文文本 diff」升级成「前端资源级指纹」：58 页 HTML + 24 个第一方 JS/CSS +
sitemap，任何一处变化都产 Signal，有信号时落一套给 agent 读的证据包。

```
fetch.py     条件抓取（ETag / If-Modified-Since / gzip / keep-alive / 6 并发 / 3 跳重定向）
asset.py     HTML 抽引用、Next.js RSC 正文、sitemap、压缩 JS 的可读字符串集合
signals.py   SQLite 快照 + diff_snapshot() + 证据包（manifest.json / BRIEF.md / pages / assets / diff）
selftest.py  离线确定性测试 + 真实站点验收
```

## 一、给 Lead 的调用方式

```python
from docwatch.collect import signals

job = signals.run_once("docwatch/data", mode="high")      # 一轮：抓 → 判定 → 落证据包
job["signals"]          # list[Signal]，没有变化时是 []
job["evidence_dir"]     # 有信号时是证据包目录，"": 基线/无变化
```

`run_once` 参数（都有默认值，日常只传 data_dir）：

| 参数 | 说明 |
| --- | --- |
| `data_dir` | 数据目录：库是 `<data_dir>/docs.db`，证据包在 `<data_dir>/jobs/<id>/` |
| `mode` | 档位 high/normal/quiet，只记录进 job，不影响采集 |
| `pages` / `limit` | 调试用：只跑指定页面 / 前 N 页（会标记 `partial`，不产生「资源消失」假信号） |
| `fetcher` | 可注入（自检里用 FakeFetcher，不联网） |
| `write_evidence_package` | 默认 True；False 时只更新库 |

CLI：`python -m docwatch.collect.signals --data-dir docwatch/data [--json] [--limit N] [--only URL]`

## 二、Job / Signal 字段

Job 除 CONTRACT §3.2 的字段外，多了：`baseline`、`partial`、`n_errors`、`duration_ms`、
`wire_bytes`、`assets_changed`（只算 JS/CSS）、`refs_changed`（图片 / 元信息引用变化处数）、
`run`（本轮请求数 / 304 数 / 下载字节 / 耗时）、`caps`（磁盘占用与淘汰数）。

Signal 除 §3.1 的 7 个字段外，还有给规则用的补充字段：

| 字段 | 用于 | 说明 |
| --- | --- | --- |
| `bytes_delta` | 所有信号 | 字节变化；图片 URL 变化记 0 |
| `action` | asset_url | `rename` / `added` / `removed` |
| `before_url` / `after_url` | asset_url | 改名前后地址 |
| `pages` | asset_url | 引用该资源的页面清单（`page` 为空表示多页共用） |
| `ref_kind` | asset_url | `img` / `other`（图片与 canonical/alternate 这类不算 JS/CSS 的引用） |
| `refs_only` | page_html | true = HTML 变化只来自资源文件名（站点重新构建，正文没改） |
| `before_sha` / `after_sha` | page_html / asset_content | 16 位短哈希 |
| `added_lines` / `removed_lines` / `changed_chars` | page_text | 段落级 diff 统计 |

## 三、证据包（§3.2）

```
jobs/<id>/
  manifest.json          Job + 每个页面条目（含 page_class / text_changed_chars / refs_added /
                         refs_removed）+ 每个资源条目
                         （含 strings_added / strings_removed / affected_page_classes）+ 信号 +
                         错误 + evidence_files 索引 + notes
  BRIEF.md               人读简报（≤200 行）：一句话结论、本轮数字、哪几页变了（表格）、
                         正文 diff、JS 字符串增删各 top 20、新/下线页面、抓取失败
  pages/<slug>.before.txt / .after.txt   正文真变了的页面的新旧正文
  assets/<slug>.before.txt / .after.txt  变化资源的新旧文本（截断到 200 KB）
  diff/<slug>.diff       统一 diff（页面=正文行；资源=字符串集合）
```

`slug` 由 URL 生成，规则与 `docwatch/toolbox.py` 的 `_slugish()` 完全一致
（全小写、非字母数字变 `-`，如 `https-api-docs-deepseek-com-zh-cn-quick-start-pricing`），
所以 `toolbox diff <URL>` 能直接命中证据文件；完整映射在 `manifest.json` 的
`pages[].slug` / `assets[].slug` 与 `evidence_files` 里。

## 四、数据库（表名统一 `v2_`，可与 v1 的 pages/changes 同库共存）

`v2_pages`（当前状态：etag/两个哈希/正文/refs/失败计数）、`v2_page_versions`（历史正文）、
`v2_assets`（当前状态：sha256/size/字符串集合）、`v2_asset_versions`（历史字符串集合）、
`v2_asset_blobs`（内容寻址的原文，按 sha256 去重，上限 200 MB 先进先出淘汰）、
`v2_sitemap`、`v2_jobs`、`v2_signals`（机器可读信号 + signal_json 原样副本）、`v2_meta`。

给 toolbox 的只读查询建议：

```sql
-- 一次作业的全部信号
SELECT kind, page, asset, detail, bytes_delta FROM v2_signals WHERE job_id = ? ORDER BY ord;
-- 最近 N 次作业
SELECT id, created_at, pages_changed, n_signals, n_errors, evidence_dir
  FROM v2_jobs ORDER BY id DESC LIMIT ?;
-- 某页面历史版本（toolbox diff --n 2）
SELECT ts, html_sha256, text FROM v2_page_versions WHERE url = ? ORDER BY id DESC LIMIT ?;
-- 某资源历史版本
SELECT ts, sha256, size FROM v2_asset_versions WHERE url = ? ORDER BY id DESC LIMIT ?;
-- 在所有历史快照里搜
SELECT url, ts FROM v2_page_versions WHERE text LIKE ? UNION
SELECT url, ts FROM v2_asset_versions WHERE strings_json LIKE ?;
```

## 五、自检与实测

```bash
python -m docwatch.collect.selftest --fake    # 离线 62 项断言，不联网，约 1 秒
python -m docwatch.collect.selftest --real    # 真实抓 58 页，26 项断言（含手工构造变化）
```

本机实测（Windows / 家用宽带，2026-11-03）：

| 项 | 实测 |
| --- | --- |
| 基线（58 页 200 + 24 资源 200 + sitemap） | 0.9 ~ 1.4 s，约 1.06 MB（资源 1.78 MB 原始内容） |
| 稳态（无变化） | **0.9 ~ 1.0 s，29.5 ~ 29.8 KB，58 页 + 24 资源 + sitemap 全部 304，0 信号** |
| 单页正文变化 | 3 条信号（page_html + page_text），约 205 KB（重新下载 1 页 + 1 资源） |
| 数据库 | 基线后约 2.2 MB（24 个资源原文 1.78 MB + 58 页正文 258 KB） |

## 六、与其它模块的对接（已联调）

- **rules.py**：直接吃 `job["signals"]`。每条的 `kind` / `page` / `bytes_delta` /
  `detail` / `before` / `after` 都有；`page_text` 额外带 `text_changed_chars`（规则用它区分措辞与实质改动）。
  `page_class` 只保留在 manifest 的页面条目里（BRIEF 分级展示用），信号里不再重复携带。
  `decide(manifest)` 也能直接跑（manifest 顶层铺了 id/created_at/mode/signals 等常用字段）。
- **toolbox.py**：`status` / `pages --changed` / `brief <id>` / `signals <id>` / `diff <URL>` /
  `search` 都在本模块产出的数据目录上验证过；证据文件名与 `_slugish()` 对齐，
  `diff` 能直接命中 `diff/<slug>.diff`；历史版本在 `v2_page_versions` / `v2_asset_versions`。
- **wake.py / render/**：只用 `job` + `verdict`，不依赖证据包内部结构。

## 七、取舍与已知限制

1. **资源每轮都用 ETag 条件请求验证**（24 个 / 约 7 KB / 0.3 s），而不是「文件名带哈希就跳过」。
   好处：同名文件被原地替换也能发现（`asset_content`）；代价是每轮多 24 个 304。
2. **页面正文优先用 `watch_docs.extract_page`**（v1 已调好），取不到时退回 `asset.text_lines_from_html`；
   官网首页（Next.js）优先用 RSC payload 抽正文（能拿到 356 行真实中文，比裸 HTML 的壳子强得多）。
3. **`fetch_error` 只在「从正常变成失败」那一轮产信号**，持续失败不每轮重复（否则一小时一封失败邮件）；
   失败明细每轮都写进 `job["n_errors"]` / `manifest.errors`，连续失败计数在 `v2_pages.fail_streak`。
   若要做「连续失败 N 次告警」，在 Lead 的 rules/pipeline 里读这两个字段即可。
4. **不做合并 / 去重 / 限流**（按 §7.5）：每页每类变化各一条信号；共用资源记成站点级（`page=""`），
   页面清单放在 `pages` 字段里。
5. **图片只比 URL/文件名，不下载内容**（97 个 img/source 引用），避免每轮多几 MB 流量。
6. **不监控第三方资源**（字体/统计脚本），只看 `deepseek.com` 域下的 js/css/img。
7. 局部跑（`--limit` / `--only`）不算「建好基线」，下一次全量跑仍按基线处理，避免调试把基线搞脏。
8. 证据包只保留最近 120 个作业目录（`_prune_jobs`），只删自己生成的数字目录。
