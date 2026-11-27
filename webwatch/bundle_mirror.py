#!/usr/bin/env python3
"""前端全量资源快照与差异工具（离线可审计，可重复运行）。

设计目标（与 webwatch/frontend_evidence.py 的分工不同）：
- 巡检链路只保存「资源清单 + 变化证据」；本工具把页面入口、rspack 运行时映射里的
  全部 JS/CSS chunk、以及 HTML/CSS 直接引用的图片/字体/manifest 全部下载下来；
- 文件按内容 sha256 存成不可变 blob，同一内容只落一份；每个快照保存完整 manifest；
- 后续快照优先复用本地 blob（只下载新增 / 变化文件），diff 不再依赖 CDN 旧链接；
- 下载失败必须写进 missing，绝不能把「下载不到」当成「没有变化」。

复用约定：
- 所有网络请求走 webwatch.safe_fetch.fetch；本模块不直接使用 urllib / requests 抓外网。
- rspack 运行时映射解析、key 命名、CSS 常量拼 URL、文案 / API / i18n / CSS 规则 /
  代码片段提取全部复用 webwatch.frontend_runtime 的公开 API（运行时映射入口仍走
  webwatch.frontend_evidence.parse_rspack_maps），避免两套解析逻辑慢慢漂移。
- 本模块只读页面与静态资源，不执行下载到的 JS，不读任何凭据。

用法：
  python tools/frontend_bundle_mirror.py snapshot --target platform --root .tmp/frontend-bundles
  python tools/frontend_bundle_mirror.py diff --root .tmp/frontend-bundles --source platform
  python tools/frontend_bundle_mirror.py diff --root .tmp/frontend-bundles --source platform \
      --from 20261118T030000Z-abc12345 --to 20261118T040000Z-def67890 --out .tmp/diff.json
  python tools/frontend_bundle_mirror.py list --root .tmp/frontend-bundles --source platform
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from . import frontend_evidence as fe
from . import frontend_runtime as fre
from .config import TARGETS
from .extract import extract_page, sha256_bytes
from .safe_fetch import (
    ACCEPT_ANY,
    ACCEPT_HTML,
    FetchError,
    fetch,
    validate_url,
)

SCHEMA_VERSION = 1
DEFAULT_WORKERS = 4
MAX_FETCH_ATTEMPTS = 2  # 首次 + 最多 1 次重试；404/403 这类确定性失败不重试
MAX_PAGE_HTML_BYTES = 8 * 1024 * 1024
MAX_LIST_ITEMS = 200
MAX_CSS_RULE_ITEMS = 120
MAX_CODE_SNIPPET_ITEMS = 20
MAX_FILE_RECORDS = 500
MAX_CSS_EXPAND_ROUNDS = 3

ROLE_ORDER = {"entry": 0, "chunk": 1, "asset": 2}
JS_EXTS = {".js", ".mjs", ".cjs"}
CSS_EXTS = {".css"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif", ".bmp"}
FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
MANIFEST_EXTS = {".json", ".webmanifest", ".manifest"}
BLOB_EXTS = (
    JS_EXTS
    | CSS_EXTS
    | IMAGE_EXTS
    | FONT_EXTS
    | MANIFEST_EXTS
    | {".html", ".htm", ".map", ".txt", ".xml", ".bin", ".wasm", ".mp4", ".webm", ".mp3", ".pdf", ".csv", ".yaml", ".yml"}
)

FetchFunc = Callable[..., object]


class MirrorError(Exception):
    """工具自身的使用错误 / 无法继续的错误（不是单个资源下载失败）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_short(value: str, fallback: str = "unknown") -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "", str(value or ""))[:16]
    return text or fallback


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_bytes(path, body.encode("utf-8"))


def _relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# URL / 扩展名 / blob 路径
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _is_http_url(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme.lower() in ("http", "https")


def _is_allowed_url(url: str) -> bool:
    try:
        validate_url(url)
        return True
    except FetchError:
        return False


def _url_ext(url: str) -> str:
    name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    if "." not in name:
        return ".bin"
    ext = "." + name.rsplit(".", 1)[-1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", ext):
        return ".bin"
    return ext if ext in BLOB_EXTS else ".bin"


def _kind_for_ext(ext: str) -> str:
    if ext in JS_EXTS:
        return "js"
    if ext in CSS_EXTS:
        return "css"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in FONT_EXTS:
        return "font"
    if ext in MANIFEST_EXTS:
        return "manifest"
    if ext == ".map":
        return "map"
    if ext in (".html", ".htm"):
        return "html"
    return "other"


def _kind_for_url(url: str, fallback: str = "other") -> str:
    return _kind_for_ext(_url_ext(url)) or fallback


_HEX_HASH_RE = re.compile(r"(?:[.-])([0-9a-f]{8,})(?=\.[A-Za-z0-9]+$)", re.I)


def _is_immutable_url(url: str) -> bool:
    """URL 文件名是否带内容哈希。只有不可变 URL 才能直接复用本地缓存。"""
    name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    if _HEX_HASH_RE.search(name):
        return True
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for values in query.values():
        for value in values:
            if re.fullmatch(r"[0-9a-f]{8,}", value or "", re.I):
                return True
    return False


# ---------------------------------------------------------------------------
# HTML 入口解析
# ---------------------------------------------------------------------------


class _PageAssetParser(HTMLParser):
    """收集页面里的脚本 / 样式 / modulepreload / preload / 图片 / 字体 / manifest。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_href = ""
        self.refs: list[tuple[str, str, str]] = []  # (url, kind, role)
        self.style_blocks: list[str] = []
        self._in_style = False
        self._style_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        tag = tag.lower()
        if tag == "base":
            if not self.base_href and values.get("href"):
                self.base_href = values["href"].strip()
        elif tag == "script":
            src = values.get("src", "").strip()
            if src:
                self.refs.append((src, "js", "entry"))
        elif tag == "link":
            href = values.get("href", "").strip()
            if not href:
                return
            rel = set(values.get("rel", "").lower().split())
            as_value = values.get("as", "").lower().strip()
            if "stylesheet" in rel:
                self.refs.append((href, "css", "entry"))
            elif "modulepreload" in rel:
                self.refs.append((href, _kind_for_url(href, "js"), "entry"))
            elif "preload" in rel:
                kind = {"script": "js", "style": "css", "font": "font", "image": "image"}.get(as_value)
                self.refs.append((href, kind or _kind_for_url(href), "asset" if as_value in ("font", "image") else "entry"))
            elif "manifest" in rel:
                self.refs.append((href, "manifest", "asset"))
            elif "icon" in rel or "apple-touch-icon" in rel or "shortcut" in rel:
                self.refs.append((href, "image", "asset"))
        elif tag == "img":
            src = values.get("src", "").strip()
            if src:
                self.refs.append((src, "image", "asset"))
        elif tag in ("source", "audio"):
            src = values.get("src", "").strip()
            if src:
                self.refs.append((src, _kind_for_url(src), "asset"))
        elif tag == "video":
            poster = values.get("poster", "").strip()
            if poster:
                self.refs.append((poster, "image", "asset"))
            src = values.get("src", "").strip()
            if src:
                self.refs.append((src, _kind_for_url(src), "asset"))
        elif tag == "style":
            self._in_style = True
            self._style_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self._in_style:
            self._in_style = False
            text = "".join(self._style_parts).strip()
            if text:
                self.style_blocks.append(text)
            self._style_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_parts.append(data)


_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)""", re.I)
_CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?["']?([^"'\s);]+)""", re.I)


def _extract_css_refs(css_text: str, css_url: str) -> list[str]:
    """从 CSS 文本里取 url(...) 和 @import 引用，转绝对地址并去重。"""
    refs: list[str] = []
    seen: set[str] = set()
    for match in _CSS_URL_RE.finditer(css_text):
        value = next((group for group in match.groups() if group is not None), "")
        _append_ref(refs, seen, value, css_url)
    for match in _CSS_IMPORT_RE.finditer(css_text):
        _append_ref(refs, seen, match.group(1), css_url)
    return refs


def _append_ref(refs: list[str], seen: set[str], raw: str, base_url: str) -> None:
    value = (raw or "").strip().strip("\"'")
    if not value or value.startswith(("data:", "blob:", "javascript:", "mailto:", "#")):
        return
    if value.startswith("//"):
        value = "https:" + value
    absolute = _normalize_url(urllib.parse.urljoin(base_url, value))
    if not _is_http_url(absolute) or not _is_allowed_url(absolute):
        return
    if absolute not in seen:
        seen.add(absolute)
        refs.append(absolute)


def parse_page_assets(html_text: str, base_url: str) -> dict:
    """解析页面入口资源；返回 base / assets / inline_css。

    assets 每项：{"url","kind","role","name"}。role 为 entry（脚本/样式/预加载）
    或 asset（图片/字体/manifest）。所有 URL 已转绝对地址并做官方域名白名单过滤。
    """
    parser = _PageAssetParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - 异常 HTML 不应让整轮快照失败
        pass
    base = urllib.parse.urljoin(base_url, parser.base_href or base_url)
    assets: list[dict] = []
    by_url: dict[str, dict] = {}

    def add(raw: str, kind: str, role: str) -> None:
        value = (raw or "").strip()
        if not value or value.startswith(("data:", "blob:", "javascript:", "mailto:", "#")):
            return
        if value.startswith("//"):
            value = "https:" + value
        absolute = _normalize_url(urllib.parse.urljoin(base, value))
        if not _is_http_url(absolute) or not _is_allowed_url(absolute):
            return
        existing = by_url.get(absolute)
        if existing:
            # preload 先出现、script 后出现时，以真正的 entry 身份为准。
            if existing["role"] == "asset" and role == "entry":
                existing["role"] = "entry"
                existing["kind"] = kind if kind != "other" else existing["kind"]
            return
        item = {
            "url": absolute,
            "kind": kind,
            "role": role,
            "name": fre.logical_name(absolute),
        }
        by_url[absolute] = item
        assets.append(item)

    for raw, kind, role in parser.refs:
        add(raw, kind, role)
    inline_css = "\n".join(parser.style_blocks)
    for ref in _extract_css_refs(inline_css, base):
        add(ref, _kind_for_url(ref), "asset")
    return {"base": base, "assets": assets, "inline_css": inline_css}


# ---------------------------------------------------------------------------
# 清单条目
# ---------------------------------------------------------------------------


def _make_item(url: str, kind: str, role: str, name: str, chunk_id: str = "") -> dict:
    logical = name or fre.logical_name(url)
    key = f"chunk:{kind}:{logical}" if role == "chunk" else f"{kind}:{logical}"
    return {
        "key": key,
        "url": url,
        "kind": kind,
        "role": role,
        "name": logical,
        "chunk_id": str(chunk_id or ""),
        "hash": "",
        "size": 0,
        "blob": "",
        "status": "pending",
        "declared_hash": fre.filename_hash(url),
        "optional_probe": False,
        "content_type": "",
        "error_kind": "",
        "http_status": 0,
        "reason": "",
    }


def _chunk_item(url: str, kind: str, name: str, chunk_id: str) -> dict:
    item = _make_item(url, kind, "chunk", name, chunk_id)
    item["declared_hash"] = fre.filename_hash(url)
    return item


def build_chunk_items(main_js_url: str, maps: dict) -> list[dict]:
    """把 rspack `.u` / `.k` 映射展开成完整 chunk 清单。

    直接调用 frontend_runtime.chunk_urls / css_chunk_url，避免自己重复解析 runtime。
    JS 映射里带名字表时，key 用逻辑名（跨版本稳定）；没有名字表才退化为 chunk id。
    """
    items: list[dict] = []
    if not main_js_url or not isinstance(maps, dict):
        return items
    js_map = maps.get("js") if isinstance(maps.get("js"), dict) else None
    css_map = maps.get("css") if isinstance(maps.get("css"), dict) else None
    js_chunks: list[tuple[str, str]] = []

    if js_map and js_map.get("hashes"):
        raw = fre.chunk_urls(main_js_url, js_map, "js")
        for (chunk_id, _declared), entry in zip(js_map["hashes"].items(), raw):
            name = str(entry.get("name") or chunk_id)
            item = _chunk_item(str(entry["url"]), "js", name, str(chunk_id))
            item["declared_hash"] = str(entry.get("hash") or item["declared_hash"])
            items.append(item)
            js_chunks.append((str(chunk_id), name))

    if css_map and css_map.get("hashes"):
        raw = fre.chunk_urls(main_js_url, css_map, "css")
        for (chunk_id, _declared), entry in zip(css_map["hashes"].items(), raw):
            name = str(entry.get("name") or chunk_id)
            item = _chunk_item(str(entry["url"]), "css", name, str(chunk_id))
            item["declared_hash"] = str(entry.get("hash") or item["declared_hash"])
            items.append(item)
    elif css_map and css_map.get("constant"):
        # platform 的 `.k=e=>"static/"+e+".<常量>.css"`：每个 JS chunk 都可能带 CSS，
        # 按 name + 常量拼 URL；404 属于正常情况，由调用方记入 missing。
        constant = str(css_map.get("constant") or "")
        for chunk_id, name in js_chunks:
            url = fre.css_chunk_url(main_js_url, name, constant)
            if url:
                item = _chunk_item(url, "css", name, chunk_id)
                # 这是「按常量猜测 CSS 文件名」的探测资源：404 是正常候选落空，
                # 不能当成整包缺文件；summary 会单独计 css_probe_404。
                item["optional_probe"] = True
                items.append(item)
    return items


def dedupe_items(items: list[dict]) -> list[dict]:
    """按 URL 去重（同一 main.js 可能既是 script 又被 preload）；key 冲突时加稳定后缀。"""
    out: list[dict] = []
    by_url: dict[str, dict] = {}
    seen_keys: set[str] = set()
    for item in items:
        url = str(item.get("url") or "")
        if not url:
            continue
        if url in by_url:
            existing = by_url[url]
            if existing.get("role") == "asset" and item.get("role") == "entry":
                existing["role"] = "entry"
                existing["kind"] = item.get("kind", existing.get("kind"))
            continue
        base_key = str(item.get("key") or f"other:{url}")
        key = base_key
        suffix = 2
        while key in seen_keys:
            key = f"{base_key}#{suffix}"
            suffix += 1
        item["key"] = key
        seen_keys.add(key)
        by_url[url] = item
        out.append(item)
    return out


def _sort_items(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda item: (
            ROLE_ORDER.get(str(item.get("role") or ""), 9),
            str(item.get("kind") or ""),
            str(item.get("key") or ""),
        ),
    )


def _main_candidates(entries: list[dict]) -> list[str]:
    """挑出入口 JS；platform / chat / harness 都适用。"""
    out: list[str] = []
    for item in entries:
        if item.get("kind") != "js":
            continue
        url = str(item.get("url") or "")
        # 与 frontend_evidence._ENTRY_CANDIDATE_RE 保持一致，兼容 main/index/
        # app/runtime/framework/polyfills 入口，避免非 main 页面漏采 chunk。
        if re.search(r"/(?:main|index|app|runtime|framework|polyfills)(?:[.-]|$)", url, re.I):
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


@dataclass
class FetchOutcome:
    ok: bool
    url: str
    final_url: str = ""
    status: int = 0
    body: bytes = b""
    error_kind: str = ""
    reason: str = ""
    attempts: int = 0


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return b""


def _fetch_with_retry(url: str, accept: str, fetch_func: FetchFunc | None) -> FetchOutcome:
    """抓一个 URL；最多 1 次重试，404/403 等确定性 HTTP 错误不重试。"""
    func = fetch_func or fetch
    last = FetchOutcome(False, url)
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            response = func(url, accept=accept)
            if isinstance(response, (bytes, bytearray, str)):
                # 方便测试 / 其他调用方注入极简 fake：直接返回正文即视为 HTTP 200。
                status = 200
                body = _as_bytes(response)
                final_url = url
            elif isinstance(response, dict):
                status = int(response.get("status", 200) or 200)
                body = _as_bytes(response.get("body", b""))
                final_url = str(response.get("final_url") or url)
            else:
                status = int(getattr(response, "status", 200) or 200)
                body = _as_bytes(getattr(response, "body", b""))
                final_url = str(getattr(response, "final_url", "") or url)
            if status != 200:
                last = FetchOutcome(
                    False,
                    url,
                    final_url=final_url,
                    status=status,
                    error_kind="http",
                    reason=f"HTTP {status}",
                    attempts=attempt,
                )
                if status in (400, 401, 403, 404, 405, 410):
                    break
                continue
            if not body:
                last = FetchOutcome(
                    False,
                    url,
                    final_url=final_url,
                    status=status,
                    error_kind="empty",
                    reason="响应正文为空",
                    attempts=attempt,
                )
                continue
            return FetchOutcome(
                True,
                url,
                final_url=final_url,
                status=status,
                body=body,
                attempts=attempt,
            )
        except FetchError as exc:
            last = FetchOutcome(
                False,
                url,
                status=int(getattr(exc, "status", 0) or 0),
                error_kind=str(getattr(exc, "kind", "") or "fetch"),
                reason=str(getattr(exc, "reason", "") or exc)[:300],
                attempts=attempt,
            )
            if last.error_kind == "unsafe_url" or last.status in (400, 401, 403, 404, 405, 410):
                break
        except Exception as exc:  # noqa: BLE001 - 单个资源失败不阻塞整轮
            last = FetchOutcome(
                False,
                url,
                error_kind="exception",
                reason=f"{type(exc).__name__}: {exc}"[:300],
                attempts=attempt,
            )
    return last


# ---------------------------------------------------------------------------
# Blob 存储 / 本地索引
# ---------------------------------------------------------------------------


def _blob_path_for(root: Path, digest: str, ext: str) -> Path:
    return Path(root) / "blobs" / digest[:2] / f"{digest}{ext or '.bin'}"


def write_blob(root: Path, data: bytes, ext: str) -> tuple[str, bool]:
    """写内容寻址 blob；返回（相对 root 的路径, 是否新建）。

    先在 sha256 前缀目录里找同 hash 的已有文件，确保同一内容只存一份
    （即使 URL 扩展名不同也不重复占空间）。
    """
    digest = sha256_bytes(data)
    prefix = Path(root) / "blobs" / digest[:2]
    prefix.mkdir(parents=True, exist_ok=True)
    existing: Path | None = None
    for candidate in prefix.glob(f"{digest}.*"):
        if candidate.is_file():
            existing = candidate
            break
    if existing is not None:
        try:
            if existing.stat().st_size == len(data):
                # 同尺寸也要校验内容，否则损坏文件会被当成有效 blob 永久复用。
                if sha256_bytes(existing.read_bytes()) == digest:
                    return _relpath(existing, Path(root)), False
        except OSError:
            pass
    path = _blob_path_for(root, digest, ext)
    _atomic_write_bytes(path, data)
    return _relpath(path, Path(root)), True


def read_blob(root: Path, rel: str) -> bytes | None:
    if not rel:
        return None
    root_path = Path(root).resolve()
    try:
        path = (root_path / rel).resolve()
        path.relative_to(root_path)
    except (OSError, ValueError):
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def load_url_index(root: Path, source: str) -> dict[str, dict]:
    """扫描已有快照，建立 URL -> 本地 blob 的索引，供后续快照免下载复用。"""
    index: dict[str, dict] = {}
    snap_root = Path(root) / "snapshots" / source
    if not snap_root.is_dir():
        return index
    for manifest_path in sorted(snap_root.glob("*/manifest.json"), key=lambda path: path.parent.name):
        data = _read_json(manifest_path)
        for item in data.get("files") or []:
            if not isinstance(item, dict) or item.get("status") == "missing":
                continue
            url = str(item.get("url") or "")
            blob = str(item.get("blob") or "")
            digest = str(item.get("hash") or "")
            if not url or not blob or not digest:
                continue
            body = read_blob(root, blob)
            if body is None or sha256_bytes(body) != digest:
                continue
            index[url] = {
                "hash": digest,
                "size": len(body),
                "blob": blob,
                "kind": str(item.get("kind") or ""),
            }
    return index


def _reuse_from_index(root: Path, item: dict, url_index: dict[str, dict]) -> bool:
    if not _is_immutable_url(str(item.get("url") or "")):
        return False
    cached = url_index.get(str(item.get("url") or ""))
    if not cached:
        return False
    body = read_blob(root, str(cached.get("blob") or ""))
    if body is None or sha256_bytes(body) != str(cached.get("hash") or ""):
        return False
    item["hash"] = str(cached["hash"])
    item["size"] = len(body)
    item["blob"] = str(cached["blob"])
    item["status"] = "cached"
    item["optional_probe"] = False  # 历史上真实存在过，之后若 404 应算必需缺失
    return True


def _store_body(root: Path, item: dict, body: bytes, *, cached_hint: bool = False) -> None:
    ext = _url_ext(str(item.get("url") or ""))
    blob, created = write_blob(root, body, ext)
    item["hash"] = sha256_bytes(body)
    item["size"] = len(body)
    item["blob"] = blob
    item["status"] = "cached" if cached_hint else ("downloaded" if created else "deduped")
    item["optional_probe"] = False  # 已成功取到正文，说明不是候选落空
    item["error_kind"] = ""
    item["http_status"] = 200
    item["reason"] = ""


def _mark_missing(item: dict, outcome: FetchOutcome) -> None:
    item["hash"] = ""
    item["size"] = 0
    item["blob"] = ""
    item["status"] = "missing"
    item["error_kind"] = outcome.error_kind or "fetch"
    item["http_status"] = outcome.status or 0
    item["reason"] = outcome.reason[:300]


def download_items(
    root: Path,
    items: list[dict],
    *,
    fetch_func: FetchFunc | None,
    workers: int,
    url_index: dict[str, dict],
    prefetched: dict[str, dict] | None = None,
) -> list[dict]:
    """处理所有 pending 条目；返回其中失败（missing）的条目。"""
    prefetched = prefetched or {}
    pending: list[dict] = []
    for item in items:
        if item.get("status") != "pending":
            continue
        if _reuse_from_index(root, item, url_index):
            continue
        cached = prefetched.get(str(item.get("url") or ""))
        if cached is not None:
            _store_body(root, item, cached["body"], cached_hint=bool(cached.get("cached")))
            continue
        pending.append(item)

    def work(item: dict) -> tuple[dict, FetchOutcome]:
        return item, _fetch_with_retry(str(item["url"]), ACCEPT_ANY, fetch_func)

    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futures = [pool.submit(work, item) for item in pending]
            for future in concurrent.futures.as_completed(futures):
                item, outcome = future.result()
                if outcome.ok:
                    _store_body(root, item, outcome.body)
                else:
                    _mark_missing(item, outcome)

    return [item for item in items if item.get("status") == "missing"]


def _item_bytes(root: Path, item: dict | None) -> bytes | None:
    if not isinstance(item, dict) or item.get("status") == "missing":
        return None
    digest = str(item.get("hash") or "")
    body = read_blob(root, str(item.get("blob") or ""))
    if body is None:
        return None
    if digest and sha256_bytes(body) != digest:
        return None
    return body


def _item_text(root: Path, item: dict | None) -> str:
    body = _item_bytes(root, item)
    return body.decode("utf-8", "replace") if body else ""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def _short_commit(build_meta: dict, page_meta: dict, html_bytes: bytes) -> str:
    for value in (
        build_meta.get("commitId"),
        page_meta.get("commit-id"),
        page_meta.get("commit_id"),
    ):
        cleaned = _safe_short(str(value or ""))
        if cleaned and cleaned != "unknown":
            return cleaned
    return sha256_bytes(html_bytes)[:8]


def _snapshot_id(now: datetime, short: str) -> str:
    return f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{_safe_short(short)}"


def _load_main_js(
    candidates: list[str],
    *,
    root: Path,
    url_index: dict[str, dict],
    fetch_func: FetchFunc | None,
) -> tuple[str, str, dict, dict[str, dict], bool]:
    """抓 main.js 候选并解析运行时映射。

    返回 (main_url, main_text, maps, prefetched, any_fetched)。any_fetched=False 且
    candidates 非空表示 main.js 全部下载失败，属于关键失败。
    """
    prefetched: dict[str, dict] = {}
    first_ok: tuple[str, str, dict] | None = None
    for url in candidates[:10]:
        cached = url_index.get(url)
        body: bytes | None = None
        from_cache = False
        if cached and _is_immutable_url(url):
            candidate_body = read_blob(root, str(cached.get("blob") or ""))
            if candidate_body is not None and sha256_bytes(candidate_body) == str(cached.get("hash") or ""):
                body = candidate_body
                from_cache = True
        if body is None:
            outcome = _fetch_with_retry(url, ACCEPT_ANY, fetch_func)
            if not outcome.ok:
                continue
            body = outcome.body
        prefetched[url] = {"body": body, "cached": from_cache}
        text = body.decode("utf-8", "replace")
        maps = parse_rspack_maps(text)
        if first_ok is None:
            first_ok = (url, text, maps)
        if (maps.get("js") or maps.get("css") or "appVersion" in text):
            return url, text, maps, prefetched, True
    if first_ok is not None:
        return first_ok[0], first_ok[1], first_ok[2], prefetched, True
    return "", "", {"js": None, "css": None}, prefetched, False


def _normalize_exponent_keys(text: str) -> str:
    """把 rspack 压缩后对象里的 `1e3:"hash"` 展开成 `1000:"hash"`。

    这是 frontend_runtime._parse_pairs 的一个已知盲点：它的正则只认纯数字键，
    会把 `1e3:"..."` 误读成 `3:"..."`。这里只在对象键位置做无损归一化，
    后续仍交给 frontend_evidence.parse_rspack_maps 解析，不另写一套映射逻辑。
    """
    return re.sub(
        r"(?<=[{,])(\d+)e(\d+)(?=\s*:)",
        lambda match: str(int(match.group(1)) * (10 ** int(match.group(2)))),
        text,
        flags=re.I,
    )


def parse_rspack_maps(main_js_text: str) -> dict:
    """公开包装：完全复用 frontend_evidence.parse_rspack_maps，仅补压缩键归一化。"""
    return fe.parse_rspack_maps(_normalize_exponent_keys(main_js_text))


def create_snapshot(
    target: dict | str,
    root: Path,
    *,
    fetch_func: FetchFunc | None = None,
    workers: int = DEFAULT_WORKERS,
    now: datetime | None = None,
    snapshot_id: str | None = None,
) -> dict:
    """抓取一个目标的页面与全部前端资源，写入 root，返回结果字典。"""
    started = time.monotonic()
    now = now or _utc_now()
    root = Path(root)
    if isinstance(target, str):
        if target not in TARGETS:
            raise MirrorError(f"未知 target：{target}")
        target = dict(TARGETS[target])
    source = str(target.get("source") or target.get("label") or "page")
    page_url = str(target.get("url") or "")
    if not page_url:
        raise MirrorError("target 缺少 url")

    page_outcome = _fetch_with_retry(page_url, ACCEPT_HTML, fetch_func)
    if not page_outcome.ok:
        return {
            "exit_code": 1,
            "error": f"页面抓取失败：{page_outcome.reason}",
            "summary": {
                "source": source,
                "url": page_url,
                "error": page_outcome.reason,
                "error_kind": page_outcome.error_kind,
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        }

    html_bytes = page_outcome.body
    if len(html_bytes) > MAX_PAGE_HTML_BYTES:
        raise MirrorError("页面 HTML 超过单文件上限")
    html_text = html_bytes.decode("utf-8", "replace")
    final_url = page_outcome.final_url or page_url
    page_info = extract_page(html_text, final_url)
    parsed = parse_page_assets(html_text, final_url)

    url_index = load_url_index(root, source)

    entries = [
        _make_item(
            str(asset["url"]),
            str(asset["kind"]),
            str(asset["role"]),
            str(asset["name"]),
        )
        for asset in parsed["assets"]
    ]
    main_candidates = _main_candidates(entries)
    main_url, main_text, maps, prefetched, main_any_fetched = _load_main_js(
        main_candidates,
        root=root,
        url_index=url_index,
        fetch_func=fetch_func,
    )
    build_meta = fe.extract_build_meta(main_text) if main_text else {}
    engine = "rspack" if ((maps.get("js") or maps.get("css"))) else "html"

    items = list(entries)
    items.extend(build_chunk_items(main_url, maps))
    items = dedupe_items(items)

    download_items(
        root,
        items,
        fetch_func=fetch_func,
        workers=workers,
        url_index=url_index,
        prefetched=prefetched,
    )

    # CSS 里直接引用的字体 / 图片 / @import 也纳入全量清单。
    for _ in range(MAX_CSS_EXPAND_ROUNDS):
        known = {str(item.get("url") or "") for item in items}
        new_items: list[dict] = []
        for item in items:
            if item.get("kind") != "css" or item.get("status") == "missing":
                continue
            text = _item_text(root, item)
            if not text:
                continue
            for ref in _extract_css_refs(text, str(item["url"])):
                if ref in known:
                    continue
                known.add(ref)
                new_items.append(_make_item(ref, _kind_for_url(ref), "asset", fre.logical_name(ref)))
        if not new_items:
            break
        items.extend(new_items)
        download_items(
            root,
            items,
            fetch_func=fetch_func,
            workers=workers,
            url_index=url_index,
            prefetched=prefetched,
        )

    items = _sort_items(items)

    page_html_blob, _ = write_blob(root, html_bytes, ".html")
    page_text_bytes = page_info["text"].encode("utf-8", "replace")
    page_text_blob, _ = write_blob(root, page_text_bytes, ".txt")

    short = _short_commit(build_meta, page_info.get("meta") or {}, html_bytes)
    if snapshot_id:
        sid = re.sub(r"[^0-9A-Za-z._-]+", "", str(snapshot_id))[:80] or _snapshot_id(now, short)
    else:
        sid = _snapshot_id(now, short)
    snap_root = root / "snapshots" / source
    snap_dir = snap_root / sid
    suffix = 2
    while snap_dir.exists():
        snap_dir = snap_root / f"{sid}-{suffix}"
        suffix += 1
    snap_dir.mkdir(parents=True, exist_ok=True)

    missing = [item for item in items if item.get("status") == "missing"]
    optional_missing = [item for item in missing if item.get("optional_probe")]
    required_missing = [item for item in missing if not item.get("optional_probe")]
    css_probe_404 = [item for item in optional_missing if item.get("kind") == "css"]
    downloaded = sum(1 for item in items if item.get("status") == "downloaded")
    deduped = sum(1 for item in items if item.get("status") == "deduped")
    cached = sum(1 for item in items if item.get("status") == "cached")
    total_bytes = sum(int(item.get("size") or 0) for item in items if item.get("status") != "missing")
    duration = round(time.monotonic() - started, 3)
    critical_main_failed = bool(main_candidates) and not main_any_fetched
    complete = bool(main_candidates) and not critical_main_failed and not required_missing
    incomplete_reason = ""
    if not main_candidates:
        incomplete_reason = "未识别到主脚本入口"
    elif critical_main_failed:
        incomplete_reason = "主脚本 main.js 未取到"
    elif required_missing:
        incomplete_reason = f"必需资源缺失 {len(required_missing)} 个"

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "webwatch/bundle_mirror.py",
        "source": source,
        "target": str(target.get("label") or source),
        "label": str(target.get("label") or source),
        "snapshot_id": snap_dir.name,
        "url": page_url,
        "final_url": final_url,
        "fetched_at": _iso(now),
        "engine": engine,
        "complete": complete,
        "incomplete_reason": incomplete_reason,
        "build_meta": build_meta,
        "main_js_url": main_url,
        "main_css_url": next((str(item["url"]) for item in items if item.get("kind") == "css" and item.get("role") == "entry"), ""),
        "css_constant": str((maps.get("css") or {}).get("constant") or ""),
        "page": {
            "title": page_info.get("title", ""),
            "meta": page_info.get("meta") or {},
            "text_sha256": page_info.get("text_sha256", ""),
            "html_sha256": sha256_bytes(html_bytes),
            "html_size": len(html_bytes),
            "page_html": "page.html",
            "page_text": "page.txt",
            "page_html_blob": page_html_blob,
            "page_text_blob": page_text_blob,
        },
        "files": items,
        "manifest": items,  # 兼容 frontend_evidence 的命名习惯；files 是主字段
        "missing": [
            {
                "key": item.get("key"),
                "url": item.get("url"),
                "kind": item.get("kind"),
                "role": item.get("role"),
                "name": item.get("name"),
                "status": "missing",
                "optional_probe": bool(item.get("optional_probe")),
                "error_kind": item.get("error_kind"),
                "http_status": item.get("http_status"),
                "reason": item.get("reason"),
            }
            for item in missing
        ],
        "main_error": "" if main_any_fetched else "；".join(
            f"{url}: 未取到" for url in main_candidates
        ),
    }
    summary = {
        "source": source,
        "target": str(target.get("label") or source),
        "snapshot_id": snap_dir.name,
        "snapshot_dir": str(snap_dir),
        "url": page_url,
        "final_url": final_url,
        "fetched_at": _iso(now),
        "engine": engine,
        "build_meta": build_meta,
        "main_js_url": main_url,
        "main_css_url": manifest["main_css_url"],
        "page": {
            "title": manifest["page"]["title"],
            "html_sha256": manifest["page"]["html_sha256"],
            "text_sha256": manifest["page"]["text_sha256"],
            "html_size": manifest["page"]["html_size"],
        },
        "totals": {
            "files": len(items),
            "total": len(items),
            "actual_files": sum(1 for item in items if item.get("status") != "missing"),
            "candidate_files": len(optional_missing),
            "downloaded": downloaded + deduped,
            "downloaded_new_blob": downloaded,
            "deduped": deduped,
            "cached": cached,
            "missing": len(missing),
            "required_missing": len(required_missing),
            "optional_missing": len(optional_missing),
            "css_probe_404": len(css_probe_404),
            "bytes": total_bytes,
            "duration_seconds": duration,
        },
        "missing_sample": [
            {"url": item.get("url"), "reason": item.get("reason")} for item in required_missing[:10]
        ],
        "optional_missing_sample": [
            {"url": item.get("url"), "reason": item.get("reason")} for item in optional_missing[:10]
        ],
        "critical_main_failed": critical_main_failed,
        "complete": complete,
        "incomplete_reason": incomplete_reason,
        "exit_code": 0 if complete else 1,
    }
    manifest["totals"] = summary["totals"]

    _atomic_write_bytes(snap_dir / "page.html", html_bytes)
    _atomic_write_bytes(snap_dir / "page.txt", page_text_bytes)
    _atomic_write_json(snap_dir / "manifest.json", manifest)
    _atomic_write_json(snap_dir / "summary.json", summary)
    return {
        "exit_code": summary["exit_code"],
        "snapshot_dir": snap_dir,
        "manifest": manifest,
        "summary": summary,
    }


def snapshot(target: dict | str, root: Path, **kwargs) -> dict:
    """create_snapshot 的简短别名，便于其他工具 import 调用。"""
    return create_snapshot(target, root, **kwargs)


def format_snapshot_summary(summary: dict) -> str:
    if summary.get("error"):
        return f"前端快照失败：{summary.get('source', '')} — {summary['error']}"
    totals = summary.get("totals") or {}
    lines = [
        f"前端快照完成：{summary.get('source')}（{summary.get('target', '')}）",
        f"快照目录：{summary.get('snapshot_dir')}",
        (
            f"资源：清单项 {totals.get('files', 0)} 个（实际 {totals.get('actual_files', totals.get('files', 0))}，"
            f"CSS 候选 {totals.get('candidate_files', 0)}；下载 {totals.get('downloaded', 0)}"
            f"（新写入 {totals.get('downloaded_new_blob', 0)}，去重 {totals.get('deduped', 0)}），"
            f"缓存命中 {totals.get('cached', 0)}，"
            f"缺失 {totals.get('missing', 0)}：必需 {totals.get('required_missing', 0)}，"
            f"CSS 候选探测 404 {totals.get('css_probe_404', 0)}）"
        ),
        f"总字节：{int(totals.get('bytes', 0)):,}；耗时：{totals.get('duration_seconds', 0)}s",
        f"engine：{summary.get('engine')}；main.js：{summary.get('main_js_url') or '（未找到）'}",
        f"页面：{summary.get('page', {}).get('title', '')!r}；HTML sha256：{str(summary.get('page', {}).get('html_sha256', ''))[:16]}",
    ]
    if summary.get("critical_main_failed"):
        lines.append("关键失败：发现了 main.js 候选但全部下载失败。")
    missing_sample = summary.get("missing_sample") or []
    if missing_sample:
        lines.append(f"必需缺失示例（共 {totals.get('required_missing', 0)} 个，前 {len(missing_sample)} 个）：")
        for item in missing_sample:
            lines.append(f"  - {item.get('url')} — {item.get('reason')}")
    optional_sample = summary.get("optional_missing_sample") or []
    if optional_sample:
        lines.append(f"CSS 候选探测 404 示例（共 {totals.get('css_probe_404', 0)} 个，前 {len(optional_sample)} 个）：")
        for item in optional_sample[:3]:
            lines.append(f"  - {item.get('url')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def list_snapshots(root: Path, source: str) -> list[dict]:
    snap_root = Path(root) / "snapshots" / source
    if not snap_root.is_dir():
        return []
    out: list[dict] = []
    for directory in sorted(snap_root.iterdir(), key=lambda path: path.name):
        if not directory.is_dir():
            continue
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        if not manifest:
            continue
        out.append(
            {
                "snapshot_id": str(manifest.get("snapshot_id") or directory.name),
                "dir_name": directory.name,
                "dir": directory,
                "manifest": manifest,
            }
        )
    return out


def _find_snapshot(snapshots: list[dict], snapshot_id: str) -> dict:
    for snapshot in snapshots:
        if snapshot_id in (snapshot["snapshot_id"], snapshot["dir_name"]):
            return snapshot
    raise MirrorError(f"找不到快照：{snapshot_id}")


def _page_hash(manifest: dict, key: str) -> str:
    return str(((manifest.get("page") or {}).get(key)) or "")


def _extract_new_content(kind: str, text: str) -> dict:
    result: dict = {
        "cjk_added": [],
        "cjk_removed": [],
        "api_added": [],
        "api_removed": [],
        "i18n_added": [],
        "i18n_removed": [],
        "css_rules_added": [],
        "css_rules_removed": [],
        "code_snippets": [],
    }
    if kind == "js":
        result["cjk_added"] = sorted(fre.cjk_strings(text))
        result["api_added"] = sorted(fre.api_paths(text))
        result["i18n_added"] = sorted(fre.i18n_keys(text))
    elif kind == "css":
        result["css_rules_added"] = sorted(fre.css_rules(text))
    return result


def _extract_removed_content(kind: str, text: str) -> dict:
    result: dict = {
        "cjk_added": [],
        "cjk_removed": [],
        "api_added": [],
        "api_removed": [],
        "i18n_added": [],
        "i18n_removed": [],
        "css_rules_added": [],
        "css_rules_removed": [],
        "code_snippets": [],
    }
    if kind == "js":
        result["cjk_removed"] = sorted(fre.cjk_strings(text))
        result["api_removed"] = sorted(fre.api_paths(text))
        result["i18n_removed"] = sorted(fre.i18n_keys(text))
    elif kind == "css":
        result["css_rules_removed"] = sorted(fre.css_rules(text))
    return result


def _extract_changed_content(kind: str, old_text: str, new_text: str) -> dict:
    result: dict = {
        "cjk_added": [],
        "cjk_removed": [],
        "api_added": [],
        "api_removed": [],
        "i18n_added": [],
        "i18n_removed": [],
        "css_rules_added": [],
        "css_rules_removed": [],
        "code_snippets": [],
    }
    if kind == "js":
        result["cjk_added"] = sorted(fre.cjk_strings(new_text) - fre.cjk_strings(old_text))
        result["cjk_removed"] = sorted(fre.cjk_strings(old_text) - fre.cjk_strings(new_text))
        result["api_added"] = sorted(fre.api_paths(new_text) - fre.api_paths(old_text))
        result["api_removed"] = sorted(fre.api_paths(old_text) - fre.api_paths(new_text))
        result["i18n_added"] = sorted(fre.i18n_keys(new_text) - fre.i18n_keys(old_text))
        result["i18n_removed"] = sorted(fre.i18n_keys(old_text) - fre.i18n_keys(new_text))
        result["code_snippets"] = fre.code_diff_snippets(old_text, new_text)
    elif kind == "css":
        result["css_rules_added"] = sorted(fre.css_rules(new_text) - fre.css_rules(old_text))
        result["css_rules_removed"] = sorted(fre.css_rules(old_text) - fre.css_rules(new_text))
    return result


def _sets_for_text(kind: str, text: str) -> dict[str, set[str]]:
    """把一段正文归成集合，供 added/removed 全局差分使用。"""
    out: dict[str, set[str]] = {"cjk": set(), "api": set(), "i18n": set(), "css": set()}
    if kind == "js":
        out["cjk"] = fre.cjk_strings(text)
        out["api"] = fre.api_paths(text)
        out["i18n"] = fre.i18n_keys(text)
    elif kind == "css":
        out["css"] = fre.css_rules(text)
    return out


def _filter_analysis(analysis: dict, keep: dict[str, set[str]]) -> dict:
    """只保留全局差分后真正新增 / 真正删除的值，避免把公共内容算成变化。"""
    out = dict(analysis)
    out["cjk_added"] = [value for value in analysis.get("cjk_added") or [] if value in keep.get("cjk", set())]
    out["cjk_removed"] = [value for value in analysis.get("cjk_removed") or [] if value in keep.get("cjk_removed", set())]
    out["api_added"] = [value for value in analysis.get("api_added") or [] if value in keep.get("api", set())]
    out["api_removed"] = [value for value in analysis.get("api_removed") or [] if value in keep.get("api_removed", set())]
    out["i18n_added"] = [value for value in analysis.get("i18n_added") or [] if value in keep.get("i18n", set())]
    out["i18n_removed"] = [value for value in analysis.get("i18n_removed") or [] if value in keep.get("i18n_removed", set())]
    out["css_rules_added"] = [value for value in analysis.get("css_rules_added") or [] if value in keep.get("css", set())]
    out["css_rules_removed"] = [value for value in analysis.get("css_rules_removed") or [] if value in keep.get("css_removed", set())]
    return out


def _append_unique(target: list, values: list, cap: int = MAX_LIST_ITEMS) -> None:
    for value in values:
        text = str(value)
        if text not in target and len(target) < cap:
            target.append(text)


def _file_record(
    status: str,
    old_item: dict | None,
    new_item: dict | None,
    *,
    baseline_missing: bool = False,
    note: str = "",
    analysis: dict | None = None,
) -> dict:
    item = new_item or old_item or {}
    record = {
        "key": item.get("key", ""),
        "name": item.get("name", ""),
        "kind": item.get("kind", ""),
        "role": item.get("role", ""),
        "status": status,
        "old_url": (old_item or {}).get("url", ""),
        "new_url": (new_item or {}).get("url", ""),
        "old_hash": (old_item or {}).get("hash", ""),
        "new_hash": (new_item or {}).get("hash", ""),
        "old_status": (old_item or {}).get("status", ""),
        "new_status": (new_item or {}).get("status", ""),
        "optional_probe": bool(item.get("optional_probe")),
        "baseline_missing": bool(baseline_missing),
        "note": note,
    }
    if analysis:
        record.update(analysis)
    return record


def diff_snapshots(
    root: Path,
    source: str,
    *,
    from_id: str | None = None,
    to_id: str | None = None,
) -> dict:
    """按稳定 key 比较两个快照；默认取该 source 最近两个。"""
    snapshots = list_snapshots(root, source)
    if len(snapshots) < 2 and not (from_id and to_id):
        raise MirrorError(f"source={source} 的快照不足两个，无法 diff（当前 {len(snapshots)} 个）")
    old_snapshot = _find_snapshot(snapshots, from_id) if from_id else snapshots[-2]
    new_snapshot = _find_snapshot(snapshots, to_id) if to_id else snapshots[-1]
    old_manifest = old_snapshot["manifest"]
    new_manifest = new_snapshot["manifest"]
    old_files = [item for item in old_manifest.get("files") or [] if isinstance(item, dict)]
    new_files = [item for item in new_manifest.get("files") or [] if isinstance(item, dict)]
    old_map = {str(item.get("key") or ""): item for item in old_files if item.get("key")}
    new_map = {str(item.get("key") or ""): item for item in new_files if item.get("key")}

    added: list[dict] = []
    removed: list[dict] = []
    content_changed: list[tuple[dict, dict]] = []
    url_changed: list[tuple[dict, dict]] = []
    unchanged = 0
    missing_items: list[tuple[dict | None, dict | None]] = []

    def is_ok(item: dict | None) -> bool:
        return bool(item and item.get("hash") and item.get("status") != "missing")

    for key in sorted(set(old_map) | set(new_map)):
        old_item = old_map.get(key)
        new_item = new_map.get(key)
        if old_item is None:
            if is_ok(new_item):
                added.append(new_item or {})
            else:
                missing_items.append((None, new_item))
        elif new_item is None:
            if is_ok(old_item):
                removed.append(old_item)
            else:
                missing_items.append((old_item, None))
        elif not is_ok(old_item) or not is_ok(new_item):
            missing_items.append((old_item, new_item))
        elif str(old_item.get("hash")) != str(new_item.get("hash")):
            content_changed.append((old_item, new_item))
        elif str(old_item.get("url")) != str(new_item.get("url")):
            url_changed.append((old_item, new_item))
        else:
            unchanged += 1

    cjk_added: list[str] = []
    cjk_removed: list[str] = []
    api_added: list[str] = []
    api_removed: list[str] = []
    i18n_added: list[str] = []
    i18n_removed: list[str] = []
    css_rules_added: list[str] = []
    css_rules_removed: list[str] = []
    code_snippets: list[dict] = []
    file_records: list[dict] = []
    baseline_missing_files = 0

    def merge_analysis(record: dict, analysis: dict, file_name: str) -> None:
        _append_unique(cjk_added, analysis.get("cjk_added") or [])
        _append_unique(cjk_removed, analysis.get("cjk_removed") or [])
        _append_unique(api_added, analysis.get("api_added") or [])
        _append_unique(api_removed, analysis.get("api_removed") or [])
        _append_unique(i18n_added, analysis.get("i18n_added") or [])
        _append_unique(i18n_removed, analysis.get("i18n_removed") or [])
        _append_unique(css_rules_added, analysis.get("css_rules_added") or [], MAX_CSS_RULE_ITEMS)
        _append_unique(css_rules_removed, analysis.get("css_rules_removed") or [], MAX_CSS_RULE_ITEMS)
        for snippet in analysis.get("code_snippets") or []:
            if len(code_snippets) < MAX_CODE_SNIPPET_ITEMS:
                code_snippets.append({"file": file_name, "text": snippet})
        if record:
            record["code_snippets"] = list(analysis.get("code_snippets") or [])[:5]

    # rspack 重编号时常见「旧 chunk key 删除 + 新 chunk key 新增」。先把内容哈希完全
    # 相同的 added/removed 配成 renamed_same_content，避免把整块旧文案误报成新增。
    removed_by_content: dict[tuple[str, str], list[dict]] = {}
    for item in removed:
        removed_by_content.setdefault((str(item.get("kind") or ""), str(item.get("hash") or "")), []).append(item)
    renamed_pairs: list[tuple[dict, dict]] = []
    unmatched_added: list[dict] = []
    matched_removed: set[int] = set()
    for item in added:
        bucket = removed_by_content.get((str(item.get("kind") or ""), str(item.get("hash") or "")))
        if bucket:
            old_item = bucket.pop(0)
            renamed_pairs.append((old_item, item))
            matched_removed.add(id(old_item))
        else:
            unmatched_added.append(item)
    unmatched_removed = [item for item in removed if id(item) not in matched_removed]

    # 全局集合差分：按整个快照的 old_all / new_all 计算（含 unchanged 与
    # content_changed），only_added = new_all - old_all、only_removed = old_all - new_all。
    # 只从 added/removed 两堆算会把未变分片里的公共文案/API 误报成新增或删除。
    added_texts: dict[int, str] = {}
    removed_texts: dict[int, str] = {}
    old_all: dict[str, set[str]] = {"cjk": set(), "api": set(), "i18n": set(), "css": set()}
    new_all: dict[str, set[str]] = {"cjk": set(), "api": set(), "i18n": set(), "css": set()}
    old_unreadable = 0
    new_unreadable = 0

    def collect_snapshot_sets(files: list[dict], target: dict[str, set[str]]) -> int:
        unreadable = 0
        for item in files:
            if not is_ok(item):
                continue
            body = _item_bytes(root, item)
            if body is None:
                unreadable += 1
                continue
            text = body.decode("utf-8", "replace")
            sets = _sets_for_text(str(item.get("kind") or ""), text)
            for key in target:
                target[key].update(sets[key])
        return unreadable

    old_unreadable = collect_snapshot_sets(old_files, old_all)
    new_unreadable = collect_snapshot_sets(new_files, new_all)
    for item in unmatched_added:
        body = _item_bytes(root, item)
        if body is not None:
            added_texts[id(item)] = body.decode("utf-8", "replace")
    for item in unmatched_removed:
        body = _item_bytes(root, item)
        if body is not None:
            removed_texts[id(item)] = body.decode("utf-8", "replace")

    if old_unreadable:
        # 旧 blob 有缺失时无法知道旧快照全量内容，不能推断任何「新增」。
        only_added = {key: set() for key in new_all}
    else:
        only_added = {key: new_all[key] - old_all[key] for key in new_all}
    if new_unreadable:
        only_removed = {key: set() for key in old_all}
    else:
        only_removed = {key: old_all[key] - new_all[key] for key in old_all}
    _append_unique(cjk_added, sorted(only_added["cjk"]))
    _append_unique(cjk_removed, sorted(only_removed["cjk"]))
    _append_unique(api_added, sorted(only_added["api"]))
    _append_unique(api_removed, sorted(only_removed["api"]))
    _append_unique(i18n_added, sorted(only_added["i18n"]))
    _append_unique(i18n_removed, sorted(only_removed["i18n"]))
    _append_unique(css_rules_added, sorted(only_added["css"]), MAX_CSS_RULE_ITEMS)
    _append_unique(css_rules_removed, sorted(only_removed["css"]), MAX_CSS_RULE_ITEMS)
    filtered_keep = {
        "cjk": only_added["cjk"],
        "api": only_added["api"],
        "i18n": only_added["i18n"],
        "css": only_added["css"],
        "cjk_removed": only_removed["cjk"],
        "api_removed": only_removed["api"],
        "i18n_removed": only_removed["i18n"],
        "css_removed": only_removed["css"],
    }

    for old_item, new_item in renamed_pairs:
        file_records.append(
            _file_record(
                "renamed_same_content",
                old_item,
                new_item,
                note="新旧 key 不同但内容哈希完全一致（文件搬家/重编号），不产生文案差异",
            )
        )

    for item in unmatched_added:
        text = added_texts.get(id(item))
        if text is None:
            file_records.append(
                _file_record("added", None, item, note="新增文件，但本地 blob 不可读，未提取文案")
            )
            continue
        analysis = _filter_analysis(
            _extract_new_content(str(item.get("kind") or ""), text),
            filtered_keep,
        )
        record = _file_record("added", None, item, note="新增文件（旧快照无此 key）", analysis=analysis)
        merge_analysis(record, analysis, str(item.get("name") or ""))
        file_records.append(record)

    for item in unmatched_removed:
        text = removed_texts.get(id(item))
        if text is None:
            file_records.append(
                _file_record(
                    "removed",
                    item,
                    None,
                    baseline_missing=True,
                    note="删除文件，但旧 blob 不可读，未提取删除内容",
                )
            )
            continue
        analysis = _filter_analysis(
            _extract_removed_content(str(item.get("kind") or ""), text),
            filtered_keep,
        )
        record = _file_record("removed", item, None, note="删除文件（新快照无此 key）", analysis=analysis)
        merge_analysis(record, analysis, str(item.get("name") or ""))
        file_records.append(record)

    for old_item, new_item in content_changed:
        old_body = _item_bytes(root, old_item)
        new_body = _item_bytes(root, new_item)
        if old_body is None or new_body is None:
            record = _file_record(
                "content_changed",
                old_item,
                new_item,
                baseline_missing=True,
                note="旧 blob 缺失（或损坏），无法读取正文：只报告文件变化，不推断新增/删除内容",
            )
            file_records.append(record)
            continue
        kind = str(new_item.get("kind") or "")
        if kind not in ("js", "css"):
            record = _file_record("content_changed", old_item, new_item, note="非文本资源，只比较内容哈希")
            file_records.append(record)
            continue
        # content_changed 也走同一套全局集合过滤：新 chunk 里重复出现的公共文案
        # 不能被当成新增，搬走的文案也不能同时出现在 added/removed。
        analysis = _filter_analysis(
            _extract_changed_content(
                kind, old_body.decode("utf-8", "replace"), new_body.decode("utf-8", "replace")
            ),
            filtered_keep,
        )
        record = _file_record("content_changed", old_item, new_item, analysis=analysis)
        merge_analysis(record, analysis, str(new_item.get("name") or ""))
        file_records.append(record)

    for old_item, new_item in url_changed:
        file_records.append(
            _file_record("url_changed", old_item, new_item, note="内容哈希相同、URL 变化（通常是重新命名/重新上传）")
        )

    for old_item, new_item in missing_items:
        item = new_item or old_item or {}
        file_records.append(
            _file_record(
                "missing",
                old_item,
                new_item,
                baseline_missing=bool(
                    old_item
                    and not is_ok(old_item)
                    and not bool((new_item or old_item or {}).get("optional_probe"))
                ),
                note=(
                    "CSS 常量候选探测 404（正常情况，不是必需资源缺失）"
                    if bool((new_item or old_item or {}).get("optional_probe"))
                    else "资源缺失：无法读取正文，不能判定为没有变化"
                ),
            )
        )

    required_missing_items = [
        pair for pair in missing_items if not bool((pair[1] or pair[0] or {}).get("optional_probe"))
    ]
    optional_missing_items = [
        pair for pair in missing_items if bool((pair[1] or pair[0] or {}).get("optional_probe"))
    ]
    css_probe_missing = [
        pair
        for pair in optional_missing_items
        if str(((pair[1] or pair[0] or {}).get("kind") or "")) == "css"
    ]

    page_text_changed = _page_hash(old_manifest, "text_sha256") != _page_hash(new_manifest, "text_sha256")
    page_title_changed = _page_hash(old_manifest, "title") != _page_hash(new_manifest, "title")

    if len(file_records) > MAX_FILE_RECORDS:
        file_records = file_records[:MAX_FILE_RECORDS]
        files_truncated = True
    else:
        files_truncated = False

    baseline_missing_files = old_unreadable + new_unreadable
    totals = {
        "added": len(added),
        "removed": len(removed),
        "content_changed": len(content_changed),
        "url_changed": len(url_changed),
        "unchanged": unchanged,
        "missing": len(missing_items),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "from": {
            "snapshot_id": old_snapshot["snapshot_id"],
            "dir_name": old_snapshot["dir_name"],
            "fetched_at": old_manifest.get("fetched_at", ""),
            "build_meta": old_manifest.get("build_meta") or {},
        },
        "to": {
            "snapshot_id": new_snapshot["snapshot_id"],
            "dir_name": new_snapshot["dir_name"],
            "fetched_at": new_manifest.get("fetched_at", ""),
            "build_meta": new_manifest.get("build_meta") or {},
        },
        "totals": totals,
        "missing_breakdown": {
            "total": len(missing_items),
            "required": len(required_missing_items),
            "optional": len(optional_missing_items),
            "css_probe_404": len(css_probe_missing),
        },
        "renamed_same_content": len(renamed_pairs),
        "page_text_changed": page_text_changed,
        "page_title_changed": page_title_changed,
        "page": {
            "old_title": _page_hash(old_manifest, "title"),
            "new_title": _page_hash(new_manifest, "title"),
            "old_text_sha256": _page_hash(old_manifest, "text_sha256"),
            "new_text_sha256": _page_hash(new_manifest, "text_sha256"),
        },
        "cjk_added": cjk_added,
        "cjk_removed": cjk_removed,
        "api_added": api_added,
        "api_removed": api_removed,
        "i18n_added": i18n_added,
        "i18n_removed": i18n_removed,
        "css_rules_added": css_rules_added,
        "css_rules_removed": css_rules_removed,
        "code_snippets": code_snippets,
        "files": file_records,
        "files_truncated": files_truncated,
        "baseline_missing": baseline_missing_files > 0,
        "baseline_missing_files": baseline_missing_files,
        "notes": [
            "missing 表示该资源在某一侧下载/读取失败，不等于没有变化；optional_probe=true 的是 CSS 常量候选探测 404。",
            "chunk 没有名字表时 key 只能退化为 chunk id，rspack 整体重编号会表现为新增/删除；已按整个快照 old_all/new_all 做全局集合差分，并用 renamed_same_content 降低误报。",
        ],
    }


def diff(root: Path, source: str, **kwargs) -> dict:
    """diff_snapshots 的简短别名，便于其他工具 import 调用。"""
    return diff_snapshots(root, source, **kwargs)


def format_diff_summary(diff: dict) -> str:
    totals = diff.get("totals") or {}
    breakdown = diff.get("missing_breakdown") or {}
    lines = [
        f"前端资源差异：{diff.get('source')}（{diff.get('from', {}).get('snapshot_id')} → {diff.get('to', {}).get('snapshot_id')}）",
        (
            f"文件：新增 {totals.get('added', 0)}，删除 {totals.get('removed', 0)}，"
            f"内容变化 {totals.get('content_changed', 0)}，URL 变化 {totals.get('url_changed', 0)}，"
            f"未变 {totals.get('unchanged', 0)}，缺失 {totals.get('missing', 0)}"
            f"（必需 {breakdown.get('required', 0)}，CSS 候选探测 404 {breakdown.get('css_probe_404', 0)}），"
            f"重命名同内容 {diff.get('renamed_same_content', 0)}"
        ),
        f"页面文本：{'有变化' if diff.get('page_text_changed') else '无变化'}",
        (
            f"文案：新增 {len(diff.get('cjk_added') or [])} 条，删除 {len(diff.get('cjk_removed') or [])} 条；"
            f"API：新增 {len(diff.get('api_added') or [])} 个，删除 {len(diff.get('api_removed') or [])} 个；"
            f"i18n key：新增 {len(diff.get('i18n_added') or [])} 个，删除 {len(diff.get('i18n_removed') or [])} 个；"
            f"CSS 规则：新增 {len(diff.get('css_rules_added') or [])} 条，删除 {len(diff.get('css_rules_removed') or [])} 条"
        ),
    ]
    if diff.get("baseline_missing"):
        lines.append(f"基线缺失：{diff.get('baseline_missing_files', 0)} 个文件旧 blob 不可读，未推断新增/删除内容。")
    if diff.get("files_truncated"):
        lines.append("文件明细超过 500 条，已截断；totals 仍为全量计数。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeepSeek 官方页面前端全量资源快照与差异工具（只读，不执行下载到的 JS）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snap = subparsers.add_parser("snapshot", help="抓取一个目标的页面与全部前端资源")
    snap.add_argument("--target", required=True, choices=sorted(TARGETS))
    snap.add_argument("--root", default=".tmp/frontend-bundles", help="快照根目录")
    snap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发下载数（默认 4）")
    snap.add_argument("--snapshot-id", default="", help="手动指定快照 id（调试用）")

    diff = subparsers.add_parser("diff", help="比较两个快照（默认最近两个）")
    diff.add_argument("--root", default=".tmp/frontend-bundles")
    diff.add_argument("--source", required=True)
    diff.add_argument("--from", dest="from_id", default=None)
    diff.add_argument("--to", dest="to_id", default=None)
    diff.add_argument("--out", default="", help="把 JSON 写到文件；不填则输出到 stdout")

    list_cmd = subparsers.add_parser("list", help="列出某个 source 的快照")
    list_cmd.add_argument("--root", default=".tmp/frontend-bundles")
    list_cmd.add_argument("--source", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 非交互终端可能不支持
        pass
    args = _build_parser().parse_args(argv)

    if args.command == "snapshot":
        target = dict(TARGETS[args.target])
        result = create_snapshot(
            target,
            Path(args.root),
            workers=args.workers,
            snapshot_id=args.snapshot_id or None,
        )
        summary = result.get("summary") or {}
        print(format_snapshot_summary(summary))
        if summary.get("error"):
            print(f"错误类型：{summary.get('error_kind', '')}", file=sys.stderr)
        return int(result.get("exit_code", 1))

    if args.command == "diff":
        try:
            diff = diff_snapshots(
                Path(args.root),
                args.source,
                from_id=args.from_id or None,
                to_id=args.to_id or None,
            )
        except MirrorError as exc:
            print(f"差异失败：{exc}", file=sys.stderr)
            return 1
        if args.out:
            _atomic_write_json(Path(args.out), diff)
            print(format_diff_summary(diff))
            print(f"JSON 已写入：{args.out}")
        else:
            print(format_diff_summary(diff), file=sys.stderr)
            print(json.dumps(diff, ensure_ascii=False, indent=2))
        return 0

    if args.command == "list":
        snapshots = list_snapshots(Path(args.root), args.source)
        if not snapshots:
            print(f"没有找到 source={args.source} 的快照。")
            return 0
        for snapshot in snapshots:
            manifest = snapshot["manifest"]
            files = manifest.get("files") or []
            print(
                f"{snapshot['dir_name']}  {manifest.get('fetched_at', '')}  "
                f"files={len(files)}  engine={manifest.get('engine', '')}"
            )
        return 0

    print("未知命令", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
