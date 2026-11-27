"""从 HTML 页面里抽取可比较的信号。

只做解析，不发网络请求。所有文本都有长度上限，避免异常页面把内存撑爆。
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from html.parser import HTMLParser

MAX_TEXT_CHARS = 60_000
MAX_META_CHARS = 500
MAX_ASSETS = 30


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    """压缩空白，保留可读的中文/英文内容，供摘要和比较用。"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:MAX_TEXT_CHARS]


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            key = (
                values.get("name")
                or values.get("property")
                or values.get("http-equiv")
                or ""
            ).strip().lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content[:MAX_META_CHARS]
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            src = values.get("src", "").strip()
            if src:
                self.scripts.append(src)
            self._skip_depth += 1
        elif tag == "style":
            self._skip_depth += 1
        elif tag == "link":
            rel = values.get("rel", "").lower()
            href = values.get("href", "").strip()
            if href and "stylesheet" in rel:
                self.styles.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0:
            self.text_parts.append(data)


def _absolute(url: str, base_url: str) -> str:
    return urllib.parse.urljoin(base_url, url.strip())


def _absolute_unique(urls: list[str], base_url: str) -> list[str]:
    """转绝对地址、去重并限个数；scripts / styles 共用。"""
    out: list[str] = []
    for url in urls:
        absolute = _absolute(url, base_url)
        if absolute not in out:
            out.append(absolute)
    return out[:MAX_ASSETS]


def extract_page(html_text: str, base_url: str) -> dict:
    """返回 title/meta/scripts/styles/text/text_sha256。解析失败时尽量保留已有内容。"""
    parser = _PageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001 - 异常 HTML 不应该让整轮监控失败
        pass
    title = normalize_text(" ".join(parser.title_parts))[:300]
    text = normalize_text(" ".join(parser.text_parts))
    return {
        "title": title,
        "meta": dict(parser.meta),
        "scripts": _absolute_unique(parser.scripts, base_url),
        "styles": _absolute_unique(parser.styles, base_url),
        "text": text,
        "text_sha256": sha256_text(text),
    }
