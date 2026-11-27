"""可选的邮件通知。

默认不启用；只有环境变量 WEBWATCH_MAIL=1 且配置了 SMTP_USER/SMTP_PASS 时才发送。
邮件正文只包含事件标题和摘要，不包含任何密钥。
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

from .config import SITE_NAME, SITE_URL
from .redact import redact

SMTP_TIMEOUT = 20


def mail_enabled() -> bool:
    return os.getenv("WEBWATCH_MAIL", "").strip().lower() in {"1", "true", "yes", "on"}


def _smtp_settings() -> tuple[str, str, str, int, str]:
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not user or not password:
        raise RuntimeError("缺少 SMTP_USER / SMTP_PASS")
    domain = user.rsplit("@", 1)[-1].lower() if "@" in user else ""
    defaults = {
        "qq.com": "smtp.qq.com",
        "foxmail.com": "smtp.qq.com",
        "163.com": "smtp.163.com",
        "126.com": "smtp.126.com",
        "yeah.net": "smtp.yeah.net",
    }
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        host = defaults.get(domain, "")
    if not host:
        # 未知邮箱域名时绝不能猜一个第三方 SMTP：那会把 SMTP_USER/SMTP_PASS
        # 发给不相干的服务器。必须由运维显式配置。
        raise RuntimeError(f"未配置 SMTP_HOST，且无法从域名 {domain or '（空）'} 推断；请显式设置 SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "").strip() or "465")
    to = os.getenv("MAIL_TO", "").strip() or user
    return user, password, host, port, to


def _deliver(
    message: EmailMessage,
    user: str,
    password: str,
    host: str,
    port: int,
    error_label: str,
) -> bool:
    """465 走 SMTPS、其它端口 STARTTLS；两种发信路径共用这一份连接代码。"""
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(user, password)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001
        print(f"[webwatch] {error_label}：{type(exc).__name__}: {redact(exc)}", file=sys.stderr)
        return False
    return True


def send_text(subject: str, body: str) -> bool:
    """发送一封普通告警邮件；只在 WEBWATCH_MAIL=1 且 SMTP 配置完整时生效。"""
    if not mail_enabled():
        return False
    try:
        user, password, host, port, to = _smtp_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"[webwatch] 邮件配置不完整：{redact(exc)}", file=sys.stderr)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = to
    message.set_content(body[:6000])
    return _deliver(message, user, password, host, port, "告警邮件发送失败")


def _is_css_only(event: dict) -> bool:
    """B-4 观察期：纯 CSS 事件升 2 级但不发邮件，先观察一周。

    只改样式、没有新增用户文案/API/代码片段的 CSS 事件视为“纯 CSS”。
    """
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    evidence = payload.get("frontend") if isinstance(payload.get("frontend"), dict) else {}
    css = evidence.get("css_rule_changes") if isinstance(evidence.get("css_rule_changes"), dict) else {}
    if not (css.get("added") or css.get("removed")):
        return False
    for key in (
        "cjk_added", "cjk_removed", "api_added", "api_removed",
        "changed_code_snippets", "removed_code_snippets", "removed_i18n_refs",
        "added_i18n_refs", "snippets",
    ):
        if evidence.get(key):
            return False
    return not (
        (evidence.get("i18n") or {}).get("added")
        or (evidence.get("i18n") or {}).get("removed")
    )


def send_events(events: list[dict]) -> tuple[int, int]:
    """发送一批值得看的事件。返回 (成功, 失败)。"""
    if not mail_enabled() or not events:
        return 0, 0
    worthy = [
        event
        for event in events
        if int(event.get("level") or 0) >= 2 and not _is_css_only(event)
    ]
    if not worthy:
        return 0, 0
    try:
        user, password, host, port, to = _smtp_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"[webwatch] 邮件配置不完整：{redact(exc)}", file=sys.stderr)
        return 0, 1

    lines = [f"DeepSeek 观测站发现 {len(worthy)} 条新变化：", ""]
    for event in worthy[:30]:
        lines.append(f"- [{event.get('level_label')}] {event.get('title')}")
        summary = str(event.get("summary") or "").strip()
        if summary:
            lines.append(f"  {summary[:500]}")
        lines.append(f"  {event.get('page_url') or SITE_URL}")
    if len(worthy) > 30:
        lines.append(f"……另有 {len(worthy) - 30} 条，请打开网站查看。")
    lines.extend(["", f"网站：{SITE_URL}"])
    body = "\n".join(lines)

    message = EmailMessage()
    message["Subject"] = f"[{SITE_NAME}] {len(worthy)} 条新变化"
    message["From"] = user
    message["To"] = to
    message.set_content(body)
    if not _deliver(message, user, password, host, port, "发信失败"):
        return 0, 1
    return 1, 0
