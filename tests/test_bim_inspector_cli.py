"""CLI and the de-identified sample fixtures."""
import json

from bim_inspector.checks import run_checks
from bim_inspector.cli import main
from bim_inspector.model import load_rules, load_snapshot

from tests.bim_inspector_support import FIXTURES


def _expected_keys():
    return {
        ("EQ-SCADA-MECH", "E-AHU-MISSING", "AHU-Sample"),
        ("EQ-SCADA-MECH", "E-AHU-EMPTY", "AHU-Sample"),
        ("OPEN-END", "E-TRAY-OPEN", "Tray-Sample"),
        ("OPEN-END", "E-DUCT-LOW", "Duct-Sample"),
        ("ELEV-DUCT-SAMPLE", "E-DUCT-LOW", "Duct-Sample"),
        ("ELEV-LEVEL-MISMATCH", "E-PUMP-HIGH", "Pump-Sample"),
        ("DUP-LOCATION", "E-PUMP-A", "Pump-Sample"),
        ("DUP-LOCATION", "E-PUMP-B", "Pump-Sample"),
        ("DUP-FAMILY-NAME", "", "Fan-Sample (2)"),
        ("DUP-FAMILY-NAME", "", "Tray-Sample1"),
        ("COUNT-AHU-SAMPLE", "", "AHU-Sample"),
    }


def test_sample_fixture_hits_each_check():
    snap = load_snapshot(FIXTURES / "sample_snapshot.json")
    rule = load_rules(FIXTURES / "sample_rules.json")
    findings = run_checks(snap, rule)
    got = {(item.rule_id, item.element_id, item.family) for item in findings}
    assert got == _expected_keys()
    checks = {item.check for item in findings}
    assert checks == {"required_parameters", "open_connectors", "elevation", "duplicates"}
    joined = "\n".join(item.issue for item in findings)
    for snippet in (
        "缺少必要參數 SCADA_TAG",
        "參數 SCADA_POINT 值為空",
        "未連接端點 index=1",
        "未連接端點 index=0",
        "偏移 1000 mm",
        "較近樓層為 Level-Sample-2",
        "同群元件：E-PUMP-A, E-PUMP-B",
        "族名稱疑似重複：Fan-Sample (2)",
        "族名稱疑似重複：Tray-Sample1",
        "數量 3 超出允許範圍 [1, 2]",
    ):
        assert snippet in joined, snippet


def test_clean_fixture_has_no_findings():
    snap = load_snapshot(FIXTURES / "sample_snapshot_clean.json")
    rule = load_rules(FIXTURES / "sample_rules.json")
    assert run_checks(snap, rule) == []


def test_cli_writes_both_reports_for_the_sample(tmp_path, capsys):
    xlsx = tmp_path / "report.xlsx"
    html = tmp_path / "report.html"
    code = main([
        "--snapshot", str(FIXTURES / "sample_snapshot.json"),
        "--rules", str(FIXTURES / "sample_rules.json"),
        "--xlsx", str(xlsx),
        "--html", str(html),
    ])
    assert code == 0
    assert "findings=11" in capsys.readouterr().out
    assert xlsx.is_file() and html.is_file()
    text = html.read_text(encoding="utf-8")
    assert "E-AHU-MISSING" in text and "E-DUCT-LOW" in text
    assert xlsx.read_bytes().startswith(b"PK")


def test_cli_clean_fixture_still_writes_reports(tmp_path, capsys):
    code = main([
        "--snapshot", str(FIXTURES / "sample_snapshot_clean.json"),
        "--rules", str(FIXTURES / "sample_rules.json"),
        "--xlsx", str(tmp_path / "clean.xlsx"),
        "--html", str(tmp_path / "clean.html"),
    ])
    assert code == 0
    assert "findings=0" in capsys.readouterr().out
    assert "沒有發現" in (tmp_path / "clean.html").read_text(encoding="utf-8")


def test_cli_rejects_bad_input_and_model_outputs(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    code = main([
        "--snapshot", str(bad),
        "--rules", str(FIXTURES / "sample_rules.json"),
        "--xlsx", str(tmp_path / "out.xlsx"),
        "--html", str(tmp_path / "out.html"),
    ])
    assert code == 2
    assert "bim_inspector:" in capsys.readouterr().err

    refused = main([
        "--snapshot", str(FIXTURES / "sample_snapshot.json"),
        "--rules", str(FIXTURES / "sample_rules.json"),
        "--xlsx", str(tmp_path / "out.rvt"),
        "--html", str(tmp_path / "out.html"),
    ])
    assert refused == 2
    missing = tmp_path / "missing.json"
    code = main([
        "--snapshot", str(missing),
        "--rules", str(FIXTURES / "sample_rules.json"),
        "--xlsx", str(tmp_path / "out.xlsx"),
        "--html", str(tmp_path / "out.dwg"),
    ])
    assert code == 2
    assert not (tmp_path / "out.rvt").exists()
    assert json.loads((FIXTURES / "sample_snapshot.json").read_text(encoding="utf-8"))["source"]["project_label"] == "SAMPLE-DEIDENTIFIED"
