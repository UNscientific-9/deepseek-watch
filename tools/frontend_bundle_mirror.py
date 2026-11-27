#!/usr/bin/env python3
"""离线前端镜像 CLI 兼容入口。

实现已移入 `webwatch/bundle_mirror.py`，生产巡检可以直接复用；这里保留
原有命令行和测试导入路径，避免两套解析/下载逻辑漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from webwatch import bundle_mirror as _impl

# 让 `from tools import frontend_bundle_mirror as mirror` 得到同一个模块对象，
# 测试里对 `mirror.fetch` 的 monkeypatch 才会命中真实实现。
sys.modules[__name__] = _impl


if __name__ == "__main__":
    raise SystemExit(_impl.main())
