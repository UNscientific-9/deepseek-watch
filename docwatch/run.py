#!/usr/bin/env python3
"""docwatch v2 入口：采集 → 规则判级 → 唤醒 pi → 排版 → 发信。

分工（CONTRACT §3.3 / §7.5）：
    采集与判级 = 纯脚本（collect + rules.py），决定「发不发、几级」；
    pi agent   = 只负责「把变化讲成人话」；
    发信       = 本文件（HTML + 纯文本，夜间静默合并到 07:00）。

用法：
    python3 -m docwatch.run                    # 正常巡检（systemd timer 调这个）
    python3 -m docwatch.run --dry-run          # 只跑采集+规则+排版，不发信不叫模型
    python3 -m docwatch.run --no-llm           # 不发模型，用规则兜底文案
    python3 -m docwatch.run --now              # 忽略夜间静默，立刻发
    python3 -m docwatch.run --flush            # 只把静默期积压的发掉
    python3 -m docwatch.run --data-dir /tmp/x --limit 3   # 本地调试
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import sqlite3
import ssl
import smtplib
import subprocess
import sys
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

from docwatch import rules
from docwatch.collect.signals import run_once
from docwatch.render.mail import (attach_cards as _mail_attach_cards, render,
                                  render_with_status)

# ---------------------------------------------------------------- 配置

QUIET_START = 23
QUIET_END = 7
HEARTBEAT_HOUR = 7         # 每日心跳最早 07:00 发（静默 23:00–07:00 一结束就发）；错过/失败则当天后续轮次补发
HEALTH_FAIL_STREAK = 3     # 连续失败几次算「监控瞎了」
# 外层硬上限必须大于 wake.py 内部预算，否则慢轮次会被外层先掐死、verdict.json 来不及落盘。
WAKE_TIMEOUT = 780         # 外层硬上限（wake.py budget 720 + 60s 收尾余量）
WAKE_BUDGET = WAKE_TIMEOUT - 60
WAKE_PI_TIMEOUT = 300      # 单次 pi 调用上限（传给 wake.py --timeout）
COLLECT_ALERT_COOLDOWN_H = 24
LOG_MAX_BYTES = 1_000_000
DEFAULT_DATA_DIR = "/opt/docwatch/data"

# 夜间静默 / 每日心跳都按北京时间判断，不能依赖服务器的 /etc/localtime
# （云服务器常是 UTC，否则静默窗口会整体偏 8 小时）。
BEIJING = dt.timezone(dt.timedelta(hours=8))


# ---------------------------------------------------------------- 小工具


def _now() -> dt.datetime:
    """取北京时间的墙钟值，与服务器本地时区无关。

    返回 naive datetime：日志、health-last 标记、数据库里的字符串格式
    全部沿用原有写法，只是「现在是几点」改成按北京时间算。
    """
    return dt.datetime.now(BEIJING).replace(tzinfo=None)


def log(msg: str) -> None:
    stamp = _now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {msg}", flush=True)


def load_envfile(path: Path) -> None:
    """读 KEY=VALUE 文件；已存在的环境变量优先。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


def append_log(data_dir: Path, msg: str) -> None:
    """落到 run.log，超过上限就截断重建（只留最近一半）。"""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        p = data_dir / "run.log"
        line = f"{_now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        if p.exists() and p.stat().st_size > LOG_MAX_BYTES:
            tail = p.read_text(encoding="utf-8", errors="replace")[-LOG_MAX_BYTES // 2:]
            p.write_text(tail, encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def is_quiet(now: dt.datetime | None = None) -> bool:
    hour = (now or _now()).hour
    return hour >= QUIET_START or hour < QUIET_END


# ---------------------------------------------------------------- 发信


def send_mail(subject: str, text: str, html: str) -> bool:
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "").strip()
    if not user or not password:
        log("[notify] 缺 SMTP_USER / SMTP_PASS，跳过发信")
        return False
    host = os.getenv("SMTP_HOST", "").strip() or "smtp.qq.com"
    to = os.getenv("MAIL_TO", "").strip() or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=30,
                             context=ssl.create_default_context()) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        log(f"[notify] 发送失败：{type(exc).__name__}: {exc}")
        return False
    log(f"[notify] 已发送：{subject}")
    return True


# ---------------------------------------------------------------- 组卡（喂给排版层）


def attach_cards(verdict: dict, job: dict) -> dict:
    """按页面分组生成卡片（唯一实现已收拢到 `render.mail.attach_cards`）。

    watchsite/collect.py 也会从这里导入；保留这个入口，避免它退回简版分组。
    组卡实现、diff 提取与页脚依据统一在 mail 侧，normal / 夜间 / 补发共用一套。
    """
    return _mail_attach_cards(verdict, job, job.get("evidence_dir") or "")


# ---------------------------------------------------------------- 唤醒 pi


def _wake_cmd(job_dir: Path, data_arg: Path, level: int, review: bool,
              out_path: Path, timeout: float = WAKE_PI_TIMEOUT,
              budget: float = WAKE_BUDGET) -> list[str]:
    """构造 wake.py 命令：超时/预算由 run.py 显式给，避免两边各用默认值互相打架。"""
    cmd = [sys.executable, "-m", "docwatch.wake",
           "--job", str(job_dir),
           "--data-dir", str(data_arg),
           "--rule-level", str(level),
           "--out", str(out_path),
           "--json", "--cleanup",
           "--timeout", str(int(timeout)),
           "--budget", str(int(budget))]
    if review:
        cmd.append("--review")
    return cmd


def _kill_process_group(proc: subprocess.Popen) -> None:
    """杀掉 wake.py 及其同组子进程（pi / systemd-run 客户端），不留孤儿。"""
    terminate = getattr(signal, "SIGTERM", None)
    kill = getattr(signal, "SIGKILL", None)
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), terminate)
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except OSError:
                pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=20)
        return
    except Exception:  # noqa: BLE001
        pass
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), kill)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            proc.kill()
        except OSError:
            pass


def _run_process_group(cmd: list[str], out_file, err_file, timeout: float,
                       cwd: str) -> int:
    """跑子进程并让它自成一个进程组；超时时连同孙进程一起清理。"""
    proc = subprocess.Popen(cmd, stdout=out_file, stderr=err_file, cwd=cwd,
                            start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        raise


def wake(job: dict, level: int, review: bool, timeout: int = WAKE_TIMEOUT,
         budget: int = WAKE_BUDGET, pi_timeout: int = WAKE_PI_TIMEOUT,
         data_dir: Path | None = None) -> dict:
    """调 docwatch.wake 拿 Verdict；失败用规则兜底文案。

    timeout=外层进程组硬上限；pi_timeout=单次 pi 调用上限（传给 wake.py --timeout）；
    budget=wake.py 内部总预算（外层留 60s 收尾余量）。
    """
    job_dir = Path(job.get("evidence_dir") or "")
    if not job_dir.is_dir():
        log(f"[wake] 找不到证据目录 {job_dir}，跳过模型")
        return _fallback_verdict(level, job, "证据目录缺失")
    # wake.py 默认数据目录是 v1 的 /opt/docwatch/data；必须显式传 v2 的目录，
    # 否则 agent 的 status/pages/brief/history/search/diff 会去查错库。
    data_arg = Path(data_dir) if data_dir else job_dir.parent.parent
    out_path = job_dir / "verdict.json"

    cmd = _wake_cmd(job_dir, data_arg, level, review, out_path,
                    timeout=pi_timeout, budget=budget)
    started = time.monotonic()
    # 用文件重定向而不是 PIPE：沙箱里 CreatePipe 会被拒绝，文件方式两种环境都能跑
    out_file = job_dir / "wake.stdout.log"
    err_file = job_dir / "wake.stderr.log"
    try:
        with out_file.open("w", encoding="utf-8") as fo, err_file.open("w", encoding="utf-8") as fe:
            rc = _run_process_group(cmd, fo, fe, timeout,
                                    cwd=str(Path(__file__).resolve().parent.parent))
        if rc != 0:
            log(f"[wake] wake.py 退出码 {rc}")
        verdict = _read_verdict(job_dir)
    except subprocess.TimeoutExpired:
        log(f"[wake] 超时 {timeout}s，用规则兜底（进程组已清理）")
        verdict = _read_verdict(job_dir)
    except Exception as exc:  # noqa: BLE001
        log(f"[wake] 调用异常：{type(exc).__name__}: {exc}")
        verdict = None

    verdict = verdict or _fallback_verdict(level, job, "模型无输出")
    verdict["level"] = level                 # 铁律：级别只认规则
    verdict["level_source"] = "rule"
    verdict.setdefault("noise", level == 0)
    verdict.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    return verdict


def _read_verdict(job_dir: Path) -> dict | None:
    p = Path(job_dir) / "verdict.json"
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _fallback_verdict(level: int, job: dict, why: str) -> dict:
    """模型不可用时的兜底：只讲事实，不编影响。"""
    signals = job.get("signals") or []
    pages = sorted({s.get("page") for s in signals if s.get("page")})
    detail = "；".join(str(s.get("detail") or "") for s in signals[:3] if s.get("detail"))
    return {
        "level": level,
        "level_source": "rule",
        "noise": level == 0,
        "headline": f"检测到 {len(signals)} 处变化" if signals else "检测到变化",
        "what_changed": detail or f"{len(pages)} 个页面有变化",
        "why_matters": "这次没有拿到自动解读（模型不可用），先按原文差异给你；影响暂未确认。",
        "action": "打开原始页面核对变化。",
        "evidence": (job.get("rule_reasons") or [])[:3],
        "model": "规则兜底",
        "model_note": why,
        "degraded": True,
    }


# ---------------------------------------------------------------- 每日心跳


def _stats_24h(data_dir: Path) -> dict:
    """从库里取过去 24 小时的巡检统计（轮数 / 变化 / 信号数）。"""
    out = {"runs": 0, "jobs_with_signals": 0, "signals": 0}
    db = data_dir / "docs.db"
    if not db.is_file():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        since = (_now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        row = con.execute(
            "select count(*), "
            "coalesce(sum(case when n_signals > 0 then 1 else 0 end),0), "
            "coalesce(sum(n_signals),0) "
            "from v2_jobs where created_at >= ?", (since,)).fetchone()
        if row:
            out.update(runs=int(row[0] or 0), jobs_with_signals=int(row[1] or 0),
                       signals=int(row[2] or 0))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _heartbeat_marker(data_dir: Path, now: dt.datetime | None = None) -> Path:
    day = (now or _now()).strftime("%Y-%m-%d")
    return data_dir / f"heartbeat-{day}.flag"


def _mark_heartbeat(data_dir: Path) -> None:
    """今天已经有一条邮件证明监控活着（心跳/变化/告警都算）。"""
    try:
        _heartbeat_marker(data_dir).write_text("ok", encoding="utf-8")
    except OSError:
        pass


def send_heartbeat(data_dir: Path, job: dict, force: bool = False) -> bool:
    """每天 08:00 后第一条「我还活着」；没变化也发，让人知道监控没停。

    08:00 那一轮失败/错过时，后面任意一轮都会补发；发送成功才写当日标记。
    当天已经发过别的邮件（变化/告警/夜间合并）也视为心跳，避免重复发。
    """
    now = _now()
    marker = _heartbeat_marker(data_dir, now)
    if marker.exists() and not force:
        return False
    if not force and (is_quiet(now) or now.hour < HEARTBEAT_HOUR):
        return False

    st = _stats_24h(data_dir)
    pages = job.get("pages_total") or 58
    assets = job.get("assets_total") or 24
    dur = job.get("duration_ms") or 0
    wire = job.get("wire_bytes") or 0

    if job.get("collect_failed"):
        head = "本轮采集失败，监控进程仍在运行"
        note = "这不是正常巡检结果；采集失败告警已经单独发送（或正在队列重试），请以那条为准。"
    elif st["jobs_with_signals"]:
        head = "过去 24 小时有 %d 处变化（%d 轮触发）" % (st["signals"], st["jobs_with_signals"])
        note = "变化已经单独发信，这条只是每日汇总。"
    else:
        head = "过去 24 小时一切正常，没有检测到变化"
        note = "这条是每日心跳：没变化也会发一条，让你知道监控没停。"

    rows = [
        ("巡检轮数", "%d 轮（每 15 分钟一轮）" % st["runs"]),
        ("监控范围", "%d 个页面 + %d 个前端资源" % (pages, assets)),
        ("最近一轮", "%d ms · %d 字节线流量" % (dur, wire)),
        ("当前档位", "高敏（任何变化都发）"),
    ]
    body_rows = "".join(
        '<tr><td style="padding:6px 0;font-size:15px;color:#6b7280;width:110px;">%s</td>'
        '<td style="padding:6px 0;font-size:15px;color:#1f2328;">%s</td></tr>' % (k, v)
        for k, v in rows)
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#f4f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"'
        ' style="background:#f4f5f7;"><tr><td align="center" style="padding:16px 8px;">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"'
        ' style="width:100%;max-width:640px;background:#ffffff;border:1px solid #e2e5e9;'
        'border-radius:12px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,'
        'sans-serif;color:#1f2328;line-height:1.65;">'
        '<tr><td style="padding:20px 22px 8px;">'
        '<span style="display:inline-block;background:#2b6cb0;color:#ffffff;font-size:15px;'
        'font-weight:700;padding:3px 11px;border-radius:5px;">每日心跳</span>'
        '<p style="margin:12px 0 0;font-size:20px;font-weight:700;color:#111827;">' + head + '</p>'
        '<p style="margin:8px 0 0;font-size:15px;color:#6b7280;">' + note + '</p>'
        '</td></tr><tr><td style="padding:8px 22px 18px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        + body_rows + '</table></td></tr>'
        '<tr><td style="padding:14px 22px 18px;border-top:1px solid #e8eaed;background:#fbfbfc;'
        'border-radius:0 0 12px 12px;">'
        '<p style="margin:0;font-size:15px;color:#8a8f98;">判读：规则 + AI（有变化时才叫模型）</p>'
        '<p style="margin:4px 0 0;font-size:15px;color:#8a8f98;">'
        '觉得太吵？跟 DSH 说一句「调安静点」。</p></td></tr></table></td></tr></table></body></html>')

    text = "\n".join([
        "【每日心跳】" + head,
        "",
        note,
        "",
        "\n".join("%s：%s" % (k, v) for k, v in rows),
        "",
        "判读：规则 + AI（有变化时才叫模型）",
        "觉得太吵？跟 DSH 说一句「调安静点」。",
    ])
    ok = send_mail("[DeepSeek 监控] " + head, text, html)
    if ok:
        _mark_heartbeat(data_dir)
    append_log(data_dir, "heartbeat sent=%s" % ok)
    return ok


# ---------------------------------------------------------------- 健康检查


def _fail_streaks(data_dir: Path) -> list[tuple[str, str, int, str]]:
    """读库里连续失败计数，返回 [(类别, url, 次数, 错误)]。"""
    db = data_dir / "docs.db"
    if not db.is_file():
        return []
    out: list[tuple[str, str, int, str]] = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        for table, label in (("v2_pages", "页面"), ("v2_assets", "资源")):
            try:
                rows = con.execute(
                    # 不截断：告警正文必须列出所有仍活跃的失败地址，
                    # 否则排在前面的旧地址会把真问题挤出列表。
                    f"select url, fail_streak, coalesce(last_error,'') from {table} "
                    f"where fail_streak >= ? and active = 1 "
                    f"order by fail_streak desc", (HEALTH_FAIL_STREAK,)
                ).fetchall()
            except sqlite3.Error:
                continue
            out += [(label, url, int(streak or 0), err or "") for url, streak, err in rows]
    finally:
        con.close()
    return out


def _read_marker_time(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_marker_time(path: Path, now: dt.datetime | None = None) -> None:
    try:
        path.write_text((now or _now()).isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


def _queued_alert_exists(data_dir: Path, alert: str) -> bool:
    """队列里已有同类告警时不再重复入队（SMTP 一直失败也不会堆几十封）。"""
    for path in queue_dir(data_dir).glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(item, dict) and item.get("alert") == alert:
            return True
    return False


def _collect_fail_streak_path(data_dir: Path) -> Path:
    return data_dir / "collect-fail-streak"


def _bump_collect_fail_streak(data_dir: Path) -> int:
    path = _collect_fail_streak_path(data_dir)
    try:
        streak = int(path.read_text(encoding="utf-8").strip() or "0") + 1
    except (OSError, ValueError):
        streak = 1
    try:
        path.write_text(str(streak), encoding="utf-8")
    except OSError:
        pass
    return streak


def _reset_collect_fail_streak(data_dir: Path) -> None:
    try:
        _collect_fail_streak_path(data_dir).unlink(missing_ok=True)
    except OSError:
        pass


def send_collect_failure_alert(data_dir: Path, exc: BaseException) -> bool:
    """采集整体失败：不受夜间静默限制，独立冷却，触发心跳。

    run_once 抛异常说明这一轮**什么都没检查**，用户看到的「没变化」其实完全不可信；
    这类告警必须和普通变化邮件分开，不能因为静默或健康检查冷却而被吞掉。
    """
    streak = _bump_collect_fail_streak(data_dir)
    now = _now()
    marker = data_dir / "collect-fail-last"
    last = _read_marker_time(marker)
    if last and (now - last).total_seconds() < COLLECT_ALERT_COOLDOWN_H * 3600:
        log(f"[health] 采集连续失败第 {streak} 次，告警在 {COLLECT_ALERT_COOLDOWN_H}h 冷却内")
        append_log(data_dir, f"collect-failed streak={streak} (alert cooldown)")
        return False

    detail = f"{type(exc).__name__}: {exc}"
    verdict = {
        "level": rules.LEVEL_IMPORTANT,
        "level_source": "rule",
        "noise": False,
        "headline": "监控采集失败，本轮没有检查任何页面",
        "what_changed": f"巡检程序在采集阶段就退出了（连续第 {streak} 次）：{detail[:300]}",
        "why_matters": "这一轮没有产生任何结果，用户看到的「没有变化」并不代表官方站点没变；"
                       "如果连续发生，监控等于停摆。",
        "action": "不用你动手，我先排查数据库/磁盘/网络；恢复前请把这段时间当成监控盲区。",
        "evidence": [f"采集异常：{detail[:300]}"],
        "model": "规则兜底",
        "model_note": "这条由监控自身发出，不是 DeepSeek 文档变化。",
    }
    job = {
        "id": 0, "created_at": now.isoformat(timespec="seconds"), "mode": "high",
        "pages_total": 0, "pages_changed": 0, "assets_total": 0,
        "signals": [{"kind": "fetch_error", "page": "docs.db",
                     "detail": f"采集失败（连续 {streak} 次）：{detail[:200]}"}],
    }
    out = render(verdict, job)
    ok = send_mail("【监控告警】" + out["subject"], out["text"], out["html"])
    queued = False
    if not ok:
        queued = hold(data_dir, verdict, job, out=out, retry=True, alert="collect")
    if ok or queued:
        _write_marker_time(marker, now)
    if ok:
        _mark_heartbeat(data_dir)
    append_log(data_dir, f"collect-alert sent={ok} queued={queued} streak={streak}")
    return ok or queued


def _check_health(data_dir: Path, job: dict) -> None:
    """连续抓取失败 / sitemap 失败 → 发一封「监控可能瞎了」，24 小时内最多一次，失败会重试。"""
    problems = _fail_streaks(data_dir)
    sitemap_error = str(job.get("sitemap_error") or "").strip()
    if not problems and not sitemap_error:
        return
    now = _now()
    last = _read_marker_time(data_dir / "health-last")
    if last and (now - last).total_seconds() < 24 * 3600:
        return
    if _queued_alert_exists(data_dir, "health"):
        return                      # 上一轮已入队但还没发出去，别重复堆邮件

    level = rules.LEVEL_NOTABLE
    lines = [f"{label}连续失败 {streak} 次：{url}（{err[:60]}）"
             for label, url, streak, err in problems]
    if sitemap_error:
        lines.insert(0, f"sitemap：{sitemap_error[:200]}")
    if problems:
        headline = f"有 {len(problems)} 个地址连续抓取失败，监控可能不准了"
    else:
        headline = "sitemap 抓取失败，新页面/下线页面可能漏报"
    verdict = {
        "level": level,
        "level_source": "rule",
        "noise": False,
        "headline": headline,
        "why_matters": "这些页面/资源拿不到内容，等于这段时间的监控是睁眼瞎；"
                       "如果是官方改版或封了我们的请求，需要处理。",
        "action": "不用你动手，我先看一眼；连续失败会自动重试。",
        "evidence": lines,
        "model": "规则兜底",
        "model_note": "这条由监控自身发出，不是 DeepSeek 文档变化。",
    }
    alert_job = dict(job)
    alert_job["signals"] = [{"kind": "fetch_error", "page": url, "detail": f"连续失败 {streak} 次"}
                            for _, url, streak, _ in problems]
    if sitemap_error:
        alert_job["signals"].append(
            {"kind": "fetch_error", "page": "sitemap", "detail": sitemap_error[:200]})
    out = render(verdict, alert_job)
    # 监控自身的故障告警不受夜间静默限制（README 约定）：直接发，
    # 发不出去时进 retry 队列（flush 会逐封原样重发），不掺进夜间合并信。
    ok = send_mail("【监控告警】" + out["subject"], out["text"], out["html"])
    queued = False
    if ok:
        # 只有真的发出去才算「已处理」；入队不算，否则 SMTP 抖动一次会假装健康 24h。
        _write_marker_time(data_dir / "health-last")
        _mark_heartbeat(data_dir)
    else:
        queued = hold(data_dir, verdict, alert_job, out=out, retry=True, alert="health")
    append_log(data_dir, f"health-alert sent={ok} queued={queued}")


# ---------------------------------------------------------------- 静默期队列


def queue_dir(data_dir: Path) -> Path:
    p = data_dir / "queue"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_queue_item(data_dir: Path, item: dict, kind: str) -> Path | None:
    """原子写一个队列文件；失败返回 None。"""
    job = item.get("job") or {}
    # 纳秒时间戳：同一轮里健康告警和变化邮件可能在同一毫秒入队，毫秒会撞名覆盖。
    path = queue_dir(data_dir) / (
        f"{time.time_ns()}-{os.getpid()}-{job.get('id', 0)}-{kind}.json")
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)          # 原子替换：坏在半路也不会留下半个 JSON
    except OSError as exc:
        log(f"[queue] 写入失败：{exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    log(f"[queue] 已积压 {path.name}")
    return path


def hold(data_dir: Path, verdict: dict, job: dict,
         out: dict | None = None, retry: bool = False, alert: str = "") -> bool:
    """把一封待发邮件落盘。out=已渲染好的 subject/text/html（重试用）；
    retry=True 表示这是「白天发送失败、下轮立刻重试」，不会被合并进夜间摘要。
    alert 标记告警类型（health/collect），用于去重与送达后补健康标记。
    """
    item = {
        "held_at": _now().isoformat(timespec="seconds"),
        "job_id": job.get("id", 0),
        "verdict": verdict,
        "job": {k: job.get(k) for k in
                ("id", "created_at", "mode", "pages_total", "pages_changed",
                 "assets_total", "signals", "duration_ms")},
    }
    if out is not None:
        item["out"] = {"subject": out.get("subject", ""),
                       "text": out.get("text", ""),
                       "html": out.get("html", "")}
    if retry:
        item["retry"] = True
    if alert:
        item["alert"] = alert
    return _write_queue_item(data_dir, item, "retry" if retry else "hold") is not None


def stage_pending(data_dir: Path, verdict: dict, job: dict, out: dict,
                  night: bool = False) -> Path | None:
    """run_once 返回后立刻写的「未通知」凭据（write-ahead）。

    它只含渲染兜底邮件所需的最小字段；进程随后被杀，下一轮 flush 也能凭它先发出去。
    夜里创建的记为夜间项（不立刻发），白天创建的记为 retry（下一轮先重试）。
    """
    item = {
        "held_at": _now().isoformat(timespec="seconds"),
        "pending": True,
        "job_id": job.get("id", 0),
        "verdict": verdict,
        "job": {k: job.get(k) for k in
                ("id", "created_at", "mode", "pages_total", "pages_changed",
                 "assets_total", "signals", "duration_ms")},
        "out": {"subject": out.get("subject", ""),
                "text": out.get("text", ""),
                "html": out.get("html", "")},
    }
    if not night:
        item["retry"] = True
    return _write_queue_item(data_dir, item, "pending")


def _send_queued_out(item: dict) -> bool:
    out = item.get("out") or {}
    if not out.get("subject") or not out.get("text"):
        return False
    return send_mail(out["subject"], out["text"], out.get("html", ""))


def _mark_job_notified(data_dir: Path, job_id) -> None:
    """把 v2_jobs.notified 置 1：发送/入队成功后，恢复扫描不再补发这条。"""
    try:
        jid = int(job_id or 0)
    except (TypeError, ValueError):
        return
    if jid <= 0:
        return
    db = data_dir / "docs.db"
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(db, timeout=5)
    except sqlite3.Error:
        return
    try:
        try:  # 老库补列：已有行默认视为已通知，避免把历史作业全部补发一遍
            con.execute("ALTER TABLE v2_jobs ADD COLUMN notified INTEGER DEFAULT 1")
        except sqlite3.Error:
            pass
        con.execute("UPDATE v2_jobs SET notified = 1 WHERE id = ?", (jid,))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()


def _mark_job_attention(data_dir: Path, job_id, reason: str) -> None:
    """把无法自动恢复的作业标成 notified=2（待人工处理），避免每轮重复扫描。"""
    try:
        jid = int(job_id or 0)
    except (TypeError, ValueError):
        return
    if jid <= 0:
        return
    db = data_dir / "docs.db"
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(db, timeout=5)
    except sqlite3.Error:
        return
    try:
        try:
            con.execute("ALTER TABLE v2_jobs ADD COLUMN notified INTEGER DEFAULT 1")
        except sqlite3.Error:
            pass
        con.execute("UPDATE v2_jobs SET notified = 2 WHERE id = ?", (jid,))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()
    append_log(data_dir, f"recover-incomplete job={jid} reason={reason}")


def _queued_job_ids(data_dir: Path) -> set[int]:
    ids: set[int] = set()
    for path in queue_dir(data_dir).glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(item, dict):
            continue
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        jid = item.get("job_id") or job.get("id")
        try:
            if jid:
                ids.add(int(jid))
        except (TypeError, ValueError):
            pass
    return ids


def _digest_cards(verdict: dict, job: dict) -> list[dict]:
    """夜间合并：保留每条 verdict 的 headline/what_changed/why_matters/action。"""
    verdict = verdict if isinstance(verdict, dict) else {}
    job = job if isinstance(job, dict) else {}
    raw = verdict.get("cards")
    cards = [dict(card) for card in raw if isinstance(card, dict)] if isinstance(raw, list) else []
    if not cards:
        sig = (job.get("signals") or [{}])[0]
        ev = verdict.get("evidence")
        bullets = [str(x) for x in ev if x][:4] if isinstance(ev, list) else []
        cards = [{
            "level": int(verdict.get("level") or 0),
            "page": sig.get("page") or sig.get("asset") or "",
            "bullets": bullets,
        }]

    # 整次变化的解读挂在第一张真实卡片上；其余页卡继续保留自己的 bullets/diff。
    target = next((card for card in cards if not card.get("noise")), None)
    if target is None:
        # 全是噪音卡时也要让模型解读露面：补一张摘要卡，别只留「前端重建」一行。
        target = {"level": int(verdict.get("level") or 0)}
        cards.insert(0, target)
    headline = str(verdict.get("headline") or "").strip()
    why = str(verdict.get("why_matters") or "").strip()
    what = str(verdict.get("what_changed") or "").strip()
    action = str(verdict.get("action") or "").strip()
    if headline and not target.get("headline"):
        target["headline"] = headline
    if why and not target.get("why_matters"):
        target["why_matters"] = why
    if action and not target.get("action"):
        target["action"] = action
    if what and not target.get("what_changed"):
        target["what_changed"] = what
    return cards


def flush(data_dir: Path, include_night: bool = True) -> int:
    """把待发队列发掉：重试项逐封原样重发，夜间积压合并成一封；返回发送封数。

    `include_night=False` 用于静默期：只补发白天失败的 retry 项（含健康/采集告警），
    夜间积压继续留着，等 07:00 后再合并。
    """
    qdir = queue_dir(data_dir)
    retry_items: list[tuple[Path, dict]] = []
    night_items: list[tuple[Path, dict]] = []
    for path in sorted(qdir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            try:
                path.rename(path.with_name(path.name + ".bad"))
            except OSError:
                path.unlink(missing_ok=True)
            continue
        if not isinstance(item, dict):
            continue
        if item.get("retry") and item.get("out"):
            retry_items.append((path, item))
        else:
            night_items.append((path, item))

    sent = 0
    # 白天发送失败的那封：原样重发，不和其他积压混在一起，保住卡片/证据/标题。
    for path, item in retry_items:
        if _send_queued_out(item):
            path.unlink(missing_ok=True)
            sent += 1
            _mark_heartbeat(data_dir)
            if item.get("alert") == "health":
                _write_marker_time(data_dir / "health-last")
            _mark_job_notified(data_dir, item.get("job_id") or (item.get("job") or {}).get("id"))
            log(f"[queue] 重试成功：{path.name}")
        else:
            log(f"[queue] 重试仍失败，留到下轮：{path.name}")

    if not night_items:
        return sent
    if not include_night:
        log(f"[queue] 静默期保留 {len(night_items)} 条夜间积压，07:00 后合并")
        return sent

    cards, levels, notes, models = [], [], [], []
    for path, it in night_items:
        v = it.get("verdict") if isinstance(it.get("verdict"), dict) else {}
        j = it.get("job") if isinstance(it.get("job"), dict) else {}
        if not v.get("degraded"):
            model = str(v.get("model") or "").strip()
            if model and model != "规则兜底" and model not in models:
                models.append(model)
        cards.extend(_digest_cards(v, j))
        levels.append(int(v.get("level") or 0))
        if v.get("model_note"):
            notes.append(str(v.get("model_note")))

    top = max(levels + [int(c.get("level") or 0) for c in cards]) if cards else 0
    digest_job = {
        "id": (night_items[-1][1].get("job") or {}).get("id", 0),
        "created_at": _now().isoformat(timespec="seconds"),
        "mode": "high",
        "pages_total": max((int((it.get("job") or {}).get("pages_total") or 0)
                            for _, it in night_items), default=0),
        "pages_changed": sum(int((it.get("job") or {}).get("pages_changed") or 0)
                             for _, it in night_items),
        "signals": [s for _, it in night_items
                    for s in ((it.get("job") or {}).get("signals") or [])],
    }
    digest = {
        "level": top,
        "noise": top == 0,
        "headline": f"夜间共 {len(night_items)} 条更新（最高 {rules.LEVEL_LABEL.get(top, '轻微')}）",
        "why_matters": "夜间 23:00–07:00 的更新合并在这一封里，逐条看下面的卡片。",
        "action": "扫一眼卡片标题即可，需要细看再点原文。",
        "evidence": [f"合并 {len(night_items)} 条夜间告警"],
        "cards": cards,
        "model": " / ".join(models) if models else "规则兜底",
    }
    if notes:
        digest["model_note"] = "；".join(notes)
    out, fallback_used = render_with_status(digest, digest_job)
    if fallback_used:
        # 兜底信丢掉了每一卡的解读；不能发出去、更不能删队列，下一轮继续试。
        log("[queue] 夜间合并排版走进兜底，保留队列等下一轮")
        append_log(data_dir, "night-digest render fallback; queue kept")
        return sent
    if send_mail(out["subject"], out["text"], out["html"]):
        for path, it in night_items:
            path.unlink(missing_ok=True)
            _mark_job_notified(data_dir, it.get("job_id") or (it.get("job") or {}).get("id"))
        sent += 1
        _mark_heartbeat(data_dir)
    return sent


def recover_unnotified_jobs(data_dir: Path, limit: int = 20) -> int:
    """启动补偿：把 v2_jobs 里「有信号但没通知过」的作业补发/入队。

    run_once 提交快照与「发送邮件」之间被杀掉时，变化只留在 v2_jobs 里；下一轮 diff
    不会再产同一条信号，只能靠这里补。只处理带 job_json 的完整作业，最多 limit 条。
    """
    db = data_dir / "docs.db"
    if not db.is_file():
        return 0
    try:
        con = sqlite3.connect(db, timeout=5)
    except sqlite3.Error:
        return 0
    recovered = 0
    try:
        try:
            con.execute("ALTER TABLE v2_jobs ADD COLUMN notified INTEGER DEFAULT 1")
        except sqlite3.Error:
            pass
        try:
            # 旧库先迁移：新列默认 0，只把迁移前的老作业置 1；新作业由 insert_job 显式写 0。
            last_id = int(con.execute(
                "SELECT COALESCE(MAX(id), 0) FROM v2_jobs").fetchone()[0] or 0)
            con.execute("ALTER TABLE v2_jobs ADD COLUMN ready INTEGER DEFAULT 0")
            con.execute("UPDATE v2_jobs SET ready = 1 WHERE id <= ?", (last_id,))
        except sqlite3.Error:
            pass
        try:
            con.commit()   # DDL 在 SQLite 里也是事务，不 commit 会在 close 时回滚
        except sqlite3.Error:
            pass
        try:
            rows = con.execute(
                "SELECT id, job_json FROM v2_jobs "
                "WHERE baseline = 0 AND n_signals > 0 AND notified = 0 "
                "AND COALESCE(ready, 1) = 1 "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            return 0
    finally:
        con.close()

    queued_ids = _queued_job_ids(data_dir)
    for jid, raw in rows:
        try:
            jid = int(jid)
        except (TypeError, ValueError):
            continue
        if jid in queued_ids:
            continue
        try:
            job = json.loads(raw or "{}")
        except (TypeError, ValueError):
            job = {}
        if not isinstance(job, dict) or not job.get("signals"):
            # 空 job_json 说明作业在写证据包/更新 job 前被硬中断；不能当作
            # 「已通知」丢掉，否则用户既收不到邮件，站点也不会出现这条变化。
            _mark_job_attention(data_dir, jid, "empty or incomplete job_json")
            log(f"[recover] 作业 #{jid} 缺少可补发内容，已标记待人工处理")
            continue
        job["id"] = jid
        job.setdefault("mode", "high")
        decided = rules.decide(job)
        level = int(decided["level"])
        job["rule_reasons"] = decided["reasons"]
        if not rules.should_send(level, str(job.get("mode") or "high")):
            _mark_job_notified(data_dir, jid)
            continue
        verdict = _fallback_verdict(level, job, "进程中断，补发未通知的变化")
        out = render(verdict, job)
        ok = False
        queued = False
        if is_quiet():
            queued = hold(data_dir, verdict, job, out=out)
        else:
            ok = send_mail(out["subject"], out["text"], out["html"])
            if not ok:
                queued = hold(data_dir, verdict, job, out=out, retry=True)
        if ok or queued:
            _mark_job_notified(data_dir, jid)
            queued_ids.add(jid)
            recovered += 1
            log(f"[recover] 补发未通知作业 #{jid} sent={ok} queued={queued}")
    if recovered:
        append_log(data_dir, f"recover-unnotified count={recovered}")
    return recovered


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="docwatch v2：前端资源监控 + agent 判读")
    ap.add_argument("--data-dir", default=os.getenv("DOCWATCH_DATA_DIR", DEFAULT_DATA_DIR))
    ap.add_argument("--env-file", default="")
    ap.add_argument("--mode", default="", choices=["", "high", "normal", "quiet"])
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 页（调试）")
    ap.add_argument("--no-llm", action="store_true", help="不叫模型，用规则兜底文案")
    ap.add_argument("--dry-run", action="store_true", help="不发信、不叫模型")
    ap.add_argument("--now", action="store_true", help="忽略夜间静默")
    ap.add_argument("--flush", action="store_true", help="只处理静默期积压")
    ap.add_argument("--test-notify", action="store_true", help="用假数据发一封测试信")
    ap.add_argument("--heartbeat", action="store_true", help="立刻发一条每日心跳（自检用）")
    ap.add_argument("--label", default="", help="邮件标题前缀（演练用，例如 【演练】）")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    load_envfile(Path(args.env_file or data_dir.parent / "env"))
    load_envfile(Path.cwd() / ".env")

    # 空 = 沿用 v2_meta 里持久化的档位；只有显式 --mode / DOCWATCH_MODE
    # 才覆盖并持久化，避免一次 --mode quiet 被下一轮 timer 又改回 high。
    mode = args.mode or os.getenv("DOCWATCH_MODE", "").strip()

    if args.dry_run and (args.heartbeat or args.test_notify or args.flush):
        log("[dry-run] --dry-run 不能和 --heartbeat / --test-notify / --flush 同时使用")
        return 2

    if args.heartbeat:
        ok = send_heartbeat(data_dir, {"pages_total": 58, "assets_total": 24}, force=True)
        log("[heartbeat] sent=%s" % ok)
        return 0

    if args.test_notify:
        return _test_notify(data_dir)

    if args.flush:
        n = flush(data_dir)
        log(f"[flush] 发出 {n} 封")
        return 0

    # --dry-run 必须在任何 flush/采集之前短路：否则会真的发掉夜间积压，
    # 而且 run_once 会把快照写进库，预览完这轮变化下一轮就没了。
    if args.dry_run:
        return _dry_run(data_dir, mode, args)

    # 启动补偿：先补发上次进程中断时「快照已提交、通知没发出」的作业，
    # 再把队列里的重试项发掉（静默期只发 retry，夜间积压留到 07:00）。
    try:
        recover_unnotified_jobs(data_dir)
    except Exception as exc:  # noqa: BLE001
        log(f"[recover] 补发检查失败：{type(exc).__name__}: {exc}")
    if any(queue_dir(data_dir).glob("*.json")):
        try:
            flush(data_dir, include_night=args.now or not is_quiet())
        except Exception as exc:  # noqa: BLE001
            log(f"[queue] flush 失败：{type(exc).__name__}: {exc}")

    started = time.monotonic()
    try:
        job = run_once(data_dir, mode=mode, limit=args.limit or None)
    except Exception as exc:  # noqa: BLE001
        log(f"[collect] 采集失败：{type(exc).__name__}: {exc}")
        append_log(data_dir, f"collect-failed {type(exc).__name__}: {exc}")
        try:
            send_collect_failure_alert(data_dir, exc)
        except Exception as alert_exc:  # noqa: BLE001
            log(f"[health] 采集告警发送失败：{type(alert_exc).__name__}: {alert_exc}")
        try:
            send_heartbeat(data_dir, {"pages_total": 0, "assets_total": 0,
                                      "collect_failed": True})
        except Exception:  # noqa: BLE001
            pass
        return 1

    dur = job.get("duration_ms") or int((time.monotonic() - started) * 1000)
    log(f"[collect] {job.get('pages_total')} 页 / {job.get('assets_total')} 资源 "
        f"/ {dur} ms / {job.get('wire_bytes', 0)} B 线流量 / 信号 {len(job.get('signals') or [])}")

    if job.get("baseline"):
        # 基线也可能带着 sitemap/页面抓取失败进来；健康告警不能因为「首次建库」被跳过。
        try:
            _check_health(data_dir, job)
        except Exception as exc:  # noqa: BLE001
            log(f"[health] 基线健康检查失败：{type(exc).__name__}: {exc}")
        _mark_job_notified(data_dir, job.get("id"))
        log("[baseline] 首次基线已建立，本轮不发信")
        append_log(data_dir, "baseline-created")
        return 0
    _reset_collect_fail_streak(data_dir)

    signals = job.get("signals") or []
    decided = rules.decide(job) if signals else {"level": 0, "reasons": []}
    level = int(decided["level"])
    job["rule_reasons"] = decided["reasons"]
    run_mode = str(job.get("mode") or mode or "high")
    should_send = bool(signals) and rules.should_send(level, run_mode)

    # write-ahead：run_once 已提交快照，先落一个「最小可发」的占位队列项；
    # 进程在模型/排版阶段被杀时，下一轮靠它或 v2_jobs 补偿，不会永久静默丢这一条。
    pending_path: Path | None = None
    if should_send:
        try:
            pending_verdict = _fallback_verdict(level, job, "等待模型解读（进程中断时的兜底占位）")
            pending_out = render(pending_verdict, job)
            pending_path = stage_pending(
                data_dir, pending_verdict, job, pending_out,
                night=is_quiet() and not args.now)
        except Exception as exc:  # noqa: BLE001
            log(f"[queue] pending 写入失败：{type(exc).__name__}: {exc}")

    def _drop_pending() -> None:
        nonlocal pending_path
        if pending_path is not None:
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            pending_path = None

    # 抓取连续失败是「监控本身瞎了」，和有变化不冲突；每轮都检查，不再只挂在无信号分支。
    # 健康检查自己出错不能挡住下面的变化通知（快照已经更新，这封不发就真丢了）。
    try:
        _check_health(data_dir, job)
    except Exception as exc:  # noqa: BLE001
        log(f"[health] 检查失败：{type(exc).__name__}: {exc}")

    if not signals:
        _mark_job_notified(data_dir, job.get("id"))
        log("[diff] 没有检测到变化")
        send_heartbeat(data_dir, job)
        return 0

    log(f"[rules] 级别 {level}（{rules.LEVEL_LABEL.get(level, '?')}）：{'; '.join(decided['reasons'][:3])}")

    if not should_send:
        _mark_job_notified(data_dir, job.get("id"))
        log(f"[rules] 档位 {run_mode} 不发送该级别，仅记录")
        append_log(data_dir, f"skip level={level} mode={run_mode} signals={len(signals)}")
        send_heartbeat(data_dir, job)
        return 0

    verdict = (_fallback_verdict(level, job, "指定不发模型") if args.no_llm
               else wake(job, level, review=level >= 2, data_dir=data_dir))
    verdict = attach_cards(verdict, job)
    if args.label:
        note = str(verdict.get("model_note") or "").strip()
        verdict["model_note"] = (args.label + " 这条由人为构造的变化触发，只为演示真实邮件长什么样；"
                                 "官方文档没有任何改动。" + (" " + note if note else ""))
    out, render_fallback = render_with_status(verdict, job)
    if render_fallback:
        # 兜底信丢掉了这一封的真实解读，不能当作已处理：保留 pending 队列、
        # 不标 notified，交给下一轮 recover/flush 重试（P2-27）。
        log("[mail] 排版走进兜底，保留 pending 队列下一轮重试")
        append_log(data_dir, f"render-fallback job={job.get('id')} kept-pending")
        return 0
    if args.label:
        out["subject"] = args.label + out["subject"]
    log(f"[mail] {out['subject']}")

    if is_quiet() and not args.now:
        queued = hold(data_dir, verdict, job, out=out)
        if queued:
            _drop_pending()
            _mark_job_notified(data_dir, job.get("id"))
        append_log(data_dir, f"held queued={queued} level={level} job={job.get('id')}")
        return 0

    ok = send_mail(out["subject"], out["text"], out["html"])
    queued = False
    if ok:
        _mark_heartbeat(data_dir)          # 今天已经有邮件证明监控活着，不再另发心跳
        _drop_pending()
        _mark_job_notified(data_dir, job.get("id"))
    else:
        # 快照已在 run_once 里更新，下一轮不会再产这条信号；必须把这封原样存下来重试。
        queued = hold(data_dir, verdict, job, out=out, retry=True)
        if queued:
            _drop_pending()
            _mark_job_notified(data_dir, job.get("id"))
    append_log(data_dir, f"sent={ok} queued={queued} level={level} job={job.get('id')} "
                          f"signals={len(signals)} dur={dur}ms")
    send_heartbeat(data_dir, job)
    return 0 if (ok or queued) else 1


def _dry_run(data_dir: Path, mode: str, args) -> int:
    """只读预览：复制一份库到临时目录再跑，绝不动真实快照/队列，也绝不发信。"""
    tmp = Path(tempfile.mkdtemp(prefix="docwatch-dry-"))
    try:
        src = data_dir / "docs.db"
        if src.is_file():
            # 用 SQLite 自己的 backup API 做一致性副本：直接拷贝在定时任务写入时可能拿到半个库。
            src_con = dst_con = None
            try:
                src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
                dst_con = sqlite3.connect(tmp / "docs.db")
                src_con.backup(dst_con)
            except sqlite3.Error as exc:
                log(f"[dry-run] 复制快照库失败：{exc}")
                return 1
            finally:
                if dst_con is not None:
                    dst_con.close()
                if src_con is not None:
                    src_con.close()
        started = time.monotonic()
        try:
            job = run_once(tmp, mode=mode, limit=args.limit or None)
        except Exception as exc:  # noqa: BLE001
            log(f"[dry-run] 采集失败：{type(exc).__name__}: {exc}")
            return 1
        dur = job.get("duration_ms") or int((time.monotonic() - started) * 1000)
        log(f"[dry-run] {job.get('pages_total')} 页 / {job.get('assets_total')} 资源 "
            f"/ {dur} ms / 线流量 {job.get('wire_bytes', 0)} B "
            f"/ 信号 {len(job.get('signals') or [])}")
        if job.get("baseline"):
            log("[dry-run] 首次基线，本轮没有可预览的变化")
            return 0
        signals = job.get("signals") or []
        if not signals:
            log("[dry-run] 没有检测到变化")
            return 0
        decided = rules.decide(job)
        level = int(decided["level"])
        job["rule_reasons"] = decided["reasons"]
        run_mode = str(job.get("mode") or mode or "high")
        log(f"[dry-run] 级别 {level}（{rules.LEVEL_LABEL.get(level, '?')}）："
            f"{'; '.join(decided['reasons'][:3])}")
        if not rules.should_send(level, run_mode):
            log(f"[dry-run] 档位 {run_mode} 不会发送该级别")
            return 0
        verdict = attach_cards(
            {"level": level, "headline": f"{len(signals)} 处变化",
             "why_matters": "；".join(decided["reasons"][:2])}, job)
        if args.label:
            verdict["model_note"] = (args.label + " 这条由人为构造的变化触发，"
                                     "只为演示真实邮件长什么样；官方文档没有任何改动。")
        preview = render(verdict, job)
        if args.label:
            preview["subject"] = args.label + preview["subject"]
        log(f"[dry-run] 标题：{preview['subject']}")
        print(preview["text"])
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_notify(data_dir: Path) -> int:
    """--test-notify：不抓站、不叫模型，用假数据验证发信链路。"""
    job = {
        "id": 0, "created_at": _now().isoformat(timespec="seconds"), "mode": "high",
        "pages_total": 58, "pages_changed": 1, "assets_total": 24,
        "signals": [{"kind": "page_text",
                     "page": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                     "detail": "价格行 13.5 → 15",
                     "before": "13.50 元/百万 tokens", "after": "15.00 元/百万 tokens"}],
    }
    verdict = {
        "level": 3, "noise": False,
        "headline": "测试：输出价 13.5 → 15 元/百万 tokens",
        "why_matters": "这是一封连通性测试信，忽略即可。",
        "action": "无需操作。",
        "evidence": ["测试数据"],
        "model": "规则兜底",
        "degraded": True,
    }
    out = render(verdict, job)
    ok = send_mail("[测试] " + out["subject"], out["text"], out["html"])
    log(f"[test-notify] {'成功' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
