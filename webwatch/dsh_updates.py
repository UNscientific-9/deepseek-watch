"""DSH（DeepSeek Harness）软件更新监控。

监控三类官方分发信号：
1. GitHub release note（中文说明优先）；
2. GitHub master 提交（按轮次合并成一条「新增 N 个提交」预告）；
3. npm @deepseek-ai/dsh 的新版本，以及 DSH 桌面端 stable / nightly 更新清单。

用户口径（2026-11-22 确认）：
- 只认「新的版本号」；同一版本换通道（latest / next / alpha 等）不算变化。
- 旧分支补丁（例如 0.2.2-rc.6）也算新版本。
- GitHub / npm / 桌面端发现同一版本时只生成一条发版事件；历史版本首次接入时全部补录，
  补录事件级别为 1（不触发 QQ/邮件，也不在调级时重推）。
- 新产生的版本发版事件级别为 3（重要），包括等待 release note 后补发的版本事件。
- master 提交按每轮检测批次合并成一条，级别 1（只上网站/RSS），不单独发每个 commit。
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import quote

from . import config
from .events import build_event, iso, parse_iso
from .redact import redact
from .safe_fetch import ACCEPT_ANY, fetch, fetch_json

RELEASE_PREFIX = "dsh-v"
# GitHub API 单页上限 100。轮询也取满一页，避免 15 分钟内出现超过 20 个
# release/commit 时，第二页之后的新条目永远看不到。
RELEASE_PAGE_SIZE = 100
RELEASE_BACKFILL_PAGE_SIZE = 100
NPM_RELEASE_GRACE_SECONDS = 30 * 60
FULL_METADATA_INTERVAL_SECONDS = 6 * 3600
MAX_VERSIONS_SEEN = 1000
MAX_RELEASES_SEEN = 500
MAX_COMMITS_SEEN = 500
MAX_REPORTED_VERSIONS = 1000
MAX_DESKTOP_SEEN = 100
MAX_AWAITING_NOTES = 200
# DSH 的发版说明按用户口径「直接照搬更新日志」：不能只留前 20 条，也不能
# 按 500 字截断条目。实测最大的一个 release 中文区约 5.4 KB / 75 条，这里的
# 上限只是防御异常输入，正常不会触发。
MAX_NOTES = 500
MAX_NOTE_CHARS = 2000
MAX_NOTE_BLOCKS = 1000
RELEASE_COMMIT_RE = re.compile(r"^release\(dsh\):\s*(\S+)", re.I)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*•]\s+(.+)$")
_LINK_LINE_RE = re.compile(
    r"^\[[^\]]*\]\(#[^)]*\)(?:\s*\|\s*\[[^\]]*\]\(#[^)]*\))?$"
)
_HEADING_TAG_RE = re.compile(r"<h([1-6])([^>]*)>(.*?)</h\1>", re.I | re.S)
_ENGLISH_HEADING_RE = re.compile(r"(?im)^#{1,6}\s+[^\n]*(?:english|英文)\s*$")


def _npm_package_path() -> str:
    return quote(config.DSH_NPM_PACKAGE, safe="")


def _npm_metadata_url() -> str:
    return f"{config.DSH_NPM_REGISTRY}/{_npm_package_path()}"


def _npm_dist_tags_url() -> str:
    return f"{config.DSH_NPM_REGISTRY}/-/package/{_npm_package_path()}/dist-tags"


def _github_releases_url(page: int, per_page: int) -> str:
    return (
        f"{config.DSH_GITHUB_API}/repos/{config.DSH_GITHUB_REPO}/releases"
        f"?per_page={per_page}&page={page}"
    )


def _github_commits_url() -> str:
    return f"{config.DSH_GITHUB_API}/repos/{config.DSH_GITHUB_REPO}/commits?per_page=100"


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _cut_english_section(text: str) -> str:
    """截掉 release body 的英文区。

    兼容三种格式：
    - `<h3 id="en-v0.2.3">New Features</h3>`（当前格式，id 带 en-）
    - `<h2 id="english">0.1.3-alpha.2 · English</h2>`（旧格式）
    - `## English` / 独立一行 `English`（Markdown 格式）
    """
    for match in _HEADING_TAG_RE.finditer(text):
        attrs = match.group(2) or ""
        inner = html.unescape(re.sub(r"<[^>]+>", " ", match.group(3))).lower()
        id_match = re.search(r'\bid\s*=\s*["\']([^"\']*)["\']', attrs, re.I)
        heading_id = (id_match.group(1) if id_match else "").lower()
        if (
            re.match(r"^(?:en|english)(?:$|[-_])", heading_id)
            or "english" in heading_id
            or "english" in inner
            or "英文" in inner
        ):
            return text[: match.start()]
    marker = _ENGLISH_HEADING_RE.search(text)
    if marker:
        return text[: marker.start()]
    marker = re.search(r"(?im)^\s*English\s*$", text)
    if marker:
        return text[: marker.start()]
    return text


def _html_to_lines(text: str) -> str:
    """把 release body 的 HTML 片段转成带 Markdown 标题/列表标记的纯文本。"""
    def heading_repl(match: re.Match[str]) -> str:
        level = match.group(1)
        inner = html.unescape(re.sub(r"<[^>]+>", " ", match.group(3))).strip()
        return f"\n{'#' * int(level)} {_normalize_text(inner)}\n"

    text = _HEADING_TAG_RE.sub(heading_repl, text)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</(?:li|p|div|ul|ol|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _parse_release_note_text(text: str) -> dict:
    blocks: list[dict] = []
    notes: list[str] = []
    for raw_line in text.splitlines():
        line = _normalize_text(raw_line)
        if not line:
            continue
        heading = _MD_HEADING_RE.match(line)
        if heading:
            title = _normalize_text(heading.group(2))
            if not title:
                continue
            # 顶部「0.1.3-alpha.2 · 中文」只是版本区标题，不是变更分类；
            # 不能让它变成详情页里的一个空章节。
            if "· 中文" in title or title in ("中文", "Chinese"):
                continue
            blocks.append({"type": "heading", "text": title[:MAX_NOTE_CHARS]})
            continue
        if _LINK_LINE_RE.match(line) or re.fullmatch(r"中文\s*\|\s*English", line):
            continue
        if re.fullmatch(r"-{3,}", line) or line.lower().startswith("full changelog"):
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            item = bullet.group(1).strip()
            if item and len(item) >= 2:
                note = item[:MAX_NOTE_CHARS]
                blocks.append({"type": "bullet", "text": note})
                notes.append(note)
            continue
        # 没有列表标记的普通段落也照搬（有些 release 会在标题下先写一段说明）。
        # 少数 release 的英文总结段落排在英文标题之前，不能让它混进中文更新日志。
        if not any("\u4e00" <= char <= "\u9fff" for char in line):
            continue
        paragraph = re.sub(r"^>\s*", "", line)
        blocks.append({"type": "paragraph", "text": paragraph[:MAX_NOTE_CHARS]})
        if len(blocks) >= MAX_NOTE_BLOCKS:
            break
    return {"blocks": blocks[:MAX_NOTE_BLOCKS], "notes": notes[:MAX_NOTES]}


def parse_release_note(body: object) -> dict:
    """把 GitHub release body 解析成完整的中文更新日志。

    返回 ``{"blocks": [...], "notes": [...]}``：blocks 保留标题 / 条目 / 段落的
    原始顺序，供站点「直接照搬」渲染；notes 是扁平 bullet 列表，供兼容代码和
    搜索使用。英文区会整段丢弃，中文区分组标题会保留。
    """
    text = str(body or "")
    if not text.strip():
        return {"blocks": [], "notes": []}
    chinese = _cut_english_section(text)
    document = _parse_release_note_text(_html_to_lines(chinese))
    if document["blocks"]:
        return document
    # 没有中文区（或英文区标记缺失）时退回全文，避免整条说明丢失。
    return _parse_release_note_text(_html_to_lines(text))


def parse_release_notes(body: object) -> list[str]:
    """返回 release note 的中文 bullet 列表（兼容旧调用方）。"""
    document = parse_release_note(body)
    if document["notes"]:
        return document["notes"]
    # 没有 bullet 的旧 release：取第一段非标题文字。
    plain = " ".join(
        str(block.get("text") or "").strip()
        for block in document["blocks"]
        if block.get("type") == "paragraph"
    )
    plain = _normalize_text(plain)
    return [plain[:MAX_NOTE_CHARS]] if plain else []


def _fetch_releases_page(page: int, per_page: int) -> tuple[list[dict], int]:
    data = fetch_json(
        _github_releases_url(page, per_page),
        allowed_hosts=config.DSH_ALLOWED_HOSTS,
        expect=list,
    )
    releases: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not tag.startswith(RELEASE_PREFIX):
            continue
        version = tag[len(RELEASE_PREFIX) :].strip()
        if not version:
            continue
        note = parse_release_note(item.get("body"))
        releases.append(
            {
                "version": version,
                "tag": tag,
                "name": str(item.get("name") or "").strip(),
                "published_at": str(item.get("published_at") or item.get("created_at") or ""),
                "html_url": str(item.get("html_url") or "").strip(),
                "prerelease": bool(item.get("prerelease")),
                "notes": note["notes"],
                "note": note,
            }
        )
    return releases, len(data)


def _fetch_releases() -> list[dict]:
    releases, _raw_count = _fetch_releases_page(1, RELEASE_PAGE_SIZE)
    return releases


def _fetch_releases_all() -> list[dict]:
    """首次接入时拉全量 release，用于补录历史版本。"""
    collected: list[dict] = []
    page = 1
    while True:
        batch, raw_count = _fetch_releases_page(page, RELEASE_BACKFILL_PAGE_SIZE)
        collected.extend(batch)
        if raw_count < RELEASE_BACKFILL_PAGE_SIZE:
            break
        page += 1
    return collected


def _fetch_npm_metadata() -> tuple[list[str], dict[str, str]]:
    data = fetch_json(
        _npm_metadata_url(),
        allowed_hosts=config.DSH_ALLOWED_HOSTS,
    )
    versions = [str(item) for item in (data.get("versions") or {})]
    time_raw = data.get("time") or {}
    times = {
        version: str(time_raw.get(version) or "")
        for version in versions
    }
    return versions, times


def _fetch_npm_dist_tags() -> dict[str, str]:
    data = fetch_json(
        _npm_dist_tags_url(),
        allowed_hosts=config.DSH_ALLOWED_HOSTS,
    )
    return {
        str(key): str(value)
        for key, value in (data or {}).items()
        if str(value or "").strip()
    }


def _fetch_commits() -> list[dict]:
    """用 GitHub REST API 拉 master 最近提交（GitHub Atom 在部分云机房会断连）。"""
    data = fetch_json(
        _github_commits_url(),
        allowed_hosts=config.DSH_ALLOWED_HOSTS,
        expect=list,
    )
    commits: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            continue
        commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
        raw_message = str(commit.get("message") or "")
        first_line = raw_message.splitlines()[0] if raw_message else ""
        commits.append(
            {
                "id": sha,
                "sha": sha,
                "short": sha[:8],
                # 只取首行 subject；先 normalize 再 splitlines 会把整个多行正文
                # 压成一行，导致提交标题带上一大段 body。
                "title": _normalize_text(first_line)[:300],
                "updated": _normalize_text(
                    (commit.get("author") or {}).get("date")
                    if isinstance(commit.get("author"), dict)
                    else ""
                ),
                "url": str(item.get("html_url") or "").strip(),
            }
        )
    return commits


def _fetch_desktop_channels(errors: list[str] | None = None) -> dict[str, str]:
    """拉 stable / nightly 清单。

    不传 ``errors`` 时保持「任一通道失败就抛异常」（首次补录需要完整基线）；
    轮询传入列表时改为按通道隔离：一个通道坏了，另一个通道的版本仍能处理，
    同时把失败写进本轮错误列表。
    """
    channels: dict[str, str] = {}
    for channel in ("stable", "nightly"):
        try:
            response = fetch(
                f"{config.DSH_DESKTOP_FEED_BASE}/{channel}.yml",
                accept=ACCEPT_ANY,
                allowed_hosts=config.DSH_ALLOWED_HOSTS,
            )
            match = re.search(r"(?m)^version:\s*([^\s#]+)", response.text)
            if not match:
                raise RuntimeError(f"DSH 桌面端 {channel} 更新清单没有 version 字段")
            channels[channel] = match.group(1).strip()
        except Exception as exc:  # noqa: BLE001 - 通道之间互不阻断
            if errors is None:
                raise
            errors.append(f"桌面端 {channel}：{type(exc).__name__}: {exc}")
    return channels


def _dsh_state(state: dict) -> dict:
    section = state.get("dsh")
    if not isinstance(section, dict):
        section = {}
        state["dsh"] = section
    return section


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _version_entries(section: dict, key: str) -> dict[str, dict]:
    """把 state 里的版本字典归一化成 {version: {first_seen, signals, npm_time}}。"""
    raw = section.get(key)
    result: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return result
    for version_key, value in raw.items():
        version = str(version_key or "").strip()
        if not version:
            continue
        if isinstance(value, dict):
            result[version] = {
                "first_seen": str(value.get("first_seen") or ""),
                "signals": [str(item) for item in (value.get("signals") or [])],
                "npm_time": str(value.get("npm_time") or ""),
                "changed_at": str(value.get("changed_at") or ""),
                # 首次补录时等待 release note 的历史版本要记住 backfill 标记，
                # 否则跨轮后会按新版本 3 级补发，把历史补录静默口径破坏掉。
                "backfill": bool(value.get("backfill")),
            }
        else:
            result[version] = {
                "first_seen": str(value or ""),
                "signals": ["npm"],
                "npm_time": "",
                "changed_at": "",
                "backfill": False,
            }
    return result


def _pending_versions(section: dict) -> dict[str, dict]:
    return _version_entries(section, "pending_versions")


def _awaiting_notes(section: dict) -> dict[str, dict]:
    return _version_entries(section, "awaiting_notes")


def _cap_unique(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result[-limit:]


def _time_or_now(value: object, now: datetime) -> str:
    parsed = parse_iso(value)
    if parsed is None:
        return iso(now)
    now_utc = now.astimezone(timezone.utc)
    # 服务端拒绝未来超过 300 秒的 changed_ts；源时间漂移时宁可落到本轮时间。
    if parsed > now_utc:
        return iso(now_utc)
    return iso(parsed)


def _channels_for(version: str, desktop: dict[str, str]) -> list[str]:
    return [channel for channel, value in desktop.items() if value == version]


def _release_note_doc(release: dict | None) -> dict:
    """兼容两种 release 结构：真实抓取带 ``note`` 文档，测试/旧数据只带 ``notes``。"""
    note = (release or {}).get("note")
    if isinstance(note, dict) and (note.get("blocks") or note.get("notes")):
        return note
    return {"blocks": [], "notes": list((release or {}).get("notes") or [])}


def _release_notes(release: dict | None) -> list[str]:
    return list(_release_note_doc(release).get("notes") or [])


def _note_text(blocks: list[dict]) -> str:
    """把结构化更新日志还原成可搜索 / 可照搬的纯文本。"""
    lines: list[str] = []
    for block in blocks:
        kind = str(block.get("type") or "")
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        if kind == "heading":
            lines.append(f"## {text}")
        elif kind == "bullet":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _release_event(
    version: str,
    *,
    release: dict | None,
    npm_time: str,
    desktop_channels: list[str],
    signals: list[str],
    level: int,
    backfill: bool,
    now: datetime,
    changed_at_floor: datetime | None = None,
) -> dict:
    note_document = _release_note_doc(release)
    blocks = list(note_document.get("blocks") or [])
    notes = list(note_document.get("notes") or [])
    # 用户口径（2026-11-23）：DSH 更新不做省流，详情页直接照搬完整更新日志；
    # 没有 release note 时只记「发布」，不自己编摘要。列表页只显示条数提示。
    if notes:
        summary = f"官方更新日志共 {len(notes)} 条。"
    else:
        summary = ""
    changed_at = _time_or_now(
        (release or {}).get("published_at") or npm_time, now
    )
    if changed_at_floor is not None:
        parsed_changed = parse_iso(changed_at)
        # 服务端按 id upsert 时要求新 changed_ts >= 旧值；补发 release note 的
        # 时间若早于先发出的版本事件，会更新不进去，这里把时间抬到旧事件时间。
        if parsed_changed is not None and parsed_changed < changed_at_floor:
            changed_at = iso(changed_at_floor)
    tag = str((release or {}).get("tag") or f"{RELEASE_PREFIX}{version}")
    url = str((release or {}).get("html_url") or "").strip()
    if not url:
        if "npm" in signals:
            url = f"https://www.npmjs.com/package/{config.DSH_NPM_PACKAGE}/v/{version}"
        else:
            url = f"https://github.com/{config.DSH_GITHUB_REPO}/releases"
    title = f"DSH 新版本 v{version}"
    if backfill or not notes:
        title = f"DSH v{version} 发布"
    event = build_event(
        event_id=f"dsh:release:{version}",
        slug=f"dsh-release-{version}",
        source="harness",
        kind="dsh_release",
        level=level,
        title=title,
        summary=summary[:4000],
        changed_at=changed_at,
        page_url=url,
        impact=(
            "DeepSeek Harness（DSH）发布了新版本，可能包含新功能、体验优化和问题修复。"
            if notes
            else ""
        ),
        action=(
            "历史版本补录，仅用于回看。"
            if backfill
            else "打开 GitHub 发版说明查看完整变更；需要升级时按官方指引更新 DSH。"
        ),
        signal_count=max(1, len(notes)),
        evidence=notes[:20],
        payload={
            "version": version,
            "tag": tag,
            "published_at": changed_at,
            "npm_published_at": _time_or_now(npm_time, now) if npm_time else "",
            "desktop_channels": desktop_channels,
            "notes": notes[:MAX_NOTES],
            "release_note": {"blocks": blocks[:MAX_NOTE_BLOCKS]},
            "signals": signals,
            "backfill": bool(backfill),
            "what_changed": _note_text(blocks)[:20_000] if blocks else summary[:4000],
        },
    )
    if notes:
        event["changes_detail"] = [
            {
                "page": f"Release {tag}",
                "level": level,
                "bullets": notes[:8],
                "note": url,
                "diff": "",
            }
        ]
    return event


def _commit_event(commits: list[dict], *, now: datetime) -> dict | None:
    if not commits:
        return None
    ordered = list(commits)
    newest = ordered[0]
    oldest = ordered[-1]
    release_versions: list[str] = []
    for commit in ordered:
        match = RELEASE_COMMIT_RE.match(str(commit.get("title") or ""))
        if match:
            release_versions.append(match.group(1))
    title = f"DSH 仓库新增 {len(ordered)} 个提交"
    if release_versions:
        title = (
            f"DSH 仓库新增 {len(ordered)} 个提交"
            f"（含 v{release_versions[0]} 发版准备）"
        )
    lines = [
        f"{commit.get('short') or ''} {commit.get('title') or ''}".strip()[:300]
        for commit in ordered[:5]
    ]
    evidence = [
        f"{commit.get('short') or ''} {commit.get('title') or ''} {commit.get('url') or ''}".strip()[:1000]
        for commit in ordered[:20]
    ]
    changed_at = _time_or_now(newest.get("updated"), now)
    if len(ordered) == 1:
        page_url = str(newest.get("url") or "").strip()
    else:
        page_url = (
            f"https://github.com/{config.DSH_GITHUB_REPO}/compare/"
            f"{oldest.get('sha') or ''}...{newest.get('sha') or ''}"
        )
    return build_event(
        event_id=f"dsh:commits:{newest.get('sha') or newest.get('id')}",
        slug=f"dsh-commits-{newest.get('sha') or newest.get('id')}",
        source="harness",
        kind="dsh_commit",
        level=1,
        title=title,
        summary="；".join(lines),
        changed_at=changed_at,
        page_url=page_url,
        impact="DSH 仓库 master 出现新提交，通常说明正在准备下一个版本；正式发版后还会有一条版本事件。",
        action="打开 GitHub 查看提交详情；如果只是日常开发提交，可以等正式发版说明。",
        signal_count=len(ordered),
        evidence=evidence,
        payload={
            "count": len(ordered),
            "commits": [
                {
                    "sha": str(commit.get("sha") or ""),
                    "short": str(commit.get("short") or ""),
                    "title": str(commit.get("title") or "")[:300],
                    "updated": str(commit.get("updated") or ""),
                    "url": str(commit.get("url") or ""),
                }
                for commit in ordered[:20]
            ],
        },
    )


def _backfill(state: dict, now: datetime) -> tuple[list[dict], bool, str]:
    errors: list[str] = []
    versions, times = _fetch_npm_metadata()
    releases = _fetch_releases_all()
    # 空响应不能当成「历史版本就是 0 个」：一旦写入空基线，下一轮会把所有现存
    # 版本当成新版本补发 3 级事件。宁可本轮失败、下一轮重试。
    if not versions:
        return [], False, "NPM 元数据没有返回任何版本，拒绝建立空基线"
    if not releases:
        return [], False, "GitHub releases 没有返回任何版本，拒绝建立空基线"
    commits_ok = True
    try:
        commits = _fetch_commits()
    except Exception as exc:  # noqa: BLE001 - 提交基线与历史补录互不阻断
        commits = []
        commits_ok = False
        errors.append(f"GitHub commits：{type(exc).__name__}: {exc}")
    desktop = _fetch_desktop_channels()
    release_map = {item["version"]: item for item in releases}
    npm_versions = set(versions)
    all_versions = list(versions)
    for item in releases:
        if item["version"] not in npm_versions and item["version"] not in all_versions:
            all_versions.append(item["version"])
    for version in desktop.values():
        if version not in all_versions:
            all_versions.append(version)
    events: list[dict] = []
    reported: list[str] = []
    def sort_time(version: str) -> str:
        return times.get(version) or str((release_map.get(version) or {}).get("published_at") or "")

    for version in sorted(all_versions, key=lambda item: (sort_time(item), item)):
        release = release_map.get(version)
        channels = _channels_for(version, desktop)
        signals: list[str] = []
        if version in npm_versions:
            signals.append("npm")
        if release:
            signals.append("github_release")
        for channel in channels:
            signals.append(f"desktop:{channel}")
        events.append(
            _release_event(
                version,
                release=release,
                npm_time=times.get(version, ""),
                desktop_channels=channels,
                signals=signals,
                level=1,
                backfill=True,
                now=now,
            )
        )
        reported.append(version)
    notes_versions = [
        version
        for version in reported
        if _release_notes(release_map.get(version))
    ]
    awaiting: dict[str, dict] = {}
    for version in reported:
        release = release_map.get(version)
        if release is None or _release_notes(release):
            continue
        awaiting[version] = {
            "first_seen": iso(now),
            "signals": ["github_release"],
            "npm_time": times.get(version, ""),
            "changed_at": str(release.get("published_at") or ""),
            "backfill": True,
        }
    section = _dsh_state(state)
    section.update(
        {
            "initialized": True,
            "backfilled_at": iso(now),
            "versions_seen": _cap_unique(versions, MAX_VERSIONS_SEEN),
            "releases_seen": _cap_unique(
                [item["version"] for item in releases], MAX_RELEASES_SEEN
            ),
            "desktop_seen": _cap_unique(
                [f"{channel}:{version}" for channel, version in desktop.items()],
                MAX_DESKTOP_SEEN,
            ),
            "commits_seen": _cap_unique(
                [str(item.get("id") or "") for item in commits], MAX_COMMITS_SEEN
            ),
            "commits_initialized": commits_ok,
            "reported_versions": _cap_unique(reported, MAX_REPORTED_VERSIONS),
            "notes_versions": _cap_unique(notes_versions, MAX_REPORTED_VERSIONS),
            "pending_versions": {},
            "awaiting_notes": awaiting,
            "last_full_scan": iso(now),
            "updated_at": iso(now),
        }
    )
    return events, not errors, "；".join(errors)


def _poll(
    state: dict,
    now: datetime,
    known_changed_at: dict[str, str] | None = None,
) -> tuple[list[dict], bool, str]:
    section = _dsh_state(state)
    versions_seen = _string_list(section.get("versions_seen"))
    version_set = set(versions_seen)
    errors: list[str] = []
    try:
        dist_tags = _fetch_npm_dist_tags()
    except Exception as exc:  # noqa: BLE001 - 单个源失败不阻断其他源
        dist_tags = {}
        errors.append(f"NPM dist-tags：{type(exc).__name__}: {exc}")
    unseen_tag = [value for value in dist_tags.values() if value not in version_set]
    last_full = parse_iso(section.get("last_full_scan"))
    full_scan_due = (
        last_full is None
        or (now - last_full).total_seconds() >= FULL_METADATA_INTERVAL_SECONDS
    )
    full_scan_ok = False
    if unseen_tag or full_scan_due:
        try:
            versions, times = _fetch_npm_metadata()
            new_npm_versions = [
                version for version in versions if version not in version_set
            ]
            full_scan_ok = True
        except Exception as exc:  # noqa: BLE001
            versions = []
            times = {}
            new_npm_versions = []
            errors.append(f"NPM 元数据：{type(exc).__name__}: {exc}")
    else:
        versions = []
        times = {}
        new_npm_versions = []
    try:
        # 升级前的老状态没有 notes_versions，需要一次性补全历史 update log；
        # 这时翻全部分页，避免 release 超过 100 条时更老的版本永远补不到。
        releases = (
            _fetch_releases_all()
            if "notes_versions" not in section
            else _fetch_releases()
        )
    except Exception as exc:  # noqa: BLE001
        releases = []
        errors.append(f"GitHub release：{type(exc).__name__}: {exc}")
    commits_ok = True
    try:
        commits = _fetch_commits()
    except Exception as exc:  # noqa: BLE001
        commits = []
        commits_ok = False
        errors.append(f"GitHub commits：{type(exc).__name__}: {exc}")
    desktop_errors: list[str] = []
    try:
        desktop = _fetch_desktop_channels(errors=desktop_errors)
        errors.extend(desktop_errors)
    except Exception as exc:  # noqa: BLE001
        desktop = {}
        errors.append(f"桌面端更新清单：{type(exc).__name__}: {exc}")

    existing_releases = _string_list(section.get("releases_seen"))
    releases_seen = set(existing_releases)
    new_release_versions = [
        item["version"] for item in releases if item["version"] not in releases_seen
    ]
    existing_desktop = _string_list(section.get("desktop_seen"))
    desktop_seen = set(existing_desktop)
    new_desktop_versions = [
        version
        for channel, version in desktop.items()
        if f"{channel}:{version}" not in desktop_seen
    ]
    existing_reported = _string_list(section.get("reported_versions"))
    reported = set(existing_reported)
    notes_versions = set(_string_list(section.get("notes_versions")))
    pending = _pending_versions(section)
    awaiting = _awaiting_notes(section)
    commits_initialized = section.get("commits_initialized") is True

    candidates: list[str] = []
    for version in (
        new_npm_versions
        + new_release_versions
        + new_desktop_versions
        + list(pending)
    ):
        if version not in reported and version not in candidates:
            candidates.append(version)

    release_map = {item["version"]: item for item in releases}
    events: list[dict] = []
    existing_commits = _string_list(section.get("commits_seen"))
    commits_seen = set(existing_commits)
    if commits_ok and not commits_initialized:
        # 首次补录时 commit Atom 失败过：恢复后先把当前提交全部记为基线，
        # 不能把几十条旧提交当成新变化发一条大事件。
        existing_commits = [str(item.get("id") or "") for item in commits]
        commits_seen = set(existing_commits)
        commits_initialized = True
        new_commits: list[dict] = []
    else:
        new_commits = [
            commit
            for commit in commits
            if str(commit.get("id") or "") not in commits_seen
        ]
    commit_event = _commit_event(new_commits, now=now)
    if commit_event:
        events.append(commit_event)

    emitted_versions: set[str] = set()
    for version in candidates:
        if version in reported:
            pending.pop(version, None)
            continue
        signals: list[str] = []
        if version in new_npm_versions:
            signals.append("npm")
        if version in new_release_versions:
            signals.append("github_release")
        channels = _channels_for(version, desktop)
        for channel in channels:
            signals.append(f"desktop:{channel}")
        release = release_map.get(version)
        stored = pending.get(version)
        npm_time = times.get(version, "") or str((stored or {}).get("npm_time") or "")
        has_desktop_signal = any(str(item).startswith("desktop:") for item in signals)
        if release is None and not has_desktop_signal:
            if stored is None:
                pending[version] = {
                    "first_seen": iso(now),
                    "signals": signals or ["npm"],
                    # 宽限期内可能不再拉全量元数据，先把 npm 发布时间存下来，
                    # 否则 30 分钟后发事件时 changed_at 会变成「现在」。
                    "npm_time": npm_time,
                }
                continue
            if not signals:
                signals = list(stored.get("signals") or ["npm"])
            first_seen = parse_iso(stored.get("first_seen"))
            now_utc = now.astimezone(timezone.utc)
            if first_seen is not None and first_seen > now_utc:
                # 状态里的 first_seen 在未来（时钟回拨/手改状态）不能把版本无限期
                # 压住；从本轮重新计时，最多再等一个宽限期。
                first_seen = now_utc
                stored["first_seen"] = iso(now_utc)
            age = (now - first_seen).total_seconds() if first_seen else NPM_RELEASE_GRACE_SECONDS
            if age < NPM_RELEASE_GRACE_SECONDS:
                continue
        else:
            pending.pop(version, None)
        events.append(
            _release_event(
                version,
                release=release,
                npm_time=npm_time,
                desktop_channels=channels,
                signals=signals,
                # 新版本发版事件按用户 2026-11-22 口径统一为「重要」；
                # 历史补录仍由 _backfill() 固定 level=1，不能在这里放宽。
                level=3,
                backfill=False,
                now=now,
            )
        )
        emitted_versions.add(version)
        has_notes = bool(_release_notes(release))
        if release is None or not has_notes:
            # 发版说明晚于 30 分钟宽限期，或 GitHub release 先建了空壳：
            # 先发 3 级版本事件，但记住它；等说明补上后用同一 id 回填。
            awaiting[version] = {
                "first_seen": str((stored or {}).get("first_seen") or iso(now)),
                "signals": signals or ["npm"],
                "npm_time": npm_time,
                "changed_at": str(events[-1].get("changed_at") or ""),
                "backfill": False,
            }
            notes_versions.discard(version)
        else:
            awaiting.pop(version, None)
            notes_versions.add(version)
        pending.pop(version, None)

    # 之前因等待 release note 而先发过事件的版本：release 出现后补发同 id 更新，
    # 让站点 / RSS 拿到完整说明。服务端按 id upsert；QQ 已发过的 id 不会重复播报。
    for version in list(awaiting):
        if version in emitted_versions:
            continue
        release = release_map.get(version)
        if release is None or not _release_notes(release):
            continue
        stored = awaiting.get(version) or {}
        backfill = bool(stored.get("backfill"))
        signals = list(stored.get("signals") or ["npm"])
        if "github_release" not in signals:
            signals.append("github_release")
        channels = _channels_for(version, desktop)
        for channel in channels:
            marker = f"desktop:{channel}"
            if marker not in signals:
                signals.append(marker)
        events.append(
            _release_event(
                version,
                release=release,
                npm_time=str(stored.get("npm_time") or times.get(version, "")),
                desktop_channels=channels,
                signals=signals,
                # 首次补录时等待说明的历史版本保持 1 级；轮询新版本仍是 3 级。
                level=1 if backfill else 3,
                backfill=backfill,
                now=now,
                changed_at_floor=parse_iso(stored.get("changed_at")),
            )
        )
        emitted_versions.add(version)
        notes_versions.add(version)
        awaiting.pop(version, None)
        pending.pop(version, None)

    # 旧版本事件修复（2026-11-23 用户口径）：升级前发出的 DSH 发版事件只带了
    # 前 20 条说明，详情页还被当成「省流」只展示 2 条。这里按 notes_versions
    # 记一次账：只要版本已上报但从未带过完整更新日志，就用同一 id 重发一次，
    # 由服务端 upsert 回填完整 release note。以后新增版本会直接进 notes_versions，
    # 不会每轮重复。
    for release in releases:
        version = str(release.get("version") or "")
        if not version or version not in reported or version in emitted_versions:
            continue
        notes = _release_notes(release)
        if not notes:
            if version not in awaiting:
                awaiting[version] = {
                    "first_seen": iso(now),
                    "signals": ["github_release"],
                    "npm_time": times.get(version, ""),
                    "changed_at": str(release.get("published_at") or ""),
                    "backfill": False,
                }
            continue
        if version in notes_versions:
            continue
        channels = _channels_for(version, desktop)
        signals = ["github_release"] + [f"desktop:{channel}" for channel in channels]
        events.append(
            _release_event(
                version,
                release=release,
                npm_time=times.get(version, ""),
                desktop_channels=channels,
                signals=signals,
                # 这是历史版本补录的“补全说明”路径：必须和首次补录一样保持 1 级，
                # 不能因为补上了 release note 就重新升成 3 级（2026-11-25 实测根因）。
                level=1,
                backfill=True,
                now=now,
                changed_at_floor=parse_iso(
                    (known_changed_at or {}).get(f"dsh:release:{version}")
                ),
            )
        )
        emitted_versions.add(version)
        notes_versions.add(version)

    section.update(
        {
            "versions_seen": _cap_unique(
                versions_seen + versions, MAX_VERSIONS_SEEN
            ),
            "releases_seen": _cap_unique(
                existing_releases + [item["version"] for item in releases],
                MAX_RELEASES_SEEN,
            ),
            "desktop_seen": _cap_unique(
                existing_desktop
                + [f"{channel}:{version}" for channel, version in desktop.items()],
                MAX_DESKTOP_SEEN,
            ),
            "commits_seen": _cap_unique(
                existing_commits
                + [str(item.get("id") or "") for item in commits],
                MAX_COMMITS_SEEN,
            ),
            "commits_initialized": commits_initialized,
            "reported_versions": _cap_unique(
                existing_reported
                + [event["payload"]["version"] for event in events if event["kind"] == "dsh_release"],
                MAX_REPORTED_VERSIONS,
            ),
            "notes_versions": _cap_unique(
                list(notes_versions), MAX_REPORTED_VERSIONS
            ),
            "pending_versions": pending,
            "awaiting_notes": dict(list(awaiting.items())[-MAX_AWAITING_NOTES:]),
            "last_full_scan": (
                iso(now)
                if full_scan_ok
                else str(section.get("last_full_scan") or "")
            ),
            "updated_at": iso(now),
        }
    )
    return events, not errors, "；".join(errors)


def check_dsh_updates(
    state: dict,
    now: datetime,
    *,
    known_changed_at: dict[str, str] | None = None,
) -> tuple[list[dict], bool, str]:
    """检查 DSH 更新；返回 (事件列表, 是否成功, 错误文本)。

    成功时会直接修改 state 的 ``dsh`` 段（只改内存，落盘由 run.py 统一负责）。
    单个源失败时，成功源的基线仍会推进，失败源保留原基线并在下一轮重试。

    ``known_changed_at`` 是事件 id → 已有 changed_at 的映射。回填旧版本完整
    release note 时用它做 changed_ts 下限，避免服务端 upsert 因时间倒退被拒。
    """
    try:
        section = state.get("dsh") if isinstance(state.get("dsh"), dict) else {}
        if not section.get("initialized"):
            return _backfill(state, now)
        return _poll(state, now, known_changed_at=known_changed_at)
    except Exception as exc:  # noqa: BLE001 - 任何外部源失败都不能拖垮整轮巡检
        return [], False, f"{type(exc).__name__}: {redact(exc)}"
