#!/usr/bin/env python3
"""CSV → XLSX，**只用标准库**（对齐 DSH「技能自带脚本」的做法）。

为什么手写而不是依赖 openpyxl：code-agent 的 venv 默认**没有** openpyxl
（pyproject 里只有 pydantic/httpx/fastapi/uvicorn/pyjwt）。发一份"请用 openpyxl"
的说明书等于发一个一跑就报 ImportError 的技能——那比不发更糟：模型会以为
自己会做 xlsx，然后拿一个 ImportError 去猜别的原因。所以这里用标准库把
xlsx（本质是一个装了几份 XML 的 zip）直接写出来，**开箱即用**。

能力边界（写清而不是含糊）：
- 单个或多个工作表、表头、列宽、字符串/数字/布尔/空单元格；
- **不做**：字体/颜色/边框/合并/公式/冻结窗格/图表/列筛选。
  需要这些时用 openpyxl（`pip install -e .[office]`），技能正文里也这么写。

用法：
    python write_xlsx.py --out report.xlsx --csv data.csv [--sheet Summary]
                         [--no-header] [--delimiter ,] [--widths 12,20,8]

数字推断规则（保守优先，猜错代价大）：
- 整串能解析成 int/float，且**没有前导零**（`007` 是编号不是数字）、
  不是 `nan`/`inf` → 写成数字单元格（Excel 里能直接求和）；
- 其余一律写成文本单元格——宁可是文本（用户自己转一下），也不要把
  `1-2` / `2026-10-07` / `3,200` 猜成奇怪的数。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

#: 前导零的串（`007`、`0012`）是编号，别当数字
_LEADING_ZERO = re.compile(r"^[+-]?0\d")
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheet_overrides}
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
</Relationships>"""

_SHEET_REL = (
    '<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
)


def _col_name(index: int) -> str:
    """0 → A、25 → Z、26 → AA。"""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def cell_xml(ref: str, value: object) -> str:
    """一个单元格。数字写 `<v>`，其余写 inlineStr（不需要 sharedStrings 表）。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _looks_numeric(text: str) -> bool:
    """保守的"这看着是数字吗"判断（见模块头部的规则）。"""
    if _LEADING_ZERO.match(text):
        return False
    if _INT.match(text) or _FLOAT.match(text):
        low = text.lower()
        return low not in ("nan", "inf", "-inf", "+inf")
    return False


def coerce(text: str) -> object:
    """CSV 里的字符串 → 单元格值（数字就转数字，其余保持文本）。"""
    value = (text or "").strip()
    if not value:
        return ""
    if not _looks_numeric(value):
        return text if text == value else text
    try:
        if _INT.match(value):
            return int(value)
        return float(value)
    except ValueError:
        return text


def rows_to_sheet_xml(rows: list[list[object]], widths: list[int] | None = None) -> str:
    cols = ""
    if widths:
        cols = "<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)
        ) + "</cols>"
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(
            cell_xml(f"{_col_name(c)}{r}", value) for c, value in enumerate(row)
        )
        body.append(f'<row r="{r}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{cols}<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def write_xlsx(path: Path, sheets: dict[str, list[list[object]]], widths: list[int] | None = None) -> Path:
    """把 {工作表名: 行} 写成 xlsx。返回落盘路径。"""
    if not sheets:
        raise ValueError("至少要有一个工作表")
    names = list(sheets)
    sheet_tags = "".join(
        f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, n in enumerate(names)
    )
    overrides = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(names))
    )
    rels = "\n".join(_SHEET_REL.format(i=i + 1) for i in range(len(names)))

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES.format(sheet_overrides=overrides))
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", _WORKBOOK.format(sheets=sheet_tags))
        zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS.format(rels=rels))
        for i, name in enumerate(names, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", rows_to_sheet_xml(sheets[name], widths))
    return target


def read_csv(path: Path, delimiter: str | None) -> list[list[object]]:
    """读 CSV。默认嗅探分隔符；编码先用 utf-8-sig（Excel 导出的 BOM 头）。"""
    raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    return [[coerce(cell) for cell in row] for row in reader]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CSV -> XLSX（只用标准库）")
    parser.add_argument("--out", required=True, help="输出 .xlsx 路径")
    parser.add_argument("--csv", required=True, help="输入 CSV 路径")
    parser.add_argument("--sheet", default="Sheet1", help="工作表名（默认 Sheet1）")
    parser.add_argument("--no-header", action="store_true", help="第一行不是表头")
    parser.add_argument("--delimiter", default=None, help="CSV 分隔符（默认嗅探）")
    parser.add_argument("--widths", default="", help="列宽，逗号分隔，如 12,20,8")
    args = parser.parse_args(argv)

    rows = read_csv(Path(args.csv), args.delimiter)
    if args.no_header:
        rows = [[""] * (len(rows[0]) if rows else 0)] + rows
    widths = [int(w) for w in args.widths.split(",") if w.strip()] or None
    out = write_xlsx(Path(args.out), {args.sheet: rows}, widths)
    print(json.dumps({"ok": True, "out": str(out), "rows": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
