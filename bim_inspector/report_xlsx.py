"""Minimal xlsx writer (OOXML via zipfile and xml). Standard library only.

String cells that start with a formula trigger are prefixed with a quote so
Excel treats them as text.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import CHECK_LABELS, CHECK_ORDER
from .checks import Finding

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CTRL = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f]")
_FORMULA = ("=", "+", "-", "@")
_MAX_CELL = 32000

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
<sheet name="摘要" sheetId="1" r:id="rId1"/>
<sheet name="明細" sheetId="2" r:id="rId2"/>
</sheets>
</workbook>
"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>
"""


def write_xlsx(path: Path, snapshot: dict, rules: dict, findings: list[Finding]) -> None:
    summary = _summary_rows(snapshot, rules, findings)
    detail = _detail_rows(findings)
    payload = {
        "[Content_Types].xml": _CONTENT_TYPES.encode("utf-8"),
        "_rels/.rels": _RELS.encode("utf-8"),
        "xl/workbook.xml": _WORKBOOK.encode("utf-8"),
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS.encode("utf-8"),
        "xl/styles.xml": _STYLES.encode("utf-8"),
        "xl/worksheets/sheet1.xml": _sheet(summary).encode("utf-8"),
        "xl/worksheets/sheet2.xml": _sheet(detail).encode("utf-8"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in payload.items():
            info = zipfile.ZipInfo(filename=name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def _summary_rows(snapshot: dict, rules: dict, findings: list[Finding]) -> list[list]:
    counts = {check: 0 for check in CHECK_ORDER}
    for finding in findings:
        counts[finding.check] = counts.get(finding.check, 0) + 1
    rows: list[list] = [
        ["專案標籤", snapshot["source"]["project_label"]],
        ["單位", snapshot["source"]["units"]],
        ["發現總數", len(findings)],
        ["檢查項", "狀態", "發現數"],
    ]
    for check in CHECK_ORDER:
        state = "啟用" if rules["checks"][check]["enabled"] else "未啟用"
        rows.append([CHECK_LABELS[check], state, counts[check]])
    note = rules.get("notes") or ""
    if note:
        rows.append(["規則說明", note])
    return rows


def _detail_rows(findings: list[Finding]) -> list[list]:
    rows: list[list] = [["元件 Id", "品類", "內建品類", "族", "類型", "樓層", "檢查項", "規則 Id", "問題"]]
    for item in findings:
        rows.append([
            item.element_id,
            item.category,
            item.builtin_category,
            item.family,
            item.type_name,
            item.level,
            CHECK_LABELS.get(item.check, item.check),
            item.rule_id,
            item.issue,
        ])
    return rows


def _sheet(rows: list[list]) -> str:
    body = ["<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>", f'<worksheet xmlns="{_NS}"><sheetData>']
    for r_index, row in enumerate(rows, start=1):
        cells = []
        for c_index, value in enumerate(row):
            ref = f"{_column(c_index)}{r_index}"
            cells.append(_cell(ref, value))
        body.append(f'<row r="{r_index}">' + "".join(cells) + "</row>")
    body.append("</sheetData></worksheet>")
    return "".join(body)


def _cell(ref: str, value: object) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = "" if value is None else str(value)
    text = _CTRL.sub("", text)
    if len(text) > _MAX_CELL:
        text = text[:_MAX_CELL]
    if text.startswith(_FORMULA):
        text = "'" + text
    space = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f'<c r="{ref}" t="inlineStr"><is><t{space}>{escape(text)}</t></is></c>'


def _column(index: int) -> str:
    name = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name
