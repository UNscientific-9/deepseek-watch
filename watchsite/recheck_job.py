"""构建复核 job 目录：脱敏、精简、确定性、5MB 上限。

agent 只读 job，worker 只写 job；`out/` 是 agent 唯一可写目录。
同一事件 + 同一版本仓库重复生成时，除 `out/` 外所有文件字节一致。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from . import recheck_config as cfg

# 顶层身份字段：复核只读，不改。
TOP_LEVEL_FIELDS = (
    "schema_version", "id", "slug", "source", "kind", "level", "level_label",
    "title", "summary", "impact", "action", "signal_count", "changed_at",
    "page_url", "archive_url", "evidence",
)
# payload 里允许进入 job 的部分；level_decision / recheck / regrade / quality_state
# 等内部记录一律不投喂，避免泄密和判读锚定。
PAYLOAD_FIELDS = (
    "frontend", "changes", "commit_id", "level_rule", "what_changed", "analysis",
    "degraded", "evidence_unavailable", "frontend_health", "backfill",
)
ANALYSIS_FIELDS = (
    "headline", "tldr", "tldr_points", "overview", "sections", "visible_changes",
    "impact", "action", "evidence_gaps", "model_note", "degraded",
)
FRONTEND_FIELDS = (
    "truncated", "truncation_reasons", "baseline_missing", "totals", "chunk_changes", "css_rule_changes",
    "i18n", "fetched_bytes", "source", "label", "engine", "old_commit", "new_commit",
    "old_build", "new_build", "main_js", "main_css", "api_added", "api_removed",
    "cjk_added", "cjk_removed", "upper_added", "css_vars_added",
    "changed_code_snippets", "removed_code_snippets", "removed_i18n_refs",
    "added_i18n_refs", "file_diffs", "changed_files", "added_files",
    "removed_files", "snippets",
)
SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|"
    r"session|private[_-]?key|auth[_-]?token)",
    re.I,
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(access_token=)[^&\s\"']+", re.I),
    re.compile(r"([?&](?:api[_-]?key|key|token|secret|password)=)[^&\s\"']+", re.I),
    re.compile(r"(?<=//)[^/@\s]{0,300}@"),
)

LIST_CAPS = {
    "api_added": 30,
    "api_removed": 30,
    "cjk_added": 30,
    "cjk_removed": 30,
    "upper_added": 30,
    "css_vars_added": 30,
    "truncation_reasons": 8,
    "changed_code_snippets": 6,
    "removed_code_snippets": 6,
    "removed_i18n_refs": 20,
    "added_i18n_refs": 20,
    "file_diffs": 8,
    "changed_files": 40,
    "added_files": 20,
    "removed_files": 20,
    "snippets": 12,
}
MAX_CHANGES_DIFF_CHARS = 400_000
# 复核证据只放在 job 目录里；任务完成后 recheck.cleanup_job_evidence() 会删除
# 这些副本，所以可以把预算放宽到能装下完整的关键文件。2026-11-25 的真实案例：
# 1.18MB 的 Platform JS 被旧 400KB 上限截断，导致 Claude 只能给出 partial。
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_BLOB_FILE_BYTES = 8 * 1024 * 1024
MAX_RECENT_EVENTS = 10


class JobError(RuntimeError):
    pass


def _scrub_text(value: str) -> str:
    text = str(value)
    for pattern in SECRET_TEXT_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) + "***") if match.groups() else "***", text)
    return text


def _clean(value, *, key: str = ""):
    """递归脱敏：敏感键整键替换，字符串按密钥模式替换。"""
    if isinstance(value, dict):
        out: dict = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if SECRET_KEY_RE.search(name):
                out[name] = "[redacted]"
            else:
                out[name] = _clean(raw_value, key=name)
        return out
    if isinstance(value, list):
        return [_clean(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_clean(item, key=key) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _limit_list(value, limit: int):
    if not isinstance(value, list):
        return value
    return value[:limit]


def _clean_analysis(analysis) -> dict | None:
    if not isinstance(analysis, dict):
        return None
    out = {}
    for key in ANALYSIS_FIELDS:
        if key not in analysis:
            continue
        value = _clean(analysis[key], key=key)
        if key in ("tldr_points", "visible_changes"):
            value = _limit_list(value, 12)
        elif key == "sections":
            sections = []
            for section in (value if isinstance(value, list) else [])[:12]:
                if not isinstance(section, dict):
                    continue
                paragraphs = _limit_list(section.get("paragraphs"), 8)
                sections.append({"title": section.get("title", ""), "paragraphs": paragraphs})
            value = sections
        out[key] = value
    return out


def sanitize_frontend(evidence) -> dict | None:
    if not isinstance(evidence, dict):
        return None
    out = {}
    for key in FRONTEND_FIELDS:
        if key not in evidence:
            continue
        value = _clean(evidence[key], key=key)
        if key in LIST_CAPS:
            value = _limit_list(value, LIST_CAPS[key])
        if key in ("chunk_changes", "css_rule_changes", "i18n", "totals", "main_js", "main_css"):
            value = _clean(value, key=key)
        out[key] = value
    return out


def sanitize_event(event: dict) -> dict:
    """返回可投喂给 agent 的事件副本；不修改原事件。"""
    if not isinstance(event, dict):
        raise JobError("event 不是对象")
    out: dict = {}
    for key in TOP_LEVEL_FIELDS:
        if key in event:
            out[key] = _clean(copy.deepcopy(event[key]), key=key)
    if isinstance(out.get("evidence"), list):
        out["evidence"] = out["evidence"][:20]

    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    clean_payload: dict = {}
    for key in PAYLOAD_FIELDS:
        if key not in payload:
            continue
        if key == "frontend":
            frontend = sanitize_frontend(payload.get("frontend"))
            if frontend:
                clean_payload["frontend"] = frontend
        elif key == "analysis":
            analysis = _clean_analysis(payload.get("analysis"))
            if analysis:
                clean_payload["analysis"] = analysis
        else:
            clean_payload[key] = _clean(copy.deepcopy(payload[key]), key=key)
    out["payload"] = clean_payload
    return out


def _json_dump(path: Path, value) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _write_text(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def changes_diff(event: dict, limit: int = MAX_CHANGES_DIFF_CHARS) -> str:
    """把 changes_detail 拼成人读 diff 文本；按字符上限截断。"""
    detail = event.get("changes_detail")
    if not isinstance(detail, list):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        detail = payload.get("changes") if isinstance(payload.get("changes"), list) else []
    lines: list[str] = []
    for index, group in enumerate(detail, start=1):
        if not isinstance(group, dict):
            continue
        page = str(group.get("page") or group.get("kind") or f"变化 {index}")
        lines.append(f"## {index}. {page}")
        bullets = group.get("bullets")
        if isinstance(bullets, list):
            for bullet in bullets:
                text = str(bullet or "").strip()
                if text:
                    lines.append(f"- {_scrub_text(text)}")
        note = str(group.get("note") or "").strip()
        if note:
            lines.append(f"备注：{_scrub_text(note)}")
        diff = str(group.get("diff") or "").strip()
        if diff:
            lines.append("```diff")
            lines.append(_scrub_text(diff))
            lines.append("```")
        lines.append("")
    text = "\n".join(lines).strip() + ("\n" if lines else "")
    if len(text) > limit:
        marker = f"\n…（已截断，原文 {len(text)} 字符）\n"
        text = text[: max(1, limit - len(marker))] + marker
    return text


def _frontend(event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    frontend = payload.get("frontend")
    return frontend if isinstance(frontend, dict) else {}


def _manifest_entries(manifest: dict | None) -> list[dict]:
    files: list[dict] = []
    for item in (manifest or {}).get("files") or []:
        if not isinstance(item, dict):
            continue
        files.append(
            {
                "url": _scrub_text(str(item.get("url") or "")),
                "blob": _scrub_text(str(item.get("blob") or "")),
                "hash": _scrub_text(str(item.get("hash") or "")),
                "kind": _scrub_text(str(item.get("kind") or "")),
                "status": _scrub_text(str(item.get("status") or "")),
                "key": _scrub_text(str(item.get("key") or "")),
                "name": _scrub_text(str(item.get("name") or "")),
            }
        )
    return files


def _snapshot_index(vault_dir: Path, source: str, event: dict) -> dict | None:
    """读取版本仓库，返回匹配该事件的新版 manifest，并附带旧版 manifest。"""
    try:
        from webwatch import bundle_mirror
    except Exception:  # noqa: BLE001
        return None
    root = Path(vault_dir)
    if not root.is_dir():
        return None
    try:
        snapshots = bundle_mirror.list_snapshots(root, source)
    except Exception:  # noqa: BLE001
        return None
    if not snapshots:
        return None
    frontend = _frontend(event)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    commit = str(frontend.get("new_commit") or payload.get("commit_id") or "")
    old_commit = str(frontend.get("old_commit") or "")
    main_js = str((frontend.get("main_js") or {}).get("new_url") or "")
    chosen = None
    chosen_index: int | None = None
    # snapshots 按目录名升序；同一 commit 可能有多次快照，优先取最新一份。
    for index in range(len(snapshots) - 1, -1, -1):
        snapshot = snapshots[index]
        manifest = snapshot.get("manifest") if isinstance(snapshot, dict) else None
        if not isinstance(manifest, dict):
            continue
        build = manifest.get("build_meta") if isinstance(manifest.get("build_meta"), dict) else {}
        snap_commit = str(build.get("commitId") or build.get("commit_id") or "")
        snap_main = str(manifest.get("main_js_url") or "")
        if commit and snap_commit and commit == snap_commit:
            chosen = manifest
            chosen_index = index
            break
        if main_js and snap_main and main_js == snap_main:
            chosen = manifest
            chosen_index = index
            break
    if chosen is None and not commit and not main_js:
        # 没有版本标识时才退回最近完整快照；有 commit/main_js 但匹配不上，
        # 宁可标记不可用，也不要把错误版本喂给复核。
        for index in range(len(snapshots) - 1, -1, -1):
            manifest = snapshots[index].get("manifest") if isinstance(snapshots[index], dict) else None
            if isinstance(manifest, dict) and bool(manifest.get("complete", True)):
                chosen = manifest
                chosen_index = index
                break
    if chosen is None:
        return None

    # 旧版快照：优先按 old_commit 精确匹配；找不到就取新版前一个完整快照。
    previous = None
    candidates = snapshots[:chosen_index] if chosen_index is not None else []
    for index in range(len(candidates) - 1, -1, -1):
        manifest = candidates[index].get("manifest") if isinstance(candidates[index], dict) else None
        if not isinstance(manifest, dict):
            continue
        build = manifest.get("build_meta") if isinstance(manifest.get("build_meta"), dict) else {}
        snap_commit = str(build.get("commitId") or build.get("commit_id") or "")
        if old_commit and snap_commit == old_commit:
            previous = manifest
            break
    if previous is None and candidates:
        for index in range(len(candidates) - 1, -1, -1):
            manifest = candidates[index].get("manifest") if isinstance(candidates[index], dict) else None
            if isinstance(manifest, dict) and bool(manifest.get("complete", True)):
                previous = manifest
                break

    page = chosen.get("page") if isinstance(chosen.get("page"), dict) else {}
    result = {
        "available": True,
        "source": source,
        "snapshot": str(chosen.get("snapshot_id") or chosen.get("id") or ""),
        "complete": bool(chosen.get("complete", True)),
        "build_meta": _clean(chosen.get("build_meta") or {}, key="build_meta"),
        "main_js_url": _scrub_text(str(chosen.get("main_js_url") or "")),
        "page": {
            "url": _scrub_text(str(page.get("url") or "")),
            "html_sha256": _scrub_text(str(page.get("html_sha256") or "")),
            "page_html_blob": _scrub_text(str(page.get("page_html_blob") or "")),
            "page_text_blob": _scrub_text(str(page.get("page_text_blob") or "")),
        },
        "entries": _manifest_entries(chosen),
        "previous_snapshot": "",
        "previous_entries": [],
    }
    if previous is not None:
        result["previous_snapshot"] = str(previous.get("snapshot_id") or previous.get("id") or "")
        result["previous_entries"] = _manifest_entries(previous)
    return result


def _is_main_entry(item: dict) -> bool:
    """识别 main/index 入口文件；生产清单的 name 形如 platform/static/main.js。"""
    if str(item.get("key") or "") in ("main_js", "main_css"):
        return True
    name = str(item.get("name") or item.get("key") or "").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    return (
        bool(dot)
        and stem.split(".", 1)[0].lower() in ("main", "index")
        and ext.lower() in ("js", "mjs", "css")
    )


def _compact_manifest(manifest: dict, event: dict) -> dict:
    """只保留本条事件需要的 manifest 条目，避免把 399KB 全量清单塞给模型。

    完整版本仓库仍在只读目录里；job 里的清单只用于复制关键旧/新文件，
    因此保留 event 指向的 URL 即可。
    """
    frontend = _frontend(event)
    keep_urls: set[str] = set()
    for key in ("main_js", "main_css"):
        block = frontend.get(key) if isinstance(frontend.get(key), dict) else {}
        for field in ("old_url", "new_url"):
            url = str(block.get(field) or "")
            if url:
                keep_urls.add(url)
    changed = frontend.get("changed_files")
    if isinstance(changed, list):
        for item in changed[:12]:
            if not isinstance(item, dict):
                continue
            for field in ("old_url", "new_url"):
                url = str(item.get(field) or "")
                if url:
                    keep_urls.add(url)

    def pick(items: object) -> list[dict]:
        entries = [item for item in (items or []) if isinstance(item, dict)]
        if keep_urls:
            return [item for item in entries if str(item.get("url") or "") in keep_urls]
        # 没有 URL 锚点（纯 i18n、证据不全的事件）：不能返回全量 399KB 清单，
        # 也不能盲取前 12 条（可能全是碎片而错过主文件）。按 main/index 入口
        # 优先保留，再按原顺序补足到 12 条。
        preferred: list[dict] = []
        rest: list[dict] = []
        for item in entries:
            if _is_main_entry(item):
                preferred.append(item)
            else:
                rest.append(item)
        return (preferred + rest)[:12]

    entries_all = [item for item in (manifest.get("entries") or []) if isinstance(item, dict)]
    previous_all = [
        item for item in (manifest.get("previous_entries") or []) if isinstance(item, dict)
    ]
    entries = pick(entries_all)
    previous_entries = pick(previous_all)
    truncated = len(entries) < len(entries_all) or len(previous_entries) < len(previous_all)
    compact = dict(manifest)
    compact["entries_total"] = len(entries_all)
    compact["previous_entries_total"] = len(previous_all)
    compact["entries"] = entries
    compact["previous_entries"] = previous_entries
    if keep_urls:
        scope = "relevant"
    elif truncated:
        scope = "partial-no-url"
    else:
        scope = "complete-no-url"
    compact["manifest_scope"] = scope
    if scope == "relevant":
        compact["manifest_note"] = (
            "仅保留本条事件涉及的 URL；完整版本清单在 "
            "/run/recheck-data/bundle-vault/"
        )
    elif scope == "partial-no-url":
        compact["manifest_note"] = (
            "事件没有 URL 锚点，仅保留最多 12 条主要条目；"
            "完整版本清单在 /run/recheck-data/bundle-vault/"
        )
    else:
        compact["manifest_note"] = (
            "事件没有 URL 锚点，已保留全部清单条目；版本仓库仍可在 "
            "/run/recheck-data/bundle-vault/ 查阅"
        )
    return compact


def _blob_candidates(event: dict, manifest: dict) -> list[str]:
    frontend = _frontend(event)
    urls: list[str] = []
    for key, old_key, new_key in (
        ("main_js", "old_url", "new_url"),
        ("main_css", "old_url", "new_url"),
    ):
        block = frontend.get(key) if isinstance(frontend.get(key), dict) else {}
        for field in (old_key, new_key):
            url = str(block.get(field) or "")
            if url:
                urls.append(url)
    changed = frontend.get("changed_files")
    if isinstance(changed, list):
        for item in changed[:12]:
            if not isinstance(item, dict):
                continue
            for field in ("old_url", "new_url"):
                url = str(item.get(field) or "")
                if url:
                    urls.append(url)
    # 去重并保持顺序；只要 URL 出现在新版或旧版任一清单里就保留。
    # 以前只用新版 manifest 过滤，旧版 URL 永远不在新版清单里，导致复核 job
    # 只复制了新版文件，Claude 根本没法做新旧对比。
    known = {
        str(item.get("url") or "")
        for source in ("entries", "previous_entries")
        for item in manifest.get(source) or []
        if isinstance(item, dict)
    }
    return [url for url in dict.fromkeys(urls) if not known or url in known]


def _write_blob_files(
    event: dict,
    manifest: dict,
    vault_dir: Path,
    files_dir: Path,
    budget: int,
) -> tuple[list[dict], int]:
    try:
        from webwatch import bundle_vault
    except Exception:  # noqa: BLE001
        return [], 0
    source = str(event.get("source") or "")
    vault = bundle_vault.BundleVault(Path(vault_dir), source)
    frontend = _frontend(event)
    old_urls: set[str] = set()
    new_urls: set[str] = set()
    for key in ("main_js", "main_css"):
        block = frontend.get(key) if isinstance(frontend.get(key), dict) else {}
        old_urls.add(str(block.get("old_url") or ""))
        new_urls.add(str(block.get("new_url") or ""))
    for item in frontend.get("changed_files") or []:
        if isinstance(item, dict):
            old_urls.add(str(item.get("old_url") or ""))
            new_urls.add(str(item.get("new_url") or ""))
    old_urls.discard("")
    new_urls.discard("")
    used = 0
    written: list[dict] = []
    for url in _blob_candidates(event, manifest):
        if used >= budget:
            break
        try:
            body = vault.read(url)
        except Exception:  # noqa: BLE001
            body = b""
        if not body:
            continue
        truncated = False
        allowed = min(len(body), MAX_BLOB_FILE_BYTES, budget - used)
        if allowed <= 0:
            break
        data = body[:allowed]
        if len(data) < len(body):
            truncated = True
        # 文件名由 URL 哈希决定，避免路径穿越/超长文件名。
        path_name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        suffix = Path(url.split("?", 1)[0]).suffix
        if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            path_name += suffix
        target = files_dir / path_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if truncated:
            text = data.decode("utf-8", "replace") + f"\n…（已截断，原文 {len(body)} 字节）\n"
            target.write_text(_scrub_text(text), encoding="utf-8")
            used += len(target.read_bytes())
        else:
            try:
                # 公开 JS 也可能带查询串 / 调试 token；复制进 job 前统一脱敏。
                target.write_text(_scrub_text(data.decode("utf-8")), encoding="utf-8")
                used += len(target.read_bytes())
            except UnicodeDecodeError:
                target.write_bytes(data)
                used += len(data)
        written.append(
            {
                "url": url,
                "file": f"evidence/files/{target.name}",
                "bytes": len(data),
                "original_bytes": len(body),
                "truncated": truncated,
                "side": "旧版" if url in old_urls else ("新版" if url in new_urls else ""),
            }
        )
    return written, used


def recent_events(
    events_dir: Path,
    source: str,
    kind: str,
    current_id: str,
    limit: int = MAX_RECENT_EVENTS,
) -> list[dict]:
    if not events_dir.is_dir():
        return []
    rows: list[tuple[int, str, dict]] = []
    for path in events_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("source") or "") != source or str(data.get("kind") or "") != kind:
            continue
        event_id = str(data.get("id") or "")
        if not event_id or event_id == current_id:
            continue
        changed = str(data.get("changed_at") or "")
        rows.append(
            (
                _parse_ts(changed),
                event_id,
                {
                    "id": _scrub_text(event_id)[:200],
                    "title": _scrub_text(str(data.get("title") or ""))[:200],
                    "summary": _scrub_text(str(data.get("summary") or ""))[:240],
                    "level": data.get("level"),
                    "changed_at": _scrub_text(changed)[:80],
                },
            )
        )
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row[2] for row in rows[:limit]]


def _parse_ts(value: str) -> int:
    try:
        from webwatch.events import parse_iso

        parsed = parse_iso(value)
        return int(parsed.timestamp()) if parsed else 0
    except Exception:  # noqa: BLE001
        return 0


def _render_template(template: str, replacements: dict[str, str]) -> str:
    text = template
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def load_template(name: str) -> str:
    path = Path(__file__).with_name(name)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobError(f"缺少模板 {path}: {exc}") from exc


def _manifest_brief_line(manifest: dict) -> str:
    if not manifest.get("available"):
        return "- `evidence/manifest.json` 无可用清单：版本仓库不可用，无法提供完整资源清单"
    scope = manifest.get("manifest_scope")
    if scope == "relevant":
        return "- `evidence/manifest.json` 只列本条事件相关 URL；完整版本清单在只读目录 `bundle-vault/`"
    if scope == "complete-no-url":
        return "- `evidence/manifest.json` 已保留全部清单条目；版本仓库仍可在只读目录 `bundle-vault/` 查阅"
    return "- `evidence/manifest.json` 仅保留最多 12 条主要条目（不完整），可到只读目录 `bundle-vault/` 查全量"


def build_brief(event: dict, manifest: dict, written_files: list[dict]) -> str:
    frontend = _frontend(event)
    totals = frontend.get("totals") if isinstance(frontend.get("totals"), dict) else {}
    lines = [
        "# 复核简报",
        "",
        f"- 事件：`{event.get('id')}`",
        f"- 来源：`{event.get('source')}` / 类型：`{event.get('kind')}`",
        f"- 当前级别：{event.get('level')}（{event.get('level_label')}）",
        f"- 当前质量：{(event.get('payload') or {}).get('quality', 'unknown')}",
        f"- 页面：{event.get('page_url') or '（无）'}",
        f"- 变化时间：{event.get('changed_at') or '（无）'}",
        "",
        "## 当前标题与摘要",
        "",
        f"标题：{event.get('title') or '（空）'}",
        "",
        f"摘要：{event.get('summary') or '（空）'}",
        "",
        f"影响：{event.get('impact') or '（空）'}",
        "",
        f"建议：{event.get('action') or '（空）'}",
        "",
        "## 证据概览",
        "",
        f"- 资源清单总数：{totals.get('manifest', '?')}，新增 {totals.get('added', '?')}，"
        f"移除 {totals.get('removed', '?')}，内容变化 {totals.get('changed', '?')}",
        f"- 证据截断：{frontend.get('truncated')}；旧基线缺失：{frontend.get('baseline_missing')}",
        f"- 版本仓库：{'可用' if manifest.get('available') else '不可用'}"
        f"（新版 {manifest.get('snapshot') or '无快照'}"
        f" / 旧版 {manifest.get('previous_snapshot') or '未找到'}）",
        _manifest_brief_line(manifest),
        f"- 只读全量数据目录：`/run/recheck-data`（events / state / frontend-cache / bundle-vault，只读）",
        f"- 已复制关键文件：{len(written_files)} 个",
    ]
    for item in written_files[:16]:
        side = f"{item.get('side')}：" if item.get("side") else ""
        lines.append(f"  - {side}`{item['file']}` ← {item['url']}（{item['bytes']} 字节）")
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "1. 先读 `CONTEXT.md` 与 `event.json`，再按需读 `evidence/frontend.json` 和 `evidence/changes.diff`。",
            "2. 必须打开旧版和新版关键文件（或从只读目录找对应快照），不能只看新版就下结论。",
            "3. 用 `recent-events.json` 判断是否与近期事件重复。",
            "4. 把结论写入 `out/verdict.json`（结构见 AGENT.md），可另写 `out/RESULT.md`。",
        ]
    )
    return "\n".join(lines) + "\n"


def _job_size(job_dir: Path) -> int:
    total = 0
    for path in job_dir.rglob("*"):
        if path.is_file() and "out" not in path.relative_to(job_dir).parts:
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def job_digest(job_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(job_dir.rglob("*")):
        rel = path.relative_to(job_dir).as_posix()
        if not path.is_file() or rel.startswith("out/"):
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_job(
    job_dir: Path,
    event: dict,
    *,
    events_dir: Path,
    vault_dir: Path,
    max_bytes: int | None = None,
    prompt_template: str | None = None,
    context_template: str | None = None,
) -> dict:
    """生成 job；返回 job 信息（大小、摘要、快照等）。"""
    max_bytes = max_bytes or cfg.job_max_bytes()
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    # 只清理 worker 自己生成的旧内容；保留 out/ 让失败现场可回放。
    for name in ("AGENT.md", "CONTEXT.md", "event.json", "BRIEF.md", "evidence", "recent-events.json"):
        target = job_dir / name
        if target.is_dir() and not target.is_symlink():
            import shutil

            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                pass
    (job_dir / "out").mkdir(parents=True, exist_ok=True)

    clean_event = sanitize_event(event)
    manifest = _snapshot_index(Path(vault_dir), str(event.get("source") or ""), event)
    if manifest is None:
        manifest = {
            "available": False,
            "source": str(event.get("source") or ""),
            "snapshot": "",
            "complete": False,
            "entries": [],
            "previous_snapshot": "",
            "previous_entries": [],
        }
    else:
        manifest = _compact_manifest(manifest, event)
    written_files: list[dict] = []
    diff_limit = MAX_CHANGES_DIFF_CHARS
    blob_budget = MAX_BLOB_BYTES
    trimmed = False
    while True:
        # 每一轮都清空上一轮的 blob 副本：预算缩小时旧的大文件不会被覆盖，
        # 不清空会残留在目录里被 _job_size 计入，也会让 agent 读到新旧混杂的证据。
        import shutil

        files_dir = job_dir / "evidence" / "files"
        if files_dir.is_dir() and not files_dir.is_symlink():
            shutil.rmtree(files_dir, ignore_errors=True)
        files_dir.mkdir(parents=True, exist_ok=True)
        _json_dump(job_dir / "event.json", clean_event)
        _json_dump(job_dir / "evidence" / "frontend.json", clean_event.get("payload", {}).get("frontend") or {"available": False})
        _json_dump(job_dir / "evidence" / "manifest.json", manifest)
        _write_text(job_dir / "evidence" / "changes.diff", changes_diff(event, diff_limit))
        written_files, _ = _write_blob_files(
            event, manifest, Path(vault_dir), files_dir, blob_budget
        )
        _json_dump(
            job_dir / "recent-events.json",
            recent_events(
                Path(events_dir),
                str(event.get("source") or ""),
                str(event.get("kind") or ""),
                str(event.get("id") or ""),
            ),
        )
        _write_text(job_dir / "BRIEF.md", build_brief(clean_event, manifest, written_files))

        prompt = prompt_template if prompt_template is not None else load_template("recheck_prompt.md")
        context = context_template if context_template is not None else load_template("recheck_context.md")
        replacements = {
            "{{EVENT_ID}}": str(event.get("id") or ""),
            "{{EVENT_SOURCE}}": str(event.get("source") or ""),
            "{{EVENT_CHANGED_AT}}": str(event.get("changed_at") or ""),
            "{{JOB_DIR}}": ".",
            "{{OUT_DIR}}": "out",
            "{{MAX_TITLE_CHARS}}": str(cfg.MAX_TITLE_CHARS),
            "{{MAX_SUMMARY_CHARS}}": str(cfg.MAX_SUMMARY_CHARS),
        }
        _write_text(job_dir / "AGENT.md", _render_template(prompt, replacements))
        _write_text(job_dir / "CONTEXT.md", _render_template(context, replacements))

        size = _job_size(job_dir)
        if size <= max_bytes:
            break
        if diff_limit > 8_000:
            diff_limit //= 2
            continue
        if blob_budget > 0:
            blob_budget //= 2
            continue
        # 最后手段：削减大列表后重建一次。裁剪必须落盘再重算体积，
        # 并且最多裁一次，否则裁完仍超限时会在这一分支反复重写目录。
        if not trimmed:
            trimmed = True
            for key in (
                "changed_files", "snippets", "changed_code_snippets",
                "removed_code_snippets", "removed_i18n_refs", "added_i18n_refs",
                "api_added", "api_removed", "cjk_added", "cjk_removed",
            ):
                value = (clean_event.get("payload", {}).get("frontend") or {}).get(key)
                if isinstance(value, list) and len(value) > 5:
                    clean_event["payload"]["frontend"][key] = value[:5]
            continue
        raise JobError(f"job 超过上限：{size} > {max_bytes} 字节")

    return {
        "dir": str(job_dir),
        "digest": job_digest(job_dir),
        "bytes": _job_size(job_dir),
        "files": len(written_files),
        "vault_available": bool(manifest.get("available")),
        "vault_snapshot": str(manifest.get("snapshot") or ""),
    }
