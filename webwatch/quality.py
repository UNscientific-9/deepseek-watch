"""前端事件发布前的质量闸门与判级终审。

判读模型不是唯一的风险点：证据包漏了 CSS/代码差异时，模型也可能给出
「本次没有可见变化」这种看似合理、实际错误的结论。这里做三件事：

1. 标出每条页面事件的证据覆盖情况（payload.quality）；
2. 证据里已经有明确变化、但分析仍说「没有可见变化」时，自动改用规则兜底；
3. 结合证据和模型建议，给前端页面事件定最终级别（payload.level_decision）。

B-10：判级集中到 `decide()` 纯函数；`gate_events()`、`audit.py`、回填工具
都只通过它（和 `apply_event()`）得到同一结果，不再各写一套分支。

B-8：`gate_events(mark_audit_state=True)` 给事件记录 `quality_state` /
`next_check_at`。只有未决（contradiction/missing）事件进入 `pending` 等待
`audit.py` 复核；已给出终态的事件保持 repaired/partial/final，旧事件默认
final，避免每轮重复复查和二次重推。

只有带 payload.frontend.totals 的 page_change 事件才进入这里。平台公告、
纯正文/HTML 变化没有前端证据，不能被当成「证据不合格」而拦在站外。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config
from .ai import (
    ai_enabled as ai_is_enabled,
    apply_analysis,
    contains_tech,
    fallback_analysis,
)
from .config import LEVEL_LABELS
from .events import iso
from .frontend_evidence import technical_change_lines

NO_VISIBLE_PHRASES = (
    "未发现用户可见",
    "未发现界面",
    "没有可见变化",
    "没有文字摘要",
    "本次为内部调整",
    "未发现可核对",
    "未捕获到用户可见变化",
    "本轮没有抓到可读的界面变化",
)

# 出现这些词说明变化可能影响核心使用路径；只扫描新增中文文案/i18n key，
# 不扫大段代码，避免构建常量误命中。
CORE_HINTS = (
    "登录", "注册", "验证", "验证码", "密码", "账号", "绑定", "手机号", "邮箱", "实名",
    "权限", "安全", "隐私", "密钥", "支付", "充值", "退款", "账单", "价格", "定价",
    "费用", "收费", "计费", "余额", "额度", "套餐", "订阅", "配额", "限流", "模型",
    "api key", "token",
)

# payload.quality 的合法值。station 与推送端按这一组值做兼容。
QUALITY_VALUES = ("ok", "internal", "partial", "contradiction", "missing", "repaired")

# B-8 审计状态机：pending 才会被 audit 处理；其余都是终态。
AUDIT_STATES = ("pending", "repaired", "partial", "final")
AUDIT_TRANSITIONS = {
    "pending": ("repaired", "partial", "final"),
    "repaired": (),
    "partial": (),
    "final": (),
}
AUDIT_RECHECK_SECONDS = 30 * 60

# 未决质量：只有这些事件值得进入 pending 等下一轮复核。
UNRESOLVED_QUALITIES = ("contradiction", "missing")

# 「确定」「取消」这类通用按钮词不能单独作为升级到 3 级的依据。
GENERIC_VISIBLE_WORDS = frozenset(
    {
        "确定", "取消", "关闭", "保存", "删除", "提交", "返回", "上一步", "下一步",
        "是", "否", "好的", "完成", "编辑", "更多", "搜索", "设置", "复制", "分享",
        "重命名", "移除", "添加", "新建", "打开", "选择", "全部", "重试", "加载中",
        "请稍后", "暂无数据", "无数据", "确认",
    }
)


def _analysis_text(analysis: dict) -> str:
    parts = [
        analysis.get("headline"),
        analysis.get("tldr"),
        *(analysis.get("visible_changes") or []),
    ]
    return " ".join(str(part or "") for part in parts)


def has_visible_signal(evidence: dict) -> bool:
    """证据里是否存在能支撑「用户会看到变化」的具体信号。"""
    if not isinstance(evidence, dict):
        return False
    for key in (
        "cjk_added", "cjk_removed", "api_added", "api_removed", "css_vars_added",
        "changed_code_snippets", "removed_code_snippets", "removed_i18n_refs",
        "added_i18n_refs", "snippets",
    ):
        value = evidence.get(key)
        if isinstance(value, (list, tuple)) and value:
            return True
    if (evidence.get("i18n") or {}).get("added"):
        return True
    if (evidence.get("i18n") or {}).get("removed"):
        return True
    if (evidence.get("css_rule_changes") or {}).get("added"):
        return True
    if (evidence.get("css_rule_changes") or {}).get("removed"):
        return True
    return False


def coverage(evidence: dict) -> dict:
    """返回分片核对覆盖度；用于识别「证据没取全就下结论」。"""
    if not isinstance(evidence, dict):
        return {}
    i18n = evidence.get("i18n") or {}
    chunk = evidence.get("chunk_changes") or {}
    css = evidence.get("css_rule_changes") or {}
    reasons = evidence.get("truncation_reasons")
    total = int(chunk.get("changed") or i18n.get("changed_chunks") or 0)
    checked = int(chunk.get("changed_checked") or 0)
    return {
        "changed_chunks": total,
        "changed_checked": checked,
        "i18n_checked": int(i18n.get("checked") or 0),
        "css_checked": int(css.get("checked") or 0),
        "css_changed": int(css.get("changed") or 0),
        "css_unchecked": int(css.get("unchecked") or 0),
        "added_chunks": int(chunk.get("added") or 0),
        "removed_chunks": int(chunk.get("removed") or 0),
        "paired_chunks": int(chunk.get("paired") or 0),
        "chunk_checked": bool(chunk.get("checked")),
        "partial_pairing": bool(chunk.get("partial_pairing")),
        "truncated": bool(evidence.get("truncated")),
        "truncation_reasons": [str(item) for item in reasons] if isinstance(reasons, list) else [],
        "baseline_missing": bool(evidence.get("baseline_missing")),
    }


def coverage_gaps(evidence: dict) -> list[str]:
    """用普通人能懂的话列出「缺了什么、为什么缺」，供待核实理由展示。"""
    if not isinstance(evidence, dict):
        return []
    cov = coverage(evidence)
    gaps: list[str] = []

    def add(text: str) -> None:
        if text and text not in gaps:
            gaps.append(text)

    if cov["baseline_missing"]:
        add("旧版主包或样式无法下载，无法对比新增内容")
    for reason in cov["truncation_reasons"][:3]:
        add(str(reason))
    if cov["truncated"] and not cov["truncation_reasons"]:
        add("部分资源因预算限制未展开")
    if cov["changed_chunks"] and cov["changed_checked"] < cov["changed_chunks"]:
        add(f"变更分片核对 {cov['changed_checked']}/{cov['changed_chunks']}，其余没取到新旧正文")
    if cov["partial_pairing"]:
        add("新增/删除分片没有完整配对，无法确认哪些内容是真新增")
    if (cov["added_chunks"] or cov["removed_chunks"]) and not cov["chunk_checked"]:
        add("新增/删除分片未核对")
    if cov["css_changed"] and not cov["css_checked"]:
        add(f"CSS 分片有 {cov['css_changed']} 个变化，但正文没取到，未做规则对比")
    return gaps


def has_verified_change_detail(evidence: dict) -> bool:
    """是否已经拿到「可对照的具体变化」，而不只是未配对的线索。

    这里的「可对照」指新旧两侧都拿到并做过差分：主包/分片代码 diff、
    i18n 引用增减，或绝大多数变更分片已逐对核对。只有这种证据才足以
    在部分资源未展开时仍给出「变了什么」的结论。
    """
    if not isinstance(evidence, dict):
        return False
    if evidence.get("file_diffs"):
        return True
    if evidence.get("removed_i18n_refs") or evidence.get("added_i18n_refs"):
        return True
    cov = coverage(evidence)
    if cov["changed_chunks"] and cov["changed_checked"]:
        # 至少 90% 的变更分片已核对：少数缺失不再影响「变了什么」的结论。
        if cov["changed_checked"] * 10 >= cov["changed_chunks"] * 9:
            return True
    if cov["chunk_checked"] and cov["paired_chunks"]:
        return True
    return False


def coverage_sufficient(evidence: dict) -> bool:
    """证据是否足以「说清变化」并发布为 ok（允许注明部分资源未展开）。

    比 `coverage_complete()` 宽：只要已有可对照的具体变化 + 用户可见信号，
    预算截断或少量未配对分片不应把事件压成待核实。缺失关键材料
    （旧版主包/样式）时仍然不够。
    """
    if not isinstance(evidence, dict):
        return False
    if evidence.get("baseline_missing"):
        return False
    if coverage_complete(evidence):
        return True
    return has_visible_signal(evidence) and has_verified_change_detail(evidence)


def coverage_complete(evidence: dict) -> bool:
    """证据是否完整到可以下「内部调整」或「重要」的结论。

    旧包/旧分片下载失败、预算截断、新增/删除分片只抓到一个方向，都不算完整；
    这种情况下只能按「值得看」发布并标注待核实，不能压成轻微，也不能升重要。
    """
    if not isinstance(evidence, dict):
        return False
    if evidence.get("truncated") or evidence.get("baseline_missing"):
        return False
    cov = coverage(evidence)
    if cov["changed_chunks"] and cov["changed_checked"] < cov["changed_chunks"]:
        return False
    if cov["partial_pairing"]:
        return False
    if (cov["added_chunks"] or cov["removed_chunks"]) and not cov["chunk_checked"]:
        return False
    if cov["css_changed"] and not cov["css_checked"]:
        return False
    return True


def frontend_evidence(event: dict) -> dict | None:
    """只认真正的「前端页面变化」事件；公告、纯正文等没有前端证据。"""
    if str(event.get("source") or "") not in config.TARGETS:
        return None
    if str(event.get("kind") or "") != "page_change":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    evidence = payload.get("frontend")
    if not isinstance(evidence, dict) or not evidence.get("totals"):
        return None
    return evidence


def assess_event(event: dict) -> dict:
    """给单条事件定质量状态，不修改事件。"""
    evidence = frontend_evidence(event)
    if evidence is None:
        return {"status": "no_evidence", "signals": False, "coverage": {}}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    signals = has_visible_signal(evidence)
    cov = coverage(evidence)
    complete = coverage_complete(evidence)
    if not analysis and signals:
        return {"status": "missing", "signals": signals, "coverage": cov}
    if signals and any(phrase in _analysis_text(analysis) for phrase in NO_VISIBLE_PHRASES):
        return {"status": "contradiction", "signals": True, "coverage": cov}
    if not signals:
        # 没有用户可见信号时，「内部调整」必须以证据完整为前提；否则待核实。
        return {
            "status": "internal" if complete else "partial",
            "signals": False,
            "coverage": cov,
        }
    if not complete and not coverage_sufficient(evidence):
        # 证据没取全、也没有可对照的具体变化时，既不能当成「内部调整」压成轻微，
        # 也不能当成「重要」；统一按 partial 发布，由判级终审落在「值得看」。
        return {"status": "partial", "signals": signals, "coverage": cov}
    # 已有具体可对照的变化：预算截断/少量分片未配对不再阻止 ok，
    # 缺口由 level_decision.coverage_gaps 和 changes 页资源核对组注明。
    return {"status": "ok", "signals": True, "coverage": cov}


def _confidence(analysis: dict) -> str:
    raw = str((analysis or {}).get("confidence") or "").strip().lower()
    return {"高": "high", "中": "medium", "低": "low"}.get(raw, raw)


def _short_reason(analysis: dict) -> str:
    reason = str((analysis or {}).get("level_reason") or "").strip()
    if not reason or contains_tech(reason):
        return ""
    return reason[:200]


def _event_level(event: dict) -> int:
    """读取事件当前级别；0 是合法级别，不能用 `or` 兜底。"""
    raw = event.get("level")
    if raw is None:
        return 0
    try:
        return max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        return 0


def _model_analysis(analysis: dict) -> bool:
    """这段 analysis 是否是模型真正生成的文本（不是规则兜底）。"""
    if not isinstance(analysis, dict) or not analysis:
        return False
    if analysis.get("degraded"):
        return False
    return any(
        str(analysis.get(key) or "").strip()
        for key in ("headline", "tldr")
    ) or bool(analysis.get("visible_changes"))


def _promotion_items(evidence: dict) -> list[str]:
    """收集去重后的用户可见文案，用于「两条独立证据」门槛（P2-03）。"""
    items: list[str] = []
    seen: set[str] = set()

    def add(text: object) -> None:
        value = str(text or "").strip()
        if not value:
            return
        key = value.strip("。；;，,、！!？?「」\"' ").lower()
        if not key or key in seen:
            return
        seen.add(key)
        items.append(value)

    for text in evidence.get("cjk_added") or []:
        add(text)
    for text in evidence.get("cjk_removed") or []:
        add(text)
    for item in evidence.get("removed_i18n_refs") or []:
        if isinstance(item, dict):
            for value in item.get("values") or []:
                add(value)
    for item in evidence.get("added_i18n_refs") or []:
        if isinstance(item, dict):
            for value in item.get("values") or []:
                add(value)
    i18n_added = (evidence.get("i18n") or {}).get("added") or {}
    if isinstance(i18n_added, dict):
        for value in i18n_added.values():
            # 只有带中文原文的 i18n 值才算一条用户可见文案；key 是内部标识，
            # 计数也不能当成一句独立文案（P2-03：同一句中英各算一条的根因）。
            if isinstance(value, str) and value.strip():
                add(value)
    return items


def _substantive_items(items: list[str]) -> list[str]:
    out = []
    for item in items:
        normalized = item.strip("。；;，,、！!？?「」\"' ").lower()
        if normalized and normalized not in GENERIC_VISIBLE_WORDS:
            out.append(item)
    return out


def _can_promote(evidence: dict, analysis: dict) -> bool:
    """AI 想把事件升到「重要」时必须满足的证据门槛。

    P2-03：中文文案和 i18n key 可能是同一句，先按文本去重；「确定/取消」这类
    通用按钮词不计入独立证据条数。
    """
    if not coverage_complete(evidence):
        return False
    if _confidence(analysis) not in ("high", "medium"):
        return False
    if not _short_reason(analysis):
        return False
    items = _promotion_items(evidence)
    if not items:
        return False
    blob = " ".join(items).lower()
    core = any(term.lower() in blob for term in CORE_HINTS)
    return core or len(_substantive_items(items)) >= 2


def _decide_level(evidence: dict, analysis: dict, status: str, *, ai_enabled: bool = True) -> tuple[int, str, str]:
    """返回 (最终级别, 来源, 一句人话理由)。规则始终是最后拍板的人。"""
    gaps = coverage_gaps(evidence)
    if status == "internal":
        # 「证据完整、只是没有用户可见文案」≠「什么都没抓到」。
        # 有技术变化时把变化的文件/字节写进理由，避免笼统的「未捕获」。
        facts = technical_change_lines(evidence)
        if facts:
            detail = "；".join(facts[:2])
            return 1, "rule", f"仅技术变化：{detail}；未提取到用户可见文案"
        return 1, "rule", "仅有技术变化，未提取到用户可见文案"
    if status == "partial":
        if gaps:
            return 2, "guard", f"证据不完整（{'；'.join(gaps[:2])}），按值得看发布并标注待核实"
        return 2, "guard", "证据不完整，按值得看发布并标注待核实"
    if status == "contradiction":
        if ai_enabled:
            return 2, "gate", "证据与结论不一致，已按证据修正"
        return 2, "gate", "证据与结论不一致（AI 已关闭），按证据先行定级"
    if status == "missing":
        if ai_enabled:
            return 2, "gate", "缺少判读，已按证据修正"
        return 2, "gate", "缺少判读（AI 已关闭），按证据先行定级"
    ai_level = analysis.get("level") if not analysis.get("degraded") else None
    if ai_level == 3 and _can_promote(evidence, analysis):
        reason = _short_reason(analysis) or "AI 判断影响核心使用路径"
        return 3, "ai", reason
    if ai_level == 2:
        reason = _short_reason(analysis) or "用户可见的局部更新"
        if gaps:
            reason = f"{reason}（{'；'.join(gaps[:2])}）"
        return 2, "ai", reason
    reason = "普通前端更新，按值得看处理"
    if gaps:
        reason = f"已核对到具体变化；{'；'.join(gaps[:2])}"
    return 2, "rule", reason


def decide(event: dict, evidence: dict, status: str, *, ai_enabled: bool = True) -> dict:
    """B-10 判级纯函数：只读 event/evidence，返回最终决定，不修改事件。

    返回字段：
        level         最终级别 0-3
        quality       最终质量状态（QUALITY_VALUES 之一）
        reason        一句人话判级理由
        ai_suggestion 原模型建议级别（被丢弃也保留，P2-14）
        discarded     原模型文本是否被规则结论替换（P2-01/P2-14）
    """
    if status not in ("ok", "internal", "partial", "contradiction", "missing"):
        raise ValueError(f"未知质量状态：{status}")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    ai_level = analysis.get("level")
    if isinstance(ai_level, bool) or not isinstance(ai_level, int) or not 0 <= ai_level <= 3:
        ai_level = None
    ai_reason = str(analysis.get("level_reason") or "")[:200]
    model_text = _model_analysis(analysis)
    rule_level = _event_level(event)
    quality = status
    if status in ("contradiction", "missing"):
        quality = "repaired" if ai_enabled else status
    discarded = bool(ai_enabled and model_text and status in ("contradiction", "missing", "internal"))
    level, source, reason = _decide_level(evidence, analysis, status, ai_enabled=ai_enabled)
    return {
        "status": status,
        "level": level,
        "quality": quality,
        "source": source,
        "rule": rule_level,
        "reason": reason,
        "coverage_gaps": coverage_gaps(evidence),
        "ai_suggestion": ai_level,
        "ai_reason": ai_reason,
        "discarded": discarded,
    }


def _repair_analysis(event: dict, evidence: dict, status: str) -> None:
    """按状态用规则兜底替换模型文本（只在 AI 开启时调用）。"""
    if status == "contradiction":
        apply_analysis(event, fallback_analysis(event, evidence, "证据与结论不一致，已自动按证据修正"))
    elif status == "missing":
        apply_analysis(event, fallback_analysis(event, evidence, "缺少判读，已自动生成规则结论"))
    elif status == "internal":
        apply_analysis(
            event,
            fallback_analysis(
                event,
                evidence,
                "本轮只检测到技术层面的资源变化，未提取到用户可见文案",
            ),
        )


def apply_decision(event: dict, decision: dict) -> None:
    """把 Decision 写回事件：级别、level_label、quality、level_decision、分组级别。"""
    level = max(0, min(3, int(decision.get("level") or 0)))
    event["level"] = level
    event["level_label"] = LEVEL_LABELS.get(level, "值得看")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        event["payload"] = payload
    payload["quality"] = str(decision.get("quality") or "ok")
    payload["level_decision"] = {
        "final": level,
        "source": str(decision.get("source") or "rule"),
        "rule": int(decision.get("rule") or 0),
        "ai": decision.get("ai_suggestion"),
        "reason": str(decision.get("reason") or "")[:200],
        "ai_reason": str(decision.get("ai_reason") or "")[:200],
        "discarded": bool(decision.get("discarded")),
    }
    gaps = decision.get("coverage_gaps")
    if gaps:
        payload["level_decision"]["coverage_gaps"] = [
            str(item)[:160] for item in gaps[:5]
        ]
    detail = event.get("changes_detail")
    if isinstance(detail, list):
        for group in detail:
            if isinstance(group, dict):
                group["level"] = level


def apply_event(event: dict, *, ai_enabled: bool = True, status: str | None = None) -> dict:
    """对单条前端事件执行闸门与判级；返回 Decision（事件会被修改）。"""
    evidence = frontend_evidence(event)
    if evidence is None:
        return {}
    if status is None:
        status = str(assess_event(event)["status"])
    decision = decide(event, evidence, status, ai_enabled=ai_enabled)
    if ai_enabled and status in ("contradiction", "missing", "internal"):
        _repair_analysis(event, evidence, status)
    apply_decision(event, decision)
    return decision


def quality_state(event: dict) -> str:
    """读取审计状态；没有记录（旧事件/外部事件）一律按 final 处理。"""
    if not isinstance(event, dict):
        return "final"
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    state = str(event.get("quality_state") or payload.get("quality_state") or "").strip()
    return state if state in AUDIT_STATES else "final"


def mark_quality_state(event: dict, state: str, *, now: datetime | None = None) -> None:
    """显式迁移审计状态；pending 会写入到期时间，其余终态清除它。"""
    if state not in AUDIT_STATES:
        raise ValueError(f"未知审计状态：{state}")
    current = str(event.get("quality_state") or "").strip()
    if current in AUDIT_STATES and current != state:
        allowed = AUDIT_TRANSITIONS.get(current, ())
        if state not in allowed:
            raise ValueError(f"非法审计迁移：{current} → {state}")
    event["quality_state"] = state
    if state == "pending":
        current_time = now or datetime.now(timezone.utc)
        event["next_check_at"] = iso(current_time + timedelta(seconds=AUDIT_RECHECK_SECONDS))
    else:
        event.pop("next_check_at", None)


def gate_events(
    events: list[dict],
    *,
    ai_enabled: bool | None = None,
    mark_audit_state: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    """执行发布前质量闸门与判级终审；返回事件和统计。

    保持原有 `gate_events(events)` 签名/返回兼容。`ai_enabled=False` 时只做
    quality/level 判定，不生成任何规则兜底文案（P2-04）。
    `mark_audit_state=True` 时在事件顶层写 `quality_state` / `next_check_at`
    （B-8，审计用；不进入 payload，避免仅因状态推进触发站点重推）。
    """
    if ai_enabled is None:
        ai_enabled = ai_is_enabled()
    counts: dict[str, int] = {}
    for event in events:
        evidence = frontend_evidence(event)
        if evidence is None:
            # 平台公告、纯正文/HTML 变化没有前端证据：不写 quality，正常推送。
            continue
        status = str(assess_event(event)["status"])
        counts[status] = counts.get(status, 0) + 1
        apply_event(event, ai_enabled=ai_enabled, status=status)
        if mark_audit_state:
            # 新事件统一先进入 pending，由 audit.py 在到期后显式落终态
            # （pending → repaired / partial / final），旧事件没有该字段按 final。
            event.pop("quality_state", None)
            event.pop("next_check_at", None)
            mark_quality_state(event, "pending", now=now)
    return events, counts


def quality_summary(counts: dict) -> str:
    if not counts:
        return ""
    return "，".join(f"{key}={value}" for key, value in sorted(counts.items()))
