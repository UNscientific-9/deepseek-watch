"""推送失败时的邮件告警。

只在配置了 SMTP 且有可写状态目录时发信；同一个故障 6 小时内只提醒一次，避免每 5 分钟刷屏。
没有 SMTP 或状态目录时只打印日志，不阻塞推送本身。
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

from .state import load_json, save_json, state_dir

ALERT_COOLDOWN_SECONDS = 6 * 3600
SMTP_TIMEOUT = 20


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
        # 未知域名不能猜第三方 SMTP，否则会把邮箱密码发给不相干的服务器。
        raise RuntimeError(f"未配置 SMTP_HOST，且无法从域名 {domain or '（空）'} 推断；请显式设置 SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "").strip() or "465")
    to = os.getenv("MAIL_TO", "").strip() or user
    return user, password, host, port, to


def send_text(subject: str, body: str) -> bool:
    try:
        user, password, host, port, to = _smtp_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"[watchsite] 告警邮件配置不完整：{exc}", file=sys.stderr)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = to
    message.set_content(body[:6000])

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
        print(f"[watchsite] 告警邮件发送失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    return True


def notify(key: str, subject: str, body: str, now_ts: int, cooldown: int = ALERT_COOLDOWN_SECONDS) -> bool:
    if state_dir() is None:
        print("[watchsite] 没有可写状态目录，跳过告警邮件（请在 unit 中设置 StateDirectory）", file=sys.stderr)
        return False
    state = load_json("alert-state.json", {})
    last = int(state.get(key) or 0)
    if last and now_ts - last < cooldown:
        return False
    if not send_text(subject, body):
        return False
    state[key] = now_ts
    if not save_json("alert-state.json", state):
        # 邮件已经发出去了，但冷却时间没落盘；下一轮同一故障可能再发一封。
        # 必须把原因写进 journal，不能静默。
        print(
            "[watchsite] 错误：告警冷却时间写盘失败，同一故障可能每轮重发；请检查状态目录权限/空间",
            file=sys.stderr,
        )
    return True


def clear(key: str) -> None:
    state = load_json("alert-state.json", {})
    if key in state:
        state.pop(key, None)
        if not save_json("alert-state.json", state):
            print("[watchsite] 警告：告警状态清除失败，冷却可能仍然生效", file=sys.stderr)
