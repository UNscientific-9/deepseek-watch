"""错误信息脱敏。

日志和状态文件里绝不能出现 API key、SMTP 密码或带 token 的 URL。
"""

from __future__ import annotations

import os
import re


def redact(text: object) -> str:
    value = str(text)
    for env_name in (
        "DEEPSEEK_API_KEY",
        "SMTP_PASS",
        "SMTP_USER",
        "INGEST_TOKEN",
        "WEBWATCH_TOKEN",
    ):
        secret = os.getenv(env_name, "").strip()
        if len(secret) >= 6:
            value = value.replace(secret, "***")
    value = re.sub(r"sk-[A-Za-z0-9]{6,}", "***", value)
    value = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", value)
    value = re.sub(r"(access_token=)[^&\s\"']+", r"\1***", value)
    value = re.sub(r"([?&](?:key|token|secret)=)[^&\s\"']+", r"\1***", value)
    # URL userinfo（https://user:token@host）不在上面的参数白名单里；状态页会
    # 公开展示 last_error，这里统一去掉。控制字符也一并剥离，避免污染 journal。
    value = re.sub(r"(?<=//)[^/@\s]{0,300}@", "***@", value)
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    return value[:1000]
