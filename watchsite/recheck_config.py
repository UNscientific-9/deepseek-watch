"""复核 worker 的运行参数。

所有配置都从环境变量读取，并且**在调用时读取**，便于测试用临时目录覆盖；
模块导入时不读取任何密钥、不创建目录。
"""

from __future__ import annotations

import os
from pathlib import Path

# 与 webwatch 的硬契约保持一致；生产 unit 会把 /opt/deepseek-webwatch 放进
# PYTHONPATH。导入失败时退回同一组常量，保证 watchsite 单独运行也能工作。
try:  # pragma: no cover - 生产环境一定存在
    from webwatch.config import ALLOWED_HOSTS as _WEBWATCH_ALLOWED_HOSTS
    from webwatch.config import LEVEL_LABELS as _WEBWATCH_LEVEL_LABELS
except Exception:  # noqa: BLE001
    _WEBWATCH_ALLOWED_HOSTS = (
        "chat.deepseek.com",
        "platform.deepseek.com",
        "www.deepseek.com",
        "deepseek.com",
        "fe-static.deepseek.com",
        "api-docs.deepseek.com",
        "api.deepseek.com",
    )
    _WEBWATCH_LEVEL_LABELS = {0: "噪音", 1: "轻微", 2: "值得看", 3: "重要"}

LEVEL_LABELS = dict(_WEBWATCH_LEVEL_LABELS)
DEFAULT_SOURCES = ("chat", "platform", "harness")
DEFAULT_EVENTS_DIR = "/var/lib/deepseek-webwatch/events"
DEFAULT_STATE_DIR = "/var/lib/deepseek-recheck"
DEFAULT_VAULT_DIR = "/var/lib/deepseek-webwatch/bundle-vault"
DEFAULT_AGENT = "/opt/agent/bin/agent"
DEFAULT_AGENT_SRC = "/home/user/.claude"
DEFAULT_AGENT_USER = "pi"
DEFAULT_MODEL = "your-model-id"

# 复核任务只保留到 Agent 跑完；证据副本在完成后由 recheck.cleanup_job_evidence()
# 删除，因此作业上限可以放宽到 10MB，避免大文件被截断影响结论。
JOB_MAX_BYTES = 10 * 1024 * 1024
RECHECK_MAX_BYTES = 8 * 1024
VERDICT_MAX_BYTES = 64 * 1024
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 400
MAX_IMPACT_CHARS = 400
MAX_ACTION_CHARS = 300
MAX_REASON_CHARS = 500
MAX_EVIDENCE_ITEMS = 8
MAX_TEXT_LIST_ITEMS = 12
MAX_TEXT_ITEM_CHARS = 500
MAX_JOBS_KEPT = 20
MAX_JOBS_BYTES = 500 * 1024 * 1024
JOB_KEEP_DAYS = 7
QUEUE_KEEP_DAYS = 30
FAILED_KEEP = 50
AGENT_TIMEOUT_SECONDS = 3600
AGENT_MAX_ATTEMPTS = 3
AGENT_MAX_TURNS = 40
STALE_RUNNING_SECONDS = 2 * AGENT_TIMEOUT_SECONDS


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 10**9) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def state_dir() -> Path:
    raw = _env("RECHECK_STATE_DIR") or _env("STATE_DIRECTORY")
    if raw:
        # systemd 的 STATE_DIRECTORY 可能是冒号分隔的多个目录；取第一个。
        raw = raw.split(":", 1)[0] if os.name == "posix" else raw
        return Path(raw).expanduser()
    return Path(DEFAULT_STATE_DIR)


def events_dir() -> Path:
    raw = _env("RECHECK_EVENTS_DIR") or _env("WEBWATCH_EVENTS_DIR")
    return Path(raw).expanduser() if raw else Path(DEFAULT_EVENTS_DIR)


def vault_dir() -> Path:
    raw = _env("RECHECK_VAULT_DIR")
    return Path(raw).expanduser() if raw else Path(DEFAULT_VAULT_DIR)


def push_state_dir() -> Path:
    raw = _env("RECHECK_PUSH_STATE_DIR") or _env("PUSH_STATE_DIR")
    return Path(raw).expanduser() if raw else Path("/var/lib/deepseek-watch-push")


def require_pushed() -> bool:
    """只复核已经成功上站的事件；首次部署的基线不受此限制。"""
    return _env_bool("RECHECK_REQUIRE_PUSHED", True)


def queue_db_path() -> Path:
    return state_dir() / "queue.db"


def jobs_dir() -> Path:
    return state_dir() / "jobs"


def logs_dir() -> Path:
    return state_dir() / "logs"


def sources() -> tuple[str, ...]:
    raw = _env("RECHECK_SOURCES")
    if not raw:
        return DEFAULT_SOURCES
    items = tuple(item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip())
    # 复核范围硬限制在 chat/platform/harness；即使 env 写了 docs/fingerprint 也不放行。
    allowed = tuple(source for source in items if source in DEFAULT_SOURCES)
    return allowed or DEFAULT_SOURCES


def allowed_hosts() -> tuple[str, ...]:
    extra = tuple(
        item.strip().lower()
        for item in _env("RECHECK_EXTRA_HOSTS").replace(";", ",").split(",")
        if item.strip()
    )
    return tuple(dict.fromkeys(tuple(_WEBWATCH_ALLOWED_HOSTS) + extra))


def apply_level_enabled() -> bool:
    return _env_bool("RECHECK_APPLY_LEVEL", False)


def level_min_gap() -> int:
    return _env_int("RECHECK_LEVEL_MIN_GAP", 2, 1, 3)


def agent_path() -> str:
    return _env("RECHECK_AGENT") or DEFAULT_AGENT


def agent_src() -> Path:
    return Path(_env("RECHECK_AGENT_SRC") or DEFAULT_AGENT_SRC).expanduser()


def agent_home_config() -> Path | None:
    """Claude Code 旧版会把 ~/.claude.json 放在 home 根目录；可显式关闭/指定。"""
    raw = _env("RECHECK_AGENT_HOME_CONFIG", "/home/user/.claude.json")
    if raw.lower() in {"", "0", "false", "off", "no", "none"}:
        return None
    return Path(raw).expanduser()


def agent_user() -> str:
    return _env("RECHECK_AGENT_USER") or DEFAULT_AGENT_USER


def agent_group() -> str:
    return _env("RECHECK_AGENT_GROUP") or agent_user()


def agent_model() -> str:
    return _env("RECHECK_MODEL") or DEFAULT_MODEL


def agent_timeout() -> int:
    return _env_int("RECHECK_AGENT_TIMEOUT", AGENT_TIMEOUT_SECONDS, 60, 6 * 3600)


def max_attempts() -> int:
    return _env_int("RECHECK_MAX_ATTEMPTS", AGENT_MAX_ATTEMPTS, 1, 10)


def agent_max_turns() -> int:
    return _env_int("RECHECK_AGENT_MAX_TURNS", AGENT_MAX_TURNS, 4, 200)


def job_max_bytes() -> int:
    return _env_int("RECHECK_JOB_MAX_BYTES", JOB_MAX_BYTES, 256 * 1024, 64 * 1024 * 1024)


def recheck_max_bytes() -> int:
    return _env_int("RECHECK_RECORD_MAX_BYTES", RECHECK_MAX_BYTES, 1024, 64 * 1024)


def max_jobs_kept() -> int:
    return _env_int("RECHECK_JOBS_KEEP", MAX_JOBS_KEPT, 0, 10000)


def max_jobs_bytes() -> int:
    return _env_int("RECHECK_JOBS_MAX_BYTES", MAX_JOBS_BYTES, 0, 100 * 1024**3)


def job_keep_days() -> int:
    return _env_int("RECHECK_JOB_KEEP_DAYS", JOB_KEEP_DAYS, 1, 3650)


def queue_keep_days() -> int:
    return _env_int("RECHECK_QUEUE_KEEP_DAYS", QUEUE_KEEP_DAYS, 1, 3650)


def failed_keep() -> int:
    return _env_int("RECHECK_FAILED_KEEP", FAILED_KEEP, 0, 10000)


def stale_running_seconds() -> int:
    return _env_int("RECHECK_STALE_RUNNING_SECONDS", STALE_RUNNING_SECONDS, 600, 30 * 86400)


def memory_max() -> str:
    return _env("RECHECK_MEMORY_MAX") or "400M"


def memory_high() -> str:
    return _env("RECHECK_MEMORY_HIGH") or "280M"


def cpu_quota() -> str:
    return _env("RECHECK_CPU_QUOTA") or "100%"


def max_notifications_per_run() -> int:
    return _env_int("RECHECK_MAX_NOTIFICATIONS", 5, 0, 50)


def heartbeat_max_age_hours() -> int:
    return _env_int("RECHECK_HEARTBEAT_MAX_AGE_HOURS", 24, 1, 720)
