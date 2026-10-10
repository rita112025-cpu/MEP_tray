"""Revit 實機 AutoRun 驗收（pytest -m revit）與 setup 檔產生的單元測試（預設執行）。

實機部分會啟動 Revit 2025（數分鐘）：本機沒有 Revit／.NET SDK，或 Revit 已在執行時 skip。
"""
import json
import math
import os

import pytest

from tests import revit_live as L
from tests.dotnet_util import ROOT

TOL_MM = 0.5


# ───────────── 不需要 Revit 的單元測試（預設執行）─────────────
def test_write_scenarios_writes_setup_files_next_to_their_models(tmp_path):
    L.write_scenarios(tmp_path)
    setup_files = sorted(p.name for p in tmp_path.glob("*.setup.json"))
    assert setup_files == ["i2_pbp_moved.setup.json", "j1_shared_translate.setup.json", "j2_shared_rotated.setup.json"]
    st = json.loads((tmp_path / "i2_pbp_moved.setup.json").read_text(encoding="utf-8"))
    assert st == {"move_pbp_mm": [5000.0, -3000.0, 0.0]}
    m = json.loads((tmp_path / "i2_pbp_moved.json").read_text(encoding="utf-8"))
    assert m["coordinate_system"]["basis"] == "PROJECT_BASE_POINT"
    assert m["segments"] and m["joints"] == []


def test_every_setup_targets_an_existing_scenario_with_a_nontrivial_xy_pbp_move():
    sc, st = L.scenarios(), L.setups()
    assert set(st) <= set(sc)
    for name, s in st.items():
        assert L.validate_setup(s) == [], name
    dx, dy, dz = st["i2_pbp_moved"]["move_pbp_mm"]
    assert math.hypot(dx, dy) >= 1000 and dz == 0     # 非零平面位移；不動 Z（Level 高程）


@pytest.mark.parametrize("bad", [
    {}, [], {"move_pbp_mm": [1, 2]}, {"move_pbp_mm": [1, 2, "3"]}, {"move_pbp_mm": [1, 2, float("nan")]},
    {"move_pbp_mm": [1, 2, True]}, {"move_survey_mm": [1, 2, 3]}, {"move_pbp_mm": [1, 2, 3], "x": 1},
    {"set_project_position": {}}, {"set_project_position": [1, 2, 3, 4]},
    {"set_project_position": {"ew_mm": 1, "ns_mm": 2, "elev_mm": 3}},
    {"set_project_position": {"ew_mm": 1, "ns_mm": 2, "elev_mm": 3, "angle_deg": float("inf")}},
    {"set_project_position": {"ew_mm": 1, "ns_mm": 2, "elev_mm": 3, "angle_deg": 0, "x": 1}},
])
def test_validate_setup_rejects_malformed_input(bad):
    assert L.validate_setup(bad)


def test_write_scenarios_refuses_invalid_or_orphan_setups(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "setups", lambda: {"nope": {"move_pbp_mm": [1, 2, 3]}})
    with pytest.raises(ValueError, match="nope"):
        L.write_scenarios(tmp_path / "a")
    monkeypatch.setattr(L, "setups", lambda: {"i2_pbp_moved": {"move_pbp_mm": [1, 2]}})
    with pytest.raises(ValueError, match="move_pbp_mm"):
        L.write_scenarios(tmp_path / "b")


def test_autorun_skips_setup_and_result_files_and_applies_setup_before_import():
    t = (ROOT / "revit" / "MepTrayImport" / "AutoRunApp.cs").read_text(encoding="utf-8")
    for suffix in ('".setup.json"', '".setup_result.json"', '".revit_report.json"'):
        assert suffix in t
    assert "move_pbp_mm" in t and "ElementTransformUtils.MoveElement" in t
    assert t.index("ApplySetup(doc, setupPath)") < t.index("RevitImporter.Import(doc")
    assert "set_project_position" in t and "SetProjectPosition" in t


def test_shared_setups_are_nonzero_in_every_axis_and_one_is_rotated():
    st = L.setups()
    for name in ("j1_shared_translate", "j2_shared_rotated"):
        assert L.validate_setup(st[name]) == []
        sp = st[name]["set_project_position"]
        assert sp["ew_mm"] != 0 and sp["ns_mm"] != 0 and sp["elev_mm"] != 0       # 東西、南北、高程皆有平移
    assert st["j1_shared_translate"]["set_project_position"]["angle_deg"] == 0
    assert 0 < abs(st["j2_shared_rotated"]["set_project_position"]["angle_deg"]) < 180
    sc = L.scenarios()
    assert sc["j1_shared_translate"]["coordinate_system"]["basis"] == "SHARED_COORDINATES"
    assert sc["j2_shared_rotated"]["coordinate_system"]["basis"] == "SHARED_COORDINATES"


def _shared_fake(model_pts, shared, joints=()):
    model = {"segments": [{"id": "S0", "start": list(model_pts[0]), "end": list(model_pts[1])}],
             "joints": [{"point": list(p)} for p in joints]}
    rep = {"CreatedTrays": ["S0=1"],
           "Inspection": {"Trays": [{"Id": "1", "SharedStartMm": list(shared[0]), "SharedEndMm": list(shared[1])}]}}
    return model, rep


def test_shared_mismatches_requires_read_back_shared_point_to_equal_model_point():
    pts = [(1000, 1000, 3000), (10000, 1000, 3000)]
    m, r = _shared_fake(pts, pts)
    assert L.shared_mismatches(m, r) == []
    m, r = _shared_fake(pts, [(1000.6, 1000, 3000), pts[1]])
    assert L.shared_mismatches(m, r)                                    # 超過 0.5 mm 容差
    m, r = _shared_fake(pts, [(13000, -6000, 4500), (22000, -6000, 4500)])
    assert L.shared_mismatches(m, r)                                    # 只做了平移而未換算
    m, r = _shared_fake(pts, [(1000, 1000, 3300), pts[1]], joints=[pts[0]])
    assert L.shared_mismatches(m, r) == []                              # 接頭端不比對
    m, r = _shared_fake(pts, pts)
    r["CreatedTrays"] = ["S0=999"]
    assert L.shared_mismatches(m, r)


def test_xy_rotation_deg_is_signed_counter_clockwise_from_model_to_internal():
    seg = {"start": [0, 0, 0], "end": [1000, 0, 0]}
    assert abs(L.xy_rotation_deg(seg, {"StartMm": [0, 0, 0], "EndMm": [1000, 0, 0]})) < 1e-9
    assert abs(L.xy_rotation_deg(seg, {"StartMm": [0, 0, 0], "EndMm": [866.0254, 500, 0]}) - 30) < 1e-4
    assert abs(L.xy_rotation_deg(seg, {"StartMm": [0, 0, 0], "EndMm": [866.0254, -500, 0]}) + 30) < 1e-4


def _fake(seg_pts, offset, joints=(), shift=0.0):
    model = {"segments": [{"id": f"S{i}", "start": list(a), "end": list(b)} for i, (a, b) in enumerate(seg_pts)],
             "joints": [{"point": list(p)} for p in joints]}
    trays = [{"Id": str(100 + i), "StartMm": [a[k] + offset[k] + shift for k in range(3)],
              "EndMm": [b[k] + offset[k] for k in range(3)]} for i, (a, b) in enumerate(seg_pts)]
    rep = {"CreatedTrays": [f"S{i}={100 + i}" for i in range(len(seg_pts))], "Inspection": {"Trays": trays}}
    return model, rep


def test_endpoint_mismatches_checks_offset_and_ignores_joint_ends():
    off = (5000.0, -3000.0, 0.0)
    m, r = _fake([((0, 0, 0), (1000, 0, 0))], off)
    assert L.endpoint_mismatches(m, r, off) == []
    assert L.endpoint_mismatches(m, r, (0, 0, 0))                       # 未加 offset 必須被抓到
    m, r = _fake([((0, 0, 0), (1000, 0, 0))], off, shift=0.6)
    assert L.endpoint_mismatches(m, r, off)                              # 超過 0.5 mm 容差
    m, r = _fake([((0, 0, 0), (1000, 0, 0))], off, joints=[(0, 0, 0)], shift=150)
    assert L.endpoint_mismatches(m, r, off) == []                        # 接頭端被 fitting 修剪，不比對
    m, r = _fake([((0, 0, 0), (1000, 0, 0))], off)
    r["CreatedTrays"] = ["S0=999"]
    assert L.endpoint_mismatches(m, r, off)                              # 找不到橋架算不符


# ───────────── Revit 2025 實機（pytest -m revit）─────────────
def _revit_ready():
    if not (L.REVIT_DIR / "Revit.exe").is_file():
        return f"本機無 {L.REVIT_DIR / 'Revit.exe'}"
    if not L.TEMPLATE.is_file():
        return f"找不到樣板 {L.TEMPLATE}"
    return None


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    why = _revit_ready()
    if why:
        pytest.skip(why)
    from tests.dotnet_util import find_dotnet
    dn = find_dotnet()
    if dn is None:
        pytest.skip("本機無 .NET SDK")
    if L.revit_running():
        pytest.skip("Revit 已在執行；請先關閉再跑實機測試")
    out = os.environ.get("MEP_REVIT_LIVE_OUT")
    w = tmp_path_factory.mktemp("revit_live") if not out else __import__("pathlib").Path(out)
    res = L.run_revit(dn, w)
    assert L.wait_revit_exit(), "Revit 程序未結束"
    return res


def _pbp_mm(rep):
    return rep["Inspection"]["Coordinates"]["ProjectBasePoint"]["PositionMm"]


def _trays(rep):
    return (rep.get("Inspection") or {}).get("Trays", [])


@pytest.mark.revit
def test_live_autorun_completed_without_errors(live):
    assert live["done"], live
    assert live["errors"] == {}
    assert set(live["reports"]) == set(L.scenarios())
    assert "2025" in (live["version"] or "")


@pytest.mark.revit
def test_live_i2_pbp_moved_translates_by_the_read_back_nonzero_pbp(live):
    st = live["setups"]["i2_pbp_moved"]
    assert st["requested_mm"] == list(L.I2_PBP_DELTA_MM)
    assert math.dist(st["moved_mm"], L.I2_PBP_DELTA_MM) <= TOL_MM
    rep = live["reports"]["i2_pbp_moved"]
    assert rep["Committed"] is True and rep["Abort"] is None and rep["Basis"] == "PROJECT_BASE_POINT"
    before = st["before"]["ProjectBasePoint"]["PositionMm"]
    pbp = _pbp_mm(rep)
    assert math.dist(pbp, [before[i] + L.I2_PBP_DELTA_MM[i] for i in range(3)]) <= TOL_MM
    assert math.hypot(pbp[0], pbp[1]) >= 1000       # 非零 offset，否則此測試無鑑別力
    m = live["models"]["i2_pbp_moved"]
    assert len(_trays(rep)) == len(m["segments"])
    assert L.endpoint_mismatches(m, rep, pbp, TOL_MM) == []
    assert L.endpoint_mismatches(m, rep, (0, 0, 0), TOL_MM)     # 未平移的座標不可能也吻合


@pytest.mark.revit
@pytest.mark.parametrize("name", ["a_tee", "b_elbow", "ov_d_unspecified", "g_cross", "h_union", "k_unsupported"])
def test_live_internal_origin_scenarios_commit_at_model_points(live, name):
    rep, m = live["reports"][name], live["models"][name]
    assert rep["Committed"] is True and rep["Abort"] is None and rep["Basis"] == "INTERNAL_ORIGIN"
    assert len(_trays(rep)) == len(m["segments"])
    assert L.endpoint_mismatches(m, rep, (0, 0, 0), TOL_MM) == []
    expected = {"a_tee": ("tee", "OK"), "b_elbow": ("elbow", "OK"), "g_cross": ("cross", "OK"),
                "h_union": ("union", "OK"), "k_unsupported": ("unsupported", "FAIL")}
    if name in expected:
        assert expected[name] in [(j["Kind"], j["Status"]) for j in rep["Joints"]]
    if name != "k_unsupported":
        assert all(j["Status"] == "OK" for j in rep["Joints"])
        assert rep["Inspection"]["FittingCount"] == len(rep["Joints"])


@pytest.mark.revit
def test_live_i_pbp_matches_pbp_read_back_in_fresh_template(live):
    rep, m = live["reports"]["i_pbp"], live["models"]["i_pbp"]
    assert rep["Committed"] is True and rep["Basis"] == "PROJECT_BASE_POINT"
    assert L.endpoint_mismatches(m, rep, _pbp_mm(rep), TOL_MM) == []


@pytest.mark.revit
@pytest.mark.parametrize("name,needle", [("c_unspecified", "UNSPECIFIED"), ("e_missing_type", "NoSuchType"),
                                         ("f_rollback", "Rollback")])
def test_live_rejected_or_rolled_back_scenarios_leave_no_trays(live, name, needle):
    rep = live["reports"][name]
    assert rep["Committed"] is False and rep["Abort"] and needle in rep["Abort"]
    assert _trays(rep) == [] and rep["Inspection"]["FittingCount"] == 0


@pytest.mark.revit
@pytest.mark.parametrize("name,angle_deg", [("j1_shared_translate", 0.0), ("j2_shared_rotated", L.J2_ANGLE_DEG)])
def test_live_shared_coordinates_land_where_revit_reads_the_same_shared_point(live, name, angle_deg):
    st = live["setups"][name]
    req = st["requested_project_position"]
    assert req == {**L.J_SHARED_POS, "angle_deg": angle_deg}
    pos = st["after_project_position"]["ActiveProjectPosition"]
    assert abs(pos["EastWestMm"] - req["ew_mm"]) <= TOL_MM and abs(pos["NorthSouthMm"] - req["ns_mm"]) <= TOL_MM
    assert abs(pos["ElevationMm"] - req["elev_mm"]) <= TOL_MM
    assert abs(math.degrees(pos["AngleRad"]) - angle_deg) <= 1e-4
    rep, m = live["reports"][name], live["models"][name]
    assert rep["Committed"] is True and rep["Abort"] is None and rep["Basis"] == "SHARED_COORDINATES"
    assert len(_trays(rep)) == len(m["segments"])
    # 1) Revit 自己的 GetProjectPosition 讀回：端點的共用座標 = 模型點
    assert L.shared_mismatches(m, rep, TOL_MM) == []
    # 2) 獨立錨點：PBP 的內部座標／共用座標由 Revit 讀回，端點到 PBP 的距離與高差必須與模型點到 PBP 共用座標相同
    pbp = rep["Inspection"]["Coordinates"]["ProjectBasePoint"]
    p_in, p_sh = pbp["PositionMm"], pbp["SharedPositionMm"]
    assert math.dist(p_sh, [req["ew_mm"], req["ns_mm"], req["elev_mm"]]) <= TOL_MM
    tray = _trays(rep)[0]
    seg = m["segments"][0]
    for key, got in (("start", "StartMm"), ("end", "EndMm")):
        assert abs(math.dist(tray[got], p_in) - math.dist(seg[key], p_sh)) <= TOL_MM
        assert abs((tray[got][2] - p_in[2]) - (seg[key][2] - p_sh[2])) <= TOL_MM
    assert math.dist(tray["StartMm"], seg["start"]) > 1000       # 不是原樣放在模型座標，否則此測試無鑑別力
    # 3) 旋轉量：模型方向到內部方向的 XY 旋轉量大小等於設定的真北角度
    assert abs(abs(L.xy_rotation_deg(seg, tray)) - abs(angle_deg)) <= 0.01
