"""docwatch.collect.asset —— 从 HTML / JS / sitemap 里抽出可对比的东西。

三件事，都是纯函数：
- extract_refs / page_refs：抽 `script[src]` / `link[href]` / `img[src]` / `source[src]`，
  忽略 `data:`，归一成全站通用的 js / css / img / other 四类
- rsc_text：官网首页是 Next.js，正文在 `self.__next_f.push([1,"..."])` 的 RSC payload 里，
  解出来拼成正文（否则只能抓到导航壳子）
- js_strings：从压缩 JS 里抽可读字符串集合（长度 ≥ 6、排除路径与 URL），供 diff 用

`html_to_text` / `extract_rsc_text` 是给 toolbox 的纯文本入口，复用上面的正文抽取，
不在工具箱里再维护一套 HTML/RSC 解析。
"""

from __future__ import annotations

import html as html_mod
import json
import re
import urllib.parse
from dataclasses import dataclass

DOCS_ROOT = "https://api-docs.deepseek.com"
DOCS_SITEMAP = f"{DOCS_ROOT}/zh-cn/sitemap.xml"
SITE_PAGES = ("https://www.deepseek.com/zh/",)   # sitemap 管不到、单独盯的页面
FIRST_PARTY_HOSTS = ("api-docs.deepseek.com", "www.deepseek.com", "deepseek.com")

# ---------------------------------------------------------------- 资源引用

_TAG_RE = re.compile(r"<(script|link|img|source)\b([^>]*)>", re.I | re.S)
_ATTR_RE = re.compile(r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")

_REL_CSS = {"stylesheet"}
_REL_JS = {"modulepreload"}
_REL_IMG = {"icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed",
            "mask-icon", "fluid-icon"}

JS, CSS, IMG, OTHER = "js", "css", "img", "other"
KINDS = (JS, CSS, IMG, OTHER)


@dataclass(frozen=True)
class Ref:
    """一条资源引用。kind ∈ {js, css, img, other}。"""

    url: str
    kind: str


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        out[key] = html_mod.unescape(val.strip())
    return out


def _clean_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return ""
    low = u.lower()
    for bad in ("data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "chrome-extension:"):
        if low.startswith(bad):
            return ""
    if u.startswith("#"):
        return ""
    return u


def extract_refs(html: str, base: str) -> list[Ref]:
    """抽页面里所有外链资源。顺序 = 出现顺序，同一 (url,kind) 只留第一份。"""
    refs: list[Ref] = []
    seen: set[tuple[str, str]] = set()

    for m in _TAG_RE.finditer(html):
        tag = m.group(1).lower()
        attrs = _attrs(m.group(2))
        raw = attrs.get("src") or attrs.get("href") or ""
        url = _clean_url(raw)
        if not url:
            continue
        url = urllib.parse.urljoin(base, url)

        if tag == "script":
            kind = OTHER if (attrs.get("type") or "").lower() in (
                "application/json", "application/ld+json", "importmap") else JS
        elif tag == "link":
            rels = {t.lower() for t in (attrs.get("rel") or "").split()}
            as_ = (attrs.get("as") or "").lower()
            if rels & _REL_CSS or (("preload" in rels) and as_ == "style"):
                kind = CSS
            elif rels & _REL_JS or (("preload" in rels) and as_ == "script"):
                kind = JS
            elif rels & _REL_IMG or (("preload" in rels) and as_ == "image"):
                kind = IMG
            else:
                kind = OTHER
        else:  # img / source
            kind = IMG

        key = (url, kind)
        if key in seen:
            continue
        seen.add(key)
        refs.append(Ref(url=url, kind=kind))
    return refs


def page_refs(html: str, base: str) -> dict[str, list[str]]:
    """按类别归并成 {js: [...], css: [...], img: [...], other: [...]}（去重、保序）。"""
    out: dict[str, list[str]] = {k: [] for k in KINDS}
    for ref in extract_refs(html, base):
        bucket = out[ref.kind]
        if ref.url not in bucket:
            bucket.append(ref.url)
    return out


def is_first_party(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in FIRST_PARTY_HOSTS)


def monitor_targets(refs: dict[str, list[str]] | list[Ref]) -> list[str]:
    """真正值得下载内容的第一方 JS/CSS（实测 24 个 / 1.78 MB）。

    图片只比 URL 不下载内容，字体等第三方一律不管。
    """
    urls: list[str] = []
    if isinstance(refs, dict):
        candidates = list(refs.get(JS, [])) + list(refs.get(CSS, []))
    else:
        candidates = [r.url for r in refs if r.kind in (JS, CSS)]
    for url in candidates:
        if url in urls:
            continue
        path = urllib.parse.urlsplit(url).path.lower()
        if path.endswith((".js", ".mjs", ".css")) and is_first_party(url):
            urls.append(url)
    return urls


_HASH_IN_NAME_RE = re.compile(r"[.\-_][0-9a-f]{8,}\.(?:js|mjs|css)$", re.I)
_HASH_ANY_RE = re.compile(r"[.\-_][0-9a-f]{8,}")


def is_content_hashed(url: str) -> bool:
    """文件名里带内容哈希（如 main.0c15113f.js）——同名即同内容。"""
    return bool(_HASH_IN_NAME_RE.search(urllib.parse.urlsplit(url).path))


def asset_stem(url: str) -> str:
    """去掉文件名里的哈希，用来把「改名」和「一增一删」区分开。

    main.0c15113f.js 和 main.9ab31f02.js → 同一个 stem 'main.js'。

    页面型 URL（canonical/alternate 那种没有扩展名、带尾斜杠的）必须用整条路径当 stem：
    否则 .../zh-cn/api/create-file/ 和 .../api/create-file/ 的最后一段都是空串，
    会被误配成「改名」。
    """
    path = urllib.parse.urlsplit(url).path or "/"
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if "." in name:  # 文件型：只留文件名，去掉哈希
        return _HASH_ANY_RE.sub("", name)
    return path.rstrip("/") or "/"


def asset_hash(url: str) -> str:
    """文件名里的哈希（没有就返回空串）。"""
    path = urllib.parse.urlsplit(url).path
    m = re.search(r"[.\-_]([0-9a-f]{8,})\.[A-Za-z0-9]+$", path)
    return m.group(1) if m else ""


def basename(url: str) -> str:
    return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1] or url


# ---------------------------------------------------------------- Next.js RSC 正文

_RSC_PUSH_RE = re.compile(r"self\.__next_f\.push\((.*?)\)\s*</script>", re.S)
_JSON_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_CTRL_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_CODE_HINTS = ("=>", "function(", "function (", "className", "useState",
               "createElement", "webpack", "__next", "application/", "$@")


def rsc_chunks(html: str) -> list[str]:
    """解出所有 `self.__next_f.push([1,"..."])` 的字符串片段（按出现顺序拼接）。"""
    chunks: list[str] = []
    for m in _RSC_PUSH_RE.finditer(html):
        arg = m.group(1).strip()
        try:
            val = json.loads(arg)
        except (ValueError, TypeError):
            continue
        if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], str):
            chunks.append(val[1])
    return chunks


def _prose_lines(text: str) -> list[str]:
    """从一段 RSC/JSON 文本里挑出「像正文的」中文字符串。"""
    out: list[str] = []
    for raw in _JSON_STR_RE.findall(text):
        try:
            s = json.loads('"' + raw + '"')
        except (ValueError, TypeError):
            s = raw
        s = _CTRL_RE.sub("", s).replace("\\n", "\n")
        for piece in s.split("\n"):
            piece = _WS_RE.sub(" ", piece).strip()
            if len(piece) < 2:
                continue
            if any(h in piece for h in _CODE_HINTS):
                continue
            cjk = len(_CJK_RE.findall(piece))
            if cjk < 2 and not (cjk >= 1 and len(piece) >= 8):
                continue
            if piece.startswith(("/", "http", "{")):
                continue
            out.append(piece)
    return out


def rsc_text(html: str) -> list[str]:
    """官网首页（Next.js）正文段落。抽不到就返回空列表，上层退回普通抽取。"""
    chunks = rsc_chunks(html)
    if not chunks:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for line in _prose_lines("".join(chunks)):
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


def extract_rsc_text(html: str) -> str:
    """RSC payload → 纯文本（`rsc_text` 的字符串形式，供 toolbox 复用）。"""
    return "\n".join(rsc_text(html))


def html_to_text(html: str) -> str:
    """HTML → 纯文本（toolbox 用；与 collect 的正文抽取同一套实现）。

    先按 `text_lines_from_html` 去标签；Next.js 页面正文只藏在 RSC payload 里时
    （`rsc_text` 抽到 ≥3 行，和 `signals.parse_page` 的兜底判断一致）以 RSC 为准。
    这样 toolbox 看到的正文和采集层落盘的正文口径一致，不再维护第二套解析。
    """
    if not html:
        return ""
    lines = text_lines_from_html(html, "")
    rsc = rsc_text(html)
    if len(rsc) >= 3:
        lines = rsc
    return "\n".join(lines)


# ---------------------------------------------------------------- sitemap

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_INDEX_RE = re.compile(r"<sitemapindex\b", re.I)


def sitemap_is_index(xml: str) -> bool:
    """sitemap.xml 是「子 sitemap 索引」而不是普通页面清单。"""
    return bool(_SITEMAP_INDEX_RE.search(xml or ""))


def parse_sitemap(xml: str) -> list[str]:
    """sitemap.xml → URL 列表（保持原顺序，去重）。"""
    out: list[str] = []
    for url in _LOC_RE.findall(xml):
        url = html_mod.unescape(url.strip())
        if url and url not in out:
            out.append(url)
    return out


def sitemap_children(xml: str) -> list[str]:
    """sitemapindex 里的子 sitemap 地址（保持顺序，去重）。"""
    return parse_sitemap(xml) if sitemap_is_index(xml) else []


def canon(url: str) -> str:
    """统一 URL：去锚点/查询串；文档站统一补末尾斜杠（少一跳 302、多命中 304）。"""
    url = url.split("#")[0].split("?")[0].strip()
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if (parts.hostname or "") == "api-docs.deepseek.com" and not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def zh_page_urls(sitemap: list[str]) -> list[str]:
    """英文 sitemap → 中文镜像地址（文档站中文路径就是英文路径加 /zh-cn 前缀）。

    只接受 api-docs.deepseek.com 下的页面；sitemap 索引里的子 sitemap 地址
    （.xml / 含 sitemap 的路径）一律跳过，避免拼出 /zh-cn/sitemap-0.xml/ 这种假页面。
    """
    root_netloc = urllib.parse.urlsplit(DOCS_ROOT).netloc
    out: list[str] = []
    for url in sitemap:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.netloc != root_netloc:
            continue
        path = parts.path or "/"
        low = path.lower()
        if low.endswith(".xml") or "/sitemap" in low:
            continue
        if path.startswith("/zh-cn/") or path == "/zh-cn":
            zh = path
        else:
            zh = "/zh-cn" + path
        full = canon(DOCS_ROOT + zh)
        if full not in out:
            out.append(full)
    return out


# ---------------------------------------------------------------- JS 字符串集合

_ESC_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)")
_HEX_ONLY_RE = re.compile(r"[0-9a-fA-F]+")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")
_URLISH_RE = re.compile(
    r"^(?:https?:)?//|^www\.|^[\w.-]+\.(?:js|mjs|css|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|map)$")
_PATHISH_RE = re.compile(r"^[./~][\w./~\-]*$")
_QUOTES = "\"'`"
_TPL_EXPR_RE = re.compile(r"\$\{.*?\}", re.S)

_UNESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _unescape(s: str) -> str:
    def rep(m: re.Match[str]) -> str:
        g = m.group(1)
        if len(g) > 1 and g[0] in "ux":
            try:
                return chr(int(g[1:], 16))
            except ValueError:
                return g
        return _UNESCAPE_MAP.get(g, g)
    return _ESC_RE.sub(rep, s)


def iter_js_strings(text: str):
    """按 JS 词法扫出字符串字面量，产出 (引号类型, 内容)。

    必须自己扫而不是用正则：压缩代码里常有 `'"'` 这种「单引号里包一个双引号」，
    正则会从那个双引号一直匹配到下一个双引号，捞出一堆代码碎片。
    """
    i, n = 0, len(text)
    while i < n:
        q = text[i]
        if q not in _QUOTES:
            i += 1
            continue
        j = i + 1
        buf: list[str] = []
        closed = False
        while j < n:
            c = text[j]
            if c == "\\" and j + 1 < n:
                buf.append(c)
                buf.append(text[j + 1])
                j += 2
                continue
            if c == q:
                closed = True
                break
            if c == "\n" and q != "`":
                break
            buf.append(c)
            j += 1
        if closed:
            yield q, "".join(buf)
            i = j + 1
        else:
            i = max(j + 1, i + 1)


def readable_string(s: str, min_len: int = 6) -> bool:
    """是不是一条「人能读」的字符串（而不是路径 / 哈希 / 压缩代码碎片）。"""
    if len(s) < min_len:
        return False
    if s.startswith(("/", "http://", "https://", "//", "#", "data:")):
        return False
    if _URLISH_RE.search(s) or _PATHISH_RE.search(s):
        return False
    if _HEX_ONLY_RE.fullmatch(s) or _LONG_TOKEN_RE.fullmatch(s):
        return False
    # 必须以字母 / 数字 / 中文开头：压缩代码碎片通常以运算符、括号开头
    if not re.match(r"[A-Za-z0-9\u4e00-\u9fff]", s):
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", s):
        return False
    # 代码碎片：分号、连续运算符、箭头函数这类，人读没意义
    if re.search(r"[;{}]|=>|&&|\|\||==|!==|\.(?:slice|push|map|forEach|split)\(", s):
        return False
    return True


def js_strings(text: str, min_len: int = 6) -> list[str]:
    """从压缩 JS/CSS 里抽「人可能读得懂」的字符串集合，排序后返回（好做 diff）。

    过滤掉：路径、URL、纯十六进制（sourcemap / 模块 id）、超长 base64、代码碎片。
    保留：中文文案、报错信息、模型名、价格单位、带空格的长句等。
    模板字符串里的 `${表达式}` 会被抹掉再判断，这样 `` `切换到${u}` `` 能留下「切换到」。
    """
    found: set[str] = set()
    for quote, raw in iter_js_strings(text):
        if quote == "`":
            raw = _TPL_EXPR_RE.sub("", raw)
        s = _WS_RE.sub(" ", _unescape(raw)).strip()
        if readable_string(s, min_len):
            found.add(s)
    return sorted(found)


def text_lines_from_html(html: str, base: str, title_only: bool = False) -> list[str]:
    """兜底用的极简正文抽取（不依赖 watch_docs，toolbox 单独跑也能用）。"""
    t = re.sub(r"(?is)<(script|style|nav|header|footer|aside|svg|noscript|iframe|form)\b.*?</\1>",
               " ", html)
    t = re.sub(r"(?is)<!--.*?-->", " ", t)
    t = re.sub(r"(?is)</?(p|div|br|li|tr|h[1-6]|section|article|pre|blockquote|td|th|dt|dd|ul|ol)[^>]*>",
               "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html_mod.unescape(t)
    t = _CTRL_RE.sub("", t)
    lines: list[str] = []
    for raw in t.split("\n"):
        line = _WS_RE.sub(" ", raw).strip()
        if len(line) < 2 or not re.search(r"[\w\u4e00-\u9fff]", line):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines
