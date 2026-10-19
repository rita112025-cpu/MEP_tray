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


# ---------- C# 契約 ----------
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
    add("cfg_badcal", [], [], pcfg(calibration={"family_file": {"path": "p"}, "family_name": "F", "type_name": "X"}))
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
    return {"layout_cases": lc, "classify_cases": cc}


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
