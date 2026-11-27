"""自建网页 / 模型指纹监控入口。

用法：
    python -m webwatch.run                按调度巡检（systemd timer 使用）
    python -m webwatch.run --dry-run      强制全量抓取并打印，不写状态、不发邮件、不调付费 API
    python -m webwatch.run --no-api       跳过 API 指纹探测（网页和模型版本照常）
    python -m webwatch.run --force        忽略调度，强制全量巡检

调度：
    systemd 每 5 分钟唤醒一次；网页（Chat / Platform / Harness）与 DSH 软件更新
    （GitHub release / master 提交 / npm / 桌面端清单）每 15 分钟一次，
    模型目录（GET /models）每 5 分钟一次，模型指纹（价格页版本 + API system_fingerprint）
    每 30 分钟一次，DeepSeek 余额每 10 分钟采样一次。时间记录在 state.json 的 schedule
    字段，服务被手动启动时也不会重复抓取。

环境变量：
    WEBWATCH_STATE_DIR / WEBWATCH_EVENTS_DIR   状态与事件目录
    DEEPSEEK_API_KEY                           可选；配置后才会探测模型指纹
    WEBWATCH_AI                                默认 1；前端发版时用 LLM gateway 生成更新说明，设 0 关闭（不写模型/兜底文案，仍记录 quality/level_decision）
    WEBWATCH_LLM_MODEL                     可选；默认 example-model-b
    WEBWATCH_LLM_TIMEOUT                   可选；单次模型请求超时，默认 45 秒
    WEBWATCH_LLM_BUDGET                    可选；单条事件的判读总预算（含重试），默认 90 秒
    WEBWATCH_AI_ROUND_BUDGET                  可选；一轮巡检所有事件的 AI 总预算，默认 150 秒
    WEBWATCH_ROUND_BUDGET                     可选；抓取阶段总预算，默认 240 秒，到点停止抓取并落盘
    WEBWATCH_MAIL=1                            可选；配置 SMTP 后发送邮件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import config
from .ai import ai_enabled as ai_is_enabled, enrich_events
from .audit import audit_recent_events
from .balance import (
    THRESHOLD_YUAN,
    alert_wanted,
    mark_alerted,
    probe_balance,
    record_balance,
    record_balance_error,
)
from .dsh_updates import check_dsh_updates
from .events import iso, prune_events, write_event
from .fingerprint import (
    _source_fail,
    _source_ok,
    check_api,
    check_catalog,
    check_model_versions,
    check_platform_banner,
    check_targets,
    load_state,
    save_state,
)
from .mail import send_events, send_text
from .quality import gate_events, quality_summary
from .redact import redact
from .safe_fetch import set_round_deadline


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


PAGE_INTERVAL_SECONDS = 15 * 60
MODEL_CATALOG_INTERVAL_SECONDS = 5 * 60
FINGERPRINT_INTERVAL_SECONDS = 30 * 60
BALANCE_INTERVAL_SECONDS = 10 * 60
DUE_TOLERANCE_SECONDS = 90
FAILURE_ALERT_COOLDOWN_SECONDS = 6 * 3600
ROUND_BUDGET_SECONDS = 240


def _round_budget() -> float:
    raw = os.getenv("WEBWATCH_ROUND_BUDGET", "").strip()
    if not raw:
        return float(ROUND_BUDGET_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(ROUND_BUDGET_SECONDS)
    return max(30.0, min(900.0, value))


def _round_successes(sources: dict, now: datetime) -> list[str]:
    """本轮真正成功的来源；不能用历史 last_success，否则一次成功会永远掩盖后续全挂。

    同一轮里先成功后失败（价格页成功、API 全挂）时 fail_streak > 0，
    也不能算成功（P1-5）。
    """
    stamp = iso(now)
    return [
        name
        for name, entry in sources.items()
        if isinstance(entry, dict)
        and entry.get("last_checked") == stamp
        and entry.get("last_success") == stamp
        and int(entry.get("fail_streak") or 0) == 0
    ]


def _is_due(last_text: object, now: datetime, interval_seconds: int) -> bool:
    """判断某个检查是否到期；首次没有记录时视为到期。"""
    if not last_text:
        return True
    try:
        last = datetime.fromisoformat(str(last_text))
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return (now - last).total_seconds() >= interval_seconds - DUE_TOLERANCE_SECONDS


def _explain_api_skip(label: str, *, dry_run: bool, use_api: bool) -> None:
    """没有可用 API key 时只打一行原因；三条路径与旧的 if/elif/else 完全一致。"""
    if dry_run:
        print(f"[webwatch] dry-run：跳过 {label}")
    elif not use_api:
        print(f"[webwatch] --no-api：跳过 {label}")
    else:
        print(f"[webwatch] 未配置 DEEPSEEK_API_KEY，跳过 {label}")


def _discard_events(paths: list) -> None:
    """撤回本轮已写的事件：state 没推进时留着重试会生成重复记录。"""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _dsh_changed_at_map(events_dir: Path) -> dict[str, str]:
    """读取已有 DSH 发版事件的 changed_at，供完整更新日志回填时做时间下限。

    只为「同一 id 重发补全说明」服务；文件读失败不影响巡检，交由本轮正常事件覆盖。
    """
    mapping: dict[str, str] = {}
    try:
        paths = list(events_dir.glob("dsh-release-*.json"))
    except OSError:
        return mapping
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("kind") != "dsh_release":
            continue
        event_id = str(data.get("id") or "")
        changed_at = str(data.get("changed_at") or "")
        if event_id and changed_at:
            mapping[event_id] = changed_at
    return mapping


def _prepare_events_for_publish(
    events: list[dict],
    *,
    ai_enabled: bool | None = None,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    """AI 判读 + 统一质量闸门，run 与测试共用同一入口。

    P2-04：WEBWATCH_AI=0 时既不补 fallback 文案，也不 apply 任何 analysis；
    只让 gate 写 quality/level_decision（不生成 payload.analysis）。
    """
    if ai_enabled is None:
        ai_enabled = ai_is_enabled()
    if ai_enabled:
        events = enrich_events(events)
    else:
        print("[webwatch] WEBWATCH_AI=0：跳过 AI 判读与规则兜底文案")
    events, counts = gate_events(
        events,
        ai_enabled=ai_enabled,
        mark_audit_state=True,
        now=now,
    )
    return events, counts


def run(
    *,
    state_dir=None,
    events_dir=None,
    dry_run: bool = False,
    use_api: bool = True,
    force: bool = False,
) -> int:
    _configure_stdout()
    # 抓取阶段到点就停，保证后面的 write_event / save_state / 邮件还能跑完；
    # 否则 systemd 在 300s 时 SIGTERM，整轮事件和基线推进都会丢。
    set_round_deadline(_round_budget())
    state_dir = state_dir or config.DEFAULT_STATE_DIR
    events_dir = events_dir or config.DEFAULT_EVENTS_DIR
    if not dry_run:
        try:
            # 即使本轮没有新事件也创建事件目录；否则 watchsite 会把「从未有事件」
            # 误判成事件目录缺失/不可读。
            events_dir.mkdir(parents=True, exist_ok=True)
            # UMask=0077 下 mkdir 得到 0700，推送用户 deepseek-watch 读不到。
            # write_event 只在真正写事件时才 chmod 0750，全新安装/目录重建后的空目录会漏。
            try:
                os.chmod(events_dir, 0o750)
            except OSError:
                pass
        except OSError as exc:  # noqa: BLE001
            print(
                f"[webwatch] 无法创建事件目录：{type(exc).__name__}: {redact(exc)}",
                file=sys.stderr,
            )
            return 1
    ai_on = ai_is_enabled()
    if not dry_run:
        audit_counts = audit_recent_events(events_dir, ai_enabled=ai_on)
        if audit_counts.get("repaired") or audit_counts.get("flagged"):
            print(
                f"[webwatch] 自动复盘：修复 {audit_counts.get('repaired', 0)} 条，"
                f"标记待核实 {audit_counts.get('flagged', 0)} 条"
            )
    now = datetime.now().astimezone()
    state = load_state(state_dir)
    schedule = state.get("schedule")
    if not isinstance(schedule, dict):
        schedule = {}
        state["schedule"] = schedule

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip() if use_api and not dry_run else ""

    do_pages = force or _is_due(schedule.get("pages"), now, PAGE_INTERVAL_SECONDS)
    do_catalog = force or _is_due(
        schedule.get("catalog"), now, MODEL_CATALOG_INTERVAL_SECONDS
    )
    do_fingerprint = force or _is_due(
        schedule.get("fingerprint"), now, FINGERPRINT_INTERVAL_SECONDS
    )
    do_balance = force or _is_due(schedule.get("balance"), now, BALANCE_INTERVAL_SECONDS)
    if not do_pages and not do_catalog and not do_fingerprint and not do_balance:
        print("[webwatch] 未到检查时间，跳过本轮")
        return 0

    state["last_run"] = now.isoformat()
    events: list[dict] = []
    balance_alert: dict | None = None

    if do_balance:
        if api_key:
            try:
                currency, amount = probe_balance(api_key)
                summary = record_balance(state, state_dir, now, currency, amount)
                schedule["balance"] = now.isoformat()
                print(
                    f"[webwatch] DeepSeek 余额：{currency} {amount:.4f}，"
                    f"今日消耗 {summary['today_spend']:.4f} 元"
                    f"（开始监控后 {summary['today_spend_since_watch']:.4f} 元）"
                )
                if alert_wanted(summary, state):
                    balance_alert = summary
            except Exception as exc:  # noqa: BLE001 - 余额失败不能拖垮页面巡检
                record_balance_error(state, now, exc)
                schedule["balance"] = now.isoformat()
                print(f"[webwatch] DeepSeek 余额采样失败：{redact(exc)}", file=sys.stderr)
        else:
            _explain_api_skip("DeepSeek 余额采样", dry_run=dry_run, use_api=use_api)

    if do_pages:
        # DSH 更新检查与页面巡检共用 15 分钟节奏；先跑它，避免页面大包抓取
        # 用完本轮预算后外部源来不及检查。它和 harness 页面共用来源健康度。
        dsh_events, dsh_ok, dsh_error = check_dsh_updates(
            state,
            now,
            known_changed_at=None if dry_run else _dsh_changed_at_map(events_dir),
        )
        events.extend(dsh_events)
        # platform 来源由页面 + 公告两个子检查组成，公告检查统一记录最终健康度；
        # harness 同理，由页面 + DSH 更新两个子检查汇总，页面成功不能先洗掉另一路失败。
        events.extend(
            check_targets(
                state,
                now,
                defer_platform_ok=True,
                defer_harness_ok=True,
                state_dir=None if dry_run else state_dir,
            )
        )
        events.extend(check_platform_banner(state, now))
        if not dsh_ok:
            # 同一轮 Harness 页面也失败时，check_targets 已经把页面错误写进
            # last_error；这里追加 DSH 错误而不是覆盖，否则状态页只剩一半原因。
            detail = dsh_error
            sources_now = state.get("sources")
            harness_now = (
                sources_now.get("harness")
                if isinstance(sources_now, dict)
                else None
            )
            if (
                isinstance(harness_now, dict)
                and harness_now.get("last_checked") == iso(now)
                and harness_now.get("last_error")
            ):
                page_error = str(harness_now.get("last_error"))
                if page_error and page_error not in detail:
                    detail = f"{page_error}；DSH：{detail}"
            _source_fail(state, "harness", now, detail)
        else:
            _source_ok(state, "harness", now)
        schedule["pages"] = now.isoformat()

    if do_fingerprint:
        # 配置 API key 时，fingerprint 来源由价格页 + API 两个子检查组成，
        # 由 check_api 统一记录最终健康度。check_api 里的 /models 查询同时
        # 满足本轮的模型目录刷新，避免在同一轮重复请求。
        events.extend(check_model_versions(state, now, defer_source_ok=bool(api_key)))
        if api_key:
            events.extend(check_api(state, now, api_key, dry_run=dry_run))
        else:
            _explain_api_skip("API 模型指纹探测", dry_run=dry_run, use_api=use_api)
        # 本轮即使没有 key、没查目录，也把目录调度一起推进，避免每 5 分钟重复打印跳过。
        schedule["catalog"] = now.isoformat()
        schedule["fingerprint"] = now.isoformat()
    elif do_catalog:
        # /models 是免费接口，独立于 30 分钟的指纹探测高频刷新。P2-19：失败会写
        # fingerprint 来源健康度并打印脱敏错误；成功仍不清空来源健康度，避免把
        # 指纹子检查的连续失败洗掉。
        if api_key:
            events.extend(check_catalog(state, now, api_key, dry_run=dry_run))
        else:
            _explain_api_skip("模型目录查询", dry_run=dry_run, use_api=use_api)
        schedule["catalog"] = now.isoformat()

    if events and not dry_run:
        # 前端更新事件在落盘前补「普通用户可读的更新说明」；失败只影响文案，不影响事件。
        # WEBWATCH_AI=0 时由 _prepare_events_for_publish 一并跳过 fallback/apply（P2-04）。
        events, quality_counts = _prepare_events_for_publish(events, ai_enabled=ai_on, now=now)
        if quality_counts:
            print(f"[webwatch] 质量闸门：{quality_summary(quality_counts)}")

    # 记录全局健康度：只看本轮是否还有来源成功，历史成功不能掩盖本轮全挂。
    # 只有余额采样或模型目录刷新到期、页面/指纹都没到期时，不拿旧来源判断全局失败。
    health_checked = do_pages or do_fingerprint
    sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    round_stamp = iso(now)
    # 只统计本轮真正检查过的来源；余额轮、或指纹未到期的轮次，
    # 不能把没检查的来源算进「本轮 x/y 成功」的分母。
    checked_names = {
        name for name, entry in sources.items()
        if isinstance(entry, dict)
        and (entry.get("last_checked") == round_stamp
             or entry.get("last_success") == round_stamp)
    }
    success_sources: list[str] = []
    failure_alert = ""
    if health_checked:
        success_sources = _round_successes(sources, now)
        state["last_ok"] = max(
            (str(entry.get("last_success") or "") for entry in sources.values() if isinstance(entry, dict)),
            default="",
        )
        state["fail_streak"] = 0 if success_sources else 1
        state["last_error"] = "" if success_sources else "本轮所有监控来源都失败了"

        if not dry_run and not success_sources:
            now_ts = now.timestamp()
            last_alert = float(state.get("last_failure_alert") or 0)
            if now_ts - last_alert >= FAILURE_ALERT_COOLDOWN_SECONDS:
                details = [
                    f"- {name}: {redact(entry.get('last_error') or '失败')}"
                    for name, entry in sources.items()
                    if isinstance(entry, dict) and name in checked_names
                    and int(entry.get("fail_streak") or 0) > 0
                ]
                body = "自建监控这一轮没有任何来源成功。\n\n" + "\n".join(details[:10])
                if send_text("[DeepSeek 观测站] 自建监控全部失败", body):
                    state["last_failure_alert"] = now_ts
                    failure_alert = "已发"
                else:
                    failure_alert = "未发（检查 WEBWATCH_MAIL/SMTP 配置）"
            else:
                failure_alert = "冷却中"
        elif success_sources and not dry_run:
            state["last_failure_alert"] = 0

    if not dry_run:
        # P0-2：先把本轮事件全部写盘并 fsync，全部成功后才推进 state 基线。
        # 写事件失败时直接返回非零，不保存 state，下一轮会重新检测同一变化。
        written: list = []
        try:
            for event in events:
                written.append(write_event(events_dir, event))
        except Exception as exc:  # noqa: BLE001
            _discard_events(written)
            print(
                f"[webwatch] 写事件失败，本轮不保存状态：{type(exc).__name__}: {redact(exc)}",
                file=sys.stderr,
            )
            return 1
        try:
            save_state(state_dir, state)
        except Exception as exc:  # noqa: BLE001
            # state 没推进时，本轮事件也不能留：否则下一轮会把同一个变化
            # 再生成一条新 id，站点出现重复记录。
            _discard_events(written)
            print(
                f"[webwatch] 保存状态失败，已撤回本轮事件：{type(exc).__name__}: {redact(exc)}",
                file=sys.stderr,
            )
            return 1
        removed = prune_events(events_dir, config.MAX_EVENTS_KEPT)
        if removed:
            print(f"[webwatch] 清理旧事件 {removed} 条")

    if balance_alert is not None and not dry_run:
        body = (
            f"DeepSeek 官方账户余额采样发现异常消耗。\n\n"
            f"日期：{balance_alert['today']}（北京时间）\n"
            f"开始监控后今日消耗：{balance_alert['today_spend_since_watch']:.4f} 元\n"
            f"今日全部消耗：{balance_alert['today_spend']:.4f} 元\n"
            f"最新余额：{balance_alert['currency']} {balance_alert['amount']:.4f}\n"
            f"告警线：{THRESHOLD_YUAN:.2f} 元/天\n\n"
            "项目自身 30 分钟一次的指纹探测正常约 0.001 元/天。超过 0.01 元可能是真实调用增加，"
            "也可能是赠送额度到期、退款冲正或币种切换；请先核对调用记录，不要直接断定 key 被盗。"
        )
        if send_text("[DeepSeek 观测站] DeepSeek 余额消耗超过 0.01 元/天", body):
            mark_alerted(state, balance_alert)
            try:
                save_state(state_dir, state)
            except Exception as exc:  # noqa: BLE001
                print(f"[webwatch] 余额告警状态保存失败：{redact(exc)}", file=sys.stderr)
            print(
                f"[webwatch] 余额告警邮件已发：今日开始监控后 "
                f"{balance_alert['today_spend_since_watch']:.4f} 元",
                file=sys.stderr,
            )
        else:
            print("[webwatch] 余额告警邮件未发（检查 WEBWATCH_MAIL/SMTP 配置）", file=sys.stderr)

    if health_checked:
        print(
            f"[webwatch] 巡检完成：{len(events)} 条新事件，"
            f"本轮 {len(success_sources)}/"
            f"{max(len(checked_names), len(success_sources))} 个来源成功"
        )
    else:
        print("[webwatch] 本轮只采样 DeepSeek 余额（页面/指纹未到期）")
    for event in events:
        print(f"  - {event['id']} [{event['level_label']}] {event['title']}")
    for name, entry in sources.items():
        if isinstance(entry, dict) and int(entry.get("fail_streak") or 0) > 0:
            print(
                f"[webwatch] {name} 连续失败 {entry['fail_streak']} 次："
                f"{redact(entry.get('last_error') or '')}",
                file=sys.stderr,
            )

    if events and not dry_run:
        ok, failed = send_events(events)
        if ok or failed:
            print(f"[webwatch] 邮件：成功 {ok} 封，失败 {failed} 封")
    if failure_alert:
        print(f"[webwatch] 全量失败告警邮件：{failure_alert}", file=sys.stderr)

    # 只看页面/指纹轮次的健康度；只有余额采样的一轮不算全量失败。
    return 0 if not health_checked or success_sources else 1


def _acquire_run_lock(state_dir) -> object | None:
    """跨进程互斥：手动 --force 与 timer 同时跑会重复写事件、重复付费探测。

    用操作系统文件锁而不是「存在即锁定」的锁文件：进程被杀后锁会自动释放，
    不会留下需要人工清理的死锁文件。拿不到锁返回 None。
    """
    try:
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handle = open(directory / ".run.lock", "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            # msvcrt 不能锁空文件的零长度区域，先保证文件至少 1 字节。
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _release_run_lock(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 自建网页 / 模型指纹监控")
    parser.add_argument(
        "--dry-run", action="store_true", help="强制抓取打印，不写状态、不调付费 API"
    )
    parser.add_argument("--no-api", action="store_true", help="跳过 DEEPSEEK_API_KEY 探测")
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略调度，强制全量巡检（会立即调用官方付费 API 探测模型指纹）",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        return run(
            dry_run=True,
            use_api=not args.no_api,
            force=args.force or args.dry_run,
        )
    # 非 dry-run 才加锁：dry-run 不写状态、不调付费 API，并发无副作用。
    lock = _acquire_run_lock(config.DEFAULT_STATE_DIR)
    if lock is None:
        print(
            "[webwatch] 另一轮巡检正在运行（或状态目录不可写），本轮跳过",
            file=sys.stderr,
        )
        return 0
    try:
        return run(use_api=not args.no_api, force=args.force)
    finally:
        _release_run_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
