"""测试临时目录：优先系统临时目录，兼容 Windows 沙箱。

本机 DSH 的 Windows 沙箱里，``tempfile.mkdtemp()`` 用 0o700 建出来的目录连
当前进程都写不进去（后续 mkdir/write 全部 PermissionError，目录也删不掉）。
因此 Windows 沙箱下改用同样落在系统临时目录、但用默认权限创建的随机目录；
其它平台仍直接用 ``tempfile.mkdtemp()``。调用方负责 cleanup()。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


def temp_dir(prefix: str) -> Path:
    if sys.platform != "win32" or not os.environ.get("DSH_SHELL"):
        return Path(tempfile.mkdtemp(prefix=prefix))
    base = Path(tempfile.gettempdir())
    for _ in range(100):
        path = base / f"{prefix}{uuid.uuid4().hex[:12]}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise RuntimeError(f"无法创建测试临时目录：{base}")


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
