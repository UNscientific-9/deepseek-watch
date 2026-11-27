"""以 pi 用户启动 Claude Code，并把它关进 systemd 沙箱。

安全边界（任务书 §7/§8）：
- 只有 worker 是 root；agent 通过 `systemd-run --uid=pi --gid=pi` 降权。
- `ProtectHome=yes` 隐藏 /home/user；凭据先复制到 /run 下的随机私有目录，再只 bind 那一份。
- job 目录整体只读，只有 `job/out` 可写。
- 沙箱不可用（非 root / 无 systemd-run / 无可用凭据）→ 抛 `SandboxUnavailable`，
  **绝不**退化为普通子进程。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

from . import recheck_config as cfg
from . import recheck_verdict

LOG = logging.getLogger("watchsite.recheck.agent")


class SandboxUnavailable(RuntimeError):
    """无法建立安全沙箱；调用方必须把本次复核记为失败。"""


class AgentTimeout(RuntimeError):
    """agent 超过硬超时。"""


class AgentRunError(RuntimeError):
    """agent 启动/退出异常。"""


AUTH_FILE_ALLOWLIST = (
    "settings.json",
    "settings.local.json",
    ".credentials.json",
    "credentials.json",
    "oauth.json",
)


def _systemd_available() -> bool:
    return bool(shutil.which("systemd-run")) and hasattr(os, "geteuid") and os.geteuid() == 0


def resolve_uid_gid(user: str, group: str = "") -> tuple[int, int] | None:
    try:
        import grp
        import pwd

        pw = pwd.getpwnam(user)
        gid = grp.getgrnam(group).gr_gid if group else pw.pw_gid
        return pw.pw_uid, gid
    except (ImportError, KeyError):
        return None


def _read_regular(path: Path) -> bytes | None:
    """只读普通文件；拒绝符号链接/目录/FIFO。"""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _write_private(path: Path, data: bytes, uid: int | None, gid: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o640)
    if uid is not None and gid is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
        # 凭据副本始终 root 所有、pi 组只读；agent 只能读，不能改。
        os.chown(path, 0, gid)


def _runtime_parent() -> Path:
    raw = os.environ.get("RUNTIME_DIRECTORY", "")
    if raw:
        first = raw.split(":", 1)[0] if os.name == "posix" else raw
        if first:
            return Path(first)
    return Path("/run/deepseek-recheck-auth")


def prepare_agent_auth(
    source: Path,
    *,
    runtime_parent: Path | None = None,
    name_hint: str = "job",
    uid: int | None = None,
    gid: int | None = None,
) -> dict:
    """把允许的 Claude 配置/凭据复制到 worker 私有目录；不读 env、不复制会话。"""
    source = Path(source)
    if not source.is_dir():
        raise SandboxUnavailable(f"agent 配置目录不存在：{source}")
    parent = Path(runtime_parent) if runtime_parent else _runtime_parent()
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxUnavailable(f"无法创建凭据目录 {parent}: {exc}") from exc
    safe_hint = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name_hint))[:60] or "job"
    root = parent / f"{safe_hint}-{os.getpid()}-{time.time_ns()}-{secrets.token_hex(4)}"
    try:
        root.mkdir(mode=0o700)
    except OSError as exc:
        raise SandboxUnavailable(f"无法创建凭据子目录 {root}: {exc}") from exc

    copied: list[str] = []
    try:
        for name in AUTH_FILE_ALLOWLIST:
            body = _read_regular(source / name)
            if body is None:
                continue
            _write_private(root / name, body, uid, gid)
            copied.append(name)
        home_config = cfg.agent_home_config()
        if home_config and home_config.is_file():
            body = _read_regular(home_config)
            if body is not None:
                # Claude 只认 ~/.claude.json；不要用环境变量里的路径文件名。
                _write_private(root / ".claude.json", body, uid, gid)
                copied.append(".claude.json")
    except OSError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise SandboxUnavailable(f"复制 agent 凭据失败：{type(exc).__name__}") from exc

    if not copied:
        shutil.rmtree(root, ignore_errors=True)
        raise SandboxUnavailable(
            f"{source} 下没有可用的 Claude 配置/凭据（允许：{', '.join(AUTH_FILE_ALLOWLIST)}）"
        )

    try:
        os.chmod(root, 0o770)
        if uid is not None and gid is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(root, 0, gid)
            if parent != root:
                os.chown(parent, 0, gid)
                os.chmod(parent, 0o770)
    except OSError:
        pass
    return {"root": root, "files": copied}


def cleanup_agent_auth(auth: dict | None) -> None:
    if not auth:
        return
    root = auth.get("root")
    if root:
        shutil.rmtree(Path(root), ignore_errors=True)


def _unit_name(job_dir: Path) -> str:
    digest = re.sub(r"[^A-Za-z0-9]+", "-", job_dir.name).strip("-")[:40] or "job"
    return f"deepseek-recheck-agent-{digest}-{os.getpid()}"


def build_sandbox_command(
    *,
    job_dir: Path,
    auth_root: Path | None,
    prompt: str,
    model: str | None = None,
    timeout: int | None = None,
    max_turns: int | None = None,
    agent_path: str | None = None,
    agent_user: str | None = None,
    agent_group: str | None = None,
) -> list[str]:
    """构造 systemd-run 命令行；不可用时抛 SandboxUnavailable。"""
    if not _systemd_available():
        raise SandboxUnavailable("systemd-run 不可用或当前不是 root，拒绝无沙箱运行 agent")
    job_dir = Path(job_dir)
    out_dir = job_dir / "out"
    if not out_dir.is_dir():
        raise SandboxUnavailable(f"job out/ 不存在：{out_dir}")
    timeout = int(timeout or cfg.agent_timeout())
    uid_name = agent_user or cfg.agent_user()
    gid_name = agent_group or cfg.agent_group()
    if resolve_uid_gid(uid_name, gid_name) is None:
        raise SandboxUnavailable(f"找不到运行 agent 的用户/组：{uid_name}:{gid_name}")

    unit = _unit_name(job_dir)
    # agent 只应看到当前 job。job 源目录在 /var/lib 下，即使 bind 进去，pi
    # 也无法穿过 root:0700 的父目录；因此统一映射到 /run 下一个固定入口，
    # 同时把整个 /var/lib/deepseek-recheck 设为不可见。
    visible_job = Path(f"/run/deepseek-recheck-job-{os.getpid()}-{unit[-16:]}")
    properties = [
        "MemoryHigh=" + cfg.memory_high(),
        "MemoryMax=" + cfg.memory_max(),
        "MemorySwapMax=128M",
        "CPUQuota=" + cfg.cpu_quota(),
        "TasksMax=128",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        # 关键：不是 read-only。模型必须看不到 /home/user 下的 .claude/.pi 凭据。
        "ProtectHome=yes",
        "InaccessiblePaths=-/opt/llm-gateway/data -/opt/docwatch-v2/env "
        "-/opt/deepseek-watch/env -/opt/deepseek-webwatch/env -/opt/deepseek-recheck/env "
        "-/opt/docwatch-v2/data -/opt/docwatch/data "
        "-/home/user/.pi -/home/user/.claude -/home/user/.claude.json -/home/user/.pi/agent "
        "-/var/lib/deepseek-recheck -/var/lib/deepseek-watch-push "
        "-/var/lib/deepseek-webwatch -/var/lib/deepseek-webwatch-ro -/var/lib/private",
        "PrivateDevices=yes",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictNamespaces=yes",
        "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "OOMScoreAdjust=200",
        # job 只读；只有 out/ 额外 bind 成可写。
        f"BindReadOnlyPaths={job_dir}:{visible_job}",
        f"BindPaths={out_dir}:{visible_job / 'out'}",
        # 整个 webwatch 观测数据目录只读挂到 /run；Claude 可以查全部历史版本、
        # 缓存和事件，但改不了；/var/lib/private 其它目录仍然不可见。
        "BindReadOnlyPaths=/var/lib/private/deepseek-webwatch:/run/recheck-data",
        "WorkingDirectory=" + str(visible_job),
        "RuntimeMaxSec=%ds" % (timeout + 15),
    ]
    if auth_root:
        properties.append("BindPaths=" + str(auth_root))

    argv = [
        "systemd-run", "--wait", "--pipe", "--quiet", "--collect", f"--unit={unit}",
        *[f"--property={item}" for item in properties],
        f"--uid={uid_name}",
        f"--gid={gid_name}",
    ]
    env = {
        "HOME": str(out_dir / "home"),
        "PATH": os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "Asia/Shanghai",
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RECHECK_WEBWATCH_DIR": "/run/recheck-data",
    }
    if auth_root:
        env["CLAUDE_CONFIG_DIR"] = str(auth_root)
    for key, value in env.items():
        argv.append(f"--setenv={key}={value}")

    claude = agent_path or cfg.agent_path()
    argv.extend(
        [
            claude,
            "--print",
            "--output-format", "json",
            "--model", model or cfg.agent_model(),
            "--max-turns", str(int(max_turns or cfg.agent_max_turns())),
            # 只给文件工具：没有 Bash/WebFetch/WebSearch/Task，agent 无法出网或派生子代理。
            "--tools", "Read,Write,Glob,Grep",
            "--allowedTools", "Read,Write,Glob,Grep",
            "--disallowedTools", "Bash,WebFetch,WebSearch,Task,NotebookEdit",
            # --disallowedTools 是可变参数，必须用 -- 结束选项，否则会把提示词当成工具名。
            "--",
            prompt,
        ]
    )
    return argv


def prepare_job_permissions(job_dir: Path) -> None:
    """worker 的 UMask 可能让 job 只有 root 可读；agent 是 pi，必须显式放开。"""
    job_dir = Path(job_dir)
    try:
        os.chmod(job_dir, 0o755)
    except OSError:
        return
    for path in job_dir.rglob("*"):
        relative = path.relative_to(job_dir)
        if relative.parts and relative.parts[0] == "out":
            continue
        try:
            if path.is_dir():
                os.chmod(path, 0o755)
            elif path.is_file():
                os.chmod(path, 0o644)
        except OSError:
            continue


def _ensure_out_home(out_dir: Path, uid: int | None = None, gid: int | None = None) -> None:
    """out/ 与 out/home 必须属于 pi，否则降权后的 agent 写不了 verdict。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    home = out_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    if uid is None or gid is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    for path in (out_dir, home):
        try:
            os.chown(path, uid, gid)
            os.chmod(path, 0o750)
        except OSError:
            pass


def _kill_process_group(proc: subprocess.Popen, unit: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "stop", f"{unit}.service"],
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=8)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class ClaudeRunner:
    """生产 runner：systemd-run 沙箱 + Claude Code。"""

    def __init__(self, *, auth_source: Path | None = None) -> None:
        self.auth_source = Path(auth_source) if auth_source else cfg.agent_src()

    def run(self, job_dir: Path, event: dict, *, timeout: int | None = None) -> dict:
        job_dir = Path(job_dir)
        out_dir = job_dir / "out"
        uid_gid = resolve_uid_gid(cfg.agent_user(), cfg.agent_group())
        uid, gid = uid_gid if uid_gid else (None, None)
        _ensure_out_home(out_dir, uid, gid)
        auth = prepare_agent_auth(
            self.auth_source,
            name_hint=job_dir.name,
            uid=uid,
            gid=gid,
        )
        unit = _unit_name(job_dir)
        started = time.monotonic()
        stdout_path = out_dir / "claude-output.json"
        stderr_path = out_dir / "claude-stderr.log"
        prompt = (
            "请阅读当前目录的 AGENT.md 和 CONTEXT.md，完成这条前端事件的复核。"
            "把结论写入 out/verdict.json（严格按 CONTEXT.md 第 6 节的结构），"
            "可选写 out/RESULT.md。写完立即停止。"
        )
        try:
            argv = build_sandbox_command(
                job_dir=job_dir,
                auth_root=Path(auth["root"]),
                prompt=prompt,
                timeout=timeout,
            )
            LOG.info("SANDBOX_CMD %s", " ".join(argv))
            with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
                proc = subprocess.Popen(
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    return_code = proc.wait(timeout=int(timeout or cfg.agent_timeout()))
                except subprocess.TimeoutExpired as exc:
                    _kill_process_group(proc, unit)
                    raise AgentTimeout(
                        f"agent 超过 {int(timeout or cfg.agent_timeout())} 秒"
                    ) from exc
        finally:
            cleanup_agent_auth(auth)
        duration = time.monotonic() - started
        LOG.info("SANDBOX_DONE return_code=%s duration_seconds=%.1f", return_code, duration)
        verdict = recheck_verdict.load_verdict(
            out_dir / "verdict.json",
            event=event,
            allowed_hosts=cfg.allowed_hosts(),
        )
        return {
            "verdict": verdict,
            "return_code": return_code,
            "duration_seconds": int(round(duration)),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        }


class FakeRunner:
    """离线测试 runner：不启动沙箱、不调用模型，按测试脚本写 verdict。"""

    def __init__(self, writer=None, *, sleep: float = 0, error: str = "") -> None:
        self._writer = writer
        self.sleep = sleep
        self.error = error
        self.calls: list[dict] = []

    def run(self, job_dir: Path, event: dict, *, timeout: int | None = None) -> dict:
        self.calls.append({"job_dir": Path(job_dir), "event": event, "timeout": timeout})
        if self.sleep:
            time.sleep(self.sleep)
        if self.error:
            raise AgentRunError(self.error)
        out_dir = Path(job_dir) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        if self._writer:
            value = self._writer(Path(job_dir), event)
        else:
            value = None
        if isinstance(value, dict):
            import json

            (out_dir / "verdict.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return {"verdict": None, "return_code": 0, "duration_seconds": 0, "stdout": "", "stderr": ""}
