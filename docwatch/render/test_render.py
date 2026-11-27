"""邮件排版自检：python -m docwatch.render.test_render

不起 pytest、不联网、不发信，纯断言。覆盖：4 个预览用例、级别色标、diff 折叠、
字号下限、HTML 结构完整、缺字段/空数据/超长内容、subject 长度与 emoji、XSS 转义。
"""

from __future__ import annotations

import html.parser
import pathlib
import re
import sys
import unittest

try:  # python -m docwatch.render.test_render
    from .mail import LEVELS, MAX_DIFF_LINES, render
    from .preview import CASES
except ImportError:  # 直接 python docwatch/render/test_render.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from docwatch.render.mail import LEVELS, MAX_DIFF_LINES, render
    from docwatch.render.preview import CASES

SUBJECT_PREFIX = "[DeepSeek 更新]"
URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"

VOID_TAGS = {"meta", "br", "img", "hr", "input", "link", "base", "col", "embed", "source", "track", "wbr"}
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\uFE0F\u200D\u20E3]")


# ---------------------------------------------------------------- 小工具


class _Balance(html.parser.HTMLParser):
    """检查标签是否成对闭合（比数 <table> 个数严格）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if not self.stack:
            self.errors.append(f"多余的 </{tag}>")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
            return
        self.errors.append(f"标签不匹配：期望 </{self.stack[-1]}>，实际 </{tag}>")
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.stack.pop()
            self.stack.pop()


def balanced(text: str) -> list:
    parser = _Balance()
    parser.feed(text)
    parser.close()
    problems = list(parser.errors)
    if parser.stack:
        problems.append("未闭合：" + "/".join(parser.stack))
    return problems


def font_sizes(page: str) -> list:
    return [float(value) for value in re.findall(r"font-size:\s*([\d.]+)px", page)]


def card_of_big_diff(extra_lines: int = 30) -> dict:
    return {
        "level": 3,
        "headline": "超长差异测试",
        "cards": [
            {
                "level": 3,
                "page": URL,
                "bullets": ["第一行要点"],
                "diff": [f"＋ 新增第 {index} 行内容" for index in range(extra_lines)],
            }
        ],
    }


def check_all(result: dict, label: str) -> None:
    assert set(result) == {"subject", "text", "html"}, f"{label}: 返回键不对 {set(result)}"
    for key, value in result.items():
        assert isinstance(value, str), f"{label}: {key} 不是字符串"
        assert value.strip(), f"{label}: {key} 为空"
    problems = balanced(result["html"])
    assert not problems, f"{label}: HTML 结构坏了 {problems}"


# ---------------------------------------------------------------- 用例


def test_four_cases_render():
    """4 个预览用例都能渲染出三件套，且 HTML 结构完整。"""
    for case in CASES:
        check_all(render(case["verdict"], case["job"]), case["key"])


def test_subject_length_and_emoji():
    """subject ≤60 字、无 emoji、带级别标签、开头统一。"""
    for case in CASES:
        result = render(case["verdict"], case["job"])
        subject = result["subject"]
        assert subject.startswith(SUBJECT_PREFIX), subject
        assert len(subject) <= 60, f"{case['key']}: subject {len(subject)} 字：{subject}"
        assert not EMOJI_RE.search(subject), f"{case['key']}: subject 带 emoji：{subject}"
        level = case["verdict"].get("level")
        if level is not None:
            assert LEVELS[level][0] in subject, f"{case['key']}: subject 缺级别：{subject}"
        else:
            assert any(name in subject for name, _color in LEVELS.values()), subject


def test_subject_keeps_key_info():
    """超长 headline 截断后，subject 里仍要有开头关键词和页数。"""
    verdict = {"level": 3, "headline": "高峰输出价 " + "很长的说明" * 30}
    result = render(verdict, {"pages_total": 58, "pages_changed": 2})
    subject = result["subject"]
    assert len(subject) <= 60, subject
    assert "高峰输出价" in subject, subject
    assert "（2 页）" in subject, subject
    assert subject.endswith("（2 页）"), subject


def test_html_structure_and_no_external():
    """table + inline style、640px、无外链图片/字体/样式表/class，也不用 flex/grid。"""
    for case in CASES:
        page = render(case["verdict"], case["job"])["html"]
        low = page.lower()
        assert page.startswith("<!DOCTYPE html>"), case["key"]
        assert 'width="640"' in page, case["key"]
        assert "max-width:640px" in page, case["key"]
        for bad in ("<style", "<link", "<img", "<script", "@import", "background-image", "class=", "src=",
                    "display:flex", "display:grid", "display: flex", "display: grid", "position:fixed"):
            assert bad not in low, f"{case['key']}: 出现 {bad}"
        assert "href=" in low or "http" not in low, case["key"]


def test_font_size_floor():
    """正文字号一律 ≥15px（手机上看不费劲）。"""
    for case in CASES:
        page = render(case["verdict"], case["job"])["html"]
        sizes = font_sizes(page)
        assert sizes, f"{case['key']}: 没有任何 font-size"
        assert min(sizes) >= 15, f"{case['key']}: 有 {min(sizes)}px 的小字"


def test_level_colors_and_labels():
    """3/2/1/0 四个级别各自的色标和中文标签都对。"""
    payload = {
        3: "价格上调",
        2: "接口文档新增参数",
        1: "措辞调整",
        0: "前端重建",
    }
    for level, (label, color) in LEVELS.items():
        result = render(
            {"level": level, "noise": level == 0, "headline": payload[level]},
            {"pages_total": 58, "pages_changed": 1, "signals": []},
        )
        assert color in result["html"], f"level {level} 缺色标 {color}"
        assert label in result["html"], f"level {level} 缺标签 {label}"
        assert label in result["text"], f"level {level} 文本缺标签 {label}"


def test_diff_cap():
    """原文差异最多 8 行，其余折叠成「另有 N 行未显示」。"""
    total = 30
    verdict = card_of_big_diff(total)
    result = render(verdict, {"pages_total": 58, "pages_changed": 1})
    assert MAX_DIFF_LINES == 8
    assert result["html"].count("＋ ") == MAX_DIFF_LINES, result["html"].count("＋ ")
    assert f"另有 {total - MAX_DIFF_LINES} 行未显示" in result["html"]
    assert f"另有 {total - MAX_DIFF_LINES} 行未显示" in result["text"]
    text_diff_rows = [line for line in result["text"].split("\n") if line.strip().startswith("＋")]
    assert len(text_diff_rows) == MAX_DIFF_LINES, len(text_diff_rows)


def test_diff_delete_add_colors():
    """删除行浅红底、新增行浅绿底，用的是全角 －/＋。"""
    verdict = {
        "level": 3,
        "headline": "价格变了",
        "cards": [{"level": 3, "page": URL, "diff": ["－ 输出 13.50 元", "＋ 输出 15.00 元"]}],
    }
    result = render(verdict, {"pages_total": 58, "pages_changed": 1})
    page = result["html"]
    assert "#fdecea" in page, "删除行没有浅红底"
    assert "#e8f6ee" in page, "新增行没有浅绿底"
    assert "－ 输出 13.50 元" in page
    assert "＋ 输出 15.00 元" in page
    assert "－ 输出 13.50 元" in result["text"]
    assert "---+ " not in page  # 别退回到 ASCII 加减号


def test_noise_card_is_compact():
    """噪音：一行灰字，不展示 diff，不出现要点列表。"""
    case = next(item for item in CASES if item["key"] == "case-3-noise")
    result = render(case["verdict"], case["job"])
    page, text = result["html"], result["text"]
    assert "#8a8f98" in page
    assert "噪音" in page and "噪音" in text
    assert "重新构建，正文无变化" in page
    assert "原文差异" not in page, "噪音卡不该有 diff 块"
    assert "原文差异" not in text
    assert "＋ " not in page
    assert "①" not in page, "噪音卡不该有要点"


def test_text_version_has_source_url():
    """P2-29：纯文本版每张带 URL 的卡片都要给原文链接，方便直接点回去。"""
    case = next(item for item in CASES if item["key"] == "case-1-price")
    text = render(case["verdict"], case["job"])["text"]
    assert f"原文：{URL}" in text, text
    assert text.count("原文：http") >= 1, text


def test_text_empty_note_matches_html_body():
    """P2-29：有 headline / what_changed 的卡片不能误印「本次检查未发现任何变化」。"""
    result = render(
        {"level": 2, "headline": "有变化",
         "cards": [{"level": 2, "page": URL, "headline": "只给了标题"},
                   {"level": 2}]},
        {"pages_total": 58, "pages_changed": 1},
    )
    assert "只给了标题" in result["text"]
    assert result["text"].count("本次检查未发现任何变化") == 1, result["text"]
    # 真正空白的卡（第二条）才允许出现 EMPTY_NOTE，HTML 与文本一致。
    assert result["html"].count("本次检查未发现任何变化") == 1, result["html"]


def test_non_noise_cards_capped_at_8_with_summary():
    """P2-51：几十页同时变化时，非噪音卡片最多 8 张，其余合并成「其余 N 页」。"""
    signals = [{
        "kind": "page_text",
        "page": f"https://api-docs.deepseek.com/zh-cn/guides/page-{index}/",
        "detail": f"第 {index} 页正文有改动",
        "before": "旧内容", "after": "新内容",
    } for index in range(58)]
    result = render(
        {"level": 3, "headline": "全站 58 页同时变化"},
        {"pages_total": 58, "pages_changed": 58, "signals": signals},
    )
    page, text, subject = result["html"], result["text"], result["subject"]
    assert "其余 50 页" in text, text
    assert text.count("【文档页面") <= 8, text
    assert page.count("打开页面") <= 8, "非噪音卡片超过 8 张"
    assert len(page.encode("utf-8")) < 102 * 1024, f"HTML 太大：{len(page.encode('utf-8'))}"
    assert "（58 页）" in subject, subject


def test_rebuild_noise_collapses_to_one_line():
    """站点重建的几十页不逐条出卡，合并成一行；标题只算真正的 2 处内容变化。"""
    cards = [
        {"level": 3, "page": URL, "bullets": ["正文删了 1 行"]},
        {"level": 2, "page": "https://api-docs.deepseek.com/zh-cn/guides/search/",
         "bullets": ["正文改了 2 行"]},
    ]
    cards += [
        {"level": 1, "page": f"https://api-docs.deepseek.com/zh-cn/page-{index}/",
         "noise": True, "note": "差异只在资源文件名上（站点重新构建），正文无改动"}
        for index in range(55)
    ]
    cards.append({
        "level": 1,
        "page": "https://api-docs.deepseek.com/zh-cn/assets/js/runtime~main.65246f9a.js",
        "noise": True, "note": "前端资源文件名变了（站点重新构建），正文无改动",
    })
    result = render(
        {"level": 3, "headline": "Search/Fetch 开始计费", "cards": cards},
        {"pages_total": 58, "pages_changed": 57},
    )
    page, text = result["html"], result["text"]
    assert "（2 页）" in result["subject"], result["subject"]
    assert "57 页" not in result["subject"], result["subject"]
    assert "2 页有变化" in text, text
    assert "另有 55 页仅前端重建" in text, text
    assert "其余 55 页 + 1 个资源" in text, text
    assert "不逐条列出" in text, text
    assert page.count("噪音 ·") == 1, "重建页没有合并成一行"
    assert text.count("【噪音 ·") == 1, "重建页没有合并成一行"
    assert "原文差异" not in page, "噪音页不该有 diff"


def test_duplicate_noise_cards_counted_once():
    """夜间合并信里同一页的噪音卡出现多次，摘要只算一次。"""
    noise = [
        {"level": 1, "page": f"https://api-docs.deepseek.com/zh-cn/page-{index}/",
         "noise": True, "note": "差异只在资源文件名上（站点重新构建），正文无改动"}
        for index in range(5)
    ]
    cards = [
        {"level": 3, "page": URL, "bullets": ["正文删了 1 行"]},
        {"level": 2, "page": "https://api-docs.deepseek.com/zh-cn/guides/search/",
         "bullets": ["正文改了 2 行"]},
    ] + noise + noise + noise
    result = render(
        {"level": 3, "headline": "两处真变化", "cards": cards},
        {"pages_total": 58, "pages_changed": 15},
    )
    assert "其余 5 页" in result["text"], result["text"]
    assert "其余 15 页" not in result["text"], result["text"]
    assert "5 页仅前端重建" in result["text"], result["text"]


def test_attach_cards_marks_rebuild_only():
    """run.attach_cards：正文变化保留卡片，refs_only 页面和资源改名标成噪音。"""
    from docwatch.run import attach_cards

    signals = [
        {"kind": "page_text", "page": URL, "text_changed_chars": 169,
         "detail": "正文变了：+0 行 / -1 行（共 30 行，169 字改动）"},
        {"kind": "page_text", "page": "https://api-docs.deepseek.com/zh-cn/guides/search/",
         "text_changed_chars": 265,
         "detail": "正文变了：+1 行 / -1 行（共 20 行，265 字改动）"},
    ]
    for index in range(55):
        signals.append({
            "kind": "page_html",
            "page": f"https://api-docs.deepseek.com/zh-cn/page-{index}/",
            "detail": "HTML 变了：10.0 KB → 10.0 KB（+0 字节），差异只在资源文件名上"
                      "（站点重新构建），正文无改动",
            "refs_only": True,
        })
    signals.append({
        "kind": "asset_url", "page": "",
        "asset": "https://api-docs.deepseek.com/zh-cn/assets/js/runtime~main.65246f9a.js",
        "action": "rename",
        "detail": "runtime~main.js 文件名哈希变了（5d7c0fcb → 65246f9a）",
    })
    job = {"pages_total": 58, "pages_changed": 57, "signals": signals, "evidence_dir": ""}
    verdict = attach_cards({"level": 3, "headline": "Search/Fetch 开始计费"}, job)
    cards = verdict["cards"]
    assert len([card for card in cards if card.get("noise")]) == 56, "噪音卡数量不对"
    assert len([card for card in cards if not card.get("noise")]) == 2, "真实变化卡数量不对"
    assert "55 页只是前端重建" in " ".join(verdict["evidence"]), verdict["evidence"]
    assert "前端资源 1 个仅重命名（重建）" in " ".join(verdict["evidence"]), verdict["evidence"]

    result = render(verdict, job)
    assert "（2 页）" in result["subject"], result["subject"]
    assert "其余 55 页 + 1 个资源" in result["text"], result["text"]
    assert result["html"].count("噪音 ·") == 1, "重建页没有合并成一行"


def test_rebuild_only_signals_collapse_without_cards():
    """没有预置 cards 的旧路径：refs_only 信号也会被识别成噪音并合并。"""
    signals = [
        {"kind": "page_html",
         "page": f"https://api-docs.deepseek.com/zh-cn/page-{index}/",
         "detail": "HTML 变了，差异只在资源文件名上（站点重新构建），正文无改动",
         "refs_only": True}
        for index in range(10)
    ]
    result = render(
        {"level": 1, "headline": "前端重新构建，正文无变化"},
        {"pages_total": 58, "pages_changed": 10, "signals": signals},
    )
    assert "其余 10 页" in result["text"], result["text"]
    assert result["text"].count("【噪音 ·") == 1, result["text"]
    assert "（10 页重建）" in result["subject"], result["subject"]


def test_refs_only_page_with_asset_add_remove_stays_noise():
    """正文没变的页面，即使资源是增删而不是改名，也不能逐页出卡。"""
    from docwatch.run import attach_cards

    page = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
    signals = [
        {"kind": "page_html", "page": page, "refs_only": True,
         "detail": "HTML 变了，差异只在资源文件名上（站点重新构建），正文无改动"},
        {"kind": "asset_url", "page": page,
         "asset": "https://api-docs.deepseek.com/zh-cn/assets/js/chunk.old111.js",
         "action": "removed", "detail": "chunk.old.js 消失了（全站 1 页引用）"},
        {"kind": "asset_url", "page": page,
         "asset": "https://api-docs.deepseek.com/zh-cn/assets/js/chunk.new222.js",
         "action": "added", "detail": "chunk.new.js 是新资源（全站 1 页引用）"},
    ]
    job = {"pages_total": 58, "pages_changed": 1, "signals": signals, "evidence_dir": ""}
    verdict = attach_cards({"level": 1, "headline": "站点重新构建"}, job)
    assert len(verdict["cards"]) == 1, verdict["cards"]
    assert verdict["cards"][0].get("noise") is True, verdict["cards"][0]
    assert "有增删" in verdict["cards"][0]["note"], verdict["cards"][0]

    result = render(verdict, job)
    assert "其余" not in result["text"]  # 单张噪音卡直接一行，不叫「其余」
    assert "有增删" in result["text"], result["text"]
    assert "（1 页重建）" in result["subject"], result["subject"]


def test_asset_rename_not_mixed_into_real_card():
    """真实正文变化卡片里，不能再出现资源改名的机器文案。"""
    from docwatch.run import attach_cards

    page = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
    signals = [
        {"kind": "page_text", "page": page, "text_changed_chars": 120,
         "detail": "正文变了：+1 行 / -1 行（共 20 行，120 字改动）"},
        {"kind": "asset_url", "page": page,
         "asset": "https://api-docs.deepseek.com/zh-cn/assets/js/vision.a1b2c3d4.js",
         "action": "rename",
         "detail": "vision.a1b2c3d4.js 文件名哈希变了（11111111 → a1b2c3d4）"},
    ]
    job = {"pages_total": 58, "pages_changed": 1, "signals": signals, "evidence_dir": ""}
    card = attach_cards({"level": 2, "headline": "视觉指南正文改了"}, job)["cards"][0]
    assert not card.get("noise"), card
    joined = " ".join(card["bullets"])
    assert "正文改了" in joined, joined
    assert "哈希" not in joined, joined
    assert "vision.a1b2c3d4.js" not in joined, joined


def test_noise_summary_describes_asset_add_remove():
    """多页资源增删时，摘要不能说成「只是资源文件名变了」。"""
    pages = [f"https://api-docs.deepseek.com/zh-cn/page-{index}/" for index in range(2)]
    signals = []
    for page in pages:
        signals.append({"kind": "page_html", "page": page, "refs_only": True,
                        "detail": "HTML 变了，正文无改动"})
        signals.append({"kind": "asset_url", "page": page,
                        "asset": page + "assets/js/chunk.old.js", "action": "removed",
                        "detail": "chunk.old.js 消失了"})
        signals.append({"kind": "asset_url", "page": page,
                        "asset": page + "assets/js/chunk.new.js", "action": "added",
                        "detail": "chunk.new.js 是新资源"})
    job = {"pages_total": 58, "pages_changed": 2, "signals": signals, "evidence_dir": ""}
    result = render({"level": 1, "headline": "站点重新构建"}, job)
    text = result["text"]
    assert "2 页 HTML 有变化但正文没变" in text, text
    assert "2 页资源有增删" in text, text
    assert "只是资源文件名变了" not in text, text
    assert "（2 页重建）" in result["subject"], result["subject"]


def test_single_page_image_rename_is_real_not_noise():
    """单页图片换名可能是截图更新，不能当站点重建藏起来。"""
    from docwatch.run import attach_cards

    page = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
    signals = [
        {"kind": "page_html", "page": page, "refs_only": True,
         "detail": "HTML 变了：差异只在资源文件名上（站点重新构建），正文无改动"},
        {"kind": "asset_url", "page": page, "ref_kind": "img", "action": "rename",
         "asset": "https://api-docs.deepseek.com/zh-cn/img/vision.9f8e7d6c.png",
         "before": "https://api-docs.deepseek.com/zh-cn/img/vision.1a2b3c4d.png",
         "detail": "图片 vision.png 换了文件名（1a2b3c4d → 9f8e7d6c）"},
    ]
    job = {"pages_total": 58, "pages_changed": 1, "signals": signals, "evidence_dir": ""}
    verdict = attach_cards({"level": 2, "headline": "vision 页面截图更新"}, job)
    card = verdict["cards"][0]
    assert not card.get("noise"), card
    assert "图片" in " ".join(card["bullets"]), card["bullets"]

    result = render(verdict, job)
    assert "（1 页）" in result["subject"], result["subject"]
    assert "1 页有变化" in result["text"], result["text"]
    assert "前端资源 1 个有变化" in result["text"], result["text"]
    assert "1 页仅前端重建" not in result["text"], result["text"]


def test_shared_image_rename_is_real_asset():
    """站点共用图片换名也要出卡（采集层不下载图片，不能断定是重建）。"""
    from docwatch.run import attach_cards

    signals = [{
        "kind": "asset_url", "page": "", "ref_kind": "img", "action": "rename",
        "asset": "https://api-docs.deepseek.com/zh-cn/img/hero.9f8e7d6c.png",
        "detail": "图片 hero.png 换了文件名",
    }]
    job = {"pages_total": 58, "pages_changed": 0, "signals": signals, "evidence_dir": ""}
    verdict = attach_cards({"level": 1, "headline": "首页图片换了"}, job)
    card = verdict["cards"][0]
    assert not card.get("noise"), card
    assert card.get("is_asset") is True, card

    result = render(verdict, job)
    assert "重建" not in result["subject"], result["subject"]
    assert "前端资源 1 个有变化" in result["text"], result["text"]


def test_mixed_fetch_error_not_counted_as_page_change():
    """同轮既有正文变化又有抓取失败时，失败地址不算「有变化页」。"""
    from docwatch.run import attach_cards

    signals = [
        {"kind": "page_text", "page": URL, "text_changed_chars": 169,
         "detail": "正文变了：+0 行 / -1 行（共 86 行，169 字改动）"},
        {"kind": "page_text", "page": "https://api-docs.deepseek.com/zh-cn/guides/search/",
         "text_changed_chars": 265, "detail": "正文变了：+1 行 / -1 行（共 243 行，265 字改动）"},
        {"kind": "fetch_error", "page": "https://api-docs.deepseek.com/zh-cn/api/search/",
         "detail": "连续失败 3 次"},
    ]
    job = {"pages_total": 58, "pages_changed": 3, "signals": signals, "evidence_dir": ""}
    verdict = attach_cards({"level": 2, "headline": "两页改了，一页抓取失败"}, job)
    result = render(verdict, job)
    assert "（2 页）" in result["subject"], result["subject"]
    assert "（3 页）" not in result["subject"], result["subject"]
    assert "2 页有变化" in result["text"], result["text"]
    assert "抓取失败" in result["text"], result["text"]


def test_health_alert_has_no_misleading_page_tail():
    """健康告警标题已写「N 个地址」，不要再补一个「（N 页）」。"""
    signals = [
        {"kind": "fetch_error", "page": URL, "detail": "连续失败 3 次"},
        {"kind": "fetch_error",
         "page": "https://api-docs.deepseek.com/zh-cn/assets/js/main.1.js",
         "detail": "连续失败 4 次"},
    ]
    result = render(
        {"level": 2, "headline": "有 2 个地址连续抓取失败，监控可能不准了"},
        {"pages_total": 58, "pages_changed": 0, "signals": signals},
    )
    assert "（1 页）" not in result["subject"], result["subject"]
    assert "（2 页）" not in result["subject"], result["subject"]
    assert "页有变化" not in result["text"], result["text"]


def test_multi_card_order_and_first_is_top_level():
    """多卡按 level 降序，第一张卡是最高级别。"""
    verdict = {
        "level": 2,
        "headline": "两处变化",
        "cards": [
            {"level": 1, "page": "https://api-docs.deepseek.com/zh-cn/faq", "bullets": ["低级别"]},
            {"level": 3, "page": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/", "bullets": ["高级别"]},
            {"level": 2, "page": "https://api-docs.deepseek.com/zh-cn/api/x", "bullets": ["中级别"]},
        ],
    }
    result = render(verdict, {"pages_total": 58, "pages_changed": 3})
    text = result["text"]
    first = text.index("【重要】")
    middle = text.index("【值得看】")
    last = text.index("【轻微】")
    assert first < middle < last, "卡片顺序不是 level 降序"
    assert "（3 页）" in result["subject"]


def test_multi_card_has_job_action_once():
    """多卡时 job 级建议只出现一次，且不重复进每张卡。"""
    verdict = {
        "level": 3,
        "headline": "两页都改了",
        "action": "下个月预算加 10%。",
        "cards": [
            {"level": 3, "page": URL, "bullets": ["A 改动"]},
            {"level": 2, "page": "https://api-docs.deepseek.com/zh-cn/api/x", "bullets": ["B 改动"]},
        ],
    }
    result = render(verdict, {"pages_total": 58, "pages_changed": 2})
    assert result["text"].count("下个月预算加 10%。") == 1, result["text"]


def test_missing_and_empty_fields():
    """缺字段 / 空数据 / 类型不对：不崩、有兜底、subject 仍合规。"""
    weird = [
        ({}, {}),
        (None, None),
        ({"level": None, "headline": None, "why_matters": None, "action": None}, {"signals": None}),
        ({"level": "3"}, "not-a-dict"),
        ({"level": 99, "headline": "", "cards": ["坏数据", 3, None]}, {"pages_total": "58"}),
        ({"level": 2, "headline": {"嵌套": "字典"}, "why_matters": ["列表", "也行"]}, {"signals": [None, 1]}),
    ]
    for index, (verdict, job) in enumerate(weird):
        result = render(verdict, job)
        check_all(result, f"weird-{index}")
        assert len(result["subject"]) <= 60, result["subject"]
        assert not EMOJI_RE.search(result["subject"])
        assert "Traceback" not in result["text"]


def test_empty_verdict_is_not_counted_as_rebuild_page():
    """完全空的数据不能变成「1 页重建」——空占位卡要丢掉。"""
    result = render({}, {})
    assert "1 页" not in result["subject"], result["subject"]
    assert "1 页仅前端重建" not in result["text"], result["text"]
    assert "（1 页重建）" not in result["subject"], result["subject"]


def test_empty_signals_no_trigger_line():
    """空 signals：不给「触发 0 处」这种没意义的数字。"""
    result = render({"level": 1}, {"pages_total": 58, "signals": []})
    assert "触发" not in result["text"], result["text"]
    assert "检查 58 页" in result["text"]
    assert "本次检查" in result["text"]


def test_long_content_does_not_blow_up():
    """超长 headline / 超多信号 / 超长正文：仍能渲染，体积可控。"""
    headline = "这是超长标题" * 40
    signals = []
    for index in range(60):
        signals.append({
            "kind": "page_html",
            "page": f"https://api-docs.deepseek.com/zh-cn/page-{index}/",
            "detail": "变化点 " * 60,
            "before": "\n".join(f"旧内容第 {row} 行" for row in range(200)),
            "after": "\n".join(f"新内容第 {row} 行" for row in range(200)),
        })
    job = {"pages_total": 58, "pages_changed": 60, "created_at": "2026-11-04T09:30:12+08:00", "signals": signals}
    result = render({"level": 3, "headline": headline, "why_matters": "影响" * 400}, job)
    check_all(result, "long")
    assert len(result["subject"]) <= 60, result["subject"]
    assert len(result["html"]) < 400_000, f"HTML 太大：{len(result['html'])}"


def test_escape_and_bad_url():
    """用户内容里的尖括号、javascript: 链接都要挡住。"""
    verdict = {
        "level": 3,
        "headline": "<script>alert(1)</script> 价格变了",
        "why_matters": "a & b < c",
        "cards": [{"level": 3, "page": "javascript:alert(1)", "bullets": ["<b>加粗</b>"]}],
    }
    result = render(verdict, {"pages_total": 58, "pages_changed": 1})
    page = result["html"]
    assert "<script>" not in page.lower()
    assert "javascript:" not in page.lower()
    assert "&lt;script&gt;" in page
    assert "a &amp; b &lt; c" in page
    assert "&lt;b&gt;加粗&lt;/b&gt;" in page


def test_text_version_mirrors_html():
    """纯文本版：结构对齐、不用 ASCII 横线、无 emoji、含页脚三段。"""
    case = next(item for item in CASES if item["key"] == "case-1-price")
    result = render(case["verdict"], case["job"])
    text = result["text"]
    assert text.startswith("[重要] "), text[:20]
    for mark in ("有什么影响", "【模型 & 价格】", "原文差异", "建议：", "依据：", "判读：", "觉得太吵"):
        assert mark in text, f"纯文本缺 {mark}"
    assert "====" not in text and "----" not in text, "别退回 ASCII 横线"
    assert "====" not in result["html"], "正文里不许有等号分隔线"
    assert not EMOJI_RE.search(text)
    assert "－ " in text and "＋ " in text
    for line in text.split("\n"):
        assert len(line) <= 200, f"有超长行：{line[:40]}"


def test_footer_is_business_friendly():
    """页脚显示判读模型，但不出现环境变量、命令、英文 slug（业务用户看不懂）。"""
    for case in CASES:
        result = render(case["verdict"], case["job"])
        for blob in (result["text"], result["html"]):
            index = blob.find("依据：")
            assert index > 0, case["key"]
            footer = blob[index:]
            for bad in ("DOCWATCH_MODE", "docwatch.service", "档位", "model=", "python3 -m"):
                assert bad not in footer, f"{case['key']}: 页脚出现「{bad}」"
        for bad in ("example-provider", "example-model-d", "DOCWATCH_MODE"):
            assert bad not in result["subject"], result["subject"]
    case = next(item for item in CASES if item["key"] == "case-1-price")
    text = render(case["verdict"], case["job"])["text"]
    assert "判读：自动解读" in text, text
    assert "判读模型：Example Model D（example-model-d）" in text, text
    assert "本次检查 58 页" in text, text
    assert "觉得太吵？" in text, text
    for line in text.split("\n"):
        if line.strip().startswith("原文："):
            continue
        assert "pricing" not in line, f"纯文本正文里不该出现英文 slug：{line}"
    case = next(item for item in CASES if item["key"] == "case-2-notable")
    text = render(case["verdict"], case["job"])["text"]
    for line in text.split("\n"):
        if line.strip().startswith("原文："):
            continue
        assert "faq" not in line, f"纯文本正文里不该出现英文 slug：{line}"
    case = next(item for item in CASES if item["key"] == "case-4-empty")
    text = render(case["verdict"], case["job"])["text"]
    assert "判读：规则兜底" in text, text
    assert "判读模型：规则兜底" in text, text


def test_example_model_label():
    """默认的 Example Model 也要显示成业务用户看得懂的名字。"""
    result = render(
        {"level": 2, "headline": "有变化", "model": "example-model-a"},
        {"pages_total": 58, "pages_changed": 1},
    )
    for blob in (result["text"], result["html"]):
        assert "判读模型：Example Model（example-model-a）" in blob, blob


def test_route_group_model_label():
    """路由组要显示成「Example Model，失败自动切 DeepSeek」，不暴露 group/ 技术 id。"""
    result = render(
        {"level": 2, "headline": "有变化", "model": "example-route"},
        {"pages_total": 58, "pages_changed": 1},
    )
    for blob in (result["text"], result["html"]):
        assert "判读模型：Example Route" in blob, blob
        assert "example-route" not in blob, blob


def test_route_group_v2_model_label():
    """新的三模型顺序路由组也要显示成业务可读的名字。"""
    result = render(
        {"level": 2, "headline": "有变化", "model": "example-route-v2"},
        {"pages_total": 58, "pages_changed": 1},
    )
    for blob in (result["text"], result["html"]):
        assert "判读模型：Example Route V2" in blob, blob
        assert "example-route-v2" not in blob, blob


def test_degraded_verdict_shows_rule_fallback():
    """模型没给出解读时，页脚不能冒充分析成功。"""
    result = render(
        {"level": 3, "headline": "价格变了",
         "model": "example-model-d", "degraded": True},
        {"pages_total": 58, "pages_changed": 1},
    )
    for blob in (result["text"], result["html"]):
        assert "判读：规则兜底" in blob, blob
        assert "判读模型：规则兜底（模型未给出解读）" in blob, blob


def test_card_has_page_link_not_slug():
    """卡片右侧给一个小灰字「打开页面」，标题里不出现英文 slug。"""
    case = next(item for item in CASES if item["key"] == "case-1-price")
    page = render(case["verdict"], case["job"])["html"]
    assert "打开页面" in page
    assert "原文 ↗" not in page
    assert ">pricing<" not in page


def test_job_advice_skipped_when_all_cards_have_advice():
    """每张卡都给了建议时，全局建议不再重复出现。"""
    verdict = {
        "level": 3,
        "headline": "两页都改了",
        "action": "无需操作。",
        "cards": [
            {"level": 3, "page": URL, "bullets": ["A 改动"], "action": "无需操作。"},
            {"level": 2, "page": "https://api-docs.deepseek.com/zh-cn/api/x", "bullets": ["B 改动"],
             "action": "下个月预算加 10%。"},
        ],
    }
    result = render(verdict, {"pages_total": 58, "pages_changed": 2})
    assert result["text"].count("无需操作。") == 1, result["text"]
    assert "建议" in result["text"]


def test_model_note_optional():
    """model_note：有就一行灰字「判读补充：」，没有就不占位。"""
    with_note = render(
        {"level": 2, "headline": "有补充", "model_note": "模型自己的看法"},
        {"pages_total": 58, "pages_changed": 1},
    )
    assert "判读补充：模型自己的看法" in with_note["html"]
    assert "判读补充：模型自己的看法" in with_note["text"]

    without = render({"level": 2, "headline": "没补充"}, {"pages_total": 58, "pages_changed": 1})
    assert "判读补充" not in without["html"]
    assert "判读补充" not in without["text"]


def test_no_limit_frequency_wording():
    """用户拍板「来一封发一封」：邮件里不许出现限频/合并措辞。"""
    for case in CASES:
        result = render(case["verdict"], case["job"])
        blob = result["subject"] + result["text"] + result["html"]
        for word in ("已合并", "合并发送", "限频", "频率限制", "AI 判断等级", "AI判定等级"):
            assert word not in blob, f"{case['key']}: 出现「{word}」"


def test_level_is_not_claimed_as_ai():
    """级别措辞：级别由规则给出，邮件里不写 AI 判断等级。"""
    result = render(
        {"level": 3, "headline": "价格上调", "model": "example-model-d"},
        {"pages_total": 58, "pages_changed": 1},
    )
    blob = result["subject"] + result["text"] + result["html"]
    assert "AI 判断" not in blob and "AI判定" not in blob
    assert "重要" in result["subject"]


def test_first_screen_has_level_headline_impact():
    """第一屏（不滚动）就能看到：级别色标 + headline + 影响。"""
    case = next(item for item in CASES if item["key"] == "case-1-price")
    page = render(case["verdict"], case["job"])["html"]
    head = page[: page.index("border-top:1px solid #e8eaed")]  # 第一张卡之前 = banner + 影响段
    assert 'bgcolor="#c0392b"' in head, "第一屏没有级别色标"
    assert case["verdict"]["headline"] in head, "第一屏没有 headline"
    assert "有什么影响" in head, "第一屏没有影响段"
    assert "<strong" in head, "影响首句没加粗"


def test_preview_outputs_exist_after_running():
    """跑过 preview 的话，out/ 里应该有 4 个 case 文件和 size>0。

    没跑过 preview 时不能静默 return 冒充通过，必须显式 SKIP 并说明原因。
    """
    out_dir = pathlib.Path(__file__).resolve().parent / "out"
    if not out_dir.is_dir():
        raise unittest.SkipTest(
            "preview 未运行：docwatch/render/out 不存在；"
            "先执行 python -m docwatch.render.preview 再验证")
    files = sorted(out_dir.glob("case-*.html"))
    assert len(files) == len(CASES), [item.name for item in files]
    for path in files:
        assert path.stat().st_size > 1000, f"{path.name} 太小"


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [(name, item) for name, item in sorted(globals().items())
             if name.startswith("test_") and callable(item)]
    failed = 0
    skipped = 0
    for name, test in tests:
        try:
            test()
        except unittest.SkipTest as exc:
            skipped += 1
            print(f"[SKIP] {name}: {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 —— 出异常也算失败，别中断后面的用例
            failed += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"[OK]   {name}")
    total = len(tests)
    print(f"\n通过 {total - failed - skipped}/{total}（跳过 {skipped}）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
