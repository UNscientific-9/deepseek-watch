"""4 个用例的预览：渲染 HTML 到 docwatch/render/out/，同时打印纯文本版。

跑法（仓库根目录）：
    python -m docwatch.render.preview
产物：
    docwatch/render/out/case-1-price.html   ①重要价格变化（含 diff）
    docwatch/render/out/case-2-notable.html     ②值得看（接口文档新增参数，两张卡）
    docwatch/render/out/case-3-noise.html       ③噪音（前端重建、正文无变化）
    docwatch/render/out/case-4-empty.html       ④缺字段 / 空数据
    docwatch/render/out/index.html              四个用例的索引页
每个 case-*.html 双击就能用浏览器打开，宽度按手机/桌面自适应（外层 640px）。
"""

from __future__ import annotations

import pathlib
import sys

try:  # python -m docwatch.render.preview
    from .mail import render
except ImportError:  # 直接 python docwatch/render/preview.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from docwatch.render.mail import render

OUT_DIR = pathlib.Path(__file__).resolve().parent / "out"

# ---------------------------------------------------------------- ④ 之外三个真实感用例

VERDICT_PRICE = {
    "level": 3,
    "noise": False,
    "headline": "高峰时段输出价 13.5 → 15 元/百万 tokens",
    "what_changed": "pricing 页价格表：v4-pro 输出价 13.50 → 15.00 元/百万 tokens，"
                    "缓存与优惠时段价格同步上调，版本号 0813 → 1104。",
    "why_matters": "按现有用量估算，高峰调用成本约 +11%。预算和对外报价要跟着调，否则下个月会超支。",
    "action": "无需操作；若按量采购，下月起预算 +10%。",
    "evidence": ["价格页 HTML 变化", "main.js 重新构建", "价格行 diff 第 41-43 行"],
    "model_note": "只动了价格页，别的页面和接口文档都没变，应该是一次单独的价格调整。",
    "model": "example-model-d",
    "elapsed_ms": 41200,
}

PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"

JOB_PRICE = {
    "id": 17,
    "created_at": "2026-11-04T09:30:12+08:00",
    "mode": "high",
    "pages_total": 58,
    "pages_changed": 1,
    "assets_total": 24,
    "signals": [
        {
            "kind": "page_html",
            "page": PRICING_URL,
            "detail": "输出价 13.50 → 15.00 元/百万 tokens（高峰）",
            "before": "deepseek-chat 输入 2.00 元/百万 tokens\n"
                      "deepseek-chat 输出 8.00 元/百万 tokens\n"
                      "deepseek-v4-pro 输入 4.00 元/百万 tokens\n"
                      "deepseek-v4-pro 输出 13.50 元/百万 tokens\n"
                      "缓存命中 输入 0.50 元/百万 tokens\n"
                      "缓存未命中 输入 4.00 元/百万 tokens\n"
                      "高峰时段 08:30-00:30（价格 ×1.0）\n"
                      "优惠时段 00:30-08:30（价格 ×0.5）",
            "after": "deepseek-chat 输入 2.00 元/百万 tokens\n"
                     "deepseek-chat 输出 9.00 元/百万 tokens\n"
                     "deepseek-v4-pro 输入 4.00 元/百万 tokens\n"
                     "deepseek-v4-pro 输出 15.00 元/百万 tokens\n"
                     "缓存命中 输入 0.60 元/百万 tokens\n"
                     "缓存未命中 输入 4.80 元/百万 tokens\n"
                     "高峰时段 08:30-00:30（价格 ×1.2）\n"
                     "优惠时段 00:30-08:30（价格 ×0.6）",
        },
        {
            "kind": "page_html",
            "page": PRICING_URL,
            "detail": "v4-pro 版本号 0813 → 1104",
            "before": "版本号 0813",
            "after": "版本号 1104",
        },
        {
            "kind": "asset_url",
            "page": PRICING_URL,
            "asset": "https://api-docs.deepseek.com/zh-cn/assets/js/main.9ab31f02.js",
            "detail": "main.js 文件名哈希变了（0c15113f → 9ab31f02）",
        },
    ],
    "evidence_dir": "/opt/docwatch/data/jobs/17",
}

VERDICT_API = {
    "level": 2,
    "noise": False,
    "headline": "create-chat-completion 新增 file 参数说明",
    "why_matters": "做文件问答时可以直接把上传后的文件 id 传进来，老调用方式不用改。",
    "action": "暂不用改代码；需要文件问答时按新参数加一个字段即可。",
    "evidence": ["API 文档正文新增 10 行", "请求体参数表多一行 file", "示例代码更新"],
    "model_note": "只新增字段、没删旧字段，兼容老调用；常见问题页只是改了措辞。",
    "model": "example-model-d",
    "elapsed_ms": 38700,
    "cards": [
        {
            "level": 2,
            "page": "https://api-docs.deepseek.com/zh-cn/api/create-chat-completion",
            "tag": "接口文档",
            "headline": "请求体新增 file 字段",
            "bullets": [
                "请求体 messages 数组新增 file 字段，可传上传后的文件 id",
                "示例代码补了 10 行文件问答用法",
            ],
            "diff": [
                "＋ messages[].file: 可选，文件 id，和 content 二选一",
                "＋ 文件问答示例：client.chat.completions.create(",
                "＋   model=\"deepseek-v4-pro\",",
                "＋   messages=[{\"role\": \"user\", \"file\": file_id}],",
                "＋ )",
                "＋ 说明：file 只对 v4-pro 及以上模型生效",
                "＋ 说明：单个请求最多 3 个文件",
                "＋ 说明：单文件上限 16 MB",
                "＋ 说明：文件有效期 7 天",
                "＋ 说明：旧参数 file_id 仍然可用，后续可能下线",
            ],
            "action": "用到文件问答时，按新参数改一行即可。",
        },
        {
            "level": 1,
            "page": "https://api-docs.deepseek.com/zh-cn/faq",
            "tag": "常见问题",
            "headline": "一条 Q&A 的措辞改了",
            "bullets": ["「如何计费」的回答换了个说法，意思没变"],
        },
    ],
}

JOB_API = {
    "id": 18,
    "created_at": "2026-11-04T11:02:47+08:00",
    "mode": "high",
    "pages_total": 58,
    "pages_changed": 2,
    "assets_total": 24,
    "signals": [
        {
            "kind": "page_html",
            "page": "https://api-docs.deepseek.com/zh-cn/api/create-chat-completion",
            "detail": "正文新增 file 参数说明（10 行）",
        },
        {
            "kind": "page_html",
            "page": "https://api-docs.deepseek.com/zh-cn/faq",
            "detail": "一条 Q&A 措辞调整",
        },
    ],
    "evidence_dir": "/opt/docwatch/data/jobs/18",
}

VERDICT_NOISE = {
    "level": 0,
    "noise": True,
    "headline": "前端重新构建，正文无变化",
    "what_changed": "main.js 重新打包（哈希 0c15113f → 9ab31f02），页面正文与价格都没动。",
    "why_matters": "只是前端构建产物换了，页面文字和价格都没变，可以忽略。",
    "action": "无需操作。",
    "evidence": ["main.js 文件名哈希变了", "价格页正文逐字一致"],
    "model_note": "这类重建一天可能出现几次，属于正常发版。",
    "model": "example-model-d",
    "elapsed_ms": 22400,
}

JOB_NOISE = {
    "id": 19,
    "created_at": "2026-11-04T14:05:41+08:00",
    "mode": "high",
    "pages_total": 58,
    "pages_changed": 1,
    "assets_total": 24,
    "signals": [
        {
            "kind": "asset_url",
            "page": PRICING_URL,
            "asset": "https://api-docs.deepseek.com/zh-cn/assets/js/main.9ab31f02.js",
            "detail": "main.js 文件名哈希变了（0c15113f → 9ab31f02）",
        },
    ],
    "evidence_dir": "/opt/docwatch/data/jobs/19",
}

# ④ 缺字段 / 空数据：verdict 是空对象，job 没有 created_at / mode / signals
VERDICT_SPARSE = {}
JOB_SPARSE = {"id": 20, "pages_total": 58, "signals": []}

CASES = [
    {"key": "case-1-price", "name": "① 重要价格变化（含 diff，单张卡）",
     "verdict": VERDICT_PRICE, "job": JOB_PRICE},
    {"key": "case-2-notable", "name": "② 值得看（接口文档新增参数，两张卡）",
     "verdict": VERDICT_API, "job": JOB_API},
    {"key": "case-3-noise", "name": "③ 噪音（前端重建、正文无变化）",
     "verdict": VERDICT_NOISE, "job": JOB_NOISE},
    {"key": "case-4-empty", "name": "④ 缺字段 / 空数据（verdict 空对象，只有页数）",
     "verdict": VERDICT_SPARSE, "job": JOB_SPARSE},
]


# ---------------------------------------------------------------- 输出


def _index_html(rows: list) -> str:
    items = []
    for case, result, path in rows:
        items.append(
            '<tr><td style="padding:12px 0;border-top:1px solid #e8eaed;">'
            f'<a href="{path.name}" style="color:#2b6cb0;font-size:15px;text-decoration:none;'
            f'font-weight:700;">{case["name"]}</a>'
            f'<p style="margin:6px 0 0;font-size:15px;color:#6b7280;">'
            f'标题：{result["subject"]}</p></td></tr>'
        )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>docwatch 邮件版式预览</title></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f4f5f7;"><tr><td align="center" style="padding:24px 8px;">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:640px;background:#ffffff;border:1px solid #e2e5e9;border-radius:12px;'
        'font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:15px;color:#1f2328;'
        'line-height:1.65;text-align:left;"><tr><td style="padding:20px;">'
        '<p style="margin:0;font-size:21px;font-weight:700;color:#111827;">docwatch 邮件版式预览</p>'
        '<p style="margin:8px 0 0;font-size:15px;color:#6b7280;">点下面的链接打开每一个用例，'
        '手机宽度看效果也可以在浏览器里缩窄窗口。</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        + "".join(items) +
        '</table></td></tr></table></td></tr></table></body></html>'
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUT_DIR.glob("case-*.html"):  # 清掉上一轮用例文件，避免残留
        stale.unlink()
    rows = []
    for case in CASES:
        result = render(case["verdict"], case["job"])
        path = OUT_DIR / f"{case['key']}.html"
        path.write_text(result["html"], encoding="utf-8")
        rows.append((case, result, path))

        print("─" * 68)
        print(f"{case['name']}")
        print(f"文件：{path}")
        print("─" * 68)
        print(f"标题：{result['subject']}")
        print()
        print(result["text"])
        print()

    index = OUT_DIR / "index.html"
    index.write_text(_index_html(rows), encoding="utf-8")

    print("─" * 68)
    print("全部用例已生成（双击即可用浏览器打开）：")
    for _case, _result, path in rows:
        print(f"  {path}  ({path.stat().st_size / 1024:.1f} KB)")
    print(f"  {index}")
    print("─" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
