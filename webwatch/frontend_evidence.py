"""前端资源清单与用户可见证据提取。

只做三件事：
1. 从页面 HTML + 主 JS 运行时映射里拿到全量资源清单（只解析，不下载全部）；
2. 新旧清单按逻辑 key 配对，判断哪些资源新增 / 移除 / 内容变化；
3. 只下载变化的主包与分片，提取新增接口、中文文案、CSS 变量、i18n key。

这里不调用模型、不执行下载到的 JS、不读任何凭据。所有网络请求都走 safe_fetch。
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from pathlib import Path

from .extract import sha256_bytes
from .frontend_runtime import (
    _KEY_RE,
    api_paths,
    chunk_urls,
    cjk_strings,
    code_diff_pieces,
    code_diff_snippets,
    css_chunk_url,
    css_rules,
    filename_hash,
    i18n_keys,
    i18n_values,
    logical_name,
    runtime_map,
)
from .safe_fetch import ACCEPT_ANY, fetch

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CHANGED_FILES = 160
MAX_SNIPPETS = 16
SNIPPET_CHARS = 360
MAX_FILE_DIFF_CHARS = 12_000
CACHE_MAX_BYTES = 100 * 1024 * 1024

_UPPER_RE = re.compile(r'"([A-Z][A-Z0-9_]{3,60})"')
_VAR_RE = re.compile(r"var\((--[A-Za-z0-9_-]+)")
_REF_RE = re.compile(r"\.([A-Za-z][A-Za-z0-9_]{1,60})\b")

# 兼容旧调用方：这些私有名历史上被 tools/ 与测试直接引用，实现已移到 frontend_runtime。
_chunk_urls = chunk_urls
_css_chunk_url = css_chunk_url
_logical_name = logical_name
_filename_hash = filename_hash
_cjk_strings = cjk_strings
_api_paths = api_paths
_i18n_keys = i18n_keys
_css_rules = css_rules
_code_diff_snippets = code_diff_snippets
_runtime_map = runtime_map

# 主脚本入口：main / index（可带 6 位以上哈希）。main-app / layout 这类 Next.js 分包不算。
_MAIN_ENTRY_RE = re.compile(
    r"/(?:main|index)(?:[.-][0-9a-f]{6,})?\.(?:js|mjs)(?:$|\?)", re.I
)
_ENTRY_CANDIDATE_RE = re.compile(r"/(?:main|index|app|runtime|framework|polyfills)(?:[.-]|$)", re.I)


def _cache_dir(state_dir: Path, source: str) -> Path:
    path = Path(state_dir) / "frontend-cache" / source
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(cache_dir: Path, url: str) -> Path:
    path = urllib.parse.urlsplit(url).path
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ".bin"
    if suffix not in (".js", ".css", ".json", ".txt", ".html"):
        suffix = ".bin"
    return cache_dir / f"{sha256_bytes(url.encode('utf-8'))[:24]}{suffix}"


def _read_cache(cache_dir: Path | None, url: str) -> bytes:
    if cache_dir is None:
        return b""
    try:
        return _cache_path(cache_dir, url).read_bytes()
    except OSError:
        return b""


def _write_cache(cache_dir: Path | None, url: str, body: bytes) -> None:
    if cache_dir is None:
        return
    try:
        _cache_path(cache_dir, url).write_bytes(body)
    except OSError:
        return


def _prune_cache(cache_dir: Path | None, max_bytes: int = CACHE_MAX_BYTES) -> None:
    if cache_dir is None or not cache_dir.is_dir():
        return
    files = sorted(
        (path for path in cache_dir.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        if total > max_bytes:
            try:
                path.unlink()
            except OSError:
                pass


def _fetch_bytes(
    url: str,
    cache_dir: Path | None = None,
    *,
    use_cache: bool = True,
    vault=None,
    use_vault: bool = True,
    vault_first: bool = True,
) -> bytes:
    """取资源正文；同 URL 已有缓存时优先用缓存（URL 带内容哈希，缓存可靠）。

    `vault` 是生产版本仓库（`BundleVault`）的可选只读视图：缓存被淘汰、CDN
    旧文件过期时，仍可从这里读到历史版本正文。`vault_first=False` 用于当前
    页面的实时正文：先请求 CDN，只有请求失败时才回退到历史正文，避免稳定
    URL（如无哈希的 main.js）被旧快照顶替。
    """
    if use_cache:
        cached = _read_cache(cache_dir, url)
        if cached:
            return cached
    if vault is not None and use_vault and vault_first:
        stored = vault.read(url)
        if stored:
            _write_cache(cache_dir, url, stored)
            return stored
    try:
        response = fetch(url, accept=ACCEPT_ANY)
    except Exception:
        if vault is not None and use_vault and not vault_first:
            stored = vault.read(url)
            if stored:
                _write_cache(cache_dir, url, stored)
                return stored
        raise
    body = response.body or b""
    if not body:
        if vault is not None and use_vault and not vault_first:
            stored = vault.read(url)
            if stored:
                _write_cache(cache_dir, url, stored)
                return stored
        raise RuntimeError(f"资源正文为空：{url[:200]}")
    if len(body) > MAX_FILE_BYTES:
        raise RuntimeError(f"资源超过单文件上限：{url[:200]}")
    _write_cache(cache_dir, url, body)
    return body


def parse_rspack_maps(main_js_text: str) -> dict:
    """返回 {"js": {...}, "css": {...}}；解析不到时对应项为 None。"""
    return {
        "js": runtime_map(main_js_text, r"\.u\s*=\s*e\s*=>", '.js"', key="u"),
        "css": runtime_map(main_js_text, r"\.k\s*=\s*e\s*=>", '.css"', key="k"),
    }


def extract_build_meta(main_js_text: str) -> dict:
    patterns = {
        "appVersion": r'\bappVersion:"([^"]{1,40})"',
        "clientVersion": r'\bclientVersion:"([^"]{1,40})"',
        "commitId": r'\bcommitId:"([^"]{1,40})"',
        "commit_datetime": r'\bcommit_datetime:"([^"]{1,60})"',
    }
    out: dict[str, str] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, main_js_text)
        if match:
            out[key] = match.group(1)
    return out


def _url_kind(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    if path.endswith(".css"):
        return "css"
    if path.endswith(".js") or path.endswith(".mjs"):
        return "js"
    return "other"


def _entry(url: str, *, role: str, name: str = "", digest: str = "") -> dict:
    return {
        "key": f"{_url_kind(url)}:{name or logical_name(url)}",
        "url": url,
        "hash": digest or filename_hash(url) or sha256_bytes(url.encode("utf-8"))[:16],
        "kind": _url_kind(url),
        "role": role,
        "name": name or logical_name(url),
    }


def _merge_runtime_maps(items: list[dict]) -> dict:
    """合并多个 main 的运行时映射；同一 chunk id 以先解析到的为准。"""
    merged: dict = {"js": None, "css": None}
    for maps in items:
        for ext in ("js", "css"):
            current = maps.get(ext)
            if not current:
                continue
            if merged[ext] is None:
                merged[ext] = {
                    "prefix": current.get("prefix") or "",
                    "names": dict(current.get("names") or {}),
                    "hashes": dict(current.get("hashes") or {}),
                    "constant": str(current.get("constant") or ""),
                    "object": bool(current.get("object")),
                }
                continue
            target = merged[ext]
            if not target.get("prefix") and current.get("prefix"):
                target["prefix"] = current["prefix"]
            for chunk_id, name in (current.get("names") or {}).items():
                target["names"].setdefault(chunk_id, name)
            for chunk_id, digest in (current.get("hashes") or {}).items():
                target["hashes"].setdefault(chunk_id, digest)
            if not target.get("constant"):
                target["constant"] = str(current.get("constant") or "")
    return merged


def _load_main_js(
    page: dict,
    cache_dir: Path | None,
) -> tuple[str, str, dict, dict]:
    """抓取主 JS 并解析；找不到就返回空，不阻塞页面监控。

    候选从「只认 main」扩展到 index/main/app/runtime/framework 等多入口；
    每个入口都尝试，合并多份运行时映射（双 main 时不再只展开第一个）。
    """
    scripts = list(page.get("scripts") or [])
    candidates = [url for url in scripts if _ENTRY_CANDIDATE_RE.search(url)]
    required_entry = any(_MAIN_ENTRY_RE.search(url) for url in scripts)
    parsed_maps: list[dict] = []
    texts: list[str] = []
    urls: list[str] = []
    errors: list[str] = []
    for url in candidates[:10]:
        try:
            body = _fetch_bytes(url, cache_dir)
            text = body.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - 单个资源失败不阻塞整轮
            errors.append(f"{url.rsplit('/', 1)[-1]}: {type(exc).__name__}")
            continue
        maps = parse_rspack_maps(text)
        if maps.get("js") or maps.get("css") or "appVersion" in text:
            parsed_maps.append(maps)
            texts.append(text)
            urls.append(url)
    merged = _merge_runtime_maps(parsed_maps)
    info = {
        "required_entry": required_entry,
        "candidates": len(candidates),
        "main_urls": urls[:5],
        "errors": errors[:5],
    }
    return (urls[0] if urls else ""), "\n".join(texts), merged, info


def _manifest_entries(page: dict) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for url in list(page.get("scripts") or []) + list(page.get("styles") or []):
        item = _entry(url, role="entry")
        if item["key"] not in seen:
            seen.add(item["key"])
            entries.append(item)
    return entries


def _manifest_shrunk(old_manifest: list, new_manifest: list) -> bool:
    """资源清单数量骤减（超过一半）视为快照不完整，不直接推断删除。"""
    old_count = len(old_manifest or [])
    new_count = len(new_manifest or [])
    return bool(old_count >= 4 and new_count * 2 < old_count)


def build_frontend_snapshot(
    target: dict,
    page: dict,
    previous: dict | None,
    state_dir: Path | None,
    *,
    vault=None,
) -> tuple[dict, dict | None]:
    """构造当前前端快照；有上一版时顺带产出证据包。"""
    source = str(target.get("source") or target.get("label") or "page")
    cache_dir = _cache_dir(state_dir, source) if state_dir is not None else None
    main_js_url, main_js_text, maps, main_info = _load_main_js(page, cache_dir)
    manifest = _manifest_entries(page)
    for ext in ("js", "css"):
        ext_maps = maps.get(ext)
        if ext == "css" and ext_maps and not ext_maps.get("names") and maps.get("js"):
            # 对象式 `.k` 只有 hash 表，chunk 名回退用 JS 的名字表。
            ext_maps = {
                **ext_maps,
                "names": maps["js"].get("names") or {},
                "prefix": ext_maps.get("prefix") or maps["js"].get("prefix") or "",
            }
        if ext_maps:
            manifest.extend(chunk_urls(main_js_url, ext_maps, ext))
    # 去重（同一 main.js 可能既在 HTML 里又被运行时引用）
    dedup: dict[str, dict] = {}
    for item in manifest:
        dedup.setdefault(item["key"], item)
    manifest = list(dedup.values())
    main_css_url = str((page.get("styles") or [""])[0] or "")
    try:
        if main_css_url:
            _fetch_bytes(main_css_url, cache_dir)
    except Exception:  # noqa: BLE001 - CSS 抓取失败由证据层标 baseline_missing
        pass
    build_meta = extract_build_meta(main_js_text) if main_js_text else {}
    previous_front = previous.get("frontend") if isinstance(previous, dict) else None
    health = "ok"
    health_reason = ""
    if main_info["required_entry"] and not (maps.get("js") or maps.get("css")):
        health = "degraded"
        health_reason = "主脚本抓取失败或运行时映射为空"
    if isinstance(previous_front, dict) and str(previous_front.get("health") or "ok") == "ok":
        if _manifest_shrunk(previous_front.get("manifest") or [], manifest):
            health = "degraded"
            health_reason = (health_reason + "；" if health_reason else "") + (
                "资源清单数量骤减，按快照不完整处理"
            )
    snapshot = {
        "engine": "rspack" if (maps.get("js") or maps.get("css")) else "html",
        "manifest": manifest,
        "main_js_url": main_js_url,
        "main_js_urls": main_info["main_urls"],
        "main_css_url": main_css_url,
        "css_constant": str((maps.get("css") or {}).get("constant") or ""),
        "css_prefix": str((maps.get("css") or {}).get("prefix") or "")
        or str((maps.get("js") or {}).get("prefix") or ""),
        "build_meta": build_meta,
        "manifest_complete": not (main_info["required_entry"] and not (maps.get("js") or maps.get("css"))),
        "health": health,
        "health_reason": health_reason,
    }
    evidence = build_evidence(
        previous_front,
        snapshot,
        cache_dir=cache_dir,
        source=source,
        label=str(target.get("label") or source),
        old_commit=str((previous or {}).get("commit_id") or ""),
        new_commit=str((page.get("meta") or {}).get("commit-id") or ""),
        vault=vault,
    )
    _prune_cache(cache_dir)
    return snapshot, evidence


def _upper_tokens(text: str) -> set[str]:
    return set(_UPPER_RE.findall(text))


def _snippets(text: str, needle: str, *, limit: int = 2, width: int = SNIPPET_CHARS) -> list[str]:
    """取一个关键词的多个上下文；只看前几个，避免把整包代码塞给模型。"""
    out: list[str] = []
    start = 0
    while len(out) < limit:
        index = text.find(needle, start)
        if index < 0:
            break
        begin = max(0, index - width // 3)
        snippet = "…" + text[begin : begin + width] + "…"
        if snippet not in out:
            out.append(snippet)
        start = index + max(1, len(needle))
    return out


_BUILD_META_TEXT_RE = re.compile(
    r'\b(?:appVersion|clientVersion|commitId|commit_id|commit_datetime)\s*:\s*"[^"]*"'
)


def _strip_build_meta(text: str) -> str:
    """抹掉版本号/构建时间，避免「只重新打包」被当成代码内容变化。"""
    return _BUILD_META_TEXT_RE.sub("", text or "")


def main_code_snippets(old_text: str, new_text: str, *, limit: int = 3) -> list[str]:
    """从主脚本（entry）里提取真实代码差异片段。

    旧逻辑只给 changed chunk 做 `code_diff_snippets`，主包 main.js 变化时
    完全没有代码证据，模型只能看到「文件换名」而给出「内部调整」。这里先
    去掉构建元数据，再按语句片段做差集；只重新打包时通常不会留下片段。
    """
    if not old_text or not new_text:
        return []
    return code_diff_snippets(
        _strip_build_meta(old_text),
        _strip_build_meta(new_text),
        limit=limit,
        width=320,
    )


_REF_BOUNDARY = r"(?<![A-Za-z0-9_$.])"
_PACK_BIND_RE = re.compile(
    _REF_BOUNDARY + r"([A-Za-z_$][\w$]*)\s*=\s*(?:Object\.freeze\(\s*)?\{"
)
_STRING_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`')


def _mask_strings(text: str) -> str:
    """把字符串内容换成空格，保留引号和长度，供括号配对和引用计数使用。

    模板字面量整体按字符串处理：宁可少报模板里的引用，也不把文案当代码。
    """

    def blank(match: re.Match[str]) -> str:
        value = match.group(0)
        return value[0] + " " * (len(value) - 2) + value[-1]

    return _STRING_RE.sub(blank, text)


def _pack_bindings(
    text: str, keys: set[str], *, masked: str | None = None
) -> dict[str, set[str]]:
    """找出每个 key 所属语言包对象的绑定变量。

    用括号配对确定真正的所属对象：字符串里的 `}`、嵌套命名空间都不会
    干扰；只认完整的 key 定义，`subtitle:"..."` 不会再绑定 `title`。
    同一个 key 可能既出现在语言包里、又出现在业务对象里；语言包的特征
    是一个对象里有大量 key，因此只保留「绑定 key 数最多」的那个变量，
    数量相同时全部保留。
    """
    text = text or ""
    if not text or not keys:
        return {}
    if masked is None:
        masked = _mask_strings(text)
    brace_names = {
        match.end() - 1: match.group(1)
        for match in _PACK_BIND_RE.finditer(masked)
    }
    hits = [
        (match.start(), match.group(1))
        for match in _KEY_RE.finditer(text)
        if match.group(1) in keys and not masked[match.start()].isspace()
    ]
    stack: list[str] = []
    key_vars: dict[str, set[str]] = {}
    hit = 0
    for position, char in enumerate(masked):
        while hit < len(hits) and hits[hit][0] == position:
            name = next((item for item in reversed(stack) if item), "")
            if name:
                key_vars.setdefault(hits[hit][1], set()).add(name)
            hit += 1
        if char == "{":
            stack.append(brace_names.get(position, ""))
        elif char == "}":
            if stack:
                stack.pop()
    sizes: Counter = Counter()
    for names in key_vars.values():
        sizes.update(names)
    bound: dict[str, set[str]] = {}
    for key, names in key_vars.items():
        most = max(sizes[name] for name in names)
        bound[key] = {name for name in names if sizes[name] == most}
    return bound


def _scoped_refs(
    text: str,
    bindings: dict[str, set[str]],
    keys: set[str],
    *,
    masked: str | None = None,
) -> tuple[Counter, Counter]:
    """统计绑定变量上的属性访问；支持 a.key、a?.key、a.ns.key、a["key"]。

    `masked` 是屏蔽字符串内容后的同长文本，用来跳过字符串里的假引用；
    方括号里的 key 本身就在字符串中，匹配仍用原始 `text`，不能先屏蔽。
    返回 (全部绑定引用, 其中方括号引用)；方括号引用不在通用 `.key` 计数里，
    单独返回，供“全量引用”作兜底校验。
    """
    counts: Counter = Counter()
    brackets: Counter = Counter()
    names = sorted(
        {name for values in bindings.values() for name in values},
        key=len,
        reverse=True,
    )
    if not names:
        return counts, brackets
    prefix = _REF_BOUNDARY + "(?:" + "|".join(re.escape(name) for name in names) + ")"
    for match in re.finditer(
        prefix
        + r"(?:\?\.|\.)([A-Za-z][A-Za-z0-9_]{1,60})"
        + r"((?:(?:\?\.|\.)[A-Za-z][A-Za-z0-9_]{1,60})*)",
        text,
    ):
        if masked is not None and masked[match.start()].isspace():
            continue
        chain = (match.group(1) + match.group(2).replace("?.", ".")).split(".")
        for key in chain:
            if key in keys:
                counts[key] += 1
    for match in re.finditer(
        prefix + r"(?:\?\.)?\[\s*([\"'])([A-Za-z][A-Za-z0-9_]{1,60})\1\s*\]",
        text,
    ):
        if masked is not None and masked[match.start()].isspace():
            continue
        key = match.group(2)
        if key in keys:
            counts[key] += 1
            brackets[key] += 1
    return counts, brackets


def _reference_scope(
    text: str, keys: set[str]
) -> tuple[Counter, Counter, Counter, set[str]]:
    """返回 (通用 .key 计数, 绑定语言包计数, 全量引用计数, 已绑定 key 集合)。

    全量引用 = 通用 `.key` 计数 + 绑定变量上的 `a["key"]` 计数，用于兜底
    校验。对比时只有新旧两侧都能绑定到语言包对象才使用绑定计数；任何一侧
    绑定不到就退回通用计数，避免两侧口径不同造成的假删除 / 假新增。
    """
    text = text or ""
    keys = set(keys or ())
    if not keys:
        return Counter(), Counter(), Counter(), set()
    # 字符串里的 `a.title` 只是文本，不是代码引用；用屏蔽后的同长文本跳过
    # 字符串内的匹配，避免把提示文案 / 错误信息里的点号路径当成语言包引用。
    masked = _mask_strings(text)
    generic = Counter(
        match.group(1)
        for match in _REF_RE.finditer(text)
        if not masked[match.start()].isspace() and match.group(1) in keys
    )
    bindings = _pack_bindings(text, keys, masked=masked)
    scoped, brackets = (
        _scoped_refs(text, bindings, keys, masked=masked)
        if bindings
        else (Counter(), Counter())
    )
    return generic, scoped, generic + brackets, set(bindings)


def _ordered_values(values: object) -> list[str]:
    """优先中文词条，方便站点展示；英文仍保留在后面。"""
    items = [str(value).strip() for value in (values or []) if str(value).strip()]
    chinese = [value for value in items if re.search(r"[\u4e00-\u9fff]", value)]
    other = [value for value in items if value not in chinese]
    return chinese + other


def _removed_i18n_refs(
    old_text: str, new_text: str, *, file_name: str = "", limit: int = 20
) -> list[dict]:
    """找出「词条还在、但界面代码里不再引用」的 i18n key。

    这是删除型提示最容易漏掉的一种：例如旧代码里有一行
    `[a.deleteAccountNoMoneyBackTitle,a.deleteAccountNoMoneyBackDesc]`，新代码
    删掉了这一行，但两个词条本身仍定义在语言包里。只比 cjk_strings 的集合
    差集看不到变化，必须比较 `.key` 的引用次数。
    """
    old_values = i18n_values(old_text)
    new_values = i18n_values(new_text)
    keys = set(old_values) | set(new_values)
    old_generic, old_scoped, old_broad, old_bound = _reference_scope(old_text, keys)
    new_generic, new_scoped, new_broad, new_bound = _reference_scope(new_text, keys)
    out: list[dict] = []
    for key in sorted(keys):
        if key in old_bound and key in new_bound:
            old_count = int(old_scoped.get(key, 0))
            new_count = int(new_scoped.get(key, 0))
            # 压缩变量会被其它作用域复用；只有语言包上的引用和全量引用
            # （含 a["key"]）同时减少，才认定用户提示真的被删了。
            if old_count <= new_count or int(old_broad.get(key, 0)) <= int(new_broad.get(key, 0)):
                continue
        else:
            old_count = int(old_generic.get(key, 0))
            new_count = int(new_generic.get(key, 0))
            if old_count <= new_count:
                continue
        values = _ordered_values(old_values.get(key) or new_values.get(key) or [])
        if not values:
            continue
        out.append(
            {
                "key": key,
                "file": file_name,
                "values": values[:2],
                "old_refs": old_count,
                "new_refs": new_count,
            }
        )
        if len(out) >= limit:
            break
    return out


def _added_i18n_refs(
    old_text: str, new_text: str, *, file_name: str = "", limit: int = 20
) -> list[dict]:
    """新增引用：词条已存在、本次代码开始使用它（新增提示的另一种形态）。"""
    old_values = i18n_values(old_text)
    new_values = i18n_values(new_text)
    keys = set(old_values) | set(new_values)
    old_generic, old_scoped, old_broad, old_bound = _reference_scope(old_text, keys)
    new_generic, new_scoped, new_broad, new_bound = _reference_scope(new_text, keys)
    out: list[dict] = []
    for key in sorted(keys):
        if key in old_bound and key in new_bound:
            old_count = int(old_scoped.get(key, 0))
            new_count = int(new_scoped.get(key, 0))
            if new_count <= old_count or int(new_broad.get(key, 0)) <= int(old_broad.get(key, 0)):
                continue
        else:
            old_count = int(old_generic.get(key, 0))
            new_count = int(new_generic.get(key, 0))
            if new_count <= old_count:
                continue
        values = _ordered_values(new_values.get(key) or old_values.get(key) or [])
        if not values:
            continue
        out.append(
            {
                "key": key,
                "file": file_name,
                "values": values[:2],
                "old_refs": old_count,
                "new_refs": new_count,
            }
        )
        if len(out) >= limit:
            break
    return out


def _file_diff(old_text: str, new_text: str, file_name: str, kind: str) -> dict | None:
    """生成一份供 changes 页/复核读取的片段级 old→new diff。"""
    if not old_text or not new_text:
        return None
    pieces = code_diff_pieces(old_text, new_text, limit=40)
    added = [piece.replace("\n", " ").strip() for piece in pieces.get("added") or [] if piece.strip()]
    removed = [piece.replace("\n", " ").strip() for piece in pieces.get("removed") or [] if piece.strip()]
    if not added and not removed:
        return None
    lines = [f"--- old/{file_name}", f"+++ new/{file_name}", "@@ 变更片段 @@"]
    lines.extend(f"- {piece}" for piece in removed[:20])
    lines.extend(f"+ {piece}" for piece in added[:20])
    text = "\n".join(lines)
    if len(text) > MAX_FILE_DIFF_CHARS:
        text = text[: MAX_FILE_DIFF_CHARS - 20] + "\n…（diff 已截断）"
    return {
        "file": file_name,
        "kind": kind,
        "added_count": len(added),
        "removed_count": len(removed),
        "diff": text,
    }


def _changed_entries(
    old: dict,
    new: dict,
    old_map: dict | None = None,
    new_map: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    if old_map is None:
        old_map = {item["key"]: item for item in old.get("manifest") or [] if isinstance(item, dict)}
    if new_map is None:
        new_map = {item["key"]: item for item in new.get("manifest") or [] if isinstance(item, dict)}
    added = [new_map[key] for key in new_map if key not in old_map]
    removed = [old_map[key] for key in old_map if key not in new_map]
    changed = [
        new_map[key]
        for key in set(old_map) & set(new_map)
        if str(old_map[key].get("hash") or "") != str(new_map[key].get("hash") or "")
    ]
    moved = {item["key"] for item in added} | {item["key"] for item in changed}
    unchanged = [item for key, item in new_map.items() if key not in moved]
    return added, removed, changed, unchanged


def build_evidence(
    old: dict | None,
    new: dict,
    *,
    cache_dir: Path | None,
    source: str,
    label: str,
    old_commit: str = "",
    new_commit: str = "",
    vault=None,
) -> dict | None:
    """比较两版前端快照；没有上一版或没有变化时返回 None。"""
    if not old or not old.get("manifest") or not new.get("manifest"):
        return None
    # 任一侧前端快照不健康（主包抓取失败/清单骤减）时不做清单差分：
    # 失败轮不能把 html-only 清单当成「移除全部 chunk」，恢复轮也不能重复报新增。
    for side in (old, new):
        if str(side.get("health") or "ok") != "ok" or side.get("error"):
            return None
    if _manifest_shrunk(old.get("manifest") or [], new.get("manifest") or []):
        return None
    old_map = {item["key"]: item for item in old.get("manifest") or [] if isinstance(item, dict)}
    new_map = {item["key"]: item for item in new.get("manifest") or [] if isinstance(item, dict)}
    added, removed, changed, _ = _changed_entries(old, new, old_map, new_map)
    old_css_constant = str(old.get("css_constant") or "")
    new_css_constant = str(new.get("css_constant") or "")
    css_constant_changed = bool(
        old_css_constant and new_css_constant and old_css_constant != new_css_constant
    )
    if not added and not removed and not changed and not css_constant_changed:
        return None

    budget = 0
    fetched = 0
    truncated = False
    truncation_reasons: list[str] = []

    def mark_truncated(reason: str) -> None:
        """记录「为什么没取全」；站点/模型据此说明缺口，而不是笼统说预算不足。"""
        nonlocal truncated
        truncated = True
        if reason and reason not in truncation_reasons:
            truncation_reasons.append(reason)

    def fetch_one(
        item: dict | None,
        *,
        cache_fallback: bool,
        use_vault: bool = False,
        vault_first: bool = True,
    ) -> tuple[str, int]:
        nonlocal budget, fetched, truncated
        if not item:
            return "", 0
        url = str(item.get("url") or "")
        body = b""
        if budget < MAX_TOTAL_BYTES:
            try:
                body = (
                    _fetch_bytes(url, cache_dir)
                    if vault is None
                    else _fetch_bytes(
                        url,
                        cache_dir,
                        vault=vault,
                        use_vault=use_vault,
                        vault_first=vault_first,
                    )
                )
            except Exception:  # noqa: BLE001
                body = _read_cache(cache_dir, url) if cache_fallback else b""
        if not body:
            return "", 0
        # 预算在抓取前就检查，超出的文件直接丢弃，不进入解码 / 对比，
        # 避免 4×8MB 全部下完才发现超了 20MB。
        if len(body) > MAX_TOTAL_BYTES - budget:
            mark_truncated("单文件超出本轮剩余取证预算")
            return "", 0
        budget += len(body)
        fetched += 1
        return body.decode("utf-8", "replace"), len(body)

    def fetch_pair(old_item: dict | None, new_item: dict | None) -> tuple[str, int, str, int]:
        old_text, old_size = fetch_one(
            old_item, cache_fallback=True, use_vault=True, vault_first=True
        )
        new_text, new_size = fetch_one(
            new_item, cache_fallback=False, use_vault=True, vault_first=False
        )
        return old_text, old_size, new_text, new_size

    def _pick_main(snapshot: dict) -> dict | None:
        entries = [
            item
            for item in snapshot.get("manifest") or []
            if item.get("role") == "entry" and item.get("kind") == "js"
        ]
        for item in entries:
            if _MAIN_ENTRY_RE.search(str(item.get("url") or "")):
                return item
        for item in entries:
            if _ENTRY_CANDIDATE_RE.search(str(item.get("url") or "")):
                return item
        return None

    main_old = _pick_main(old)
    main_new = _pick_main(new)
    css_old = next(
        (item for item in old.get("manifest") or [] if item.get("role") == "entry" and item.get("kind") == "css"),
        None,
    )
    css_new = next(
        (item for item in new.get("manifest") or [] if item.get("role") == "entry" and item.get("kind") == "css"),
        None,
    )

    old_main_text, old_main_size, new_main_text, new_main_size = fetch_pair(main_old, main_new)
    old_css_text, old_css_size, new_css_text, new_css_size = fetch_pair(css_old, css_new)

    # 旧版 CDN 文件过期时只能报告「文件变了」，不能把新包里的全部接口/文案当成「新增」。
    main_pair_ok = bool(old_main_text and new_main_text)
    css_pair_ok = bool(old_css_text and new_css_text)
    baseline_missing = bool((main_old or main_new) and not main_pair_ok) or bool(
        (css_old or css_new) and not css_pair_ok
    )

    api_added = sorted(api_paths(new_main_text) - api_paths(old_main_text)) if main_pair_ok else []
    api_removed = sorted(api_paths(old_main_text) - api_paths(new_main_text)) if main_pair_ok else []
    cjk_added = sorted(cjk_strings(new_main_text) - cjk_strings(old_main_text)) if main_pair_ok else []
    cjk_removed = sorted(cjk_strings(old_main_text) - cjk_strings(new_main_text)) if main_pair_ok else []
    upper_added = sorted(_upper_tokens(new_main_text) - _upper_tokens(old_main_text)) if main_pair_ok else []
    vars_added = (
        sorted(set(_VAR_RE.findall(new_css_text)) - set(_VAR_RE.findall(old_css_text)))
        if css_pair_ok
        else []
    )
    removed_i18n_refs: list[dict] = []
    added_i18n_refs: list[dict] = []
    file_diffs: list[dict] = []
    if main_pair_ok:
        main_name = str((main_new or {}).get("name") or "main.js")
        diff = _file_diff(
            _strip_build_meta(old_main_text),
            _strip_build_meta(new_main_text),
            main_name,
            "js",
        )
        if diff:
            file_diffs.append(diff)

    i18n_checked = 0
    i18n_added: dict[str, int] = {}
    i18n_removed: dict[str, int] = {}
    # key -> 它实际出现的 chunk 正文。i18n key 经常只存在于分片、主包里没有，
    # 只在 main 里找会拿不到用户真正看到的中文文案。
    i18n_texts: dict[str, str] = {}
    changed_chunk_texts: list[dict] = []
    for item in changed:
        if item.get("role") != "chunk":
            continue
        # MAX_CHANGED_FILES 的语义是「核对多少个变更文件」；新旧两侧各下载一次，
        # 不能用 fetched（下载次数）比较，否则 87 个变更分片需要 174 次下载，
        # 会在 160 次时被误判为超限，最后 2 个分片没核对却标成「预算截断」。
        if len(changed_chunk_texts) >= MAX_CHANGED_FILES or budget >= MAX_TOTAL_BYTES:
            mark_truncated("变更分片达到核对上限")
            break
        old_item = old_map.get(item["key"])
        old_text, old_size, new_text, new_size = fetch_pair(old_item, item)
        # 两侧都要有正文才能对比；拿不到旧分片时不猜新增，只计为未核对。
        if not old_text or not new_text:
            continue
        changed_chunk_texts.append({"item": item, "old": old_text, "new": new_text})
        # 同编号分片里的新增中文文案也要进「新增用户文案」——Platform 在已有
        # chunk 内加提示时，旧逻辑只进技术 snippet，用户最该看到的那句会漏掉。
        if item.get("kind") == "js":
            for value in sorted(cjk_strings(new_text) - cjk_strings(old_text)):
                if value not in cjk_added:
                    cjk_added.append(value)
            for value in sorted(cjk_strings(old_text) - cjk_strings(new_text)):
                if value not in cjk_removed:
                    cjk_removed.append(value)
            for path in sorted(api_paths(old_text) - api_paths(new_text)):
                if path not in api_removed:
                    api_removed.append(path)
            chunk_name = str(item.get("name") or item.get("key") or "")
            for entry in _removed_i18n_refs(old_text, new_text, file_name=chunk_name):
                if entry not in removed_i18n_refs:
                    removed_i18n_refs.append(entry)
            for entry in _added_i18n_refs(old_text, new_text, file_name=chunk_name):
                if entry not in added_i18n_refs:
                    added_i18n_refs.append(entry)
            diff = _file_diff(old_text, new_text, chunk_name, "js")
            if diff:
                file_diffs.append(diff)
        old_keys = i18n_keys(old_text)
        new_keys = i18n_keys(new_text)
        if len(new_keys) < 8 and len(old_keys) < 8:
            continue
        i18n_checked += 1
        for key in new_keys - old_keys:
            i18n_added[key] = i18n_added.get(key, 0) + 1
            i18n_texts.setdefault(key, new_text)
        for key in old_keys - new_keys:
            i18n_removed[key] = i18n_removed.get(key, 0) + 1

    # rspack 重新构建时 chunk 编号会整体重排：业务分片在清单里表现为
    # 「旧编号删除 + 新编号新增」，按 key 一对就断了。这里把新增分片的内容
    # 和删除分片的内容做集合差分，只把真正新增的文案 / 接口 / i18n key 当证据。
    # 2026-11-17 Platform 的「请完成真人验证后继续」就是这样被旧逻辑漏掉的。
    added_chunks = [
        item for item in added if item.get("role") == "chunk" and item.get("kind") == "js"
    ]
    removed_chunks = [
        item for item in removed if item.get("role") == "chunk" and item.get("kind") == "js"
    ]
    chunk_changes: dict = {
        "added": len(added_chunks),
        "removed": len(removed_chunks),
        "changed": sum(1 for item in changed if item.get("role") == "chunk"),
        "changed_checked": len(changed_chunk_texts),
        "paired": 0,
        "checked": False,
        "partial_pairing": False,
        "truncated": False,
    }
    chunk_new_blob = ""
    chunk_cjk_added: list[str] = []
    if added_chunks and removed_chunks:
        side_limit = 8
        if len(added_chunks) > side_limit or len(removed_chunks) > side_limit:
            mark_truncated("新增/删除分片数量超过核对上限")
            chunk_changes["truncated"] = True
            added_chunks = added_chunks[:side_limit]
            removed_chunks = removed_chunks[:side_limit]
        old_chunk_texts: list[str] = []
        new_chunk_texts: list[str] = []
        for item in removed_chunks:
            if fetched >= MAX_CHANGED_FILES or budget >= MAX_TOTAL_BYTES:
                mark_truncated("取证预算在核对删除分片时耗尽")
                break
            old_text, _, _, _ = fetch_pair(item, None)
            if old_text:
                old_chunk_texts.append(old_text)
        for item in added_chunks:
            if fetched >= MAX_CHANGED_FILES or budget >= MAX_TOTAL_BYTES:
                mark_truncated("取证预算在核对新增分片时耗尽")
                break
            _, _, new_text, _ = fetch_pair(None, item)
            if new_text:
                new_chunk_texts.append(new_text)
        old_complete = len(old_chunk_texts) == len(removed_chunks)
        new_complete = len(new_chunk_texts) == len(added_chunks)
        if old_complete and new_complete and old_chunk_texts and new_chunk_texts:
            chunk_old_blob = "\n".join(old_chunk_texts)
            chunk_new_blob = "\n".join(new_chunk_texts)
            chunk_cjk_added = sorted(cjk_strings(chunk_new_blob) - cjk_strings(chunk_old_blob))
            for text in chunk_cjk_added:
                if text not in cjk_added:
                    cjk_added.append(text)
            for text in sorted(cjk_strings(chunk_old_blob) - cjk_strings(chunk_new_blob)):
                if text not in cjk_removed:
                    cjk_removed.append(text)
            for path in sorted(api_paths(chunk_new_blob) - api_paths(chunk_old_blob)):
                if path not in api_added:
                    api_added.append(path)
            for path in sorted(api_paths(chunk_old_blob) - api_paths(chunk_new_blob)):
                if path not in api_removed:
                    api_removed.append(path)
            old_keys = i18n_keys(chunk_old_blob)
            new_keys = i18n_keys(chunk_new_blob)
            for key in new_keys - old_keys:
                i18n_added[key] = i18n_added.get(key, 0) + 1
                i18n_texts.setdefault(key, chunk_new_blob)
            for key in old_keys - new_keys:
                i18n_removed[key] = i18n_removed.get(key, 0) + 1
            for entry in _removed_i18n_refs(chunk_old_blob, chunk_new_blob, file_name="分片整体"):
                if entry not in removed_i18n_refs:
                    removed_i18n_refs.append(entry)
            for entry in _added_i18n_refs(chunk_old_blob, chunk_new_blob, file_name="分片整体"):
                if entry not in added_i18n_refs:
                    added_i18n_refs.append(entry)
            diff = _file_diff(chunk_old_blob, chunk_new_blob, "分片整体", "js")
            if diff:
                file_diffs.append(diff)
            i18n_checked += 1
            chunk_changes["paired"] = min(len(old_chunk_texts), len(new_chunk_texts))
            chunk_changes["checked"] = True
        else:
            # 两侧没有全部抓全：不要把残缺集合当差分，否则旧文案会被误报成新增。
            chunk_changes["partial_pairing"] = True
    elif added_chunks or removed_chunks:
        # 只有新增或只有删除分片：没有可比对的另一侧，本轮无法完整核对。
        # 尽量展示存在的一侧，至少让删除的文案/接口不静默消失。
        chunk_changes["partial_pairing"] = True
        if added_chunks:
            side_limit = 8
            if len(added_chunks) > side_limit:
                mark_truncated("新增分片数量超过核对上限")
                chunk_changes["truncated"] = True
                added_chunks = added_chunks[:side_limit]
            unpaired_texts: list[str] = []
            for item in added_chunks:
                if fetched >= MAX_CHANGED_FILES or budget >= MAX_TOTAL_BYTES:
                    mark_truncated("取证预算在核对新增分片时耗尽")
                    break
                _, _, new_text, _ = fetch_pair(None, item)
                if new_text:
                    unpaired_texts.append(new_text)
            if unpaired_texts:
                blob = "\n".join(unpaired_texts)
                chunk_changes["unpaired_added"] = {
                    "files": len(unpaired_texts),
                    "cjk": sorted(cjk_strings(blob))[:20],
                    "api": sorted(api_paths(blob))[:20],
                }
        if removed_chunks:
            side_limit = 8
            if len(removed_chunks) > side_limit:
                mark_truncated("删除分片数量超过核对上限")
                chunk_changes["truncated"] = True
                removed_chunks = removed_chunks[:side_limit]
            removed_texts: list[str] = []
            removed_apis: list[str] = []
            removed_blob_parts: list[str] = []
            for item in removed_chunks:
                if fetched >= MAX_CHANGED_FILES or budget >= MAX_TOTAL_BYTES:
                    mark_truncated("取证预算在核对删除分片时耗尽")
                    break
                old_text, _, _, _ = fetch_pair(item, None)
                if old_text:
                    removed_blob_parts.append(old_text)
            if removed_blob_parts:
                blob = "\n".join(removed_blob_parts)
                removed_texts = sorted(cjk_strings(blob))[:20]
                removed_apis = sorted(api_paths(blob))[:20]
                for text in removed_texts:
                    if text not in cjk_removed:
                        cjk_removed.append(text)
                for path in removed_apis:
                    if path not in api_removed:
                        api_removed.append(path)
                for entry in _removed_i18n_refs(blob, "", file_name="已删除分片"):
                    if entry not in removed_i18n_refs:
                        removed_i18n_refs.append(entry)
                chunk_changes["unpaired_removed"] = {
                    "files": len(removed_blob_parts),
                    "cjk": removed_texts,
                    "api": removed_apis,
                }

    snippets: list[str] = []
    for value in api_added[:3] + cjk_added[:3]:
        found = _snippets(new_main_text, value, limit=1) or _snippets(chunk_new_blob, value, limit=1)
        for snippet in found:
            snippets.append({"kind": "js", "value": value, "text": snippet})
    for key in list(i18n_added)[:3]:
        # 优先在 key 实际出现的 chunk 里取上下文，主包只作兜底。
        found = (
            _snippets(i18n_texts.get(key, ""), key, limit=2)
            or _snippets(new_main_text, key, limit=2)
            or _snippets(chunk_new_blob, key, limit=2)
        )
        for snippet in found:
            snippets.append({"kind": "i18n", "value": key, "text": snippet})
    for value in vars_added[:3]:
        for snippet in _snippets(new_css_text, value, limit=1):
            snippets.append({"kind": "css", "value": value, "text": snippet})

    # 主包 entry 不在 changed chunks 循环里；它若只有代码逻辑变化，旧证据包
    # 会完全看不见。主包片段先放，避免后面 chunk 片段占满 6 条预算被截掉。
    changed_code_snippets: list[dict] = []
    if main_pair_ok:
        for snippet in main_code_snippets(old_main_text, new_main_text):
            changed_code_snippets.append({"file": "main.js", "text": snippet})
    for entry in changed_chunk_texts[:3]:
        for snippet in code_diff_snippets(entry["old"], entry["new"]):
            changed_code_snippets.append(
                {"file": str(entry["item"].get("name") or ""), "text": snippet}
            )

    # 反向 diff：删除型变化（删提示、删入口、删一段交互）以前完全没有证据。
    # 同一套语句片段切分反过来跑一遍，就能拿到被删掉的代码上下文。
    removed_code_snippets: list[dict] = []
    if main_pair_ok:
        for snippet in code_diff_snippets(
            _strip_build_meta(new_main_text), _strip_build_meta(old_main_text)
        ):
            removed_code_snippets.append({"file": "main.js", "text": snippet})
    for entry in changed_chunk_texts[:3]:
        for snippet in code_diff_snippets(entry["new"], entry["old"]):
            removed_code_snippets.append(
                {"file": str(entry["item"].get("name") or ""), "text": snippet}
            )

    # CSS 规则 diff 单独做三路：
    # 1) entry CSS（main.css）内容变化；2) Chat 两表 CSS 的 chunk hash 变化；
    # 3) Platform 的 `.k` 常量变化（按 JS chunk 名拼旧/新 CSS URL）。
    # 旧逻辑只在「有 changed chunk 且存在 css_constant」时比对，纯样式发版
    # 或 Chat 两表样式永远 checked=0，最终被压成 1 级。
    css_rule_changes: dict = {
        "checked": 0,
        "changed": 0,
        "files": [],
        "added": [],
        "removed": [],
    }
    old_css_constant = str(old.get("css_constant") or "")
    new_css_constant = str(new.get("css_constant") or "")
    css_pairs: list[tuple[str, str, str]] = []

    def add_css_pair(name: str, old_url: str, new_url: str) -> None:
        if not old_url or not new_url:
            return
        pair = (str(name or "css"), old_url, new_url)
        if pair not in css_pairs:
            css_pairs.append(pair)

    if css_old and css_new and str(css_old.get("hash") or "") != str(css_new.get("hash") or ""):
        add_css_pair(
            str(css_new.get("name") or "main.css"),
            str(css_old.get("url") or ""),
            str(css_new.get("url") or ""),
        )
    for item in changed:
        if item.get("role") != "chunk" or item.get("kind") != "css":
            continue
        old_item = old_map.get(item["key"]) or {}
        add_css_pair(
            str(item.get("name") or ""),
            str(old_item.get("url") or ""),
            str(item.get("url") or ""),
        )
    if old_css_constant and new_css_constant and old_css_constant != new_css_constant:
        old_prefix = str(old.get("css_prefix") or "static/")
        new_prefix = str(new.get("css_prefix") or "static/")
        old_main_url = str(old.get("main_js_url") or "")
        new_main_url = str(new.get("main_js_url") or "")
        changed_js_keys = {
            item["key"]
            for item in changed
            if item.get("role") == "chunk" and item.get("kind") == "js"
        }
        js_keys = [
            key
            for key, item in old_map.items()
            if item.get("role") == "chunk"
            and item.get("kind") == "js"
            and key in new_map
        ]
        js_keys.sort(key=lambda key: (key not in changed_js_keys, key))
        for key in js_keys:
            name = str(new_map[key].get("name") or old_map[key].get("name") or "")
            add_css_pair(
                name,
                css_chunk_url(old_main_url, name, old_css_constant, old_prefix),
                css_chunk_url(new_main_url, name, new_css_constant, new_prefix),
            )
    css_rule_changes["changed"] = len(css_pairs)
    for name, old_url, new_url in css_pairs[:8]:
        if fetched >= MAX_CHANGED_FILES or budget >= MAX_TOTAL_BYTES:
            mark_truncated("取证预算在核对 CSS 分片时耗尽")
            break
        old_css, _ = fetch_one({"url": old_url}, cache_fallback=True, use_vault=True, vault_first=True)
        new_css, _ = fetch_one({"url": new_url}, cache_fallback=False, use_vault=True, vault_first=False)
        if not old_css or not new_css:
            continue
        css_rule_changes["checked"] += 1
        css_rule_changes["files"].append(name[:120])
        for rule in sorted(css_rules(new_css) - css_rules(old_css)):
            if rule not in css_rule_changes["added"]:
                css_rule_changes["added"].append(rule)
        for rule in sorted(css_rules(old_css) - css_rules(new_css)):
            if rule not in css_rule_changes["removed"]:
                css_rule_changes["removed"].append(rule)
    if css_rule_changes["changed"] and not css_rule_changes["checked"]:
        # CSS 清单显示有变化、却一条规则都没核对到：可能是 404/不可用，也可能是
        # 预算只够列清单。这里不再冒充「预算截断」，把缺口单独记下来，
        # 由质量闸门判断：已有足够可见证据时允许发布，只有 CSS 变化时保留待核实。
        css_rule_changes["unchecked"] = int(css_rule_changes["changed"])
    css_rule_changes["added"] = css_rule_changes["added"][:20]
    css_rule_changes["removed"] = css_rule_changes["removed"][:20]

    changed_files = [
        {
            "key": item.get("key"),
            "name": item.get("name"),
            "kind": item.get("kind"),
            "role": item.get("role"),
            "old_url": (old_map.get(item["key"]) or {}).get("url", ""),
            "new_url": item.get("url", ""),
        }
        for item in changed[:40]
    ]
    return {
        # 控制字段放最前：ai._prompt_for 目前从开头截断 30k 字符，证据不全的
        # 标志（truncated/baseline_missing）和计数必须先于大列表被序列化。
        "truncated": truncated,
        "baseline_missing": baseline_missing,
        "truncation_reasons": truncation_reasons[:8],
        "totals": {
            "manifest": len(new_map),
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": max(0, len(new_map) - len(changed) - len(added)),
        },
        "chunk_changes": chunk_changes,
        "css_rule_changes": css_rule_changes,
        "i18n": {
            "checked": i18n_checked,
            "changed_chunks": sum(1 for item in changed if item.get("role") == "chunk"),
            "added": dict(sorted(i18n_added.items(), key=lambda item: (-item[1], item[0]))[:20]),
            "removed": dict(sorted(i18n_removed.items(), key=lambda item: (-item[1], item[0]))[:20]),
        },
        "fetched_bytes": budget,
        "source": source,
        "label": label,
        "engine": new.get("engine", ""),
        "old_commit": old_commit,
        "new_commit": new_commit,
        "old_build": old.get("build_meta") or {},
        "new_build": new.get("build_meta") or {},
        "main_js": {
            "old_url": (main_old or {}).get("url", ""),
            "new_url": (main_new or {}).get("url", ""),
            "old_size": old_main_size,
            "new_size": new_main_size,
        },
        "main_css": {
            "old_url": (css_old or {}).get("url", ""),
            "new_url": (css_new or {}).get("url", ""),
            "old_size": old_css_size,
            "new_size": new_css_size,
        },
        "api_added": api_added[:30],
        "api_removed": api_removed[:30],
        "cjk_added": cjk_added[:30],
        "cjk_removed": cjk_removed[:30],
        "upper_added": upper_added[:30],
        "css_vars_added": vars_added[:30],
        "changed_code_snippets": changed_code_snippets[:6],
        "removed_code_snippets": removed_code_snippets[:6],
        "removed_i18n_refs": removed_i18n_refs[:20],
        "added_i18n_refs": added_i18n_refs[:20],
        "file_diffs": file_diffs[:8],
        "changed_files": changed_files,
        "added_files": [
            {"key": item.get("key"), "name": item.get("name"), "kind": item.get("kind")}
            for item in added[:20]
        ],
        "removed_files": [
            {"key": item.get("key"), "name": item.get("name"), "kind": item.get("kind")}
            for item in removed[:20]
        ],
        "snippets": snippets[:MAX_SNIPPETS],
    }


def technical_change_lines(evidence: dict, *, limit: int = 4) -> list[str]:
    """把「只有技术变化」的证据整理成人能读的具体事实。

    internal（证据完整但没提取到用户可见文案）事件不能只说「未捕获到变化」；
    这里至少给出变化的文件与字节数，供标题、摘要和判级理由引用。
    """
    if not isinstance(evidence, dict):
        return []
    lines: list[str] = []
    totals = evidence.get("totals") or {}
    manifest = int(totals.get("manifest") or 0)
    changed = int(totals.get("changed") or 0)
    added = int(totals.get("added") or 0)
    removed = int(totals.get("removed") or 0)
    main_js = evidence.get("main_js") or {}
    main_css = evidence.get("main_css") or {}
    changed_files = [item for item in evidence.get("changed_files") or [] if isinstance(item, dict)]
    names: list[str] = []
    for item in changed_files:
        name = str(item.get("name") or item.get("key") or "").strip()
        if name and name not in names:
            names.append(name)

    size_line = ""
    for label, block in (("main.js", main_js), ("main.css", main_css)):
        old_size = int(block.get("old_size") or 0)
        new_size = int(block.get("new_size") or 0)
        if not (old_size or new_size):
            continue
        if old_size != new_size:
            size_line = f"{label}：{old_size:,} → {new_size:,} 字节（{new_size - old_size:+,}）"
        break
    if size_line:
        lines.append(size_line)
    elif names:
        lines.append("内容变化的文件：" + "、".join(names[:5]))

    counts = []
    if changed:
        counts.append(f"内容变化 {changed}")
    if added:
        counts.append(f"新增 {added}")
    if removed:
        counts.append(f"移除 {removed}")
    if counts and manifest:
        lines.append(f"资源清单 {manifest} 个，其中" + "、".join(counts))
    elif counts:
        lines.append("资源变化：" + "、".join(counts))

    diff_names = [
        str(item.get("file") or "").strip()
        for item in evidence.get("file_diffs") or []
        if isinstance(item, dict) and str(item.get("file") or "").strip() not in names
    ]
    if diff_names:
        lines.append("有代码对比的文件：" + "、".join(diff_names[:4]))
    return lines[:limit]


def build_changes_detail(evidence: dict, level: int) -> list[dict]:
    """把证据包整理成站点 changes 页可直接展示的分组（不放大段 diff）。

    分组按「人话优先」排序：用户文案 / 样式在最前，版本与构建、语言包、资源核对
    这类技术核对放最后并用 is_asset=True 标记，渲染层可据此折叠到技术区。
    """
    groups: list[dict] = []

    def add(page: str, bullets: list[str], *, is_asset: bool = False, note: str = "", diff: str = "") -> None:
        items = [str(item)[:500] for item in bullets if str(item).strip()]
        if not items:
            return
        groups.append(
            {
                "page": page[:500],
                "level": level,
                "is_asset": is_asset,
                "is_error": False,
                "noise": False,
                "note": note[:1000],
                "bullets": items[:8],
                "diff": diff[:20000],
            }
        )

    css_rule_changes = evidence.get("css_rule_changes") or {}
    chunk_changes = evidence.get("chunk_changes") or {}
    i18n = evidence.get("i18n") or {}

    # ---- 人话在前：用户文案、未配对新内容、样式 ----
    added_texts = [str(item) for item in evidence.get("cjk_added") or [] if str(item).strip()]
    for item in evidence.get("added_i18n_refs") or []:
        for value in _ordered_values(item.get("values") or [])[:2]:
            if value not in added_texts:
                added_texts.append(value)
    add("新增用户文案", [f"「{item}」" for item in added_texts])

    # 删除型变化单独成组：这是以前反复漏掉的一类。i18n 引用差比“文案集合差”
    # 更敏感——词条还在语言包里，只是界面不再引用，也算用户看不到这条提示了。
    removed_texts: list[str] = []
    for item in evidence.get("removed_i18n_refs") or []:
        values = _ordered_values(item.get("values") or [])
        if values:
            text = " / ".join(values[:2])
            if text not in removed_texts:
                removed_texts.append(text)
    for value in evidence.get("cjk_removed") or []:
        text = str(value).strip()
        if text and text not in removed_texts:
            removed_texts.append(text)
    if removed_texts:
        # 标题/短提示优先，长说明在后，站点首条摘要更好读。
        removed_texts.sort(key=len)
        add(
            "删除用户文案",
            [f"「{text}」" for text in removed_texts[:8]],
            note="以下提示/文案已从界面引用中移除（语言包词条可能仍保留）",
            diff="\n".join(f"- {text}" for text in removed_texts[:8]),
        )
    unpaired = chunk_changes.get("unpaired_added") or {}
    if unpaired.get("cjk") or unpaired.get("api"):
        unpaired_lines = [
            "新增分片没有旧分片可配对，以下内容只作线索，待下轮复核，不等于确认新增。"
        ]
        unpaired_lines.extend(f"「{item}」" for item in (unpaired.get("cjk") or [])[:6])
        unpaired_lines.extend(f"`{item}`" for item in (unpaired.get("api") or [])[:6])
        add("未配对新增内容", unpaired_lines, note="added-only 分片内容，未做旧集合差分")
    css_added = css_rule_changes.get("added") or []
    css_removed = css_rule_changes.get("removed") or []
    if css_added or css_removed:
        style_lines = []
        if css_added:
            style_lines.append(f"新增样式规则 {len(css_added)} 条")
        if css_removed:
            style_lines.append(f"删除样式规则 {len(css_removed)} 条")
        style_lines.extend(f"`{rule[:300]}`" for rule in css_added[:3])
        add("样式调整", style_lines)
    add("新增样式变量", [f"`{item}`" for item in evidence.get("css_vars_added") or []])

    # ---- 技术核对在后：用 is_asset=True 标记，渲染层默认折叠 ----
    add("新增接口", [f"`{item}`" for item in evidence.get("api_added") or []], is_asset=True)
    add("删除接口", [f"`{item}`" for item in evidence.get("api_removed") or []], is_asset=True)
    old_build = evidence.get("old_build") or {}
    new_build = evidence.get("new_build") or {}
    version_bits = []
    if evidence.get("old_commit") or evidence.get("new_commit"):
        version_bits.append(f"commit：{evidence.get('old_commit') or '（无）'} → {evidence.get('new_commit') or '（无）'}")
    if old_build.get("appVersion") or new_build.get("appVersion"):
        version_bits.append(f"版本：{old_build.get('appVersion') or '（无）'} → {new_build.get('appVersion') or '（无）'}")
    if old_build.get("commit_datetime") or new_build.get("commit_datetime"):
        version_bits.append(
            f"构建时间：{old_build.get('commit_datetime') or '（无）'} → {new_build.get('commit_datetime') or '（无）'}"
        )
    add("版本与构建", version_bits, is_asset=True)

    i18n_lines = []
    checked = int(i18n.get("checked") or 0)
    if checked:
        changed_chunks = int(i18n.get("changed_chunks") or 0)
        paired = int(chunk_changes.get("paired") or 0)
        scope = changed_chunks or paired
        if scope:
            i18n_lines.append(f"共核对 {scope} 个分片，其中 {checked} 个核对到语言包")
        else:
            i18n_lines.append(f"核对到 {checked} 个语言包分片")
    else:
        i18n_lines.append("本轮没有可配对的语言包分片，i18n 未核对（不代表没有变化）")
    for key, count in (i18n.get("added") or {}).items():
        i18n_lines.append(f"新增 key `{key}` ×{count}")
    for key, count in (i18n.get("removed") or {}).items():
        i18n_lines.append(f"删除 key `{key}` ×{count}")
    add("语言包", i18n_lines, is_asset=True)

    totals = evidence.get("totals") or {}
    main_js = evidence.get("main_js") or {}
    main_css = evidence.get("main_css") or {}
    resource_lines = [
        f"资源清单 {totals.get('manifest', 0)} 个：新增 {totals.get('added', 0)}，移除 {totals.get('removed', 0)}，内容变化 {totals.get('changed', 0)}",
    ]
    if chunk_changes.get("checked") and chunk_changes.get("paired"):
        resource_lines.append(
            f"替换分片 {chunk_changes.get('paired')} 对，已核对分片内容和新增文案"
        )
    if chunk_changes.get("partial_pairing"):
        resource_lines.append("新增/删除分片未完整配对，本轮不拿残缺内容做差分，待下轮复核")
    if css_rule_changes.get("checked"):
        resource_lines.append(
            f"核对 CSS 分片 {css_rule_changes.get('checked')}/{css_rule_changes.get('changed', 0)} 个"
        )
    elif css_rule_changes.get("unchecked"):
        resource_lines.append(
            f"CSS 分片有 {css_rule_changes.get('unchecked')} 个变化，但正文未能取到，未做规则对比"
        )
    if main_js.get("old_size") or main_js.get("new_size"):
        resource_lines.append(f"main.js：{main_js.get('old_size', 0):,} → {main_js.get('new_size', 0):,} 字节")
    if main_css.get("old_size") or main_css.get("new_size"):
        resource_lines.append(f"main.css：{main_css.get('old_size', 0):,} → {main_css.get('new_size', 0):,} 字节")
    if evidence.get("fetched_bytes"):
        resource_lines.append(f"本轮取证下载约 {evidence.get('fetched_bytes', 0) / 1024:.0f} KB")
    if evidence.get("truncated"):
        raw_reasons = evidence.get("truncation_reasons")
        reasons = (
            [str(reason).strip() for reason in raw_reasons if str(reason).strip()]
            if isinstance(raw_reasons, list)
            else []
        )
        if reasons:
            resource_lines.append("部分资源未展开：" + "；".join(reasons[:3]))
        else:
            resource_lines.append("部分资源因预算限制未展开，报告已注明")
    if evidence.get("baseline_missing"):
        resource_lines.append("旧版正文已无法下载，本轮只报告文件变化，未推断新增内容")
    add("资源核对", resource_lines, is_asset=True)

    # 变化文件的新旧片段 diff：站点 /changes 页可直接左右对照；技术区默认折叠。
    for item in evidence.get("file_diffs") or []:
        if not isinstance(item, dict) or not str(item.get("diff") or "").strip():
            continue
        add(
            f"代码对比：{item.get('file') or '前端文件'}",
            [f"删除 {int(item.get('removed_count') or 0)} 处，新增 {int(item.get('added_count') or 0)} 处"],
            is_asset=True,
            diff=str(item.get("diff") or ""),
        )
    return groups[:20]
