"""推送器的小状态文件：增量游标和告警冷却。

systemd 的 StateDirectory 会提供可写的 STATE_DIRECTORY；本地调试可用 PUSH_STATE_DIR。
没有可写目录时返回 None，调用方退回「全量发送」，不阻塞主流程。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def state_dir() -> Path | None:
    for name in ("PUSH_STATE_DIR", "STATE_DIRECTORY"):
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        for part in raw.split(os.pathsep):
            candidate = Path(part).expanduser()
            if candidate.is_dir():
                return candidate
    return None


def load_json(name: str, default: dict) -> dict:
    directory = state_dir()
    if directory is None:
        return dict(default)
    path = directory / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def save_json(name: str, data: dict) -> bool:
    directory = state_dir()
    if directory is None:
        return False
    path = directory / name
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=str(directory))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except OSError:
        try:
            os.unlink(temp_name)
        except (OSError, UnboundLocalError):
            pass
        return False
    return True
