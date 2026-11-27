#!/usr/bin/env python3
"""docwatch 规则引擎：决定「算几级」和「发不发」。

设计原则（2026-11-04 用户拍板）：
    **发送权归规则，模型只负责把变化讲成人话。**
    所以本模块是纯函数、可单测、不依赖网络、不依赖模型。

级别：
    3 重要   —— 价格/计费/限流/错误码/接口契约/更新日志/新页面
    2 值得看 —— 普通页面的实质内容变化（≥30 字）、页面下线、抓取连续失败
    1 轻微   —— 纯措辞、纯重建（HTML/资源指纹变了但正文没动）
    0 噪音   —— 只有站点级模板抖动（由更早的过滤层消掉，这里基本不会出现）

档位（mode）决定发送阈值：
    high  → level >= 0 全发（当前，用户要先观察噪音）
    normal→ level >= 2
    quiet → level >= 3
"""

from __future__ import annotations

import difflib
import re

LEVEL_NOISE = 0
LEVEL_MINOR = 1
LEVEL_NOTABLE = 2
LEVEL_IMPORTANT = 3

LEVEL_LABEL = {
    LEVEL_IMPORTANT: "重要",
    LEVEL_NOTABLE: "值得看",
    LEVEL_MINOR: "轻微",
    LEVEL_NOISE: "噪音",
}

MODE_THRESHOLD = {"high": 0, "normal": 2, "quiet": 3}

# 关键页面：这些地方改一个字都可能影响接入或成本
KEY_PAGE_MARKERS = (
    "/quick_start/pricing",
    "/quick_start/rate_limit",
    "/quick_start/error_codes",
    "/quick_start/token_usage",
    "/updates",
    "/api/",
)

# 普通页面里出现这些，也要升级成「重要」
SENSITIVE_PATTERNS = (
    (re.compile(r"元\s*/\s*百万|/百万\s*tokens|美元|USD\s*\$?\s*\d", re.I), "金额"),
    (re.compile(r"deepseek-(flash|v4|chat|reasoner|v\d)", re.I), "模型名"),
    (re.compile(r"下线|停用|废弃|不再支持|deprecat|sunset|end of life", re.I), "功能下线"),
    (re.compile(r"限流|速率限制|rate\s*limit|\bRPM\b|\bTPM\b|\b429\b", re.I), "限流"),
    # 注意：不要用裸 \b5\d\d\b 这类数字匹配 —— 会把「547.1 KB」这种字节数当成错误码（实测误判）
    (re.compile(r"错误码|error\s*code|状态码|status\s*(?:code)?\s*[45]\d\d|\bHTTP\s*[45]\d\d", re.I),
     "错误码"),
)

# 纯措辞/粉饰类改动：正文变化很小且没有敏感词时，认为是措辞
TRIVIAL_TEXT_CHARS = 30

# 关键页面的短改动里，数字/额度/单位变化不能当措辞（价格 13.5→15、限流 500→300、
# 上下文 128K→64K 往往只有几个字）。这里只看 before/after 的实际正文，不看 detail，
# 否则 detail 里的「共 18 行，6 字改动」会把所有短改动都误判成数字变化。
_NUM_UNIT_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:元|美元|万|亿|K|M|G|KB|MB|GB|tokens?|次|秒|毫秒|ms|分钟|小时|%|RPM|TPM|QPS)"
    r"(?![0-9A-Za-z])",
    re.I,
)
_CONTEXT_RE = re.compile(r"(?:上下文|context|最大输出|max\s*output)[^\n。]{0,40}?\d", re.I)
_DIGITS_RE = re.compile(r"\d+(?:\.\d+)?")


def _numeric_change(sig: dict) -> bool:
    """正文改动里是否出现数字/单位变化（只用于关键页面的短改动）。"""
    before = str(sig.get("before") or "")
    after = str(sig.get("after") or "")
    text = before + " " + after
    if _NUM_UNIT_RE.search(text) or _CONTEXT_RE.search(text):
        return True
    nums_before = _DIGITS_RE.findall(before)
    nums_after = _DIGITS_RE.findall(after)
    return bool(nums_before and nums_after and nums_before != nums_after)


IMPORTANT_KINDS = {"new_page"}
SECONDARY_KINDS = {"gone_page", "fetch_error"}


def page_class(url: str) -> str:
    """页面类别：key / regular / marketing。"""
    u = (url or "").lower()
    if "www.deepseek.com" in u:
        return "marketing"
    for marker in KEY_PAGE_MARKERS:
        if marker in u:
            return "key"
    return "regular"


def has_sensitive(text: str) -> list[str]:
    """命中的敏感主题（人话），用于升级级别与写理由。"""
    hits = []
    for pattern, label in SENSITIVE_PATTERNS:
        if pattern.search(text or ""):
            hits.append(label)
    return hits


def _text_changed_chars(sig: dict) -> int:
    """优先按 before/after 的真实字符差异计数。

    `text_changed_chars` 是采集端按差异行统计的粗粒度值：一行 197 字里只改掉
    一个「的」，也会报 197。判级如果直接用它，会把错别字/措辞微调放大成
    重要变化。这里用 difflib 只在内存里算实际改动字符数，拿不到原文时再退回
    采集值。
    """
    before = str(sig.get("before") or "")
    after = str(sig.get("after") or "")
    if before or after:
        changed = 0
        matcher = difflib.SequenceMatcher(None, before, after)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                changed += max(i2 - i1, j2 - j1)
        return changed
    return int(sig.get("text_changed_chars") or 0)


def _signal_level(sig: dict) -> tuple[int, str]:
    """单条信号 → (level, 理由)。"""
    kind = sig.get("kind", "")
    page = sig.get("page") or sig.get("asset") or ""
    cls = page_class(page)
    detail = sig.get("detail") or ""
    blob = " ".join(str(sig.get(k) or "") for k in ("detail", "before", "after"))

    if kind in IMPORTANT_KINDS:
        return LEVEL_IMPORTANT, f"新页面出现：{page}"
    if kind in SECONDARY_KINDS:
        if kind == "fetch_error":
            return LEVEL_NOTABLE, f"抓取失败，监控可能变瞎：{page}"
        return LEVEL_NOTABLE, f"页面消失：{page}"

    if kind == "page_text":
        chars = _text_changed_chars(sig)
        if chars < TRIVIAL_TEXT_CHARS:
            sens = has_sensitive(blob)
            if sens:
                return LEVEL_IMPORTANT, "措辞级改动但涉及" + "、".join(sens)
            if cls == "key" and _numeric_change(sig):
                return LEVEL_IMPORTANT, f"关键页面数字/额度变化（{chars} 字）"
            return LEVEL_MINOR, f"文字微调（{chars} 字）"
        sens = has_sensitive(blob)
        if cls == "key":
            return LEVEL_IMPORTANT, f"关键页面正文变化（{chars} 字）"
        if sens:
            return LEVEL_IMPORTANT, "正文变化且涉及" + "、".join(sens)
        return LEVEL_NOTABLE, f"正文变化 {chars} 字"

    if kind in ("asset_url", "asset_content"):
        # 纯重建：只改了带哈希的 JS/CSS 文件名，正文没动 —— 关键页也按 CONTRACT 降为 1 级。
        # 图片/其它引用只比 URL，不能断定是重建，仍按页面级别处理。
        if (kind == "asset_url" and sig.get("action") == "rename"
                and sig.get("ref_kind") not in ("img", "other")):
            return LEVEL_MINOR, "前端资源改名（站点重新构建，正文无改动）"
        sens = has_sensitive(blob)
        if sens:
            return LEVEL_IMPORTANT, "前端资源涉及" + "、".join(sens)
        if cls == "key":
            return LEVEL_NOTABLE, "关键页面前端资源变化"
        return LEVEL_MINOR, "前端资源变化"

    if kind == "page_html":
        # 只有资源文件名变了（refs_only）同样是纯重建，关键页也不该升到 2 级。
        if sig.get("refs_only"):
            return LEVEL_MINOR, "页面 HTML 变化仅资源文件名（站点重新构建）"
        if cls == "key":
            return LEVEL_NOTABLE, "关键页面 HTML 变化（正文未变）"
        return LEVEL_MINOR, "页面 HTML 变化（正文未变）"

    return LEVEL_MINOR, f"其它信号 {kind}"


def decide(job: dict) -> dict:
    """输入 job（含 signals），输出 {"level", "reasons", "signals_by_level"}。

    规则：取所有信号里最高的级别；理由按级别去重保留前 5 条。
    """
    signals = job.get("signals") or []
    if not signals:
        return {"level": LEVEL_NOISE, "reasons": ["没有检测到变化"], "by_level": {}}

    best = LEVEL_NOISE
    reasons: list[str] = []
    by_level: dict[int, int] = {}
    # 同一页已经有正文信号时，HTML 哈希信号不再单独算一遍（否则理由会自相矛盾）
    pages_with_text = {s.get("page") for s in signals if s.get("kind") == "page_text"}
    for sig in signals:
        if sig.get("kind") == "page_html" and sig.get("page") in pages_with_text:
            continue
        lvl, why = _signal_level(sig)
        by_level[lvl] = by_level.get(lvl, 0) + 1
        best = max(best, lvl)
        if why not in reasons:
            reasons.append(why)
    # 理由排序：高级别在前（按信号顺序无法保证，这里简单按出现顺序截断）
    return {"level": best, "reasons": reasons[:5], "by_level": by_level}


def should_send(level: int, mode: str = "high") -> bool:
    """档位阈值：high 全发、normal ≥2、quiet ≥3。"""
    return level >= MODE_THRESHOLD.get(mode, 0)


def subject_prefix(level: int) -> str:
    return LEVEL_LABEL.get(level, "更新")


# ---------------------------------------------------------------- self-test


def _sig(kind, page="https://api-docs.deepseek.com/zh-cn/guides/vision/", **kw):
    d = {"kind": kind, "page": page}
    d.update(kw)
    return d


def _selftest() -> None:
    key = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
    reg = "https://api-docs.deepseek.com/zh-cn/guides/vision/"
    home = "https://www.deepseek.com/zh/"

    cases = [
        # (描述, signals, 期望级别)
        ("价格页正文变化", [_sig("page_text", key, text_changed_chars=120, detail="价格行 13.5→15")], 3),
        ("价格页资源变化", [_sig("asset_url", key, detail="main.js 重建")], 2),
        ("普通页实质变化", [_sig("page_text", reg, text_changed_chars=200, detail="新增一段说明")], 2),
        ("普通页提到金额", [_sig("page_text", reg, text_changed_chars=80, detail="输出 15 元/百万 tokens")], 3),
        ("普通页措辞", [_sig("page_text", reg, text_changed_chars=6, detail="把的改为了")], 1),
        # 关键页面的短数字变化不能当「文字微调」：价格/限流/上下文长度往往只有几个字
        ("关键页短价格变化", [_sig("page_text", key, text_changed_chars=8,
                                   before="输出 13.5 元 / 百万 tokens",
                                   after="输出 15 元 / 百万 tokens")], 3),
        ("关键页短上下文变化", [_sig("page_text", key, text_changed_chars=6,
                                     before="上下文长度 128K", after="上下文长度 64K")], 3),
        ("关键页短限流变化", [_sig("page_text", key, text_changed_chars=9,
                                   before="每分钟 500 次", after="每分钟 300 次")], 3),
        ("关键页短措辞无数字", [_sig("page_text", key, text_changed_chars=6,
                                     before="把的改为了", after="把的改为")], 1),
        # 采集端按差异行报的 197 字，真实只改掉一个「的」：不能升成重要
        ("关键页错别字不升级", [_sig("page_text", key, text_changed_chars=197,
                                   before="Token 是模型用来表示自然语言文本的的最小单位",
                                   after="Token 是模型用来表示自然语言文本的最小单位")], 1),
        # 真实计费口径改写（约 57 个字符）仍应升到重要
        ("关键页真实计费改写仍升级", [_sig("page_text", key, text_changed_chars=343,
                                     before="服务端搜索、网页抓取不再计费，仅按模型 Token 用量计费。",
                                     after="服务端搜索、网页抓取，其工具调用本身不再计费；工具返回的结果会拼接到模型上下文中参与推理，按模型 Token 用量计费。")], 3),
        # 普通 API 页的措辞微调不能因为路径里有 /api/ 就升重要
        ("普通 API 页措辞不升级", [_sig("page_text", "https://api-docs.deepseek.com/zh-cn/api/create-completion/",
                                     text_changed_chars=419,
                                     before="把 prompt 的内容也输出出来。",
                                     after="把 prompt 的内容也一并返回。")], 1),
        # 回归：字节数不能被当成错误码（scout 实测 547.1 KB 曾误判成 3 级）
        ("资源大小字样不升级", [_sig("asset_content", reg, detail="main.js 547.1 KB → 560.2 KB")], 1),
        ("真错误码仍升级", [_sig("page_text", reg, text_changed_chars=90,
                                 detail="新增错误码 429：请求过于频繁")], 3),
        ("首页纯重建", [_sig("asset_url", home, detail="chunk 重建")], 1),
        # 关键页的纯重建也只能判 1 级：refs_only / action=rename 与 CONTRACT 一致
        ("关键页纯重建-资源改名", [_sig("asset_url", key, action="rename",
                                        detail="main.js 文件名哈希变了（0c15113f → 9ab31f02）")], 1),
        ("关键页纯重建-refs_only", [_sig("page_html", key, refs_only=True,
                                          detail="HTML 变了，差异只在资源文件名上")], 1),
        ("关键页图片改名不按重建", [_sig("asset_url", key, action="rename", ref_kind="img",
                                          detail="图片 hero.webp 换了地址")], 2),
        ("关键页资源新增仍判2", [_sig("asset_url", key, action="added",
                                      detail="新增 runtime.js")], 2),
        ("新页面", [_sig("new_page", "https://api-docs.deepseek.com/zh-cn/guides/search/v2/")], 3),
        ("页面消失", [_sig("gone_page", reg)], 2),
        ("抓取失败", [_sig("fetch_error", reg)], 2),
        ("空信号", [], 0),
    ]
    for desc, sigs, want in cases:
        got = decide({"signals": sigs})["level"]
        assert got == want, f"{desc}: 期望 {want} 实得 {got}"

    assert should_send(0, "high") and not should_send(0, "normal")
    assert should_send(2, "normal") and not should_send(2, "quiet")
    assert should_send(3, "quiet")
    assert page_class(key) == "key" and page_class(home) == "marketing"
    assert page_class(reg) == "regular"
    print("rules.py 自检通过：%d 个用例" % len(cases))


if __name__ == "__main__":
    _selftest()
