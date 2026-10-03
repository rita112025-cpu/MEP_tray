import copy
import html
import re
from html.parser import HTMLParser

import pytest

from mep_tray import pipeline as P
from mep_tray import report as R
from mep_tray import versioning as V
from mep_tray.findings import Finding, Report
from mep_tray.rules import load_rules
from tests.test_pipeline import ALL, isolate, sample  # noqa: F401  (isolate 為 autouse fixture)

SECTION_IDS = ["disclosure", "inputs", "governing", "findings", "compliance", "route", "files"]
ALLOWED_TAGS = {"html", "head", "meta", "title", "style", "body", "h1", "h2", "p", "div", "ul", "li", "table",
                "caption", "thead", "tbody", "tr", "th", "td", "section", "span", "strong", "footer"}
VOID = {"meta"}


class Checker(HTMLParser):
    """白名單標籤、無 on* 屬性、無 javascript: URL、標籤成對、id 唯一。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.ids, self.errors, self.title, self._in_title = [], [], [], "", False

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag {tag}")
        for k, v in attrs:
            if k.startswith("on") or (v or "").lower().startswith("javascript:") or k in ("src", "href", "srcset"):
                self.errors.append(f"attr {k}={v}")
            if k == "id":
                self.ids.append(v)
        if tag not in VOID:
            self.stack.append(tag)
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> stack={self.stack[-3:]}")
        else:
            self.stack.pop()
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def check_html(doc: str) -> Checker:
    c = Checker()
    c.feed(doc)
    c.close()
    assert not c.errors, c.errors[:5]
    assert not c.stack, c.stack
    assert len(c.ids) == len(set(c.ids)), "id 重複"
    return c


def run_ok(run_id="rp1", inp=None, codes=ALL, **kw):
    r = P.run(inp or sample(), codes, run_id, make_dwg=False, **kw)
    assert r.ok, r.error
    return r


def positions(doc):
    return [doc.index(f'<section id="{sid}"') for sid in SECTION_IDS]


# ───────────── 揭露與順序 ─────────────
def test_disclosure_is_first_section_even_when_all_unverified_and_zero_findings(isolate):
    from mep_tray.model import Inputs
    r = run_ok(inp=Inputs(cell_m=0.25))
    assert r.stats["findings_by_kind_status"] == {} and r.stats["unverified_checks"] > 0
    doc = R.render_report(r)
    check_html(doc)
    body = doc[doc.index("<body>"):]
    assert body.index('<section id="disclosure"') < body.index('<section id="inputs"')
    assert body.index('<section id="disclosure"') < body.index("零問題（僅涵蓋")       # 揭露在零問題訊息之前
    assert R.BANNER_FIXED in doc and "不得視為合規" in doc and "零問題不代表合規" in doc
    fnd = doc[doc.index('<section id="findings"'):doc.index('<section id="compliance"')]
    assert "零問題" in fnd and "符合規範" not in fnd


def test_disclosure_first_also_when_findings_exist_and_sections_are_in_fixed_order(isolate):
    r = run_ok()
    assert r.stats["findings_by_kind_status"]
    doc = R.render_report(r)
    check_html(doc)
    pos = positions(doc)
    assert pos == sorted(pos) and len(set(pos)) == len(pos)
    assert '<section id="diff"' not in doc


def test_all_disclosures_from_the_run_are_listed_verbatim(isolate):
    r = run_ok(notes=["自訂備註"])
    doc = R.render_report(r)
    for line in r.disclosures:
        assert html.escape(line, quote=True) in doc


# ───────────── 自足 / 列印 ─────────────
def test_report_is_self_contained_with_csp_and_print_css(isolate):
    doc = R.render_report(run_ok())
    for banned in ("http://", "https://", "<script", "<link", "src=", "@import", "url(", "<iframe", "<img", "<form"):
        assert banned not in doc, banned
    assert 'http-equiv="Content-Security-Policy"' in doc and "default-src 'none'" in doc
    assert "@media print" in doc and 'lang="zh-Hant"' in doc and '<meta charset="utf-8">' in doc
    assert doc.startswith("<!DOCTYPE html>")


# ───────────── 跳脫（XSS）─────────────
def marker(n):
    return f"<x{n}>&\"q'z{n}"


def test_known_xss_payloads_are_rendered_as_literal_text(isolate):
    payloads = ['<script>alert(1)</script>', '"><img src=x onerror=1>', "&amp;", "' onmouseover='x", "javascript:alert(1)",
                "</td><td>injected", "<!-- c -->", "<style>*{display:none}</style>"]
    for n, p in enumerate(payloads):
        obs = [{"name": p, "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]
        r = run_ok(f"xss{n}", inp=sample(obstacles=obs), type_name="T " + p, notes=[p])
        doc = R.render_report(r)
        check_html(doc)
        assert html.escape(p, quote=True) in doc
        if "<" in p:
            assert p not in doc


def test_every_user_controllable_string_is_escaped_in_every_table_property_test(isolate):
    rules = load_rules()
    m = {k: marker(i) for i, k in enumerate(["obst", "type", "note", "cable_kind", "clause", "label", "code_name"])}
    rules["codes"]["CNS"]["clause"] = m["clause"]
    rules["params"]["span_max_m"]["label"] = m["label"]
    rules["codes"][m["code_name"]] = rules["codes"].pop("CNS")                   # 規範代號本身也是可控字串
    codes = [m["code_name"] if c == "CNS" else c for c in ALL]
    inp = sample(obstacles=[{"name": m["obst"], "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}],
                 cables=[{"od_mm": 30, "count": 40, "kind": m["cable_kind"]}])
    r = P.run(inp, codes, "prop1", make_dwg=False, rules=rules, type_name=m["type"], notes=[m["note"]])
    assert r.ok, r.error
    doc = R.render_report(r)
    check_html(doc)
    for name, raw in m.items():
        assert raw not in doc, f"{name} 未跳脫"
        assert html.escape(raw, quote=True) in doc, f"{name} 未出現"


def test_markers_in_findings_checks_and_files_tables_are_escaped():
    f = Finding("clash", "CLASH", "CLASH", marker(1), (1.0, 2.0, 3.0), 0.0, 400.0, "mm", marker(2), marker(3), False,
                marker(4), {"axis": "Y", "sign": "+", "move_mm": 150.0})
    c = Finding("clearance", "UNVERIFIED", "PASS", marker(5), (0, 0, 0), 500.0, 400.0, "mm", marker(6), marker(7), False)
    rep = Report(findings=[f], checks=[f, c])
    from mep_tray.model import Inputs
    from mep_tray.router import Route
    manifest = {"hashes": {"input_sha256": "a", "rules_sha256": "b", "rules_snapshot_sha256": "c", "result_sha256": "d"},
                "engine": {"version": marker(8)}, "environment": {marker(9): marker(10)},
                "files": {"dxf": {"name": marker(11), "bytes": 1, "sha256": marker(12), "determinism": marker(13)}},
                "options": {"type_name": marker(14), "basis": marker(15)}}
    d = R.ReportData("r1", Inputs(), ["CNS"], {}, Route([], [], [], 0.0, cell_m=0.25), {"clash": rep},
                     {"unverified_checks": 1}, manifest, [marker(16)], marker(17), None)
    doc = R.render_report_data(d)
    check_html(doc)
    for n in range(1, 18):
        assert marker(n) not in doc and html.escape(marker(n), quote=True) in doc, n


def test_title_is_escaped_and_html_is_well_formed(isolate):
    r = run_ok("title1")
    c = check_html(R.render_report(r))
    assert c.title == "MEP 橋架設計報告 title1"


# ───────────── 內容 ─────────────
def test_governing_table_lists_winner_clause_and_verified_text_for_every_parameter(isolate):
    r = run_ok()
    doc = R.render_report(r)
    sec = doc[doc.index('<section id="governing"'):doc.index('<section id="findings"')]
    for key, g in r.gov.items():
        assert html.escape(key) in sec and html.escape(g.clause, quote=True) in sec and html.escape(g.code) in sec
    assert "未驗證" in sec and "下限（≥）" in sec and "上限（≤）" in sec


def test_fix_field_is_rendered_from_the_structured_fix_not_parsed_from_text():
    f = Finding("clearance", "FAIL", "FAIL", "水管（water）", (1.0, 1.0, 3.0), 200.0, 400.0, "mm", "MRT_APPX_C", "附錄C",
                True, "文字建議（故意與 fix 不同）", {"axis": "Y", "sign": "-", "move_mm": 275.5})
    from mep_tray.model import Inputs
    from mep_tray.router import Route
    d = R.ReportData("r1", Inputs(), ["MRT_APPX_C"], {}, Route([], [], [], 0.0, cell_m=0.25),
                     {"clash": Report(findings=[f], checks=[f])}, {"unverified_checks": 0}, None, [])
    doc = R.render_report_data(d)
    assert "沿 -Y 移動至少 275.5 mm" in doc and "文字建議（故意與 fix 不同）" in doc
    assert R.badge("FAIL") in R.badge("FAIL") and "不符合" in doc


def test_compliance_table_has_pass_and_unverified_rows_with_unverified_text(isolate):
    r = run_ok()
    doc = R.render_report(r)
    sec = doc[doc.index('<section id="compliance"'):doc.index('<section id="route"')]
    assert "UNVERIFIED" in sec and "規範值未驗證" in sec and sec.count("<tr>") > 3
    assert r.manifest["hashes"]["input_sha256"] in doc and r.manifest["hashes"]["result_sha256"] in doc


def test_route_section_marks_default_hanger_span(isolate):
    from mep_tray.model import Inputs
    r = run_ok("hs-d", inp=Inputs(cell_m=0.25), codes=["TW_BUILDING"])
    assert "預設值，非規範值" in R.render_report(r)
    r2 = run_ok("hs-c", inp=Inputs(cell_m=0.25), codes=["CNS"])
    assert "預設值，非規範值" not in R.render_report(r2)


def test_default_cables_are_flagged_and_given_cables_are_listed(isolate):
    from mep_tray.model import Inputs
    assert "預設電纜" in R.render_report(run_ok("dc1", inp=Inputs(cell_m=0.25)))
    doc = R.render_report(run_ok("dc2", inp=sample()))
    assert "<caption>電纜</caption>" in doc


# ───────────── 差異 ─────────────
def two_runs(isolate_unused=None, **b_kwargs):
    a = run_ok("df-a")
    b = run_ok("df-b", **b_kwargs)
    return a, b


def test_diff_section_only_when_given_and_shows_source_and_engine_note(isolate):
    a, b = two_runs(inp=sample(tray_w_mm=400))
    d = V.compare("df-a", "df-b")
    assert d.source == "inputs"
    doc = R.render_report(b, diff=d)
    check_html(doc)
    sec = doc[doc.index('<section id="diff"'):]
    assert "輸入變更" in sec and "source=inputs" in sec and R.ENGINE_CHANGED_NOTE not in sec
    assert "inputs.tray_w_mm" in sec and "<section id=\"diff\"" not in R.render_report(b)
    m2 = copy.deepcopy(b.manifest)
    m2["engine"]["code_sha256"] = "0" * 64
    d2 = V.compare_manifests(a.manifest, m2)
    assert d2.engine_changed and R.ENGINE_CHANGED_NOTE in R.render_report(b, diff=d2)


def test_refused_diff_shows_only_the_reason(isolate):
    run_ok("df-ok")
    d = V.compare("df-ok", "no-such-version")
    doc = R.render_report(run_ok("df-cur"), diff=d)
    sec = doc[doc.index('<section id="diff"'):]
    assert "拒絕比對" in sec and "missing_version" in sec and "輸入差異" not in sec


def test_hash_only_rule_change_is_explained_in_diff(isolate):
    run_ok("hr-a")
    rules = load_rules()
    rules["codes"]["NEC"]["values"]["span_max_m"] = 2.9
    b = run_ok("hr-b", rules=rules)
    doc = R.render_report(b, diff=V.compare("hr-a", "hr-b"))
    assert "不算規則變更" in doc and "輸入與規範值皆無變更" in doc


def test_markers_in_diff_tables_are_escaped(isolate):
    run_ok("dm-a")
    rules = load_rules()
    rules["codes"]["CNS"]["clause"] = marker(1)
    obs = [{"name": marker(2), "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]
    b = run_ok("dm-b", inp=sample(obstacles=obs), rules=rules)
    doc = R.render_diff_report(V.compare("dm-a", "dm-b"), marker(3), marker(4))
    check_html(doc)
    for n in (1, 2, 3, 4):
        assert marker(n) not in doc and html.escape(marker(n), quote=True) in doc, n


def test_diff_row_truncation_is_announced(isolate, monkeypatch):
    monkeypatch.setattr(R, "MAX_SEGMENTS", 1)
    monkeypatch.setattr(R, "MAX_DIFF_ROWS", 1)
    run_ok("tr-a")
    moved = sample(obstacles=[{"name": "W", "kind": "water", "lo": [5, 2, 0], "hi": [5.3, 6, 3.2]}], tray_w_mm=400)
    run_ok("tr-b", inp=moved)
    d = V.compare("tr-a", "tr-b")
    assert len(d.result["segments"]["removed"]) > 1 or len(d.result["segments"]["added"]) > 1
    doc = R.render_diff_report(d, "A", "B")
    assert "僅顯示前 1 筆" in doc and "共 " in doc


def test_standalone_diff_report_is_well_formed_and_has_no_other_sections(isolate):
    run_ok("sd-a")
    run_ok("sd-b", inp=sample(tray_w_mm=400))
    doc = R.render_diff_report(V.compare("sd-a", "sd-b"), "A", "B")
    c = check_html(doc)
    assert c.ids == ["diff"] and "<h1>MEP 版本差異報告</h1>" in doc


# ───────────── 截斷 ─────────────
def test_long_tables_are_truncated_with_a_visible_notice(isolate, monkeypatch):
    monkeypatch.setattr(R, "MAX_OBSTACLES", 2)
    obs = [{"name": f"o{i}", "kind": "other", "lo": [2 + i * 1.0, 4.5, 0], "hi": [2.2 + i * 1.0, 5, 1]} for i in range(5)]
    doc = R.render_report(run_ok("trunc1", inp=sample(obstacles=obs)))
    sec = doc[doc.index('<section id="inputs"'):doc.index('<section id="governing"')]     # 被截斷的是輸入摘要那張表
    assert "僅顯示前 2 筆障礙物，共 5 筆" in sec and "o4" not in sec and "o1" in sec
    f = Finding("fill", "FAIL", "FAIL", "電纜填充", (0, 0, 0), 0.6, 0.4, "ratio", "CNS", "x", True)
    monkeypatch.setattr(R, "MAX_FINDINGS", 3)
    from mep_tray.model import Inputs
    from mep_tray.router import Route
    d = R.ReportData("r", Inputs(), ["CNS"], {}, Route([], [], [], 0.0, cell_m=0.25),
                     {"clash": Report(findings=[f] * 7, checks=[f] * 7)}, {"unverified_checks": 0}, None, [])
    out = R.render_report_data(d)
    assert out.count("僅顯示前 3 筆問題，共 7 筆") == 1 and out.count("僅顯示前 3 筆檢查項，共 7 筆") == 1


# ───────────── 確定性 / 脫敏 ─────────────
def test_report_bytes_are_identical_for_same_input_and_run_id_across_roots_and_time(isolate, monkeypatch, tmp_path):
    import time
    a = R.render_report(run_ok("dt1"))
    time.sleep(1.1)
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "other"))
    b = R.render_report(run_ok("dt1"))
    assert a == b and a.encode("utf-8") == b.encode("utf-8")
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", a)                                 # 不含 ISO 建立時間


def test_report_never_contains_local_paths(isolate):
    import tempfile
    from pathlib import Path
    home = str(Path.home())
    obs = [{"name": f"at {home}", "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]
    doc = R.render_report(run_ok("pth1", inp=sample(obstacles=obs)))
    assert home.lower() not in doc.lower() and tempfile.gettempdir().lower() not in doc.lower()
    assert not re.search(r"[A-Za-z]:[\\/]", doc)


def test_fmt_is_locale_free_and_normalises_negative_zero_and_none():
    assert R.fmt(-0.0) == "0" and R.fmt(1.23456) == "1.235" and R.fmt(300.0) == "300" and R.fmt(None) == "—"
    assert R.fmt([1.0, 2.5]) == "1, 2.5" and R.fmt(True) == "是" and R.fmt(7) == "7"


# ───────────── 固定句單一來源 / 機器可讀版 ─────────────
def test_fixed_sentences_come_from_disclosure_module_single_source():
    from mep_tray import disclosure as D
    assert R.BANNER_FIXED is D.BANNER_FIXED and R.ENGINE_CHANGED_NOTE is D.ENGINE_CHANGED_NOTE
    assert "不得視為合規" in D.BANNER_FIXED and "引擎版本亦不同" in D.ENGINE_CHANGED_NOTE
    assert D.REVIT_STATUS in R.render_report_data(R.ReportData(
        "r", __import__("mep_tray.model", fromlist=["Inputs"]).Inputs(), ["CNS"], {},
        __import__("mep_tray.router", fromlist=["Route"]).Route([], [], [], 0.0, cell_m=0.25),
        {}, {"unverified_checks": 0}, {"hashes": {"input_sha256": "a", "rules_sha256": "b", "rules_snapshot_sha256": "c",
                                                  "result_sha256": "d"}, "engine": {}, "files": {}, "options": {}}, []))


# ───────────── 審查補強：控制字元、長度、總計、全 verified、屬性白名單、大小 ─────────────
def manual_data(findings=(), checks=None, obstacles=(), run_id="m1", unverified=1, manifest=None):
    from mep_tray.model import Inputs
    from mep_tray.router import Route
    rep = Report(findings=list(findings), checks=list(findings if checks is None else checks))
    return R.ReportData(run_id, Inputs(obstacles=list(obstacles)), ["CNS"], {},
                        Route([], [], [], 0.0, cell_m=0.25), {"clash": rep}, {"unverified_checks": unverified},
                        manifest, ["x"])


def test_control_and_bidi_characters_are_made_visible_never_emitted_raw(isolate):
    bad = [chr(0x202E), chr(0x202A), chr(0x2066), chr(0x2069), chr(0), chr(13), chr(0x7F), chr(0x200F), chr(0xFEFF),
           chr(0x2028), chr(1), chr(0x9F)]
    name = "A" + "".join(bad) + "B"
    r = run_ok("ctl1", inp=sample(obstacles=[{"name": name, "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]),
               notes=["note" + chr(0x202E) + "x"])
    doc = R.render_report(r)
    check_html(doc)
    for ch in bad + [chr(0x202E)]:
        assert ch not in doc, hex(ord(ch))
    bs = chr(92)
    assert bs + "u202E" in doc and bs + "u0000" in doc and bs + "u000D" in doc               # 以可見的字面值取代
    assert R.clean("a" + chr(9) + "b") == "a" + chr(9) + "b" and R.clean("a" + chr(10) + "b") == "a" + chr(10) + "b"  # \n \t 保留


def test_overlong_names_and_notes_are_truncated_with_ellipsis(isolate):
    long = "N" * 5000 + "TAIL"
    r = run_ok("long1", inp=sample(obstacles=[{"name": long, "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]),
               notes=["M" * 5000 + "ENDNOTE"])
    doc = R.render_report(r)
    assert "TAIL" not in doc and "ENDNOTE" not in doc and "N" * R.MAX_CELL not in doc and "…" in doc
    assert len(doc.encode("utf-8")) < 200_000
    assert R.clean("x" * R.MAX_CELL) == "x" * R.MAX_CELL and len(R.clean("x" * (R.MAX_CELL + 1))) == R.MAX_CELL


def synthetic_findings(n):
    out = []
    for i in range(n):
        kind, status = [("clearance", "FAIL"), ("fill", "UNVERIFIED"), ("bend", "FAIL")][i % 3]
        out.append(Finding(kind, status, "FAIL", f"s{i}", (0, 0, 0), 1.0, 2.0, "mm", "CNS", "c", status != "UNVERIFIED"))
    return out


def test_totals_are_printed_untruncated_before_a_truncated_table(isolate):
    n = R.MAX_FINDINGS + 201
    doc = R.render_report_data(manual_data(synthetic_findings(n), run_id="tot1"))
    check_html(doc)
    fnd = doc[doc.index('<section id="findings"'):doc.index('<section id="compliance"')]
    assert fnd.index("總計（未截斷）") < fnd.index("<table>")                       # 總計在表格之前
    assert f"共 {n} 筆問題" in fnd
    assert f"僅顯示前 {R.MAX_FINDINGS} 筆問題，共 {n} 筆" in fnd and "tray_tot1.json" in fnd and "DXF 標註" in fnd
    by = {k: sum(1 for i in range(n) if i % 3 == j) for j, k in enumerate(["clearance", "fill", "bend"])}
    assert f"clearance／不符合：{by['clearance']}" in fnd and f"fill／規範值未驗證：{by['fill']}" in fnd
    assert f"規範值未驗證 {by['fill']} 筆問題" in fnd
    assert fnd.count("<tr><td>") == R.MAX_FINDINGS                                   # 表格確實只有上限列


def test_revit_json_carries_status_and_fix_so_truncated_findings_are_recoverable(isolate):
    import json
    r = run_ok("rj1")
    m = json.loads(r.files["revit_json"].read_text(encoding="utf-8"))
    assert m["findings"] and all("status" in f and "fix" in f and "subject" in f for f in m["findings"])


def all_verified_rules():
    rules = load_rules()
    for c in rules["codes"].values():
        c["verified"] = True
    return rules


def test_scope_note_always_present_and_unverified_warning_only_when_needed(isolate):
    from mep_tray import disclosure as D
    from mep_tray.model import Inputs
    ver = run_ok("allv", inp=Inputs(cell_m=0.25), rules=all_verified_rules())
    assert ver.stats["unverified_checks"] == 0
    doc = R.render_report(ver)
    check_html(doc)
    assert D.SCOPE_NOTE in doc and "不得視為合規" not in doc and D.BANNER_FIXED not in doc
    assert "未驗證" not in doc.split('<section id="governing"')[0]                    # 揭露章節沒有亂報未驗證
    unv = R.render_report(run_ok("somev", inp=Inputs(cell_m=0.25)))
    assert D.SCOPE_NOTE in unv and D.BANNER_FIXED in unv
    assert "不得視為合規" not in D.SCOPE_NOTE


def test_attribute_names_and_values_are_internal_literals_only(isolate):
    obs = [{"name": '"><b id="evil" class="x" onclick="1">', "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]
    r = run_ok("attr1", inp=sample(obstacles=obs), type_name='" onmouseover="x')
    seen = []

    class Attrs(HTMLParser):
        def handle_starttag(self, tag, attrs):
            seen.extend((tag, k, v) for k, v in attrs)

    a = Attrs()
    a.feed(R.render_report(r, diff=V.compare("attr1", "attr1")))
    names = {k for _, k, _ in seen}
    assert names <= {"id", "class", "lang", "charset", "http-equiv", "content", "name"}, names
    ids = {v for _, k, v in seen if k == "id"}
    assert ids <= set(SECTION_IDS) | {"diff"}
    classes = {v for _, k, v in seen if k == "class"}
    assert classes <= {"banner", "note", "warn", "mono", "badge ok", "badge unv", "badge bad"}, classes
    fixed = {("html", "lang", "zh-Hant"), ("meta", "charset", "utf-8"), ("meta", "name", "viewport"),
             ("meta", "http-equiv", "Content-Security-Policy")}
    assert fixed <= set(seen)
    assert all(v in ("width=device-width, initial-scale=1",) or "default-src 'none'" in v
               for t, k, v in seen if k == "content")


def test_report_size_is_bounded_for_worst_case_synthetic_input(isolate):
    obs = [{"name": f"obstacle-{i}-" + "z" * 250, "kind": "other", "lo": [1, 1, 1], "hi": [2, 2, 2]} for i in range(1000)]
    doc = R.render_report_data(manual_data(synthetic_findings(3000), obstacles=obs))
    assert len(doc.encode("utf-8")) < 2_000_000
    check_html(doc)


def test_diff_section_is_rendered_by_one_shared_function(isolate, monkeypatch):
    monkeypatch.setattr(R, "diff_body", lambda diff, a="A", b="B": Raw_marker)
    run_ok("sh-a")
    r = run_ok("sh-b", inp=sample(tray_w_mm=400))
    d = V.compare("sh-a", "sh-b")
    assert Raw_marker in R.render_report(r, diff=d) and Raw_marker in R.render_diff_report(d, "A", "B")


Raw_marker = "<p>SHARED-DIFF-BODY-MARKER</p>"


# ───────────── 整合：報告存進版本資料夾並進 manifest ─────────────
def flip_byte(p):
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 1
    p.write_bytes(bytes(b))


def test_pipeline_stores_report_in_version_folder_and_lists_it_in_the_manifest(isolate):
    r = run_ok("pr1")
    rp = isolate / "pr1" / "report_pr1.html"
    assert r.files["report"] == rp and rp.is_file()
    ent = r.manifest["files"]["report"]
    assert ent["name"] == "report_pr1.html" and ent["determinism"] == "same-run-id-no-cad"
    assert ent["sha256"] == V.file_sha256(rp) and ent["bytes"] == rp.stat().st_size
    check_html(rp.read_text(encoding="utf-8"))
    assert V.load_version("pr1").ok
    assert r.to_summary_dict()["files"]["report"] == "pr1/report_pr1.html"


def test_report_inside_the_version_matches_a_fresh_render_of_the_same_run(isolate):
    r = run_ok("pr2")
    stored = (isolate / "pr2" / "report_pr2.html").read_text(encoding="utf-8")
    again = R.render_report(r)
    # 已存檔的報告渲染自「尚未含報告」的 manifest，所以檔案表不列報告本身；其餘內容必須一致
    assert 'id="files"' in stored and "report_pr2.html" not in stored.split('<section id="files"')[1]
    assert stored.split('<section id="files"')[0] == again.split('<section id="files"')[0]


def test_tampered_or_missing_report_is_detected_by_version_load(isolate):
    r = run_ok("pr3")
    flip_byte(isolate / "pr3" / "report_pr3.html")
    assert V.load_version("pr3").error.code == "file_tampered"
    (isolate / "pr3" / "report_pr3.html").unlink()
    assert V.load_version("pr3").error.code == "missing_file"


def test_old_manifests_without_a_report_entry_still_load(isolate):
    import json
    r = run_ok("pr4")
    mp = isolate / "pr4" / V.MANIFEST_NAME
    m = json.loads(mp.read_text(encoding="utf-8"))
    del m["files"]["report"]                                # 模擬舊版（報告功能之前）的 manifest
    mp.write_text(json.dumps(m), encoding="utf-8")
    lr = V.load_version("pr4")
    assert lr.ok and "report" not in lr.manifest["files"]
    assert V.compare("pr4", "pr4").ok


def test_report_entry_with_unsafe_name_is_rejected(isolate):
    import json
    run_ok("pr5")
    mp = isolate / "pr5" / V.MANIFEST_NAME
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["files"]["report"]["name"] = "../../evil.html"
    mp.write_text(json.dumps(m), encoding="utf-8")
    assert V.load_version("pr5").error.code == "corrupt_manifest"


def test_failed_runs_leave_no_report_and_no_folder(isolate, monkeypatch):
    monkeypatch.setattr(P.RP, "render_report_data", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "disk full")))
    r = P.run(sample(), ALL, "pr6", make_dwg=False)
    assert r.ok is False and r.error.code == "os_error" and not (isolate / "pr6").exists()
    assert V.scan_versions().stage_leftovers == 0


def test_report_bytes_are_deterministic_through_the_pipeline_for_same_run_id(isolate, monkeypatch, tmp_path):
    import time
    a = run_ok("pr7")
    time.sleep(1.1)
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "other"))
    b = run_ok("pr7")
    assert a.files["report"].read_bytes() == b.files["report"].read_bytes()
    assert a.manifest["files"]["report"]["sha256"] == b.manifest["files"]["report"]["sha256"]


def test_rendering_cost_is_bounded_by_the_row_limits_not_by_input_size(isolate):
    import time
    small = manual_data(synthetic_findings(R.MAX_FINDINGS))
    big = manual_data(synthetic_findings(50_000))
    t0 = time.perf_counter()
    R.render_report_data(small)
    t_small = time.perf_counter() - t0
    t0 = time.perf_counter()
    doc = R.render_report_data(big)
    t_big = time.perf_counter() - t0
    assert "共 50000 筆問題" in doc and len(doc.encode("utf-8")) < 2_000_000
    assert t_big < max(5.0, 4 * t_small + 1.0)               # 輸入放大 100 倍，成本不得跟著放大（只剩總計統計）


def test_sanitize_cache_follows_environment_changes(isolate, monkeypatch, tmp_path):
    from mep_tray.sanitize import sanitize_text
    bs = chr(92)
    a, b = tmp_path / "rootA", tmp_path / "rootB"
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(a))
    assert sanitize_text(f"x {a}{bs}f.dxf") == "x <OUT>" + bs + "f.dxf" or "<OUT>" in sanitize_text(f"x {a}{bs}f.dxf")
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(b))
    t = sanitize_text(f"x {b}{bs}f.dxf")
    assert str(b) not in t and "<OUT>" in t
