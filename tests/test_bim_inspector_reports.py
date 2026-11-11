"""HTML and xlsx reports."""
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

from bim_inspector.checks import run_checks
from bim_inspector.model import validate_rules, validate_snapshot
from bim_inspector.report_html import render_html
from bim_inspector.report_xlsx import write_xlsx

from tests.bim_inspector_support import element, rules, snapshot

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_ALLOWED = {"html", "head", "meta", "title", "style", "body", "h1", "h2", "h3", "p", "div", "section",
            "table", "thead", "tbody", "tr", "th", "td", "span"}
_VOID = {"meta"}


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.ids = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in _ALLOWED:
            self.errors.append(tag)
        for key, value in attrs:
            if key.startswith("on") or key in ("href", "src", "srcset"):
                self.errors.append(f"{key}={value}")
            if key == "id":
                self.ids.append(value)
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"end {tag}")
        else:
            self.stack.pop()


def _checked(elements=None, rule=None):
    rule = rules() if rule is None else rule
    elements = [element(parameters={})] if elements is None else elements
    snap = validate_snapshot(snapshot(elements))
    validated = validate_rules(rule)
    findings = run_checks(snap, validated)
    return snap, validated, findings


def test_html_has_summary_detail_and_escaped_text():
    hostile = element(
        id="E-HTML",
        category='A&B<script>',
        family='=1+1',
        parameters={"SCADA_POINT": "P"},
    )
    snap, rule, findings = _checked([hostile])
    doc = render_html(snap, rule, findings)
    checker = _Checker()
    checker.feed(doc)
    checker.close()
    assert not checker.errors, checker.errors
    assert not checker.stack
    assert checker.ids == ["summary", "details"]
    assert "<script" not in doc.lower()
    assert "A&amp;B&lt;script&gt;" in doc
    assert "缺少必要參數 SCADA_TAG" in doc
    assert "必要參數" in doc and "未連接端點" in doc and "高程" in doc and "重複" in doc
    assert "零筆發現只代表這些規則沒有命中" in doc
    assert "SAMPLE-DEIDENTIFIED" in doc
    assert "javascript:" not in doc.lower()


def test_html_control_characters_are_visible_text():
    named = element(family="Fan\u0000", parameters={"SCADA_POINT": "P"})
    snap, rule, findings = _checked([named])
    doc = render_html(snap, rule, findings)
    assert "\u0000" not in doc
    assert "\\u0000" in doc


def test_html_is_deterministic():
    snap, rule, findings = _checked()
    assert render_html(snap, rule, findings) == render_html(snap, rule, findings)


def test_xlsx_has_two_sheets_escaped_text_and_no_formula(tmp_path):
    hostile = element(id="E-X", category="A&B<C>", family="=1+1", parameters={})
    snap, rule, findings = _checked([hostile])
    path = tmp_path / "report.xlsx"
    again = tmp_path / "report-again.xlsx"
    write_xlsx(path, snap, rule, findings)
    write_xlsx(again, snap, rule, findings)
    assert path.read_bytes() == again.read_bytes()
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/worksheets/sheet2.xml" in names
        assert not any("vba" in name.lower() for name in names)
        detail = archive.read("xl/worksheets/sheet2.xml")
        summary = archive.read("xl/worksheets/sheet1.xml")
    root = ElementTree.fromstring(detail)
    texts = [node.text or "" for node in root.findall(".//m:t", _NS)]
    assert "元件 Id" in texts
    assert "規則 Id" in texts
    assert "E-X" in texts
    assert "A&B<C>" in texts
    assert "'=1+1" in texts
    assert "缺少必要參數 SCADA_TAG" in texts
    assert "A&amp;B&lt;C&gt;" in detail.decode("utf-8")
    summary_root = ElementTree.fromstring(summary)
    summary_text = [node.text or "" for node in summary_root.findall(".//m:t", _NS)]
    numbers = [node.text for node in summary_root.findall(".//m:v", _NS)]
    assert "SAMPLE-DEIDENTIFIED" in summary_text
    assert "必要參數" in summary_text
    assert str(len(findings)) in numbers
