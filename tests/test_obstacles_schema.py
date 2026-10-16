"""Core（C#）產生的障礙物 JSON 必須通過 Python 端既有驗證器（MANUAL §2 / webui.validate_request）。需要 .NET SDK（pytest -m revit）。"""
import json

import pytest

from mep_tray.webui import MAX_OBSTACLE_BYTES, MAX_OBSTACLES, validate_request
from tests.dotnet_util import ROOT, find_dotnet, run
from tests.test_revit_dotnet import _build_model

DOTNET = find_dotnet()
pytestmark = [pytest.mark.revit, pytest.mark.skipif(DOTNET is None, reason="本機無 .NET SDK")]

ROOM = {"x": 10.0, "y": 8.0, "z": 4.0}     # 與 ObstacleTests.cs 的 fixture 房間一致


def _core_fixture(tmp_path):
    _, mp, ep = _build_model(tmp_path)
    proj = ROOT / "revit" / "MepTray.Core.SelfTest"
    b = run([DOTNET, "build", str(proj), "-c", "Release", "-nologo"], timeout=600)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-1000:]
    fx = tmp_path / "obstacles_fixture.json"
    r = run([DOTNET, "run", "--project", str(proj), "-c", "Release", "--no-build", "--", str(mp), str(ep), str(fx)],
            timeout=120)
    assert r.returncode == 0 and "ALL PASS" in r.stdout, r.stdout[-3000:] + r.stderr[-1000:]
    return fx


def test_core_obstacles_pass_python_validator(tmp_path):
    fx = _core_fixture(tmp_path)
    raw = fx.read_bytes()
    assert len(raw) <= MAX_OBSTACLE_BYTES
    obs = json.loads(raw.decode("utf-8"))
    assert isinstance(obs, list) and len(obs) == MAX_OBSTACLES
    body = {"room": ROOM, "tray": {"width_mm": 300, "height_mm": 100, "kind": "power"},
            "start": {"x": 0.2, "y": 0.2, "z": 0.2}, "ends": [{"x": 9.8, "y": 0.2, "z": 0.2}],
            "codes": ["CNS"], "cell_m": 0.25, "obstacles": obs}
    inp, _ = validate_request(body)            # 不合法會 raise ApiError
    assert len(inp.obstacles) == MAX_OBSTACLES
    kinds = {o["kind"] for o in obs}
    assert {"water", "heat_bare", "structure", "duct"} <= kinds   # 優先序保留重要類別；被截掉的是低優先 tray_*
    for o in obs:
        assert all(0 <= o["lo"][a] < o["hi"][a] <= (10, 8, 4)[a] for a in range(3))
        assert 1 <= len(o["name"]) <= 100
    assert not any(o["name"] == "牆外" for o in obs)
    assert any(o["name"].startswith("熱水斜管 #") for o in obs)   # 斜管被分段