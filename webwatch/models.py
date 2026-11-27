"""解析官方价格页的「模型 / 模型版本」表格。

价格页是一张没有 thead 的 HTML table，第一行是模型名，后面有一行「模型版本」。
这里用最小表格解析器按列对齐，官方改版导致解析失败时返回空列表，由调用方报告。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser


class _Cell:
    __slots__ = ("text", "colspan", "start")

    def __init__(self, text: str, colspan: int) -> None:
        self.text = text
        self.colspan = colspan
        self.start = 0


def _span(values: dict[str, str], key: str) -> int:
    """colspan 解析：缺失或非法值都当 1。"""
    try:
        return max(1, int(values.get(key) or "1"))
    except ValueError:
        return 1


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[_Cell]]] = []
        self._table: list[list[_Cell]] | None = None
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._cell_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = _Cell("", _span(values, "colspan"))
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell.text = re.sub(r"\s+", " ", " ".join(self._cell_parts)).strip()
            self._row.append(self._cell)
            self._cell = None
            self._cell_parts = []
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell_parts.append(data)


def _expand_row(row: list[_Cell]) -> list[_Cell]:
    """按 colspan 计算每列起点；同一单元格只保留一次。"""
    occupied = 0
    result: list[_Cell] = []
    for cell in row:
        cell.start = occupied
        occupied += cell.colspan
        result.append(cell)
    return result


def parse_model_versions(html_text: str) -> list[dict]:
    """返回 [{"model": "deepseek-flash", "version": "DeepSeek-V4.1-Flash-1017"}, ...]。"""
    parser = _TableParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # noqa: BLE001
        return []

    for table in parser.tables:
        rows = [_expand_row(row) for row in table]
        model_cells: dict[int, str] = {}
        model_found = False
        for row in rows:
            if not row:
                continue
            first = row[0].text.strip()
            if not model_found and first == "模型":
                for cell in row[1:]:
                    # 同时剥 ASCII 半角括号和中文全角括号注记（如 deepseek-v4-pro（Beta）），
                    # 否则全角注记会被当成一个新模型，直接触发 3 级「下架+新增」。
                    name = re.sub(r"[（(][^）)]*[）)]\s*$", "", cell.text).strip()
                    if name.startswith("deepseek-"):
                        model_cells[cell.start] = name
                model_found = bool(model_cells)
                continue
            if model_found and first == "模型版本":
                versions: list[dict] = []
                for cell in row[1:]:
                    if cell.start not in model_cells:
                        continue
                    version = cell.text.strip()
                    if version:
                        versions.append(
                            {"model": model_cells[cell.start], "version": version}
                        )
                if versions:
                    return versions
    return []
