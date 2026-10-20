"""吊架支撐點核心（mep_tray/supports.py）的純邏輯測試，以及與 C#（MepTray.Core.Supports）的契約。"""
import copy
import json
import math
import os

import pytest

from mep_tray import supports as S
from mep_tray.export_revit import build_model
from tests.dotnet_util import ROOT, find_dotnet, run

DOTNET = find_dotnet()


def cfg(**kw):
    c = {"units": "mm", "max_spacing_mm": 1500.0, "start_offset_mm": 300.0, "end_offset_mm": 300.0,
         "short_run_policy": "midpoint", "near_fitting": {"enabled": False}}
    c.update(kw)
    return c


def pcfg(**kw):
    c = cfg(supported_object="cable_tray", family={"family_name": "F", "type_name": "T"}, placement_mode="level",
            widen_left_mm=50.0, widen_right_mm=50.0, insertion_to_bearing_mm=40.0, orientation_correction_deg=90.0,
            parameter_map={"cross_member_length": {"name": "P", "scope": "type"}}, rod_length={"mode": "none"},
            dedupe={"tolerance_mm": 5.0, "angle_tolerance_deg": 2.0},
            calibration={"family_file": {"path": "X:/f.rfa", "size": 1, "sha256": "ab"}, "family_name": "F", "type_name": "T",
                         "project_signature": "cd", "cross_member_model": {"ratio": 1.0, "offset_mm": 0.0}})
    c.update(kw)
    return c


def P(i, a, b, w=300.0, h=100.0, sk="free", ek="free"):
    return {"id": i, "start": list(map(float, a)), "end": list(map(float, b)), "width_mm": w, "height_mm": h,
            "start_kind": sk, "end_kind": ek}


def stations(r):
    return [s["station_mm"] for s in r["supports"]]


def gaps_ok(xs, s):
    return all(b - a <= s + 1e-6 for a, b in zip(xs, xs[1:]))


# ---------- 佈置 ----------
def test_divisible_spacing():
    r = S.layout([P("A", (0, 0, 3000), (6000, 0, 3000))], [], cfg(max_spacing_mm=1350.0))
    assert stations(r) == [300, 1650, 3000, 4350, 5700]


@pytest.mark.parametrize("L,s", [(6000, 1000), (7001, 1500), (12345, 2000), (1000.4, 333)])
def test_non_divisible_never_exceeds_max(L, s):
    r = S.layout([P("A", (0, 0, 0), (L, 0, 0))], [], cfg(max_spacing_mm=s))
    xs = stations(r)
    assert xs[0] == 300 and xs[-1] == pytest.approx(L - 300, abs=1e-3) and gaps_ok(xs, s)


def test_fixed_spacing_remainder_pending():
    c = cfg(max_spacing_mm=1500.0, start_offset_mm=0.0, end_offset_mm=0.0, spacing_mode="fixed")
    r = S.layout([P("A", (0, 0, 0), (6095.483, 0, 0))], [], c)
    assert stations(r) == [0, 1500, 3000, 4500, 6000]
    iss = [i for i in r["issues"] if i["code"] == "fixed_remainder"]
    assert len(iss) == 1 and iss[0]["level"] == "pending" and iss[0]["piece_id"] == "A"
    assert iss[0]["location_mm"] == [6000, 0, 0] and "95.483" in iss[0]["detail"] and "6000.0–6095.5" in iss[0]["detail"]
    assert len(r["issues"]) == 1


def test_fixed_spacing_with_offsets_and_exact_multiple():
    r = S.layout([P("A", (0, 0, 0), (6600, 0, 0))], [], cfg(spacing_mode="fixed"))
    assert stations(r) == [300, 1800, 3300, 4800, 6300] and r["issues"] == []
    r = S.layout([P("A", (0, 0, 0), (6000, 0, 0))], [], cfg(spacing_mode="fixed", start_offset_mm=0.0, end_offset_mm=0.0))
    assert stations(r) == [0, 1500, 3000, 4500, 6000] and r["issues"] == []
    r = S.layout([P("A", (0, 0, 0), (7000, 0, 0))], [], cfg(spacing_mode="fixed"))
    assert stations(r) == [300, 1800, 3300, 4800, 6300] and [i["code"] for i in r["issues"]] == ["fixed_remainder"]


def test_fixed_spacing_short_run_unchanged():
    for pol in ("midpoint", "skip"):
        a = S.layout([P("A", (0, 0, 0), (400, 0, 0))], [], cfg(short_run_policy=pol))
        b = S.layout([P("A", (0, 0, 0), (400, 0, 0))], [], cfg(short_run_policy=pol, spacing_mode="fixed"))
        assert a == b


@pytest.mark.parametrize("L,s", [(6000, 1000), (7001, 1500), (12345, 2000), (6095.483, 1500)])
def test_even_mode_explicit_equals_default(L, s):
    pcs = [P("A", (0, 0, 0), (L, 0, 0))]
    assert S.layout(pcs, [], cfg(max_spacing_mm=s, spacing_mode="even")) == S.layout(pcs, [], cfg(max_spacing_mm=s))


def test_spacing_mode_validation():
    assert S.validate_layout_config(cfg()) == []
    assert S.validate_layout_config(cfg(spacing_mode="fixed")) == []
    for bad in ("Fixed", "", None, 1):
        e = S.validate_layout_config(cfg(spacing_mode=bad))
        assert len(e) == 1 and e[0].startswith("spacing_mode：")


def test_short_segment_policies():
    r = S.layout([P("A", (0, 0, 0), (400, 0, 0))], [], cfg())
    assert stations(r) == [200] and r["issues"][0]["code"] == "short_run" and r["issues"][0]["level"] == "warning"
    r = S.layout([P("A", (0, 0, 0), (400, 0, 0))], [], cfg(short_run_policy="skip"))
    assert r["supports"] == [] and r["issues"][0]["level"] == "failed"
    r = S.layout([P("A", (0, 0, 0), (600, 0, 0))], [], cfg())       # 剛好等於內縮和 → 1 支
    assert stations(r) == [300]


def test_reversed_segment_mirrors():
    f = S.layout([P("A", (0, 0, 0), (5000, 0, 0))], [], cfg())
    b = S.layout([P("A", (5000, 0, 0), (0, 0, 0))], [], cfg())
    assert sorted(s["point_mm"][0] for s in f["supports"]) == sorted(s["point_mm"][0] for s in b["supports"])
    assert b["supports"][0]["direction_deg"] == 180.0
    b2 = S.layout([P("A", (5000, 0, 0), (0, 0, 0))], [], cfg(start_offset_mm=100.0))   # 起點內縮跟著段方向
    assert b2["supports"][0]["point_mm"][0] == 4900


def test_collinear_split_merged_and_widths():
    r = S.layout([P("A", (0, 0, 0), (3000, 0, 0)), P("B", (3000, 0, 0), (6000, 0, 0), w=400.0)],
                 [{"a": "A", "b": "B", "kind": "union"}], cfg())
    assert len(r["supports"]) == 5 and {s["chain_id"] for s in r["supports"]} == {"C001"}
    assert [s["tray_width_mm"] for s in r["supports"]] == [300, 300, 300, 400, 400]
    sep = S.layout([P("A", (0, 0, 0), (3000, 0, 0)), P("B", (3000, 0, 0), (6000, 0, 0), w=400.0)], [], cfg())
    assert len(sep["supports"]) == 6           # 未連結 → 各自內縮


def test_collinear_split_reversed_member():
    r = S.layout([P("A", (0, 0, 0), (3000, 0, 0)), P("B", (6000, 0, 0), (3000, 0, 0))],
                 [{"a": "A", "b": "B", "kind": "union"}], cfg())
    xs = sorted(s["point_mm"][0] for s in r["supports"])
    assert xs[0] == 300 and xs[-1] == 5700 and gaps_ok(xs, 1500)


def test_never_inside_fitting_body():
    r = S.layout([P("A", (0, 0, 0), (2950, 0, 0)), P("B", (3250, 0, 0), (6000, 0, 0))],
                 [{"a": "A", "b": "B", "kind": "union"}], cfg(max_spacing_mm=1350.0))
    xs = sorted(s["point_mm"][0] for s in r["supports"])
    assert all(x <= 2950 or x >= 3250 for x in xs) and gaps_ok(xs, 1350)
    assert any(i["code"] == "moved_out_of_fitting" for i in r["issues"])


def test_fitting_longer_than_spacing_reported():
    r = S.layout([P("A", (0, 0, 0), (1000, 0, 0)), P("B", (3000, 0, 0), (4000, 0, 0))],
                 [{"a": "A", "b": "B", "kind": "union"}], cfg())
    codes = {i["code"] for i in r["issues"]}
    assert "fitting_gap_exceeds_spacing" in codes and "spacing_not_met" in codes


def test_end_offset_conflict_and_cantilever_warning():
    r = S.layout([P("A", (0, 0, 0), (1000, 0, 0))], [], cfg(start_offset_mm=700.0, end_offset_mm=700.0))
    assert stations(r) == [500] and r["issues"][0]["code"] == "short_run"
    r = S.layout([P("A", (0, 0, 0), (9000, 0, 0))], [], cfg(start_offset_mm=2000.0))
    assert "start_offset_exceeds_spacing" in {i["code"] for i in r["issues"]}


def test_l_path_near_turn_only_when_configured():
    pieces = [P("A", (0, 0, 0), (3000, 0, 0), ek="fitting"), P("B", (3300, 300, 0), (3300, 3000, 0), sk="fitting")]
    off = S.layout(pieces, [], cfg(end_offset_mm=600.0, start_offset_mm=600.0))
    assert not any(s["reason"] == "near_fitting" for s in off["supports"])
    on = S.layout(pieces, [], cfg(end_offset_mm=600.0, start_offset_mm=600.0,
                                  near_fitting={"enabled": True, "max_distance_mm": 200.0}))
    nf = [s for s in on["supports"] if s["reason"] == "near_fitting"]
    assert [(s["piece_id"], s["station_mm"]) for s in nf] == [("A", 2800), ("B", 200)]
    assert {s["direction_deg"] for s in on["supports"] if s["piece_id"] == "B"} == {90.0}


def test_sloped_vertical_zero_reported_with_location():
    r = S.layout([P("V", (0, 0, 0), (0, 0, 3000)), P("Q", (0, 0, 0), (3000, 0, 100)), P("Z", (1, 1, 1), (1, 1, 1))], [], cfg())
    assert r["supports"] == []
    got = {(i["piece_id"], i["level"], i["code"]) for i in r["issues"]}
    assert got == {("V", "unsupported", "vertical"), ("Q", "unsupported", "sloped"), ("Z", "failed", "zero_length")}
    assert next(i for i in r["issues"] if i["piece_id"] == "Q")["location_mm"] == [1500, 0, 50]


def test_bearing_is_not_centerline():
    r = S.layout([P("A", (0, 0, 3000), (2000, 0, 3000), h=150.0)], [], cfg())
    assert all(s["bearing_z_mm"] == 2925 and s["point_mm"][2] == 3000 for s in r["supports"])


def test_union_not_collinear_kept_separate():
    r = S.layout([P("A", (0, 0, 0), (3000, 0, 0)), P("B", (3000, 0, 0), (3000, 3000, 0))],
                 [{"a": "A", "b": "B", "kind": "union"}], cfg())
    assert any(i["code"] == "union_not_collinear" for i in r["issues"])
    assert {s["chain_id"] for s in r["supports"]} == {"C001", "C002"}


# ---------- 設定 ----------
def test_layout_config_has_no_defaults_and_names_missing_fields():
    errs = S.error_fields(S.validate_placement_config({"units": "mm"}))
    for f in ("max_spacing_mm", "start_offset_mm", "end_offset_mm", "short_run_policy", "near_fitting", "family",
              "widen_left_mm", "widen_right_mm", "insertion_to_bearing_mm", "orientation_correction_deg",
              "placement_mode", "parameter_map", "rod_length.mode", "dedupe", "supported_object"):
        assert f in errs, f
    with pytest.raises(ValueError, match="max_spacing_mm"):
        S.layout([P("A", (0, 0, 0), (1000, 0, 0))], [], {"units": "mm"})


@pytest.mark.parametrize("patch,field", [
    ({"max_spacing_mm": 0}, "max_spacing_mm"), ({"max_spacing_mm": float("nan")}, "max_spacing_mm"),
    ({"start_offset_mm": -1}, "start_offset_mm"), ({"units": "m"}, "units"), ({"max_spacing_mm": True}, "max_spacing_mm"),
    ({"short_run_policy": "auto"}, "short_run_policy"),
    ({"near_fitting": {"enabled": True}}, "near_fitting.max_distance_mm"),
    ({"near_fitting": {"enabled": True, "max_distance_mm": 99999}}, "near_fitting.max_distance_mm"),
    ({"rod_length": {"mode": "fixed"}}, "rod_length.value_mm"),
    ({"rod_length": {"mode": "fixed", "value_mm": 500}}, "rod_length.param"),
    ({"parameter_map": {"cross_member_length": {"name": "P", "scope": "both"}}}, "parameter_map.cross_member_length.scope"),
    ({"placement_mode": "face"}, "placement_mode"), ({"supported_object": "conduit"}, "supported_object"),
    ({"dedupe": {"tolerance_mm": 5, "angle_tolerance_deg": 90}}, "dedupe.angle_tolerance_deg"),
    ({"calibration": None}, "calibration"),
    ({"family": {"family_name": "F", "type_name": "OTHER"}}, "calibration.family_name"),
])
def test_invalid_values_rejected(patch, field):
    c = pcfg()
    c.update(patch)
    assert field in S.error_fields(S.validate_placement_config(c))


def test_valid_config_passes():
    assert S.validate_placement_config(pcfg()) == []


# ---------- 族座標換算 ----------
def test_placement_conversion():
    sp = S.layout([P("A", (0, 0, 3000), (0, 5000, 3000), h=100.0)], [], cfg())["supports"][0]   # 沿 +Y
    pl = S.placement(sp, pcfg(widen_left_mm=100.0, widen_right_mm=0.0, insertion_to_bearing_mm=40.0, orientation_correction_deg=90.0))
    assert pl["bearing_point_mm"] == [-50.0, 300.0, 2950.0]          # 左側（-X）偏移 (100-0)/2
    assert pl["insertion_point_mm"] == [-50.0, 300.0, 2910.0]
    assert pl["rotation_deg"] == 180.0 and pl["cross_member_length_mm"] == 400.0 and pl["rod_length_mm"] is None
    pl2 = S.placement(sp, pcfg(rod_length={"mode": "fixed", "value_mm": 650.0, "param": {"name": "R", "scope": "instance"}}))
    assert pl2["rod_length_mm"] == 650.0


def test_mm_feet_units():
    from mep_tray.export_revit import MM_PER_FOOT
    assert MM_PER_FOOT == 304.8


# ---------- 去重／重跑 ----------
def _plan(r, key="T1"):
    return [{"tray_key": key, "station_mm": s["station_mm"], "point_mm": s["point_mm"], "direction_deg": s["direction_deg"]}
            for s in r["supports"]]


def test_rerun_idempotent_and_manual_untouched():
    r = S.layout([P("A", (0, 0, 0), (5000, 0, 0))], [], cfg())
    plan = _plan(r)
    placed = [dict(p, id=f"e{i}", source="tool") for i, p in enumerate(plan)]
    assert {c["status"] for c in S.classify(plan, placed, 5, 2)} == {"exists"}
    manual = [{"id": "m1", "source": "other", "point_mm": [302.0, 1.0, -500.0]}]
    cl = S.classify(plan, manual, 5, 2)
    assert cl[0] == {"status": "pending", "match": ["m1"], "reason": "附近有非本工具（手動）吊架"}
    assert all(c["status"] == "new" for c in cl[1:])


def test_dedupe_requires_same_object_and_orientation():
    plan = [{"tray_key": "T1", "station_mm": 300, "point_mm": [300, 0, 0], "direction_deg": 0}]
    other_tray = [{"id": "x", "source": "tool", "tray_key": "T2", "station_mm": 300, "point_mm": [300, 0, 0], "direction_deg": 0}]
    assert S.classify(plan, other_tray, 5, 2)[0]["status"] == "new"            # 點相同但不同橋架 → 不算重複
    rotated = [{"id": "y", "source": "tool", "tray_key": "T1", "station_mm": 301, "point_mm": [301, 0, 0], "direction_deg": 90}]
    assert S.classify(plan, rotated, 5, 2)[0]["status"] == "pending"
    flipped = [dict(rotated[0], direction_deg=180)]
    assert S.classify(plan, flipped, 5, 2)[0]["status"] == "exists"           # 反向同軸相容
    dup = rotated + [dict(rotated[0], id="z", direction_deg=0)]
    assert S.classify(plan, dup, 5, 2)[0]["status"] == "pending"              # 複製出的多支 → 待確認，不刪


# ---------- JSON / 預覽 ----------
def test_model_carries_hanger_supports_consistent_with_preview():
    from tests.test_export_revit import GOV, routed
    from mep_tray.router import place_hangers
    i, r = routed([(10, 1, 3), (6, 5, 3)])
    place_hangers(r, 1.5)
    m = build_model(i, r, [], GOV, "hs1", type_name="T", basis="INTERNAL_ORIGIN")
    hs = m["hanger_supports"]
    assert hs and len(hs) == len(m["hangers"])
    assert [h["point_mm"] for h in hs] == m["hangers"]                       # 預覽（route.hangers）與 JSON 同一套
    lay = m["hanger_layout"]
    assert lay["units"] == "mm" and lay["reference"] == "path_centerline_pre_fitting" and lay["family_insertion"] is None
    assert all(h["bearing_z_mm"] == pytest.approx(h["point_mm"][2] - i.tray_h_mm / 2) for h in hs)
    seg_ids = {s["id"] for s in m["segments"]}
    assert {h["segment_id"] for h in hs} <= seg_ids
    json.dumps(m, allow_nan=False)


def test_old_json_without_hanger_fields_still_valid_for_python_side():
    from tests.test_export_revit import GOV, routed
    i, r = routed([(10, 1, 3)])
    m = build_model(i, r, [], GOV, "old", type_name="T", basis="INTERNAL_ORIGIN")
    for k in ("hanger_supports", "hanger_layout", "hangers"):
        m.pop(k)
    pieces, links = S.pieces_from_model(m)
    assert pieces and S.layout(pieces, links, cfg())["supports"]


def test_pieces_from_model_marks_fitting_ends():
    m = {"tray": {"width_mm": 300, "height_mm": 100},
         "segments": [{"id": "S001", "start": [0, 0, 0], "end": [3000, 0, 0]}, {"id": "S002", "start": [3000, 0, 0], "end": [3000, 3000, 0]},
                      {"id": "S003", "start": [3000, 3000, 0], "end": [6000, 3000, 0]}],
         "joints": [{"id": "J1", "point": [3000, 0, 0], "kind": "elbow", "segments": ["S001", "S002"]},
                    {"id": "J2", "point": [3000, 3000, 0], "kind": "union", "segments": ["S002", "S003"]}]}
    pieces, links = S.pieces_from_model(m)
    k = {p["id"]: (p["start_kind"], p["end_kind"]) for p in pieces}
    assert k == {"S001": ("free", "fitting"), "S002": ("fitting", "free"), "S003": ("free", "free")}
    assert links == [{"a": "S002", "b": "S003", "kind": "union"}]


# ---------- 驗收用測試開關 test_fail_nth ----------
TEST_FAIL_NTH_BAD = (("zero", 0), ("neg", -1), ("float_int", 2.0), ("float", 1.5), ("str", "2"), ("bool", True),
                     ("null", None), ("list", [2]), ("obj", {}), ("huge", 2 ** 63))
TFN_MSG = "test_fail_nth：驗收用測試開關，需為 ≥ 1 的整數（未填 = 關閉；正式設定勿使用）"
HP = ROOT / "revit" / "MepTrayImport"


def test_fail_nth_absent_is_off_and_valid_values_pass():
    assert "test_fail_nth" not in pcfg() and S.validate_placement_config(pcfg()) == []
    for v in (1, 2, 50, 2 ** 63 - 1):
        assert S.validate_placement_config(pcfg(test_fail_nth=v)) == [], v


@pytest.mark.parametrize("name,value", TEST_FAIL_NTH_BAD)
def test_fail_nth_invalid_rejected_and_only_that_field(name, value):
    errs = S.validate_placement_config(pcfg(test_fail_nth=value))
    assert errs == [TFN_MSG], name


def test_fail_nth_json_literals_match_csharp_rules():
    # 由 JSON 文字解析（與 Revit 讀檔相同）：2.0 / 1e3 是 float → 拒絕；2 是 int → 接受
    for lit, ok in (("2", True), ("1", True), ("2.0", False), ("1e3", False), ("-3", False), ('"2"', False), ("true", False), ("null", False)):
        c = pcfg()
        c["test_fail_nth"] = json.loads(lit)
        assert (S.validate_placement_config(c) == []) is ok, lit


def test_fail_nth_is_not_a_layout_field_and_changes_nothing():
    assert S.validate_layout_config(pcfg(test_fail_nth=0)) == []
    pieces = [P("A", (0, 0, 3000), (6000, 0, 3000))]
    off, on = pcfg(max_spacing_mm=1350.0), pcfg(max_spacing_mm=1350.0, test_fail_nth=2)
    lo, ln = S.layout(pieces, [], off), S.layout(pieces, [], on)
    assert lo == ln and len(lo["supports"]) == 5
    assert [S.placement(s, off) for s in lo["supports"]] == [S.placement(s, on) for s in ln["supports"]]


def test_fail_nth_schema_documented_optional_and_template_clean():
    s = json.loads((ROOT / "docs" / "schemas" / "tray_hanger_config.schema.json").read_text(encoding="utf-8"))
    p = s["properties"]["test_fail_nth"]
    assert p["type"] == "integer" and p["minimum"] == 1 and "test_fail_nth" not in s["required"]
    assert p["maximum"] == S.INT64_MAX == 2 ** 63 - 1                       # 與執行期上限（C# long）一致
    assert "2.0" in p["description"] and "正式設定勿使用" in p["description"]
    cs = (ROOT / "docs" / "schemas" / "conduit_hanger_config.schema.json").read_text(encoding="utf-8")
    assert "test_fail_nth" in json.loads(cs)["description"] and "test_fail_nth" not in json.loads(cs)["properties"]
    t = (ROOT / "docs" / "examples" / "tray_hanger_config.template.json").read_text(encoding="utf-8")
    assert "test_fail_nth" not in t                                         # 正式範本不含測試開關
    m = (ROOT / "docs" / "MANUAL.md").read_text(encoding="utf-8")
    assert '"test_fail_nth": 2' in m and "正式設定勿使用" in m


def test_fail_nth_csharp_reader_mirrors_python():
    core = (ROOT / "revit" / "MepTray.Core" / "Supports.cs").read_text(encoding="utf-8")
    assert 'TestFailNthKey = "test_fail_nth"' in core and "public long? TestFailNth;" in core
    assert f'e.Add("{TFN_MSG}")' in core                                    # 錯誤訊息與 Python 相同
    vp = core[core.index("public static List<string> ValidatePlacement"):core.index("static List<string> CalibrationErrors")]
    assert vp.index("dedupe.angle_tolerance_deg") < vp.index("TestFailNthKey") < vp.index("CalibrationErrors(c)")   # 錯誤順序與 Python 相同
    assert "System.Globalization.NumberStyles.None" in core and "x >= 1" in core
    assert "TestFailNth" not in core[core.index("public static List<string> ValidateLayout"):core.index("static List<string> ParamRefErrors")]


def test_fail_nth_revit_execute_fails_only_nth_after_real_element():
    """Revit 端只能做原始碼檢查（實機另驗）：第 N 支 new 在實例建立、參數設定、讀回驗證、追蹤資料寫入之後、SubTransaction Commit 之前拋例外，
    由既有 catch 只回滾該支；未設定時不觸發。"""
    pl = (HP / "HangerPlacer.cs").read_text(encoding="utf-8")
    ex = pl[pl.index("public static void Execute("):pl.index("public static double CrossParamMm")]
    loop = ex[ex.index('foreach (var it in rep.Items.Where(i => i.Status == "new"))'):]
    assert ex.index("int ordinal = 0;") < ex.index("foreach (var it in rep.Items")
    assert loop.index("ordinal++;") < loop.index("new SubTransaction(doc)")
    i_throw = loop.index("if (TestSwitch.ShouldFail(cfg.TestFailNth, ordinal)) throw new InvalidOperationException(TestSwitch.FailMessage(")
    for before in ("PlaceInstance(doc, sym", "SetLen(fi, cfg.CrossMemberLength.Name", "doc.Regenerate();", "讀回旋轉", "fi.SetEntity(e);"):
        assert loop.index(before) < i_throw, before
    assert i_throw < loop.index("var st = sub.Commit();") < loop.index("catch (Exception ex) when (ex is not OperationCanceledException)")
    assert "sub.RollBack();" in loop and "（僅此支回滾）" in loop
    assert 'rep.Outcome = failed == 0 ? "完成" : created > 0 ? "部分完成" : "失敗";' in ex
    # 預覽：兩個 Plan 都記錄開關；摘要與預覽標題最上方顯示警告
    assert pl.count("ApplyTestSwitch(rep, cfg);") == 1 and "HangerPlacer.ApplyTestSwitch(rep, cfg);" in (HP / "ConduitHangerRun.cs").read_text(encoding="utf-8")
    assert 'var s = (TestFailNth is long tf ? TestSwitch.PreviewNote(tf) + "\\n" : "") + $"批次' in pl
    run_cs = (HP / "HangerRun.cs").read_text(encoding="utf-8")
    assert "rep.TestFailNth is long tf" in run_cs and "MainInstruction" in run_cs
    # 不計入校正簽章
    for f in ("HangerCalibration.cs", "CalibrateHangerCommand.cs"):
        t = (HP / f).read_text(encoding="utf-8")
        assert "TestFailNth" not in t and "test_fail_nth" not in t, f


def test_fail_nth_schema_bounds_match_runtime():
    """schema 的 minimum/maximum 與 Python／C# 執行期驗證一致：邊界內接受、邊界外拒絕。"""
    p = json.loads((ROOT / "docs" / "schemas" / "tray_hanger_config.schema.json").read_text(encoding="utf-8"))["properties"]["test_fail_nth"]
    for v in (p["minimum"], p["maximum"]):
        assert S.validate_placement_config(pcfg(test_fail_nth=v)) == [], v
    for v in (p["minimum"] - 1, p["maximum"] + 1):
        assert S.validate_placement_config(pcfg(test_fail_nth=v)) == [TFN_MSG], v
    core = (ROOT / "revit" / "MepTray.Core" / "Supports.cs").read_text(encoding="utf-8")
    assert "long.TryParse(v.ToJsonString()" in core and "x >= 1" in core    # C# 上限 = long.MaxValue（SelfTest 另驗 9223372036854775808 被拒）


def test_import_all_hangers_failed_rolls_back_whole_batch():
    """PR #23 review：匯入橋架＋吊架與選取補吊架一致——計畫有新增但一支都沒建立成功 → 整批（含橋架）回滾並回報失敗。"""
    core = (ROOT / "revit" / "MepTray.Core" / "Supports.cs").read_text(encoding="utf-8")
    assert "public static bool RollBackAllFailed(int plannedNew, int created) => plannedNew > 0 && created == 0;" in core
    imp = (HP / "ImportCommand.cs").read_text(encoding="utf-8")
    i0 = imp.index('var planned = hrep.Count("new");')
    blk = imp[i0:imp.index("catch (Exception ex)", i0)]
    i_place = blk.index("HangerRun.Place(doc, hrep, hcfg, res);")
    i_dec = blk.index('if (HangerBatch.RollBackAllFailed(planned, hrep.Count("created")))')
    i_rb, i_as = blk.index("tg.RollBack();"), blk.index("else tg.Assimilate();")
    assert i_place < i_dec < i_rb < i_as                                     # 放置前記計畫數；放置後先判定再 Assimilate
    dec = blk[i_dec:i_as]
    assert 'hrep.Outcome = "失敗";' in dec and "rep.Committed = false;" in dec and "rep.Abort = " in dec   # Finish → Result.Failed
    assert imp.count("tg.Assimilate()") == 1
    assert "if (report.Abort != null) { message = report.Abort; return Result.Failed; }" in imp
    # 選取入口的既有語意（全部失敗 → 整批回滾）不變
    for f in ("HangerRun.cs", "ConduitHangerRun.cs"):
        assert 'if (rep.Count("created") == 0) { tg.RollBack(); rep.Notes.Add("沒有任何吊架建立成功，整批已回滾"); }' in (HP / f).read_text(encoding="utf-8"), f
    m = (ROOT / "docs" / "MANUAL.md").read_text(encoding="utf-8")
    assert "若沒有任何吊架建立成功（全部失敗），與選取補吊架相同整批回滾，本入口的整批含橋架" in m


# ---------- instance_params（吊架實例文字參數） ----------
IP_ARRAY_MSG = "instance_params：需為陣列（每項 {\"name\": 參數確切名稱, \"value\": 文字}；未填或 [] = 不寫入）"
IP_OK = [{"name": "<參數A>", "value": "<值A>"}, {"name": "<參數B>", "value": "<值B>"}]   # 測試用佔位字，非實際名稱／編碼
IP_BAD = (
    ("not_list", {"name": "a", "value": "b"}, ["instance_params"]),
    ("str", "a", ["instance_params"]),
    ("null", None, ["instance_params"]),
    ("item_str", ["a"], ["instance_params[0]"]),
    ("item_null", [None], ["instance_params[0]"]),
    ("no_name", [{"value": "v"}], ["instance_params[0].name"]),
    ("blank_name", [{"name": "  ", "value": "v"}], ["instance_params[0].name"]),
    ("num_name", [{"name": 1, "value": "v"}], ["instance_params[0].name"]),
    ("padded_name", [{"name": " a", "value": "v"}], ["instance_params[0].name"]),
    ("no_value", [{"name": "a"}], ["instance_params[0].value"]),
    ("empty_value", [{"name": "a", "value": ""}], ["instance_params[0].value"]),
    ("blank_value", [{"name": "a", "value": " "}], ["instance_params[0].value"]),
    ("num_value", [{"name": "a", "value": 12}], ["instance_params[0].value"]),
    ("bool_value", [{"name": "a", "value": True}], ["instance_params[0].value"]),
    ("extra_key", [{"name": "a", "value": "v", "scope": "instance"}], ["instance_params[0]"]),
    ("dup", [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}, {"name": "a", "value": "3"}], ["instance_params[2].name"]),
    ("cross_member", [{"name": "P", "value": "v"}], ["instance_params[0].name"]),
    ("multi", [{"name": "", "value": ""}, 3], ["instance_params[0].name", "instance_params[0].value", "instance_params[1]"]),
)


def test_instance_params_absent_or_empty_is_valid_and_changes_nothing():
    assert S.validate_placement_config(pcfg()) == []
    assert S.validate_placement_config(pcfg(instance_params=[])) == []
    assert S.validate_placement_config(pcfg(instance_params=IP_OK)) == []
    pieces = [P("A", (0, 0, 3000), (6000, 0, 3000))]
    off, on = pcfg(max_spacing_mm=1350.0), pcfg(max_spacing_mm=1350.0, instance_params=IP_OK)
    lo, ln = S.layout(pieces, [], off), S.layout(pieces, [], on)
    assert lo == ln and [S.placement(s, off) for s in lo["supports"]] == [S.placement(s, on) for s in ln["supports"]]
    assert S.validate_layout_config(pcfg(instance_params="bad")) == []          # 不屬於佈置欄位


@pytest.mark.parametrize("name,value,fields", IP_BAD)
def test_instance_params_invalid_rejected_and_only_those_fields(name, value, fields):
    assert S.error_fields(S.validate_placement_config(pcfg(instance_params=value))) == fields, name


def test_instance_params_messages():
    assert S.validate_placement_config(pcfg(instance_params=5)) == [IP_ARRAY_MSG]
    errs = S.validate_placement_config(pcfg(instance_params=[{"name": "a", "value": "1"}, {"name": "a", "value": "2"}]))
    assert errs == ["instance_params[1].name：「a」與 instance_params[0] 重複"]
    errs = S.validate_placement_config(pcfg(instance_params=[{"name": "a", "value": ""}]))
    assert errs == ["instance_params[0].value：需為非空白文字（取自物件編碼規則；工具不寫入空值）"]
    rod = {"mode": "fixed", "value_mm": 650.0, "param": {"name": "R", "scope": "instance"}}
    errs = S.validate_placement_config(pcfg(rod_length=rod, instance_params=[{"name": "R", "value": "x"}]))
    assert errs == ["instance_params[0].name：「R」是橫擔／吊桿長度參數，不可由 instance_params 覆寫"]
    # rod_length=none 時吊桿參數不寫，同名不算衝突
    assert S.validate_placement_config(pcfg(instance_params=[{"name": "R", "value": "x"}])) == []
    # 錯誤順序：test_fail_nth 之後、calibration 之前（與 C# 相同）
    errs = S.error_fields(S.validate_placement_config(pcfg(test_fail_nth=0, instance_params=1, calibration=None)))
    assert errs == ["test_fail_nth", "instance_params", "calibration"]


def test_instance_params_schema_docs_and_no_invented_values():
    s = json.loads((ROOT / "docs" / "schemas" / "tray_hanger_config.schema.json").read_text(encoding="utf-8"))
    p = s["properties"]["instance_params"]
    assert p["type"] == "array" and "instance_params" not in s["required"]
    it = p["items"]
    assert it["required"] == ["name", "value"] and it["additionalProperties"] is False
    assert it["properties"]["name"]["type"] == it["properties"]["value"]["type"] == "string"
    assert it["properties"]["value"]["minLength"] == 1
    assert "共用參數檔" in p["description"] and "物件編碼規則" in p["description"]
    cs = json.loads((ROOT / "docs" / "schemas" / "conduit_hanger_config.schema.json").read_text(encoding="utf-8"))
    assert "instance_params" in cs["description"] and "instance_params" not in cs["properties"]
    t = (ROOT / "docs" / "examples" / "tray_hanger_config.template.json").read_text(encoding="utf-8")
    assert "instance_params" not in t
    m = (ROOT / "docs" / "MANUAL.md").read_text(encoding="utf-8")
    sec = m[m.index("`instance_params`"):]
    assert '"<共用參數名稱>"' in sec and "格式範例" in sec and "BEP 表 7" in sec and "物件編碼規則" in sec
    # 原始碼不得寫死任何參數名稱或編碼：BEP 表 7 的欄位名只可出現在說明文字
    for f in (ROOT / "revit" / "MepTray.Core" / "Supports.cs", HP / "HangerPlacer.cs", ROOT / "mep_tray" / "supports.py"):
        txt = f.read_text(encoding="utf-8")
        for w in ("設計用系統分類編碼", "施工用系統分類編碼", "工項對應編碼", "維運用編碼"):
            assert w not in txt, (f.name, w)


def test_instance_params_csharp_reader_mirrors_python():
    core = (ROOT / "revit" / "MepTray.Core" / "Supports.cs").read_text(encoding="utf-8")
    assert 'InstanceParamsKey = "instance_params"' in core and "public List<InstanceParamValue> InstanceParams = new();" in core
    assert "public sealed record InstanceParamValue(string Name, string Value);" in core
    vp = core[core.index("public static List<string> ValidatePlacement"):core.index("static List<string> CalibrationErrors")]
    assert vp.index("TestFailNthKey") < vp.index("InstanceParamErrors(c)") < vp.index("CalibrationErrors(c)")
    assert "InstanceParamValue" not in core[core.index("public static List<string> ValidateLayout"):core.index("static List<string> ParamRefErrors")]
    for msg in ("需為非空白文字（取自物件編碼規則；工具不寫入空值）", "前後不可有空白（需與 Revit 參數名稱完全相同）",
                "是橫擔／吊桿長度參數，不可由 instance_params 覆寫", "只接受 name、value（多出 "):
        assert msg in core and msg in (ROOT / "mep_tray" / "supports.py").read_text(encoding="utf-8"), msg


def test_instance_params_revit_execute_sets_then_reads_back_new_only():
    """Revit 端只能做原始碼檢查（實機另驗）：只對 Status=new 的本支新建實例 LookupParameter → 檢查（存在/可寫/String）→ Set → 讀回比對，
    都在 SubTransaction 內、Commit 之前；失敗由既有 catch 只回滾該支。"""
    pl = (HP / "HangerPlacer.cs").read_text(encoding="utf-8")
    ex = pl[pl.index("public static void Execute("):pl.index("public static double CrossParamMm")]
    loop = ex[ex.index('foreach (var it in rep.Items.Where(i => i.Status == "new"))'):]
    i_set = loop.index("foreach (var ip in cfg.InstanceParams) SetText(fi, ip);")
    i_back = loop.index("InstanceParamText.ReadbackError(ip.Name, ip.Value, fi.LookupParameter(ip.Name)?.AsString())")
    assert loop.index("new SubTransaction(doc)") < loop.index("PlaceInstance(doc, sym") < i_set < loop.index("讀回旋轉") < i_back
    assert i_back < loop.index("fi.SetEntity(e);") < loop.index("var st = sub.Commit();") < loop.index("（僅此支回滾）")
    st = pl[pl.index("static void SetText(Element e, InstanceParamValue ip)"):]
    st = st[:st.index("\n    }") + 6]
    assert "e.LookupParameter(ip.Name)" in st and "p?.IsReadOnly == true" in st and "p?.StorageType == StorageType.String" in st
    assert "p!.Set(ip.Value)" in st and "throw new InvalidOperationException" in st
    assert pl.count("SetText(") == 2                                        # 只在新建實例迴圈使用
    # 預檢（族類型同名 → 類型參數）與預覽／報告
    pf = pl[pl.index("public static Resolved? Preflight("):pl.index("static void CheckParam(")]
    assert "sym.LookupParameter(cfg.InstanceParams[i].Name) != null" in pf and "InstanceParamText.TypeParamError(i" in pf
    assert pl.count("ApplyInstanceParams(rep, cfg);") == 1 and "HangerPlacer.ApplyInstanceParams(rep, cfg);" in (HP / "ConduitHangerRun.cs").read_text(encoding="utf-8")
    assert "public List<InstanceParamValue> InstanceParams { get; } = new();" in pl
    for f in ("HangerCalibration.cs", "CalibrateHangerCommand.cs"):
        t = (HP / f).read_text(encoding="utf-8")
        assert "InstanceParamValue" not in t and "instance_params" not in t, f


# ---------- C# 契約 ----------
def _rod(**kw):
    r = {"mode": "to_structure", "param": {"name": "R", "scope": "instance"}, "offset_mm": -20.0, "min_mm": 100.0, "max_mm": 5000.0}
    r.update(kw)
    return r


def contract_cases() -> dict:
    lc = []

    def add(name, pieces, links, c, placement=True):
        errs = S.error_fields(S.validate_placement_config(c))
        case = {"name": name, "pieces": pieces, "links": links, "cfg": c, "config_errors": errs}
        if not S.validate_layout_config(c):
            res = S.layout(pieces, links, c)
            case["expect"] = res
            if placement and not errs:
                case["placements"] = [S.placement(sp, c) for sp in res["supports"]]
        lc.append(case)

    add("straight", [P("A", (0, 0, 3000), (6000, 0, 3000))], [], pcfg(max_spacing_mm=1350.0))
    add("non_div", [P("A", (100, 200, 2500), (7101, 200, 2500))], [], pcfg(max_spacing_mm=1000.0))
    add("diag", [P("A", (0, 0, 0), (3000, 4000, 0), w=200.0)], [], pcfg(widen_left_mm=30.0, widen_right_mm=10.0))
    add("reversed", [P("A", (5000, 0, 0), (0, 0, 0))], [], pcfg(start_offset_mm=100.0, end_offset_mm=250.0))
    add("short_mid", [P("A", (0, 0, 0), (400, 0, 0))], [], pcfg())
    add("short_skip", [P("A", (0, 0, 0), (400, 0, 0))], [], pcfg(short_run_policy="skip"))
    add("collinear", [P("A", (0, 0, 0), (3000, 0, 0)), P("B", (6000, 0, 0), (3000, 0, 0), w=400.0)],
        [{"a": "A", "b": "B", "kind": "union"}], pcfg())
    add("gap", [P("A", (0, 0, 0), (2950, 0, 0)), P("B", (3250, 0, 0), (6000, 0, 0))],
        [{"a": "A", "b": "B", "kind": "union"}], pcfg(max_spacing_mm=1350.0))
    add("big_gap", [P("A", (0, 0, 0), (1000, 0, 0)), P("B", (3000, 0, 0), (4000, 0, 0))],
        [{"a": "A", "b": "B", "kind": "union"}], pcfg())
    add("l_near", [P("A", (0, 0, 0), (3000, 0, 0), ek="fitting"), P("B", (3300, 300, 0), (3300, 3000, 0), sk="fitting")], [],
        pcfg(start_offset_mm=600.0, end_offset_mm=600.0, near_fitting={"enabled": True, "max_distance_mm": 200.0},
             rod_length={"mode": "fixed", "value_mm": 650.0, "param": {"name": "R", "scope": "instance"}}))
    add("bad_geom", [P("V", (0, 0, 0), (0, 0, 3000)), P("Q", (0, 0, 0), (3000, 0, 100)), P("Z", (1, 1, 1), (1, 1, 1)),
                     P("W", (0, 0, 0), (1000, 0, 0), w=0.0)], [], pcfg())
    add("not_collinear", [P("A", (0, 0, 0), (3000, 0, 0)), P("B", (3000, 0, 0), (3000, 3000, 0))],
        [{"a": "A", "b": "B", "kind": "union"}], pcfg())
    add("cfg_missing", [], [], {"units": "mm"})
    add("cfg_bad", [], [], pcfg(max_spacing_mm=-1, placement_mode="face", dedupe={"tolerance_mm": 0}))
    add("cfg_rod", [P("A", (0, 0, 0), (1000, 0, 0))], [], pcfg(rod_length={"mode": "fixed"}))
    add("cfg_nocal", [P("A", (0, 0, 0), (1000, 0, 0))], [], pcfg(calibration=None))
    add("rod_to_structure_ok", [P("A", (0, 0, 3000), (3000, 0, 3000))], [], pcfg(rod_length=_rod()))
    add("rod_to_structure_missing", [], [], pcfg(rod_length={"mode": "to_structure", "param": {"name": "R", "scope": "type"}, "min_mm": -1, "max_mm": 0}))
    add("rod_to_structure_minmax", [], [], pcfg(rod_length=_rod(min_mm=800.0, max_mm=700.0, round_to_mm=0)))
    add("cfg_badcal", [], [], pcfg(calibration={"family_file": {"path": "p"}, "family_name": "F", "type_name": "X"}))
    add("fail_nth_on", [P("A", (0, 0, 3000), (6000, 0, 3000))], [], pcfg(max_spacing_mm=1350.0, test_fail_nth=2))
    for nm, v in TEST_FAIL_NTH_BAD:
        add(f"cfg_fail_nth_{nm}", [], [], pcfg(test_fail_nth=v))
    add("ip_on", [P("A", (0, 0, 3000), (6000, 0, 3000))], [], pcfg(max_spacing_mm=1350.0, instance_params=IP_OK))
    add("ip_empty", [], [], pcfg(instance_params=[]))
    for nm, v, _ in IP_BAD:
        add(f"cfg_ip_{nm}", [], [], pcfg(instance_params=v))
    base = S.layout([P("A", (0, 0, 0), (5000, 0, 0))], [], cfg())
    plan = _plan(base)
    cc = [
        {"name": "rerun", "planned": plan, "existing": [dict(p, id=f"e{i}", source="tool") for i, p in enumerate(plan)]},
        {"name": "manual", "planned": plan, "existing": [{"id": "m1", "source": "other", "tray_key": None, "station_mm": None,
                                                          "point_mm": [302.0, 1.0, -500.0], "direction_deg": None}]},
        {"name": "orient", "planned": plan, "existing": [{"id": "y", "source": "tool", "tray_key": "T1", "station_mm": 301.0,
                                                          "point_mm": [301.0, 0, 0], "direction_deg": 90.0}]},
        {"name": "other_tray", "planned": plan, "existing": [{"id": "x", "source": "tool", "tray_key": "T2", "station_mm": 300.0,
                                                              "point_mm": [300.0, 0, 0], "direction_deg": 0.0}]},
    ]
    for c in cc:
        c["tol_mm"], c["tol_deg"] = 5.0, 2.0
        c["expect"] = S.classify(c["planned"], c["existing"], 5.0, 2.0)
    rc = []
    for nm, bz, hz, tz, kw in [
        ("ok", 2950.0, 5000.0, 3050.0, {}),
        ("round_half_up", 0.0, 2987.5, 100.0, {"offset_mm": 0.0, "round_to_mm": 5.0}),
        ("round_down", 0.0, 2982.4, 100.0, {"offset_mm": 0.0, "round_to_mm": 5.0}),
        ("fraction", 0.0, 1234.56789, 100.0, {"offset_mm": 0.0}),
        ("hit_equals_tray_top", 2950.0, 3050.0, 3050.0, {}),
        ("hit_below_tray_top", 2950.0, 3000.0, 3050.0, {}),
        ("at_min", 0.0, 120.0, 100.0, {}),
        ("below_min", 0.0, 119.999, 100.0, {}),
        ("at_max", 0.0, 5020.0, 100.0, {}),
        ("above_max", 0.0, 5020.001, 100.0, {}),
    ]:
        c = pcfg(rod_length=_rod(**kw))
        rc.append({"name": nm, "cfg": c, "bearing_z_mm": bz, "hit_z_mm": hz, "tray_top_z_mm": tz,
                   "expect": S.rod_length_to_structure(bz, hz, tz, c["rod_length"])})
    return {"layout_cases": lc, "classify_cases": cc, "rod_cases": rc}


@pytest.mark.revit
@pytest.mark.skipif(DOTNET is None, reason="本機無 .NET SDK")
def test_csharp_supports_contract(tmp_path):
    from tests.test_revit_dotnet import _build_model
    _, mp, ep = _build_model(tmp_path)
    cp = tmp_path / "support_contract.json"
    cp.write_text(json.dumps(contract_cases(), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    proj = ROOT / "revit" / "MepTray.Core.SelfTest"
    b = run([DOTNET, "build", str(proj), "-c", "Release", "-nologo"], timeout=600)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-1000:]
    os.environ["MEPTRAY_SUPPORT_CONTRACT"] = str(cp)
    try:
        r = run([DOTNET, "run", "--project", str(proj), "-c", "Release", "--no-build", "--", str(mp), str(ep)], timeout=120)
    finally:
        os.environ.pop("MEPTRAY_SUPPORT_CONTRACT", None)
    assert r.returncode == 0 and "ALL PASS" in r.stdout, r.stdout[-4000:] + r.stderr[-1000:]
    assert r.stdout.count("契約[") >= 30


def test_config_template_blocks_until_filled():
    t = json.loads((ROOT / "docs" / "examples" / "tray_hanger_config.template.json").read_text(encoding="utf-8"))
    errs = S.error_fields(S.validate_placement_config(t))
    assert "max_spacing_mm" in errs and "insertion_to_bearing_mm" in errs and "orientation_correction_deg" in errs
    assert "calibration" in errs
    text = json.dumps(t)
    assert "1000" not in text and "255" not in text


def test_run_config_schema_has_no_defaults():
    s = (ROOT / "docs" / "schemas" / "tray_hanger_config.schema.json").read_text(encoding="utf-8")
    assert '"default"' not in s and "1000" not in s and "255" not in s
    json.loads(s)



def test_calibration_block_required_and_checked():
    c = pcfg()
    c["calibration"] = dict(c["calibration"], cross_member_model={"ratio": 0, "offset_mm": 0})
    assert "calibration.cross_member_model" in S.error_fields(S.validate_placement_config(c))
    c = pcfg()
    del c["calibration"]["family_file"]["sha256"]
    assert "calibration.family_file" in S.error_fields(S.validate_placement_config(c))


# ---------- PR #21 review：schema 要求 calibration，與執行期驗證一致 ----------
def test_schema_requires_calibration_matching_runtime():
    s = json.loads((ROOT / "docs" / "schemas" / "tray_hanger_config.schema.json").read_text(encoding="utf-8"))
    assert "calibration" in s["required"]
    cal = s["properties"]["calibration"]
    assert set(cal["required"]) == {"family_file", "family_name", "type_name", "project_signature", "cross_member_model"}
    assert set(cal["properties"]["family_file"]["required"]) == {"path", "size", "sha256"}
    assert set(cal["properties"]["cross_member_model"]["required"]) == {"ratio", "offset_mm"}
    # schema 每個必填欄位缺漏時，執行期驗證也拒絕（不會出現 schema 通過、工具拒絕或相反）
    assert S.validate_placement_config(pcfg()) == []
    for k in cal["required"]:
        c = pcfg()
        c["calibration"] = {kk: v for kk, v in c["calibration"].items() if kk != k}
        assert any(f.startswith("calibration") for f in S.error_fields(S.validate_placement_config(c))), k
    for sub, keys in (("family_file", ("path", "size", "sha256")), ("cross_member_model", ("ratio", "offset_mm"))):
        for k in keys:
            c = pcfg()
            c["calibration"] = dict(c["calibration"], **{sub: {kk: v for kk, v in c["calibration"][sub].items() if kk != k}})
            assert f"calibration.{sub}" in S.error_fields(S.validate_placement_config(c)), (sub, k)
    c = pcfg()
    del c["calibration"]
    assert S.error_fields(S.validate_placement_config(c)) == ["calibration"]


# ---------- PR #21 review：校正簽章涵蓋整個族內容（Revit 端，只能做原始碼檢查；實機另驗） ----------
def test_calibration_signature_covers_whole_family():
    src = (ROOT / "revit" / "MepTrayImport" / "HangerCalibration.cs").read_text(encoding="utf-8")
    body = src[src.index("public static string? ProjectSignature"):src.index("public static bool IsCurrentSignatureFormat")]
    assert 'SignaturePrefix = "v2:"' in src and "return SignaturePrefix + Hex(" in body
    for api in ("doc.EditFamily(fam)", "fd.Close(false)", "fm.Parameters", "fm.Types", "fp.IsInstance", "fp.Formula", "fp.StorageType",
                "DefKey(p.Definition)", "DefKey(fp.Definition)", "ft.HasValue(fp)", "fam.GetFamilySymbolIds()", "s.Volume", "s.SurfaceArea",
                "fm.CurrentType = target", "tx.RollBack()", "doc.IsModifiable || doc.IsReadOnly"):
        assert api in body, api
    assert "d.GetGroupTypeId()?.TypeId" in src and "d.GetDataType()?.TypeId" in src   # 參數定義含群組與資料型別
    assert "IsToolType(s.Name)" in body and "IsToolType(ft.Name)" in body          # 工具自建類型不計入 → 放置後不自我失效
    for forbidden in ("LoadFamily", ".Save(", "SaveAs", "Commit()"):
        assert forbidden not in body, forbidden                                   # 族複本只讀、回滾、不載回、不儲存
    chk = src[src.index("public static List<string> CheckCalibration"):]
    assert "IsCurrentSignatureFormat(cfg.CalibProjectSignature)" in chk and "舊格式" in chk
    imp = (ROOT / "revit" / "MepTrayImport" / "ImportCommand.cs").read_text(encoding="utf-8")
    assert "checkCalibration: false" in imp
    assert imp.index("HangerPlacer.Preflight(doc, hcfg, pre)") < imp.index("tg.Start()")    # 交易群組開始前已完整核對
    pl = (ROOT / "revit" / "MepTrayImport" / "HangerPlacer.cs").read_text(encoding="utf-8")
    assert "if (checkCalibration) errors.AddRange(HangerCalibration.CheckCalibration(doc, cfg));" in pl


# ---------- rod_length.mode = to_structure ----------
def test_rod_to_structure_valid_and_no_defaults():
    assert S.validate_placement_config(pcfg(rod_length=_rod())) == []
    assert S.validate_placement_config(pcfg(rod_length=_rod(offset_mm=0.0, round_to_mm=5.0))) == []   # 0 必須明確填寫，但合法
    for k in ("offset_mm", "min_mm", "max_mm", "param"):
        r = _rod()
        del r[k]
        assert S.error_fields(S.validate_placement_config(pcfg(rod_length=r))) == [f"rod_length.{k}"]


@pytest.mark.parametrize("patch,field", [
    ({"param": {"name": "R", "scope": "type"}}, "rod_length.param.scope"),
    ({"min_mm": -1.0}, "rod_length.min_mm"), ({"max_mm": 0.0}, "rod_length.max_mm"),
    ({"min_mm": 800.0, "max_mm": 700.0}, "rod_length.max_mm"),
    ({"offset_mm": "10"}, "rod_length.offset_mm"), ({"offset_mm": float("nan")}, "rod_length.offset_mm"),
    ({"round_to_mm": 0}, "rod_length.round_to_mm"), ({"round_to_mm": -5.0}, "rod_length.round_to_mm"),
])
def test_rod_to_structure_invalid_values_rejected(patch, field):
    assert S.error_fields(S.validate_placement_config(pcfg(rod_length=_rod(**patch)))) == [field]


def test_rod_unknown_mode_rejected_and_old_modes_unchanged():
    assert "rod_length.mode" in S.error_fields(S.validate_placement_config(pcfg(rod_length={"mode": "structure"})))
    assert S.validate_placement_config(pcfg(rod_length={"mode": "none"})) == []
    assert S.validate_placement_config(pcfg(rod_length={"mode": "fixed", "value_mm": 500.0, "param": {"name": "R", "scope": "type"}})) == []


def test_rod_to_structure_placement_has_no_python_length():
    sp = S.layout([P("A", (0, 0, 3000), (3000, 0, 3000))], [], cfg())["supports"][0]
    assert S.placement(sp, pcfg(rod_length=_rod()))["rod_length_mm"] is None


def test_rod_length_to_structure_rules():
    r = _rod()
    assert S.rod_length_to_structure(2950, 5000, 3050, r) == {"length_mm": 2030.0, "code": None}      # 5000 − 2950 − 20
    assert S.rod_length_to_structure(2950, 3050, 3050, r)["code"] == "not_above_tray"                 # 不高於橋架頂面
    assert S.rod_length_to_structure(2950, 3000, 3050, r)["code"] == "not_above_tray"
    assert S.rod_length_to_structure(0, 120, 100, r)["length_mm"] == 100.0                            # 邊界含 min／max
    assert S.rod_length_to_structure(0, 119.999, 100, r)["code"] == "below_min"
    assert S.rod_length_to_structure(0, 5020, 100, r)["length_mm"] == 5000.0
    assert S.rod_length_to_structure(0, 5020.001, 100, r)["code"] == "above_max"


def test_rod_length_rounding_rule():
    r = _rod(offset_mm=0.0, round_to_mm=5.0)
    assert S.rod_length_to_structure(0, 2987.5, 100, r)["length_mm"] == 2990.0     # 恰在中間：遠離 0 進位
    assert S.rod_length_to_structure(0, 2987.4, 100, r)["length_mm"] == 2985.0
    assert S.rod_length_to_structure(0, 2982.4, 100, r)["length_mm"] == 2980.0
    assert S.rod_length_to_structure(0, 1234.56789, 100, _rod(offset_mm=0.0))["length_mm"] == 1234.568   # 未設定：只取到 0.001


def test_rod_to_structure_schema_documented():
    s = json.loads((ROOT / "docs" / "schemas" / "tray_hanger_config.schema.json").read_text(encoding="utf-8"))
    rod = s["properties"]["rod_length"]
    assert rod["properties"]["mode"]["enum"] == list(S.ROD_MODES)
    for k in ("offset_mm", "min_mm", "max_mm", "round_to_mm"):
        assert k in rod["properties"]
    then = rod["allOf"][0]["then"]
    assert set(then["required"]) == {"param", "offset_mm", "min_mm", "max_mm"}                     # 與執行期必填一致
    assert then["properties"]["param"]["properties"]["scope"] == {"const": "instance"}
    assert "round_to_mm" not in then["required"]
    t = (ROOT / "docs" / "examples" / "tray_hanger_config.template.json").read_text(encoding="utf-8")
    assert "to_structure" not in t                                                                   # 範本預設行為不變


def test_rod_to_structure_csharp_mirrors_python_and_targets_exclude_ceilings():
    core = (ROOT / "revit" / "MepTray.Core" / "Supports.cs").read_text(encoding="utf-8")
    assert '{ "none", "fixed", "to_structure" }' in core
    for k in ("offset_mm", "min_mm", "max_mm", "round_to_mm"):
        assert f"rod_length.{k}：" in core
    py = (ROOT / "mep_tray" / "supports.py").read_text(encoding="utf-8")
    for k in ("offset_mm", "min_mm", "max_mm", "round_to_mm"):                                         # 訊息逐字相同
        line = next(x for x in core.splitlines() if f'"rod_length.{k}：' in x and "Add(" in x)
        msg = line.split('Add("', 1)[1].rsplit('")', 1)[0]
        assert msg in py, msg
    probe = (ROOT / "revit" / "MepTrayImport" / "RodToStructure.cs").read_text(encoding="utf-8")
    assert "OST_Floors" in probe and "OST_StructuralFraming" in probe and "FindReferencesInRevitLinks = true" in probe
    assert "BuiltInCategory.OST_Ceilings" not in probe                                                                 # 天花板不是射線目標
    placer = (ROOT / "revit" / "MepTrayImport" / "HangerPlacer.cs").read_text(encoding="utf-8")
    assert "HitElementId" in placer and "讀回吊桿長度" in placer
