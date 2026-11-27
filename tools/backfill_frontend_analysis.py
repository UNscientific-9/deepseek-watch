#!/usr/bin/env python3
"""回填历史「前端资源更新」事件的解析与解读。

与线上自动链路完全一致：旧/新主包 → frontend_evidence.build_evidence
→ ai.analyze_event（本机 LLM gateway、免费模型）→ apply_analysis
→ quality.gate_events（统一 Decision 判级，P1-06/B-10）。

线上 webwatch 每次真发版会自动走这条链路；本脚本用于两类场景：
  1. 功能上线前产生的历史事件（当时没存证据包）；
  2. 需要重新解析某条事件。

用法（在仓库根执行）：
  python tools/backfill_frontend_analysis.py \
      --event-json .tmp/web-chat.json \
      --old-url https://fe-static.deepseek.com/chat/static/main.9eec11d937.js \
      --new-url https://fe-static.deepseek.com/chat/static/main.ada2c4f5c4.js \
      --label "Chat 网页" --source chat \
      --old-commit 85af6250 --new-commit ef8707be \
      --out .tmp/chat-backfill.json

输出 JSON 含：evidence（证据包）、analysis（九字段）、event（回填后的 title / summary /
impact / action / payload），供人工核对后再写入 D1。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webwatch import ai, frontend_evidence, quality  # noqa: E402
from webwatch.config import LEVEL_LABELS  # noqa: E402
from webwatch.events import parse_iso, slugify  # noqa: E402

USER_AGENT = "DeepSeekWatch/1.0 (+https://deepseek-watch.pages.dev)"


def fetch_body(url: str, cache_dir: Path) -> str:
    """下载并按 frontend_evidence 的缓存格式落盘，后续 build_evidence 直接命中缓存。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    frontend_evidence._write_cache(cache_dir, url, body)  # noqa: SLF001 - 复用同一缓存格式
    return body.decode("utf-8", "replace")


def snapshot(url: str, main_text: str) -> dict:
    name = url.rsplit("/", 1)[-1]
    maps = frontend_evidence.parse_rspack_maps(main_text) if main_text else {"js": None, "css": None}
    manifest = [
        {
            "key": "js:entry:" + frontend_evidence._logical_name(url),  # noqa: SLF001
            "url": url,
            "kind": "js",
            "role": "entry",
            "name": name,
            "hash": frontend_evidence._filename_hash(url),  # noqa: SLF001
        }
    ]
    # 与线上 build_frontend_snapshot 一致：把运行时映射里的分片也放进清单，
    # 否则回填时只能看到主包，新增/删除分片里的新文案会再次漏掉。
    for ext in ("js", "css"):
        manifest.extend(frontend_evidence._chunk_urls(url, maps.get(ext), ext))  # noqa: SLF001
    seen: set[str] = set()
    deduped = []
    for item in manifest:
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        deduped.append(item)
    return {
        "engine": "rspack" if any(maps.values()) else "html",
        "manifest": deduped,
        "main_js_url": url,
        "main_css_url": "",
        "css_constant": str((maps.get("css") or {}).get("constant") or ""),
        "build_meta": frontend_evidence.extract_build_meta(main_text) if main_text else {},
    }


def finalize_event(
    event: dict,
    evidence: dict,
    analysis: dict,
    *,
    source: str = "",
    level: int | None = None,
) -> dict:
    """把证据包和解读写回事件，并走与线上完全一致的 quality.gate_events。

    P1-06：回填不能自己拍级别：
      - kind 缺失默认 page_change（显式别的 kind 直接报错，避免绕过闸门）；
      - level 用 `is None` 判断，0 是合法级别，不能被 `or 3` 抬成 3；
      - 旧 quality / level_decision 先清掉，再由 gate 重算；
      - changes_detail 先按规则级别生成，gate 会把分组级别改成最终级别。
    """
    kind = str(event.get("kind") or "").strip() or "page_change"
    if kind != "page_change":
        raise ValueError(f"只支持 page_change 事件，收到 kind={kind!r}")
    event["kind"] = kind
    rule_level = 2 if level is None else max(0, min(3, int(level)))
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    payload.pop("quality", None)
    payload.pop("level_decision", None)
    changes_detail = frontend_evidence.build_changes_detail(evidence, rule_level)
    payload["changes"] = changes_detail
    payload["frontend"] = {
        "old_commit": evidence.get("old_commit"),
        "new_commit": evidence.get("new_commit"),
        "old_build": evidence.get("old_build"),
        "new_build": evidence.get("new_build"),
        "main_js": evidence.get("main_js"),
        "main_css": evidence.get("main_css"),
        "totals": evidence.get("totals"),
        "i18n": evidence.get("i18n"),
        "chunk_changes": evidence.get("chunk_changes"),
        "changed_code_snippets": evidence.get("changed_code_snippets"),
        "css_rule_changes": evidence.get("css_rule_changes"),
    }
    event["payload"] = payload
    event["changes_detail"] = changes_detail
    event["source"] = source or str(event.get("source") or "")
    event["level"] = rule_level
    event["level_label"] = LEVEL_LABELS[rule_level]
    ai.apply_analysis(event, analysis)
    # 回填已经拿到分析文本，闸门按「AI 已开启」判断；P2-04 的开关只约束线上 run。
    quality.gate_events([event], ai_enabled=True)
    # 回填产物是人工核对后的终稿，不再进入自动审计队列。
    quality.mark_quality_state(event, "final")
    return event


def main() -> int:
    parser = argparse.ArgumentParser(description="回填历史前端资源更新事件的解析与解读")
    parser.add_argument("--event-json", required=True, help="线上事件 JSON（webwatch 事件文件）")
    parser.add_argument("--old-url", required=True)
    parser.add_argument("--new-url", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--old-commit", default="")
    parser.add_argument("--new-commit", default="")
    parser.add_argument("--cache-dir", default=".tmp/frontend-cache")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    event_path = Path(args.event_json)
    event = json.loads(event_path.read_text(encoding="utf-8"))
    if not str(event.get("id") or "").strip():
        print("事件缺少 id，无法回填", file=sys.stderr)
        return 2
    kind = str(event.get("kind") or "").strip() or "page_change"
    if kind != "page_change":
        print(f"只支持 page_change 事件，收到 kind={kind!r}", file=sys.stderr)
        return 2
    event["kind"] = kind
    label = args.label or event.get("source") or ""
    source = args.source or str(event.get("source") or "")

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] 下载新旧主包：{args.old_commit or '?'} → {args.new_commit or '?'}", file=sys.stderr)
    old_text = fetch_body(args.old_url, cache_dir)
    new_text = fetch_body(args.new_url, cache_dir)

    old_snap = snapshot(args.old_url, old_text)
    new_snap = snapshot(args.new_url, new_text)

    print("[2/3] 提取证据包", file=sys.stderr)
    evidence = frontend_evidence.build_evidence(
        old_snap,
        new_snap,
        cache_dir=cache_dir,
        source=source,
        label=label,
        old_commit=args.old_commit,
        new_commit=args.new_commit,
    )
    if not evidence:
        print("没有可比对的证据（两版入口相同或文件不可比）", file=sys.stderr)
        return 2

    print("[3/3] 调用模型生成解读（本机 LLM gateway，免费优先）", file=sys.stderr)
    analysis = ai.analyze_event(event, evidence)
    if not analysis:
        analysis = ai.fallback_analysis(event, evidence, reason="LLM gateway 判读未生成")

    # P1-06：level 用 is None 判断；0 是合法级别，不能被 `or 3` 抬成 3。
    raw_level = event.get("level")
    level = 2 if raw_level is None else max(0, min(3, int(raw_level)))
    try:
        finalize_event(event, evidence, analysis, source=source, level=level)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # 补齐推送/ingest 必需字段，保证输出可以原样 POST /internal/ingest 或放进
    # webwatch 事件目录，而不是只能人工抄字段。
    event["slug"] = str(event.get("slug") or "").strip() or slugify(f"{source}-{event['id']}", "web-event")
    if not event.get("changed_ts"):
        parsed = parse_iso(event.get("changed_at"))
        event["changed_ts"] = int(parsed.timestamp()) if parsed else int(time.time())
    event["detected_ts"] = int(event.get("detected_ts") or time.time())
    event["signal_count"] = max(1, int(event.get("signal_count") or 1))

    result = {
        "event_id": event.get("id"),
        "evidence": evidence,
        "analysis": analysis,
        # 完整事件：id/slug/source/level/changed_ts/changes_detail 都在里面。
        "event": event,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
