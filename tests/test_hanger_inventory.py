"""吊架族群盤點：Core（C#）產生的 JSON/CSV 範例能被 Python/Excel 正確讀取；預留的吊架設定 schema。"""
import csv
import io
import json
import os

import pytest

from tests.dotnet_util import ROOT, find_dotnet, run

SCHEMA = ROOT / "docs" / "schemas" / "hanger_config.schema.json"
DOTNET = find_dotnet()


def test_hanger_config_schema_reserved_and_has_no_rule_defaults():
    s = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert s["properties"]["schema_version"] == {"const": 1}
    assert set(s["properties"]["systems"]["properties"]) == {"cable_tray", "conduit", "pipe"}
    assert set(s["$defs"]["system"]["properties"]) == {"horizontal", "sloped", "vertical"}
    rule = s["$defs"]["orientation_rule"]["properties"]
    for k in ("max_support_spacing_mm", "end_offset_mm", "near_fitting", "family", "parameter_map"):
        assert k in rule
    text = SCHEMA.read_text(encoding="utf-8")
    assert '"default"' not in text and "1000" not in text and "255" not in text


@pytest.mark.revit
@pytest.mark.skipif(DOTNET is None, reason="本機無 .NET SDK")
def test_core_inventory_sample_readable(tmp_path):
    from tests.test_revit_dotnet import _build_model
    _, mp, ep = _build_model(tmp_path)
    proj = ROOT / "revit" / "MepTray.Core.SelfTest"
    b = run([DOTNET, "build", str(proj), "-c", "Release", "-nologo"], timeout=600)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-1000:]
    out = tmp_path / "輸出 資料夾"
    out.mkdir()
    os.environ["MEPTRAY_INVENTORY_SAMPLE_DIR"] = str(out)
    try:
        r = run([DOTNET, "run", "--project", str(proj), "-c", "Release", "--no-build", "--", str(mp), str(ep)], timeout=120)
    finally:
        os.environ.pop("MEPTRAY_INVENTORY_SAMPLE_DIR", None)
    assert r.returncode == 0 and "ALL PASS" in r.stdout, r.stdout[-3000:] + r.stderr[-1000:]

    rep = json.loads((out / "吊架族群盤點 範例.json").read_text(encoding="utf-8"))
    assert rep["schema_version"] == 1 and rep["read_only_check"]["passed"] is True
    assert rep["focus"]["type_in_project"] is True and rep["families"][0]["family_name"] == "吊架(角鐵)"
    assert {f["source"] for f in rep["families"]} == {"project", "rfa"}
    raw = (out / "吊架族群盤點 範例.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                            # Excel 用 UTF-8 BOM
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    kinds = {r["row_kind"] for r in rows}
    assert {"family", "type_value", "family_default", "instance_actual"} <= kinds
    assert {r["value_status"] for r in rows if r["row_kind"] != "family"} >= {"ok", "no_value", "read_error"}
    w = next(r for r in rows if r["param_name"] == "寬度" and r["type"] == "矩形")
    assert float(w["mm"]) == pytest.approx(400) and w["raw_unit"] == "ft (Revit internal)"
    assert any(r["family"] == '壞檔, "引號"' and r["family_status"] == "failed" for r in rows)
    assert any(r["family"].startswith("'=") for r in rows)          # 防公式注入