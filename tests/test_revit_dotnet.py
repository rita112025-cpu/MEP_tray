"""需要 .NET SDK 的測試（pytest -m revit）：C# 核心的編譯與跨語言契約。"""
import json

import pytest

from mep_tray import export_revit as R
from mep_tray.clash import check_route
from mep_tray.compliance import check_compliance
from tests.dotnet_util import ROOT, find_dotnet, run
from tests.test_export_revit import GOV, routed

DOTNET = find_dotnet()
pytestmark = [pytest.mark.revit, pytest.mark.skipif(DOTNET is None, reason="本機無 .NET SDK")]


def _build_model(tmp_path):
    i, r = routed([(10, 1, 3), (6, 5, 3)])
    i.obstacles.append({"name": "W", "kind": "water", "lo": [4, 1.35, 2], "hi": [6, 1.6, 3.6]})
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    m = R.build_model(i, r, reps, GOV, "dn1", type_name="T", basis="INTERNAL_ORIGIN")
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    exp = {s["id"]: R.comment_body(m, s["id"]) for s in m["segments"]}
    ep = tmp_path / "expected.json"
    ep.write_text(json.dumps(exp, ensure_ascii=False), encoding="utf-8")
    return m, mp, ep


def test_core_builds_and_matches_python_contract(tmp_path):
    m, mp, ep = _build_model(tmp_path)
    proj = ROOT / "revit" / "MepTray.Core.SelfTest"
    b = run([DOTNET, "build", str(proj), "-c", "Release", "-nologo"], timeout=600)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-1000:]
    r = run([DOTNET, "run", "--project", str(proj), "-c", "Release", "--no-build", "--", str(mp), str(ep)],
            timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "ALL PASS" in r.stdout and "FAIL " not in r.stdout
    assert any(s["id"] in r.stdout for s in m["segments"])
