"""前端资源运行时映射与文本提取的公共 API。

webwatch.frontend_evidence（生产巡检）与 tools/frontend_bundle_mirror（离线工具）
共用这里的实现，避免两套解析逻辑慢慢漂移。本模块只做纯文本解析：
不发起网络请求、不执行下载到的 JS、不读任何凭据。
"""

from __future__ import annotations

import re
import urllib.parse

__all__ = [
    "chunk_urls",
    "css_chunk_url",
    "logical_name",
    "filename_hash",
    "cjk_strings",
    "api_paths",
    "i18n_keys",
    "i18n_values",
    "css_rules",
    "code_diff_pieces",
    "code_diff_snippets",
    "runtime_map",
]

_CJK_RE = re.compile(r'"([^"\n]{0,200}[\u4e00-\u9fff][^"\n]{0,200})"')
_API_RE = re.compile(r'"(/api/[^"\n]{1,150})"')
_KEY_RE = re.compile(r'([A-Za-z][A-Za-z0-9_]{1,60}):\s*"([^"\n]{0,300})"')
_HASH_SUFFIX_RE = re.compile(r"^(.*?)[.-]([0-9a-f]{8,})$")
_CODE_SPLIT_RE = re.compile(r"[;,{}]")
_HEX_ONLY_RE = re.compile(r"^[0-9a-f]{6,40}$", re.I)
# 运行时的 chunk 哈希表（如 2653:"59fba56615"）只是构建清单，不是业务代码。
# 一条片段里出现多组这种键值时直接过滤，避免把重新打包当成代码变化。
_RUNTIME_MAP_PAIR_RE = re.compile(r'["\']?[0-9A-Za-z_-]+["\']?\s*:\s*"[0-9a-f]{8,}"')
_CSS_RULE_RE = re.compile(r"[^{}]+\{[^{}]*\}")


def _balanced_object(text: str, start: int) -> tuple[str, int]:
    begin = text.find("{", start)
    if begin < 0:
        raise ValueError("找不到对象")
    depth = 0
    for pos in range(begin, len(text)):
        char = text[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[begin : pos + 1], pos + 1
    raise ValueError("对象没有闭合")


def _parse_pairs(obj: str) -> dict[str, str]:
    """解析 `{100:"hash", "name":"hash"}`；数字键和带引号的键都接受。"""
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r'["\']?([0-9A-Za-z_-]+)["\']?\s*:\s*"([^"]+)"', obj)
    }


def _object_assignment_map(text: str, key: str) -> dict | None:
    """解析对象式运行时映射（如 Chat 的 `.k={5:"cccc3333"}`）。

    `runtime_map` 原先只认 `.k=e=>"..."` 箭头函数，Chat 改成对象表后
    CSS 分片就再也拿不到；这里两种形态都支持。
    """
    pattern = re.compile(rf"\.{re.escape(key)}\s*=\s*")
    for match in pattern.finditer(text):
        cursor = match.end()
        while cursor < len(text) and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= len(text) or text[cursor] != "{":
            continue
        try:
            obj, _ = _balanced_object(text, cursor)
        except ValueError:
            continue
        hashes = _parse_pairs(obj)
        if hashes:
            return {"prefix": "", "names": {}, "hashes": hashes, "constant": "", "object": True}
    return None


def runtime_map(text: str, marker: str, suffix: str, key: str = "") -> dict | None:
    """解析 rspack 运行时里的 `.u`（JS）/ `.k`（CSS）资源映射。

    chat 是「名字表 + 哈希表」两个对象，platform 只有哈希表一个对象；
    chat 也可能给 `.k` 直接挂对象表。解析失败返回 None，不把整轮采集判失败。
    """
    match = re.search(marker, text)
    if not match:
        return _object_assignment_map(text, key) if key else None
    start = match.start()
    end = text.find(suffix, start)
    if end < 0:
        return _object_assignment_map(text, key) if key else None
    segment = text[start : end + len(suffix)]
    prefix_match = re.search(r'\.\w+\s*=\s*e\s*=>\s*"([^"]*)"', segment)
    prefix = prefix_match.group(1) if prefix_match else "static/"
    objects: list[str] = []
    cursor = 0
    while True:
        index = segment.find("{", cursor)
        if index < 0:
            break
        try:
            obj, cursor = _balanced_object(segment, index)
        except ValueError:
            break
        objects.append(obj)
    if not objects:
        constant = re.search(
            r'\.\w+\s*=\s*e\s*=>\s*"[^"]*"\s*\+\s*e\s*\+\s*"\.([^"]+)"', segment
        )
        return {
            "prefix": prefix,
            "names": {},
            "hashes": {},
            "constant": constant.group(1) if constant else "",
        }
    if len(objects) >= 2:
        names = _parse_pairs(objects[0])
        hashes = _parse_pairs(objects[-1])
    else:
        names = {}
        hashes = _parse_pairs(objects[0])
    return {"prefix": prefix, "names": names, "hashes": hashes, "constant": ""}


def filename_hash(url: str) -> str:
    name = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    match = _HASH_SUFFIX_RE.match(stem)
    return match.group(2) if match else ""


def logical_name(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return name or path
    stem, ext = name.rsplit(".", 1)
    match = _HASH_SUFFIX_RE.match(stem)
    if match:
        stem = match.group(1)
    folder = "/".join(path.split("/")[1:-1])
    return f"{folder}/{stem}.{ext}" if folder else f"{stem}.{ext}"


def _asset_base(main_js_url: str, prefix: str) -> str:
    if "/static/" in main_js_url:
        return main_js_url.split("/static/", 1)[0] + "/" + prefix
    return main_js_url.rsplit("/", 1)[0] + "/" + prefix


def chunk_urls(
    main_js_url: str, maps: dict | None, ext: str, fallback_names: dict | None = None
) -> list[dict]:
    if not maps or not maps.get("hashes"):
        return []
    base = _asset_base(main_js_url, str(maps.get("prefix") or "static/"))
    names = maps.get("names") or fallback_names or {}
    entries = []
    for chunk_id, digest in maps["hashes"].items():
        name = names.get(chunk_id, chunk_id)
        entries.append(
            {
                "key": f"chunk:{ext}:{chunk_id}",
                "url": f"{base}{name}.{digest}.{ext}",
                "hash": digest,
                "kind": ext,
                "role": "chunk",
                "name": str(name),
            }
        )
    return entries


def css_chunk_url(main_js_url: str, name: str, constant: str, prefix: str = "") -> str:
    """按 rspack 的 `.k` 常量拼 CSS 分片 URL；不适用时返回空串。"""
    if not main_js_url or not name or not constant:
        return ""
    value = constant.strip()
    if value.endswith(".css"):
        value = value[:-4]
    return f"{_asset_base(main_js_url, prefix or 'static/')}{name}.{value}.css"


def cjk_strings(text: str) -> set[str]:
    return set(_CJK_RE.findall(text))


def api_paths(text: str) -> set[str]:
    return set(_API_RE.findall(text))


def i18n_keys(text: str) -> set[str]:
    """只认「值里带中文」的 key: "..." 对，避免把 isDark / provider 这类代码属性当文案 key。"""
    return {
        match.group(1)
        for match in _KEY_RE.finditer(text)
        if re.search(r"[\u4e00-\u9fff]", match.group(2))
    }


def i18n_values(text: str) -> dict[str, list[str]]:
    """返回「值里带中文」的 key → 所有出现值（含同 key 的英文词条）。

    一个 key 在语言包里可能出现多次（中文/英文各一条），这里保留全部值，
    供删除引用时还原出用户实际看到的提示文案。
    """
    values: dict[str, list[str]] = {}
    cjk_keys: set[str] = set()
    for match in _KEY_RE.finditer(text):
        key, value = match.group(1), match.group(2)
        bucket = values.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)
        if re.search(r"[\u4e00-\u9fff]", value):
            cjk_keys.add(key)
    return {key: values[key] for key in cjk_keys}


def _useful_code_piece(piece: str) -> bool:
    """过滤 commit / 哈希清单这类纯构建片段，只留代码里有意义的改动。"""
    value = piece.strip()
    if len(value) < 2 or _HEX_ONLY_RE.fullmatch(value):
        return False
    if value.startswith("commitId:") or value.startswith("commit_id:"):
        return False
    # rspack 的 `.u` / `.k` 哈希表长这样：2653:"59fba56615",2679:"f5ad16022e"...
    # 它只说明构建产物换了名字，不是业务代码；不排掉会制造假“代码变化”。
    if _RUNTIME_MAP_PAIR_RE.fullmatch(value) or len(_RUNTIME_MAP_PAIR_RE.findall(value)) >= 3:
        return False
    return True


def code_diff_pieces(old_text: str, new_text: str, *, limit: int = 60) -> dict[str, list[str]]:
    """按语句片段返回 old→new 的新增/删除片段（双向）。"""
    old_parts = {piece for piece in _CODE_SPLIT_RE.split(old_text or "") if piece}
    new_parts = {piece for piece in _CODE_SPLIT_RE.split(new_text or "") if piece}
    added = sorted(
        (piece for piece in new_parts - old_parts if _useful_code_piece(piece)),
        key=len,
        reverse=True,
    )[:limit]
    removed = sorted(
        (piece for piece in old_parts - new_parts if _useful_code_piece(piece)),
        key=len,
        reverse=True,
    )[:limit]
    return {"added": added, "removed": removed}


def code_diff_snippets(old_text: str, new_text: str, *, limit: int = 3, width: int = 260) -> list[str]:
    """从变更分片的语句片段里找新增代码，并给出上下文。

    针对压缩后的单行 JS：按 ; , { } 切段做集合差，能避开整包重排的噪音，
    留下 `!o&&...`、`oE:"..."` 这类真正新增的表达式。
    """
    added = code_diff_pieces(old_text, new_text, limit=max(limit * 4, limit))["added"]
    out: list[str] = []
    for piece in added:
        index = new_text.find(piece)
        if index < 0:
            continue
        begin = max(0, index - width // 2)
        snippet = "…" + new_text[begin : index + len(piece) + width // 2] + "…"
        if snippet not in out:
            out.append(snippet)
        if len(out) >= limit:
            break
    return out


def css_rules(text: str) -> set[str]:
    return {match.group(0).strip() for match in _CSS_RULE_RE.finditer(text)}
