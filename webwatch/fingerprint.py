"""网页与模型指纹的核心逻辑。

一次巡检做四类事：
1. 抓 chat / platform / harness 三个官方页面，记录页面正文、commit-id、脚本资源哈希；
2. 抓 platform 的公开公告接口（不需要登录、不消耗 token）；
3. 抓官方价格页，解析每个模型的「模型版本」；
4. 配置了 DEEPSEEK_API_KEY 时，按独立调度查 /models 模型目录，并按模型发最小请求读 system_fingerprint。

所有网络请求都走 safe_fetch（API 请求除外，见 _api_request 的固定域名/固定 IP 校验）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import unicodedata
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

from . import bundle_vault, config
from .events import build_event, iso, now_utc
from .extract import extract_page, sha256_bytes
from .frontend_evidence import build_changes_detail, build_frontend_snapshot
from .models import parse_model_versions
from .redact import redact
from .safe_fetch import (
    ACCEPT_ANY,
    ACCEPT_HTML,
    ContentError,
    FetchError,
    _PinnedHTTPSConnection,
    _resolve_public,
    fetch,
    fetch_json,
)

API_HOST = "api.deepseek.com"
API_TIMEOUT = 20.0
FIM_PATH = "/beta/completions"
CHAT_PATH = "/chat/completions"
# 单轮付费探测的硬上限（逻辑请求数）：最坏 8 模型 × (FIM→Chat→去 thinking 重试)
# 约 24 次。超过上限立即停止，不再逐模型重试。
MAX_PAID_REQUESTS_PER_ROUND = 12
_paid_requests_this_round = 0
# 页面资产清单的采集口径版本。口径升级后的第一轮只重建基线，不把「新纳入采集」
# 的旧资源误报成网站新增资源（2026-11-17 Chat 的 CSS 误报就是这么来的）。
ASSET_SCOPE_VERSION = 2

# 200 响应也可能是 WAF / 验证码 / 维护页。命中这些特征时按抓取失败处理，
# 不能把挑战页当真实页面写进基线（P1-8）。
CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser before accessing",
    "cf-browser-verification",
    "cf-challenge",
    "ddos protection by",
    "please enable cookies",
    "access denied",
    "request blocked",
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def _state_path(state_dir: Path) -> Path:
    return state_dir / "state.json"


def _models_state(state: dict) -> dict:
    models = state.get("models")
    if not isinstance(models, dict):
        models = {}
        state["models"] = models
    return models


def load_state(state_dir: Path) -> dict:
    path = _state_path(state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(state_dir)
    tmp = path.with_name("state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "posix":
        try:
            # 0750/0640 让推送用户经 POSIX ACL 只读；0700/0600 会把 ACL mask 压成 ---。
            os.chmod(state_dir, 0o750)
            os.chmod(tmp, 0o640)
        except OSError:
            pass
    os.replace(tmp, path)


def _ensure_sources(state: dict) -> dict:
    sources = state.get("sources")
    if not isinstance(sources, dict):
        sources = {}
        state["sources"] = sources
    for source in ("chat", "platform", "harness", "fingerprint"):
        if not isinstance(sources.get(source), dict):
            sources[source] = {}
    return sources


def _source_ok(state: dict, source: str, now: datetime) -> None:
    """记录来源成功；同一轮里已经失败过的来源不能被后续成功洗白（P1-5）。"""
    if _source_failed_this_round(state, source, now):
        return
    sources = _ensure_sources(state)
    entry = sources[source]
    stamp = iso(now)
    entry["last_checked"] = stamp
    entry["last_success"] = stamp
    entry["fail_streak"] = 0
    entry["last_error"] = ""


def _source_fail(state: dict, source: str, now: datetime, exc: object) -> None:
    sources = _ensure_sources(state)
    entry = sources[source]
    stamp = iso(now)
    # 同一轮里多个模型/子检查失败只增加一次，避免一轮就被判成连续失败 3 次；
    # 先成功（价格页）后失败（API 全挂）也属于同一轮失败，不能被 last_success 掩盖。
    if entry.get("last_checked") == stamp:
        entry["fail_streak"] = max(1, int(entry.get("fail_streak") or 0))
    else:
        entry["fail_streak"] = int(entry.get("fail_streak") or 0) + 1
    entry["last_checked"] = stamp
    entry["last_error"] = redact(exc)
    entry.setdefault("last_success", "")


def _source_failed_this_round(state: dict, source: str, now: datetime) -> bool:
    """同一轮里已有子检查失败时，后面的成功不能把失败洗成正常。"""
    entries = _ensure_sources(state)
    entry = entries.get(source) if isinstance(entries.get(source), dict) else {}
    stamp = iso(now)
    return entry.get("last_checked") == stamp and int(entry.get("fail_streak") or 0) > 0


def _source_clear_catalog_error(state: dict) -> None:
    """目录检查恢复后清掉它自己造成的来源失败，不碰价格页/指纹子检查的连续失败。"""
    sources = state.get("sources")
    if not isinstance(sources, dict):
        return
    entry = sources.get("fingerprint")
    if not isinstance(entry, dict):
        return
    if str(entry.get("last_error") or "").startswith("模型目录："):
        entry["fail_streak"] = 0
        entry["last_error"] = ""


def _event_key(stamp: datetime) -> str:
    """分钟级时间戳加随机后缀；同一分钟两次变化不会互相覆盖。"""
    return f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _first_diff(old: str, new: str, width: int = 220) -> str:
    """给出一段人类能看懂的前后对比，避免把整页正文塞进事件。"""
    old = str(old or "")
    new = str(new or "")
    limit = min(len(old), len(new))
    index = 0
    while index < limit and old[index] == new[index]:
        index += 1
    start = max(0, index - 60)
    return f"…{old[start:start + width]} → …{new[start:start + width]}"


def _validate_html_response(response, label: str, *, must_contain: tuple[str, ...] = ()) -> str:
    """校验 200 响应确实是真实 HTML 页面，而不是空正文 / 挑战页 / 错误页（P1-8）。"""
    content_type = response.content_type
    if not content_type:
        raise ContentError(label, "响应缺少 Content-Type", response.status)
    if "html" not in content_type:
        raise ContentError(label, f"Content-Type 不是 HTML：{content_type}", response.status)
    if not response.body:
        raise ContentError(label, "响应正文为空", response.status)
    text = response.text
    head = text[:20000].lower()
    if "<html" not in head and "<!doctype html" not in head:
        raise ContentError(label, "响应不是已知 HTML 结构", response.status)
    for marker in CHALLENGE_MARKERS:
        if marker in head:
            raise ContentError(label, f"疑似挑战/验证页（命中 {marker}）", response.status)
    for marker in must_contain:
        if marker.lower() not in head:
            raise ContentError(label, f"正文缺少已知标识：{marker}", response.status)
    return text


def _validate_page_structure(page: dict, label: str) -> None:
    """已知结构：页面至少要有一个标题、commit-id 或有效正文。"""
    title = str(page.get("title") or "").strip()
    meta = page.get("meta") if isinstance(page.get("meta"), dict) else {}
    commit_id = str(meta.get("commit-id") or "").strip()
    text = str(page.get("text") or "").strip()
    if not title and not commit_id and len(text) < 20:
        raise ContentError(label, "页面缺少标题、commit-id 和有效正文")


def _asset_summary(assets: list[dict]) -> list[dict]:
    return [
        {
            "url": str(item.get("url") or "")[:500],
            "sha256": str(item.get("sha256") or ""),
            "size": int(item.get("size") or 0),
            "error": str(item.get("error") or ""),
        }
        for item in assets[:20]
    ]


def _previous_asset_map(assets: object) -> dict[str, dict]:
    """按请求 URL（并兼容旧基线的 final_url）索引上一轮成功资源。"""
    mapping: dict[str, dict] = {}
    if not isinstance(assets, list):
        return mapping
    for item in assets:
        if not isinstance(item, dict) or not item.get("sha256"):
            continue
        url = str(item.get("url") or "")
        if url:
            mapping[url] = item
        final_url = str(item.get("final_url") or "")
        if final_url and final_url not in mapping:
            mapping[final_url] = item
    return mapping


def _fetch_assets(
    urls: list[str], max_count: int, previous_assets: object = None
) -> tuple[list[dict], list[dict]]:
    """抓资源；成功项用请求 URL 作稳定键，失败项沿用上一轮哈希且不写入基线（P1-6）。"""
    previous = _previous_asset_map(previous_assets)
    assets: list[dict] = []
    errors: list[dict] = []
    for url in urls[:max_count]:
        try:
            response = fetch(url, accept=ACCEPT_ANY)
            if not response.body:
                raise ContentError(url, "资源正文为空", response.status)
            assets.append(
                {
                    "url": url,
                    "sha256": sha256_bytes(response.body),
                    "size": len(response.body),
                }
            )
        except Exception as exc:  # noqa: BLE001
            # 失败时保留上一轮成功哈希，避免「空 sha256 → 恢复」制造成对误报。
            old_item = previous.get(url)
            if old_item:
                assets.append(
                    {
                        "url": url,
                        "sha256": str(old_item.get("sha256") or ""),
                        "size": int(old_item.get("size") or 0),
                    }
                )
            if isinstance(exc, FetchError):
                errors.append({"url": url, "error": exc.kind, "status": exc.status})
            else:
                errors.append({"url": url, "error": type(exc).__name__})
    return assets, errors


def build_target_snapshot(
    target: dict,
    previous: dict | None = None,
    *,
    state_dir: Path | None = None,
    vault=None,
) -> dict:
    label = str(target.get("label") or target.get("url") or "页面")
    response = fetch(str(target["url"]), accept=ACCEPT_HTML)
    text = _validate_html_response(response, label, must_contain=("deepseek",))
    page = extract_page(text, response.final_url)
    _validate_page_structure(page, label)
    html_digest = sha256_bytes(response.body)
    page_for_frontend = dict(page)
    page_for_frontend["html_sha256"] = html_digest
    frontend_snapshot: dict | None = None
    frontend_evidence: dict | None = None
    if state_dir is not None:
        # 前端资源清单/证据是展示层增强，失败不能拖垮基础巡检。
        try:
            frontend_snapshot, frontend_evidence = build_frontend_snapshot(
                target, page_for_frontend, previous, state_dir, vault=vault
            )
        except Exception as exc:  # noqa: BLE001
            # 不用 {"error"} 覆盖旧基线：标记不健康，由 check_targets 保留上一版快照。
            frontend_snapshot = {
                "error": f"{type(exc).__name__}: {redact(exc)}",
                "health": "error",
                "health_reason": "前端快照构建异常",
                "engine": "",
                "manifest": [],
            }
    max_assets = int(target.get("max_assets") or 0)
    # 样式和脚本都参与 diff：只改 CSS 的发版以前会被漏掉。样式放前面，
    # 避免 max_assets 较小时主 CSS 被大量 chunk 挤掉。
    all_asset_urls: list[str] = []
    for url in list(page.get("styles") or []) + list(page.get("scripts") or []):
        if url and url not in all_asset_urls:
            all_asset_urls.append(url)
    # max_assets 只限制本轮下载量，不限制 diff 范围；完整 URL 清单写进快照，
    # 这样新增样式把第 8 个资源挤出下载窗口时不会被误判成「移除资源」。
    asset_urls = all_asset_urls[:max_assets] if max_assets else []
    previous_assets = (previous or {}).get("assets") if isinstance(previous, dict) else None
    assets, asset_errors = (
        _fetch_assets(asset_urls, max_assets, previous_assets) if max_assets else ([], [])
    )
    snapshot = {
        "checked_at": iso(now_utc()),
        "url": str(target["url"]),
        "final_url": response.final_url,
        "http_status": response.status,
        "title": page["title"],
        "commit_id": str(page["meta"].get("commit-id") or ""),
        "meta": {
            key: page["meta"][key]
            for key in ("commit-id", "description", "keywords", "og:title")
            if key in page["meta"]
        },
        "html_sha256": html_digest,
        "text_sha256": page["text_sha256"],
        # 1200 字之后的变化以前两侧预览完全相同；扩到 4000 字并保留 hash 判断，
        # 超出范围时由 _diff_target 明确写「前 4000 字相同，后部有变化」。
        "text_excerpt": page["text"][:4000],
        "assets": _asset_summary(assets),
        "asset_urls": all_asset_urls[:200],
        "asset_scope": ASSET_SCOPE_VERSION,
    }
    if frontend_snapshot is not None:
        snapshot["frontend"] = frontend_snapshot
    if frontend_evidence:
        snapshot["frontend_evidence"] = frontend_evidence
    if asset_errors:
        snapshot["asset_errors"] = asset_errors[:20]
    return snapshot


def _frontend_health(snapshot: dict) -> str:
    """前端快照健康度：ok / degraded / error / missing。

    只有两侧都健康时才比较前端签名；失败轮的 html-only 清单不能当基线，
    恢复轮也不能再生成一条「前端资源更新」。
    """
    front = snapshot.get("frontend") if isinstance(snapshot, dict) else None
    if not isinstance(front, dict):
        return "missing"
    if front.get("error"):
        return "error"
    health = str(front.get("health") or "").strip().lower()
    if health in ("ok", "degraded", "error"):
        return health
    # 兼容旧快照（没有 health 字段）。
    if not str(front.get("engine") or ""):
        return "degraded"
    manifest = front.get("manifest")
    if not isinstance(manifest, list) or not manifest:
        return "degraded"
    return "ok"


def _diff_target(source: str, label: str, previous: dict, current: dict) -> dict | None:
    if not previous:
        return None
    changes: list[dict] = []
    old_commit = str(previous.get("commit_id") or "")
    new_commit = str(current.get("commit_id") or "")
    if old_commit != new_commit:
        changes.append(
            {
                "kind": "commit",
                "old": old_commit or "（无）",
                "new": new_commit or "（无）",
            }
        )

    def _asset_view(snapshot: dict) -> tuple[dict[str, str], set[str], set[str]]:
        """返回 (有哈希的资源, 已知 URL 全集, 本轮失败/应忽略的 URL)。

        已知 URL 来自 `asset_urls`（HTML 里的完整清单），包含没被 max_assets
        下载的资源。diff 用完整集合判断增删，避免窗口滑动制造假「移除」。
        """
        hashes: dict[str, str] = {}
        known: set[str] = set()
        ignored: set[str] = set()
        for item in snapshot.get("assets") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url:
                continue
            known.add(url)
            digest = str(item.get("sha256") or "")
            if not digest or item.get("error"):
                ignored.add(url)
                continue
            hashes[url] = digest
        for url in snapshot.get("asset_urls") or []:
            if url:
                known.add(str(url))
        for item in snapshot.get("asset_errors") or []:
            if isinstance(item, dict):
                url = str(item.get("url") or "")
                if url:
                    ignored.add(url)
        return hashes, known, ignored

    frontend_evidence = current.get("frontend_evidence")
    if not isinstance(frontend_evidence, dict):
        frontend_evidence = None

    # 采集口径升级的第一轮只重建基线：旧快照没有 asset_scope 时，资源清单里
    # 多出来的条目可能只是新纳入监控的旧文件，不能当成网站新增。
    asset_scope_same = int(previous.get("asset_scope") or 0) == int(current.get("asset_scope") or 0)
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    if asset_scope_same:
        old_assets, old_known, old_ignored = _asset_view(previous)
        new_assets, new_known, new_ignored = _asset_view(current)
        for url in old_ignored | new_ignored:
            old_assets.pop(url, None)
            new_assets.pop(url, None)
            old_known.discard(url)
            new_known.discard(url)
        # 旧快照没有 asset_urls（升级前）时只在双方都下载到的 URL 上判断增删，
        # 避免把「新纳入完整清单」的旧资源误报成新增。
        old_has_full = isinstance(previous.get("asset_urls"), list) and bool(previous.get("asset_urls"))
        new_has_full = isinstance(current.get("asset_urls"), list) and bool(current.get("asset_urls"))
        if old_has_full and new_has_full:
            added = sorted(new_known - old_known)
            removed = sorted(old_known - new_known)
        else:
            added = sorted(set(new_assets) - set(old_assets))
            removed = sorted(set(old_assets) - set(new_assets))
        changed = sorted(
            url for url in set(old_assets) & set(new_assets) if old_assets[url] != new_assets[url]
        )
    if added or removed or changed:
        changes.append(
            {
                "kind": "assets",
                "added": [url[:300] for url in added[:8]],
                "removed": [url[:300] for url in removed[:8]],
                "changed": [url[:300] for url in changed[:8]],
                "added_count": len(added),
                "removed_count": len(removed),
                "changed_count": len(changed),
            }
        )

    old_text = str(previous.get("text_sha256") or "")
    new_text = str(current.get("text_sha256") or "")
    if old_text != new_text:
        old_excerpt = str(previous.get("text_excerpt") or "")
        new_excerpt = str(current.get("text_excerpt") or "")
        if old_excerpt and old_excerpt == new_excerpt:
            # 1200 字之后的变化：两侧前段预览完全相同，必须明确告诉用户。
            preview = "前 4000 字相同，后部内容有变化（需打开原文核对）"
        else:
            preview = _first_diff(old_excerpt, new_excerpt)
        changes.append({"kind": "text", "preview": preview})

    # 可见正文没变、HTML 哈希变了：通常是脚本引用/构建产物变了（harness 很常见，
    # 它 max_assets=0，只看 text 会永远漏报）。commit 变化时已由 commit 代表，不重复报。
    old_html = str(previous.get("html_sha256") or "")
    new_html = str(current.get("html_sha256") or "")
    if (old_html and new_html and old_html != new_html
            and old_text == new_text and old_commit == new_commit):
        changes.append({"kind": "html", "old": old_html[:12], "new": new_html[:12]})

    def _front_signature(snapshot: dict) -> str:
        front = snapshot.get("frontend")
        if not isinstance(front, dict):
            return ""
        manifest = front.get("manifest")
        if isinstance(manifest, list):
            manifest = sorted(
                (item for item in manifest if isinstance(item, dict)),
                key=lambda item: str(item.get("key") or ""),
            )
        return json.dumps(
            {
                "engine": front.get("engine"),
                "manifest": manifest,
                "main_js_url": front.get("main_js_url"),
                "main_css_url": front.get("main_css_url"),
                "build_meta": front.get("build_meta"),
            },
            ensure_ascii=False, sort_keys=True,
        )

    old_front_health = _frontend_health(previous)
    new_front_health = _frontend_health(current)
    # 失败轮 / 恢复轮都不比较前端签名：失败轮清单不完整，恢复轮会再报一次。
    old_front = _front_signature(previous) if old_front_health == "ok" else ""
    new_front = _front_signature(current) if new_front_health == "ok" else ""
    if old_front and new_front and old_front != new_front:
        changes.append({"kind": "frontend", "preview": "前端资源清单/构建信息变化"})
    # 只有资源清单变化、没有前端证据：前端快照健康时通常只是监控口径差异，
    # 生成出来也只会是一条没有文字摘要的空事件，直接不报；但前端快照失败时
    # 不能丢真资源变化，保留事件并标 evidence_unavailable。
    evidence_unavailable = False
    if changes and all(change.get("kind") == "assets" for change in changes) and not frontend_evidence:
        if new_front_health in ("ok", "missing"):
            return None
        evidence_unavailable = True
    if not changes:
        return None

    lines: list[str] = []
    for change in changes:
        kind = change.get("kind")
        if kind == "commit":
            lines.append(f"前端版本：{change['old']} → {change['new']}")
        elif kind == "assets":
            parts = []
            # 用原始计数，不用截断到 8 条后的列表长度（P2-09）。
            if change.get("added_count"):
                parts.append(f"新增 {change['added_count']} 个")
            if change.get("removed_count"):
                parts.append(f"移除 {change['removed_count']} 个")
            if change.get("changed_count"):
                parts.append(f"内容变化 {change['changed_count']} 个")
            lines.append("脚本资源：" + "，".join(parts))
        elif kind == "text":
            lines.append(f"页面正文变化：{change['preview']}")
        elif kind == "html":
            lines.append("页面结构变化（可见正文未变，脚本/构建可能更新）")
        elif kind == "frontend":
            lines.append(f"前端资源变化：{change['preview']}")
    if evidence_unavailable:
        reason = str(((current.get("frontend") or {}).get("health_reason")) or "前端快照本轮不可用")
        lines.append(f"前端证据本轮不可用（{reason}），资源变化仍需人工核对")
    # 规则只给默认档：前端版本变化先按「值得看」，是否升「重要」由判读闸门结合证据决定。
    level = 2
    if all(change.get("kind") == "text" for change in changes):
        level = 1
    title = f"{label}页面更新"
    if any(change.get("kind") == "commit" for change in changes):
        title = f"{label}前端版本更新"
    elif any(change.get("kind") == "frontend" for change in changes):
        title = f"{label}前端资源更新"
    elif all(change.get("kind") == "text" for change in changes):
        title = f"{label}正文更新"
    stamp = now_utc()
    event_key = _event_key(stamp)
    payload: dict = {
        "changes": changes,
        "commit_id": current.get("commit_id", ""),
        "level_rule": level,
    }
    if isinstance(frontend_evidence, dict):
        payload["frontend"] = frontend_evidence
    if evidence_unavailable:
        payload["evidence_unavailable"] = True
        payload["frontend_health"] = new_front_health
    event = build_event(
        event_id=f"web:{source}:{event_key}",
        slug=f"web-{source}-{event_key}",
        source=source,
        kind="page_change",
        level=level,
        title=title,
        summary="；".join(lines),
        changed_at=iso(stamp),
        page_url=str(current.get("final_url") or current.get("url") or ""),
        impact="DeepSeek 网页前端或内容发生了变化，可能对应功能更新、公告或部署。",
        action="打开页面确认变化内容；如果影响你的使用，再查看官方更新日志。",
        signal_count=len(changes),
        evidence=lines,
        payload=payload,
    )
    if isinstance(frontend_evidence, dict):
        detail = build_changes_detail(frontend_evidence, level)
        if detail:
            event["changes_detail"] = detail
    return event


def check_targets(
    state: dict,
    now: datetime,
    *,
    defer_platform_ok: bool = False,
    defer_harness_ok: bool = False,
    state_dir: Path | None = None,
) -> list[dict]:
    targets = state.get("targets")
    if not isinstance(targets, dict):
        targets = {}
        state["targets"] = targets
    events: list[dict] = []
    for source, target in config.TARGETS.items():
        previous = targets.get(source) if isinstance(targets.get(source), dict) else {}
        vault = None
        if state_dir is not None:
            try:
                vault = bundle_vault.BundleVault(bundle_vault.vault_root(state_dir), source)
            except Exception as exc:  # noqa: BLE001 - 版本仓库不可用不能拖垮巡检
                print(f"[webwatch] 版本仓库初始化失败（{source}）：{type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            current = build_target_snapshot(target, previous, state_dir=state_dir, vault=vault)
        except Exception as exc:  # noqa: BLE001
            _source_fail(state, source, now, f"{type(exc).__name__}: {exc}")
            continue
        front_health = _frontend_health(current)
        event = _diff_target(source, str(target.get("label") or source), previous, current)
        # 证据包只用于生成本轮事件，不写进 state（避免 state 随下载内容变大）。
        current.pop("frontend_evidence", None)
        if front_health in ("degraded", "error"):
            fresh_front = current.get("frontend") if isinstance(current.get("frontend"), dict) else {}
            reason = str(
                fresh_front.get("health_reason") or fresh_front.get("error") or "前端快照不可用"
            )
            # 失败轮不推进前端基线：保留上一版健康快照，下一轮恢复时不会
            # 把失败快照当成「新版本」再生成一条假事件。
            if _frontend_health(previous) == "ok":
                current["frontend"] = previous.get("frontend")
            _source_fail(state, source, now, f"前端快照异常：{reason}")
        targets[source] = current
        if state_dir is not None and front_health == "ok":
            try:
                vault_summary = bundle_vault.ensure_snapshot(target, state_dir, current=current)
                if vault_summary and vault_summary.get("error"):
                    print(
                        f"[webwatch] 版本仓库快照失败（{source}）：{vault_summary.get('error')}",
                        file=sys.stderr,
                    )
                elif vault_summary and vault_summary.get("complete") is False:
                    print(
                        f"[webwatch] 版本仓库快照不完整（{source}）："
                        f"{vault_summary.get('incomplete_reason') or '未知原因'}；下一轮重试",
                        file=sys.stderr,
                    )
                elif vault_summary:
                    print(
                        f"[webwatch] 版本仓库已保存（{source}）：{vault_summary.get('snapshot_id', '')} "
                        f"{int((vault_summary.get('totals') or {}).get('bytes', 0)):,} 字节",
                        file=sys.stderr,
                    )
            except Exception as exc:  # noqa: BLE001 - 版本留存失败不能拖垮巡检
                print(
                    f"[webwatch] 版本仓库快照异常（{source}）：{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        asset_errors = current.get("asset_errors")
        if asset_errors:
            first = asset_errors[0] if isinstance(asset_errors, list) else {}
            detail = str(first.get("error") or "unknown") if isinstance(first, dict) else "unknown"
            _source_fail(
                state,
                source,
                now,
                f"资源抓取失败 {len(asset_errors)} 个（{detail}），本项沿用上一轮哈希",
            )
        elif (
            front_health in ("ok", "missing")
            and not (source == "platform" and defer_platform_ok)
            and not (source == "harness" and defer_harness_ok)
        ):
            # platform 的公告子检查由 check_platform_banner 统一记录本来源成功/失败，
            # harness 的 DSH 更新子检查也同理，避免页面成功每轮把另一路失败 streak 清零。
            _source_ok(state, source, now)
        if event:
            events.append(event)
    return events


def _notice_text(notice: object) -> str:
    """从公告项里取正文；结构未知时退回规范化 JSON，保证正文变化能被比较。"""
    if isinstance(notice, dict):
        content = notice.get("content")
        if isinstance(content, dict):
            for key in ("zh_CN", "en_US"):
                value = str(content.get(key) or "").strip()
                if value:
                    return value
        elif isinstance(content, str) and content.strip():
            return content.strip()
        for key in ("zh_CN", "en_US", "title", "text", "content"):
            value = notice.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(notice, ensure_ascii=False, sort_keys=True)[:500]
    return str(notice or "").strip()[:500]


def _platform_summary(settings: dict) -> list[str]:
    lines: list[str] = []
    banner = settings.get("banner") if isinstance(settings.get("banner"), dict) else {}
    value = banner.get("value") if isinstance(banner.get("value"), dict) else {}
    items = value.get("items") if isinstance(value.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        text = str(content.get("zh_CN") or content.get("en_US") or "").strip()
        if not text:
            continue
        text = re.sub(r"\s*\[([^\]]+)\]\(([^)]+)\)", r"\1", text).strip()
        lines.append(text[:1000])
    for key, label in (("notice_usage", "用量页公告"), ("notice_topup", "充值页公告")):
        entry = settings.get(key) if isinstance(settings.get(key), dict) else {}
        notices = entry.get("value") if isinstance(entry.get("value"), list) else []
        if not notices:
            continue
        # 只比条数会漏掉「同条数、正文 A→B」的计费公告变化；把正文也纳入
        # summary（条数只作辅助前缀），check_platform_banner 的差分才能命中。
        texts = [_notice_text(item) for item in notices]
        # 保留「N 条」作为辅助信息，同时把正文纳入 summary：同条数正文 A→B
        # 也能被 check_platform_banner 的差分命中（P2-58）。
        lines.append(f"{label}：{len(notices)} 条；" + "；".join(texts)[:1500])
    return lines


def check_platform_banner(state: dict, now: datetime) -> list[dict]:
    previous = state.get("platform_banner")
    if not isinstance(previous, dict):
        previous = {}
    # 显式 sentinel：区分「还没有基线」和「基线就是空公告」（P1-7）。
    previous_initialized = previous.get("initialized") is True or "summary" in previous
    did = str(previous.get("did") or "").strip() or str(uuid.uuid4())
    target = config.TARGETS["platform"]
    url = (
        f"{target['banner_api']}?did={urllib.parse.quote(did)}"
        f"&scope=banner"
    )
    try:
        body = fetch_json(url)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        biz = data.get("biz_data") if isinstance(data.get("biz_data"), dict) else {}
        settings = biz.get("settings") if isinstance(biz.get("settings"), dict) else {}
        if not settings:
            raise RuntimeError("公告接口没有 settings")
        summary = _platform_summary(settings)
    except Exception as exc:  # noqa: BLE001
        _source_fail(state, "platform", now, f"{type(exc).__name__}: {exc}")
        return []
    old_summary = previous.get("summary") if isinstance(previous.get("summary"), list) else []
    # 口径升级：旧状态的 summary 只有「N 条」，第一次换成带正文的新格式时
    # 只重建基线，不把格式变化当成公告变化发一条假事件。
    previous_summary_version = int(previous.get("summary_version") or 1)
    state["platform_banner"] = {
        "did": did,
        "summary": summary,
        "checked_at": iso(now),
        "initialized": True,
        "summary_version": 2,
    }
    _source_ok(state, "platform", now)
    if not previous_initialized:
        return []
    if previous_summary_version < 2 and old_summary:
        return []
    if old_summary == summary:
        return []
    added = [item for item in summary if item not in old_summary]
    removed = [item for item in old_summary if item not in summary]
    lines = []
    if added:
        lines.extend(f"新增公告：{item[:500]}" for item in added[:6])
    if removed:
        lines.extend(f"移除公告：{item[:500]}" for item in removed[:6])
    if not lines:
        lines.append("公告内容发生变化。")
    now_stamp = now_utc()
    event_key = _event_key(now_stamp)
    return [
        build_event(
            event_id=f"web:platform-banner:{event_key}",
            slug=f"web-platform-banner-{event_key}",
            source="platform",
            kind="platform_banner",
            level=2,
            title="Platform 公告更新",
            summary="；".join(lines),
            changed_at=iso(now_stamp),
            page_url="https://platform.deepseek.com/",
            impact="开放平台公告发生变化，可能涉及价格、活动、模型更新或服务政策。",
            action="打开 Platform 查看公告原文。",
            signal_count=max(1, len(added) + len(removed)),
            evidence=lines,
            payload={"added": added, "removed": removed},
        )
    ]


def _normalize_model_name(value: object) -> str:
    """模型名归一化：NFKC、去空格/连字符/下划线、小写（两侧必须用同一规则）。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s_\-]+", "", text).lower()


def _versions_look_incomplete(old_versions: dict, new_versions: dict,
                              page_text: str = "") -> bool:
    """判断本次解析是不是残页，避免把「少解析一个模型」误判成下架。

    除了「少一半以上」，还要核对缺失的模型名是否仍出现在页面正文里：
    如果模型仍在页面上、只是版本单元格解析失败，就按残页处理而不是下架。
    2→1 只有在页面里确实找不到该模型名时才算合法下架。
    """
    if not old_versions:
        return False
    if not new_versions:
        return True
    missing = [model for model in old_versions if model not in new_versions]
    if missing and page_text:
        compact = _normalize_model_name(page_text)
        still_listed = [
            model for model in missing if _normalize_model_name(model) in compact
        ]
        if still_listed:
            return True
    return len(new_versions) * 2 < len(old_versions)


def check_model_versions(
    state: dict, now: datetime, *, defer_source_ok: bool = False
) -> list[dict]:
    models_state = _models_state(state)
    old_versions = models_state.get("versions")
    if not isinstance(old_versions, dict):
        old_versions = {}
    try:
        response = fetch(config.MODEL_PRICING_URL, accept=ACCEPT_HTML)
        text = _validate_html_response(
            response, "价格页", must_contain=("deepseek",)
        )
        versions = parse_model_versions(text)
        if not versions:
            raise RuntimeError("价格页没有解析到模型版本")
        if any(not str(item.get("version") or "").strip() for item in versions):
            raise RuntimeError("价格页有模型版本为空，按残页处理，不下架任何模型")
        new_versions = {item["model"]: item["version"] for item in versions}
        try:
            # 用可见正文做「模型是否仍被列出」的判断，比在原始 HTML 里查更可靠。
            visible_text = str(extract_page(text, config.MODEL_PRICING_URL).get("text") or "")
        except Exception:  # noqa: BLE001 - 解析失败退回原始 HTML
            visible_text = text
        if _versions_look_incomplete(old_versions, new_versions, visible_text):
            raise RuntimeError(
                f"价格页解析结果可能不完整：旧基线 {len(old_versions)} 个模型，"
                f"本次只解析到 {len(new_versions)} 个"
            )
    except Exception as exc:  # noqa: BLE001
        _source_fail(state, "fingerprint", now, f"{type(exc).__name__}: {exc}")
        return []

    stamp_text = iso(now)
    versions_first_seen = models_state.get("versions_first_seen")
    if not isinstance(versions_first_seen, dict):
        versions_first_seen = {}
    for model in new_versions:
        versions_first_seen.setdefault(model, stamp_text)
    models_state["versions_first_seen"] = versions_first_seen
    added = [model for model in new_versions if model not in old_versions]
    removed = [model for model in old_versions if model not in new_versions]
    if old_versions and (added or removed):
        models_state["last_versions_change"] = {
            "at": stamp_text,
            "added": added,
            "removed": removed,
        }
    changed = [
        (model, str(old_versions[model]), str(new_versions[model]))
        for model in new_versions
        if model in old_versions and str(old_versions[model]) != str(new_versions[model])
    ]
    # 合并更新：只覆盖本次解析到的模型，再显式处理确认下架的模型；
    # 不整体替换，残页不会静默清空其它模型基线。
    merged = dict(old_versions)
    merged.update(new_versions)
    for model in removed:
        merged.pop(model, None)
    models_state["versions"] = merged
    models_state["versions_checked_at"] = stamp_text
    if not defer_source_ok:
        # 配置了 API key 时，价格页成功只是 fingerprint 的第一个子检查，
        # 由 check_api 统一记录本来源的最终成功/失败，避免失败 streak 被每轮清零。
        _source_ok(state, "fingerprint", now)
    if not old_versions:
        return []
    lines: list[str] = []
    if added:
        lines.append("新增模型：" + "、".join(added[:10]))
    if removed:
        lines.append("下架模型：" + "、".join(removed[:10]))
    lines.extend(f"{model}: {old} → {new}" for model, old, new in changed)
    if not lines:
        return []
    now_stamp = now_utc()
    event_key = _event_key(now_stamp)
    title = "价格页模型变化" if (added or removed) else "模型版本更新"
    return [
        build_event(
            event_id=f"fingerprint:versions:{event_key}",
            slug=f"fingerprint-versions-{event_key}",
            source="fingerprint",
            kind="model_version",
            level=3,
            title=title,
            summary="；".join(lines),
            changed_at=iso(now_stamp),
            page_url=config.MODEL_PRICING_URL,
            impact="官方价格页的模型版本行发生变化，通常意味着模型升级、上架或下架。",
            action="对照价格页和官方更新日志确认变化内容。",
            signal_count=len(lines),
            evidence=lines,
            payload={
                "versions": merged,
                "added": added,
                "removed": removed,
                "changed": [
                    {"model": model, "old": old, "new": new}
                    for model, old, new in changed
                ],
            },
        )
    ]


def _api_request(method: str, path: str, api_key: str, payload: dict | None = None) -> dict:
    global _paid_requests_this_round
    if path in (CHAT_PATH, FIM_PATH):
        _paid_requests_this_round += 1
        if _paid_requests_this_round > MAX_PAID_REQUESTS_PER_ROUND:
            raise RuntimeError(
                f"本轮付费探测请求数超过上限 {MAX_PAID_REQUESTS_PER_ROUND}，已停止"
            )
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    ips = _resolve_public(API_HOST)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "DeepSeekWatch/1.0",
        "Accept": "application/json",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    response = None
    raw = b""
    last_error: Exception | None = None
    # DNS 可能同时返回多个地址（包括不可达的 IPv6）。不能只试第一个，
    # 否则一个坏 IP 就会让整个 API 指纹检查失败。
    for ip in ips:
        connection = _PinnedHTTPSConnection(API_HOST, ip, API_TIMEOUT, context)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(300_000)
            break
        except Exception as exc:  # noqa: BLE001 - 换下一个已校验 IP
            last_error = exc
            response = None
            raw = b""
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
    if response is None:
        raise RuntimeError(
            f"API 地址全部连接失败（{len(ips)} 个）："
            f"{type(last_error).__name__ if last_error else 'unknown'}: {last_error}"
        )
    text = raw.decode("utf-8", "replace")
    if response.status != 200:
        raise ApiError(response.status, f"HTTP {response.status}: {redact(text[:300])}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ApiError(response.status, f"响应不是 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(response.status, "响应顶层不是对象")
    return data


def probe_catalog(api_key: str) -> list[str]:
    body = _api_request("GET", "/models", api_key)
    items = body.get("data")
    if not isinstance(items, list):
        raise RuntimeError("响应里没有 data 列表")
    ids: list[str] = []
    for item in items:
        model_id = item.get("id") if isinstance(item, dict) else item
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id.strip())
    if not ids:
        raise RuntimeError("模型清单为空")
    return sorted(dict.fromkeys(ids))


def _probe_model_chat(api_key: str, model: str) -> dict:
    """兼容旧路径：FIM 不可用时退回 Chat Completions。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    try:
        body = _api_request("POST", CHAT_PATH, api_key, payload)
    except ApiError as exc:
        if exc.status in (400, 422) and "thinking" in str(exc).lower():
            payload.pop("thinking", None)
            body = _api_request("POST", CHAT_PATH, api_key, payload)
        else:
            raise
    return body


def probe_model(api_key: str, model: str) -> dict:
    """读取 system_fingerprint。

    优先走 FIM Beta 最小请求：``prompt="hi"`` + ``max_tokens=1`` 实测为
    2 个 prompt token + 1 个 completion token，比 Chat Completions 的
    5+1 少一半；两个模型返回的指纹与 Chat 完全一致。FIM 不可用时回退
    到 Chat Completions，保证监控不因 Beta 接口波动而中断。
    """
    payload = {
        "model": model,
        "prompt": "hi",
        "max_tokens": 1,
        "stream": False,
    }
    try:
        body = _api_request("POST", FIM_PATH, api_key, payload)
    except ApiError as exc:
        # 404/405：Beta 路由不可用；400：模型不支持 FIM 或参数不被接受。
        if exc.status in (400, 404, 405):
            body = _probe_model_chat(api_key, model)
        else:
            raise
    fingerprint = body.get("system_fingerprint")
    if not fingerprint:
        raise RuntimeError("响应里没有 system_fingerprint 字段")
    return {
        "fingerprint": str(fingerprint),
        "model": str(body.get("model") or ""),
    }


def fp_readable(fingerprint: str) -> str:
    mapping = {
        hashlib.md5(item.encode("utf-8")).hexdigest(): item
        for item in config.FP_PLAINTEXTS
    }
    return mapping.get(str(fingerprint), "") or str(fingerprint)


def _catalog_looks_incomplete(old_catalog: list, new_catalog: list) -> bool:
    """模型目录残页保护：/models 部分响应（灰度/限流）不能当「下架」写回基线。"""
    if not old_catalog:
        return False
    if not new_catalog:
        return True
    old_set = {str(item) for item in old_catalog}
    new_set = {str(item) for item in new_catalog}
    if not (old_set & new_set):
        # 完全不含旧目录任一模型：更像解析/接口残页，不是整体替换。
        return True
    return len(new_set) * 2 < len(old_set)


def _apply_catalog_snapshot(
    models_state: dict, catalog: list[str], now: datetime
) -> list[dict]:
    """把一次 /models 查询结果写入状态；返回目录变化事件。"""
    old_catalog = models_state.get("catalog")
    if isinstance(old_catalog, list) and _catalog_looks_incomplete(old_catalog, catalog):
        # 8→2→8 场景：一次部分响应会把缺失模型记成下架、恢复再记新增，均为 3 级。
        # 这里保留旧基线并标记待核实，不产生事件、不推进 checked_at。
        models_state["catalog_last_error"] = (
            f"模型目录疑似残页：旧基线 {len(old_catalog)} 个，本次只返回 {len(catalog)} 个，已保留旧基线"
        )
        models_state["catalog_incomplete_streak"] = int(
            models_state.get("catalog_incomplete_streak") or 0
        ) + 1
        return []
    models_state["catalog_incomplete_streak"] = 0
    models_state["catalog"] = catalog
    stamp_text = iso(now)
    models_state["catalog_checked_at"] = stamp_text
    models_state["catalog_last_error"] = ""
    models_state["catalog_fail_streak"] = 0

    # 模型目录快照供网站展示：首次记录时间只在第一次看到该模型时写入。
    first_seen = models_state.get("catalog_first_seen")
    if not isinstance(first_seen, dict):
        first_seen = {}
    for item in catalog:
        first_seen.setdefault(item, stamp_text)
    models_state["catalog_first_seen"] = first_seen

    if not isinstance(old_catalog, list) or old_catalog == catalog:
        return []
    added = [item for item in catalog if item not in old_catalog]
    removed = [item for item in old_catalog if item not in catalog]
    models_state["last_catalog_change"] = {
        "at": stamp_text,
        "added": added,
        "removed": removed,
    }
    lines = []
    if added:
        lines.append("新增：" + "、".join(added[:10]))
    if removed:
        lines.append("移除：" + "、".join(removed[:10]))
    stamp = now_utc()
    event_key = _event_key(stamp)
    return [
        build_event(
            event_id=f"fingerprint:catalog:{event_key}",
            slug=f"fingerprint-catalog-{event_key}",
            source="fingerprint",
            kind="catalog",
            level=3,
            title="模型清单变化",
            summary="；".join(lines) or "模型清单变化",
            changed_at=iso(stamp),
            page_url=config.MODEL_PRICING_URL,
            impact="可用模型列表发生变化，可能影响可调用的模型或版本。",
            action="打开官方价格页 / Models 接口确认当前可用模型。",
            signal_count=len(added) + len(removed),
            evidence=lines,
            payload={"added": added, "removed": removed, "catalog": catalog},
        )
    ]


def check_catalog(
    state: dict, now: datetime, api_key: str, dry_run: bool = False
) -> list[dict]:
    """只刷新 /models 模型目录，不探测模型指纹。

    P2-19：失败不能再静默——打印脱敏错误，并记到 fingerprint 来源的健康度里，
    让 /status 的来源健康度和快照的 catalog_fail_streak 同时可见。成功时只清掉
    目录检查自己留下的失败，不刷新价格页/指纹子检查的连续失败。
    """
    if dry_run:
        return []
    models_state = _models_state(state)
    try:
        catalog = probe_catalog(api_key)
    except Exception as exc:  # noqa: BLE001
        models_state["catalog_last_error"] = redact(
            f"{type(exc).__name__}: {exc}"
        )[:500]
        models_state["catalog_fail_streak"] = int(
            models_state.get("catalog_fail_streak") or 0
        ) + 1
        _source_fail(state, "fingerprint", now, f"模型目录：{type(exc).__name__}: {exc}")
        print(f"[webwatch] 模型目录检查失败：{redact(exc)}", file=sys.stderr)
        return []
    events = _apply_catalog_snapshot(models_state, catalog, now)
    # 只有完整快照被接受时才清失败；残页保护仍属于异常，不能把失败洗白。
    if not int(models_state.get("catalog_incomplete_streak") or 0):
        _source_clear_catalog_error(state)
    return events


def check_api(state: dict, now: datetime, api_key: str, dry_run: bool = False) -> list[dict]:
    global _paid_requests_this_round
    _paid_requests_this_round = 0
    if dry_run:
        # --dry-run 不产生费用：直接跳过所有付费 API 探测。
        return []
    models_state = _models_state(state)
    events: list[dict] = []
    try:
        catalog = probe_catalog(api_key)
    except Exception as exc:  # noqa: BLE001
        _source_fail(state, "fingerprint", now, f"模型清单：{type(exc).__name__}: {exc}")
        return events

    events.extend(_apply_catalog_snapshot(models_state, catalog, now))

    # 探测哪些模型：优先价格页里的两个，再补充 /models 返回的其它模型，最多 8 个。
    versions = models_state.get("versions") if isinstance(models_state.get("versions"), dict) else {}
    probe_models = list(versions) + [item for item in catalog if item not in versions]
    probe_models = list(dict.fromkeys(probe_models))[:8]
    fingerprints = models_state.get("fingerprints")
    if not isinstance(fingerprints, dict):
        fingerprints = {}
        models_state["fingerprints"] = fingerprints
    failures = 0
    last_failure = ""
    for model in probe_models:
        try:
            result = probe_model(api_key, model)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            last_failure = f"{model} 指纹：{type(exc).__name__}: {exc}"
            continue
        previous = fingerprints.get(model) if isinstance(fingerprints.get(model), dict) else {}
        old_value = str(previous.get("value") or "")
        new_value = result["fingerprint"]
        fingerprints[model] = {
            "value": new_value,
            "checked_at": iso(now),
            "readable": fp_readable(new_value),
        }
        if old_value and old_value != new_value:
            stamp = now_utc()
            event_key = _event_key(stamp)
            old_readable = fp_readable(old_value)
            new_readable = fp_readable(new_value)
            events.append(
                build_event(
                    event_id=f"fingerprint:{model}:{event_key}",
                    slug=f"fingerprint-{model}-{event_key}",
                    source="fingerprint",
                    kind="fingerprint",
                    level=3,
                    title=f"{model} 模型指纹变化",
                    summary=f"{old_readable} → {new_readable}",
                    changed_at=iso(stamp),
                    page_url=config.MODEL_PRICING_URL,
                    impact="system_fingerprint 变化通常意味着后端部署或模型配置发生了变化。",
                    action="对照官方更新日志确认是否为预期升级。",
                    signal_count=1,
                    evidence=[f"{model}：{old_readable} → {new_readable}"],
                    payload={
                        "model": model,
                        "old_fingerprint": old_value,
                        "new_fingerprint": new_value,
                        "old_readable": old_readable,
                        "new_readable": new_readable,
                    },
                )
            )
    if failures:
        reason = (
            "全部模型指纹探测失败"
            if failures >= len(probe_models)
            else f"{failures}/{len(probe_models)} 个模型指纹探测失败"
        )
        if failures >= len(probe_models):
            _source_fail(state, "fingerprint", now, f"{reason}；{last_failure}")
        elif not _source_failed_this_round(state, "fingerprint", now):
            # 部分模型失败是局部问题，不应把整个 fingerprint 来源判 error；
            # 保留一条 warning 文本，状态仍按本轮成功处理。
            _source_ok(state, "fingerprint", now)
            entry = _ensure_sources(state).get("fingerprint")
            if isinstance(entry, dict):
                entry["last_error"] = f"{reason}；{last_failure}"
    elif not _source_failed_this_round(state, "fingerprint", now):
        _source_ok(state, "fingerprint", now)
    return events
