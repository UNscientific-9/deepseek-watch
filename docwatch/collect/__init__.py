"""docwatch.collect —— 采集与信号判定（v2 前端资源级指纹）。

三段：
- fetch.py    条件抓取层：URL → bytes/ETag/大小，失败按类型分清
- asset.py    解析层：HTML 抽引用、Next.js RSC 正文、sitemap、JS 字符串集合
- signals.py  判定层：SQLite 快照 + 信号生成 + 证据包（manifest/BRIEF/diff）

对外只用 signals.run_once()，见该函数 docstring。
"""
