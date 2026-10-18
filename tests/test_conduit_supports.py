"""配管吊架核心（mep_tray/conduit_supports.py）的純邏輯測試、與 C#（MepTray.Core.ConduitHangers）的契約，
以及「選取配管補吊架」命令註冊的原始碼檢查（不需 Revit）。"""
import copy
import json
import math
import os
import re
import uuid
import xml.etree.ElementTree as ET

import pytest

from mep_tray import conduit_supports as C
from mep_tray import supports as S
from tests.dotnet_util import ROOT, find_dotnet, run

DOTNET = find_dotnet()
SRC = ROOT / "revit" / "MepTrayImport"


def ccfg(**kw):
    """使用者決定 2026-10-17：E75 兩管並排共用一支吊架、橫擔 300 mm、不加寬；角度/高程容差沿用橋架去重容差。"""
    c = {"units": "mm", "supported_object": "conduit_pair", "base_config": "tray_hanger_config.json", "conduits_per_hanger": 2,
         "allowed_trade_sizes_mm": [75.0], "cross_member": {"value_mm": 300.0, "basis": "parameter"},
         "pairing": {"angle_tolerance_deg": 1.0, "elevation_tolerance_mm": 1.0}}
    c.update(kw)
    return c


def bcfg(**kw):
    """與目前橋架設定同形（固定 1500、內縮 0、skip、加寬 50/50、校正 ratio 1 / offset 100）。"""
    c = {"units": "mm", "supported_object": "cable_tray", "family": {"family_name": "F", "type_name": "T"}, "placement_mode": "level",
         "max_spacing_mm": 1500.0, "start_offset_mm": 0.0, "end_offset_mm": 0.0, "short_run_policy": "skip", "spacing_mode": "fixed",
         "near_fitting": {"enabled": False}, "widen_left_mm": 50.0, "widen_right_mm": 50.0, "insertion_to_bearing_mm": 0.0,
         "orientation_correction_deg": 0.0, "parameter_map": {"cross_member_length": {"name": "P", "scope": "instance"}},
         "rod_length": {"mode": "none"}, "dedupe": {"tolerance_mm": 1.0, "angle_tolerance_deg": 1.0},
         "calibration": {"family_file": {"path": "X:/f.rfa", "size": 1, "sha256": "ab"}, "family_name": "F", "type_name": "T",
                         "project_signature": "cd", "cross_member_model": {"ratio": 1.0, "offset_mm": 100.0}}}
    c.update(kw)
    return c


SINGLE = {"value_mm": 200.0, "basis": "parameter"}      # 使用者決定 2026-10-18：單管 吊架寬度 200、不加寬


def K(i, a, b, ts=75.0, od=75.0):
    return {"id": i, "start": list(map(float, a)), "end": list(map(float, b)), "trade_size_mm": ts, "outer_diameter_mm": od}


def codes(r):
    return [(i["code"], i["piece_id"]) for i in r["issues"]]


# ---------- 配對 ----------
def test_side_by_side_pair_centerline_and_bearing():
    r = C.pair_conduits([K("1", (0, 0, 3000), (6000, 0, 3000)), K("2", (0, 150, 3000), (6000, 150, 3000))], ccfg())
    assert r["issues"] == [] and len(r["pairs"]) == 1
    p = r["pairs"][0]
    assert p["id"] == "1+2" and p["start"] == [0, 75, 3000] and p["end"] == [6000, 75, 3000]
    assert p["overlap_mm"] == 6000 and p["outer_width_mm"] == 225 and p["bearing_z_mm"] == 2962.5


def test_partial_overlap_line_is_overlap_and_tails_pending():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (6500, 150, 0), (1000, 150, 0))], ccfg())
    p = r["pairs"][0]
    assert p["start"][0] == 1000 and p["end"][0] == 6000 and p["overlap_mm"] == 5000
    tails = [i for i in r["issues"] if i["code"] == "unsupported_tail"]
    assert [(t["piece_id"], t["level"]) for t in tails] == [("1", "pending"), ("2", "pending")]
    assert tails[0]["location_mm"] == [500, 0, 0] and tails[1]["location_mm"] == [6250, 150, 0]


def test_single_conduit_not_placed():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0))], ccfg())
    assert r["pairs"] == [] and codes(r) == [("unpaired", "1")] and r["issues"][0]["level"] == "pending"


def test_non_parallel_not_paired():
    th = math.radians(2)
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 0), (6000 * math.cos(th), 150 + 6000 * math.sin(th), 0))], ccfg())
    assert r["pairs"] == [] and all(i["code"] == "unpaired" and "not_parallel" in i["detail"] for i in r["issues"])
    th = math.radians(0.5)       # 1° 容差內仍視為平行（外緣以最大中心距檢查）
    r = C.pair_conduits([K("1", (0, 0, 0), (3000, 0, 0)), K("2", (0, 100, 0), (3000 * math.cos(th), 100 + 3000 * math.sin(th), 0))], ccfg())
    assert len(r["pairs"]) == 1 and r["pairs"][0]["center_distance_max_mm"] > r["pairs"][0]["center_distance_min_mm"]


def test_different_elevation_not_paired():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 5), (6000, 150, 5))], ccfg())
    assert r["pairs"] == [] and all("elevation_mismatch" in i["detail"] for i in r["issues"])


def test_diameter_not_in_config_not_placed():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 0), (6000, 150, 0), ts=50.0, od=50.0)], ccfg())
    assert r["pairs"] == [] and codes(r) == [("diameter_not_allowed", "2"), ("unpaired", "1")]
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0), ts=None)], ccfg())
    assert codes(r) == [("no_diameter", "1")]


def test_outer_edges_must_fit_crossbar():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 230, 0), (6000, 230, 0))], ccfg())
    assert r["pairs"] == [] and ("exceeds_crossbar", "1+2") in codes(r)
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 225, 0), (6000, 225, 0))], ccfg())
    assert len(r["pairs"]) == 1 and r["pairs"][0]["outer_width_mm"] == 300


def test_overlapping_conduits_are_not_side_by_side():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 50, 0), (6000, 50, 0))], ccfg())
    assert r["pairs"] == [] and ("too_close", "1+2") in codes(r)


def test_ambiguous_three_conduits_not_guessed():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 200, 0), (6000, 200, 0))], ccfg())
    assert r["pairs"] == [] and [c for c, _ in codes(r)] == ["ambiguous_pair"] * 3


def test_two_independent_pairs():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 0), (6000, 150, 0)),
                         K("3", (0, 5000, 0), (6000, 5000, 0)), K("4", (0, 5150, 0), (6000, 5150, 0))], ccfg())
    assert [p["id"] for p in r["pairs"]] == ["1+2", "3+4"]
    assert r["issues"] == []


def test_sloped_vertical_zero_reported():
    r = C.pair_conduits([K("S", (0, 0, 0), (6000, 0, 100)), K("V", (0, 0, 0), (0, 0, 3000)), K("Z", (1, 1, 1), (1, 1, 1))], ccfg())
    assert codes(r) == [("sloped", "S"), ("vertical", "V"), ("zero_length", "Z")]


def test_outer_diameter_fallback_flagged():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0), od=None), K("2", (0, 150, 0), (6000, 150, 0), od=None)], ccfg())
    assert len(r["pairs"]) == 1 and [c for c, _ in codes(r)] == ["outer_diameter_fallback"] * 2


# ---------- 佈置與放置：沿用橋架規則 ----------
def test_layout_reuses_tray_fixed_spacing_and_remainder_pending():
    r = C.pair_conduits([K("1", (0, 0, 3000), (6095.483, 0, 3000)), K("2", (0, 150, 3000), (6095.483, 150, 3000))], ccfg())
    lay = S.layout(C.conduit_pieces(r["pairs"]), [], bcfg())
    assert [s["station_mm"] for s in lay["supports"]] == [0, 1500, 3000, 4500, 6000]
    assert [(i["level"], i["code"]) for i in lay["issues"]] == [("pending", "fixed_remainder")]
    assert all(s["point_mm"][1] == 75 and s["bearing_z_mm"] == 2962.5 for s in lay["supports"])


def test_placement_no_widening_fixed_crossbar():
    r = C.pair_conduits([K("1", (0, 0, 3000), (3000, 0, 3000)), K("2", (0, 150, 3000), (3000, 150, 3000))], ccfg())
    sp = S.layout(C.conduit_pieces(r["pairs"]), [], bcfg())["supports"][1]
    p = C.conduit_placement(sp, bcfg(), ccfg())
    assert p["insertion_point_mm"] == [1500, 75, 2962.5] and p["rotation_deg"] == 0
    assert p["cross_member_param_mm"] == 300 and p["cross_member_length_mm"] == 400     # 實測 = ratio×300 + offset 100
    p = C.conduit_placement(sp, bcfg(), ccfg(cross_member={"value_mm": 300.0, "basis": "measured_length"}))
    assert p["cross_member_length_mm"] == 300 and p["cross_member_param_mm"] == 200
    tray = S.placement(sp, bcfg())                                                   # 橋架路徑不受影響（仍加寬）
    assert tray["cross_member_length_mm"] == sp["tray_width_mm"] + 100


def test_tray_supported_objects_unchanged():
    assert S.SUPPORTED_OBJECTS == ("cable_tray",)
    assert S.validate_placement_config(bcfg()) == []


# ---------- 設定 ----------
def test_conduit_config_has_no_defaults_and_names_missing_fields():
    errs = C.error_fields(C.validate_conduit_config({"units": "mm"}))
    assert errs == ["supported_object", "base_config", "conduits_per_hanger", "allowed_trade_sizes_mm", "cross_member", "pairing"]
    assert C.validate_conduit_config(ccfg()) == []


@pytest.mark.parametrize("patch,field", [
    ({"conduits_per_hanger": 3}, "conduits_per_hanger"), ({"conduits_per_hanger": 2.0}, "conduits_per_hanger"),
    ({"allowed_trade_sizes_mm": []}, "allowed_trade_sizes_mm"), ({"allowed_trade_sizes_mm": [0]}, "allowed_trade_sizes_mm"),
    ({"cross_member": {"value_mm": -1, "basis": "parameter"}}, "cross_member.value_mm"),
    ({"cross_member": {"value_mm": 300}}, "cross_member.basis"),
    ({"pairing": {"angle_tolerance_deg": 90, "elevation_tolerance_mm": 1}}, "pairing.angle_tolerance_deg"),
    ({"pairing": {"angle_tolerance_deg": 1, "elevation_tolerance_mm": 0}}, "pairing.elevation_tolerance_mm"),
    ({"supported_object": "cable_tray"}, "supported_object"),
])
def test_conduit_config_invalid_values_rejected(patch, field):
    assert field in C.error_fields(C.validate_conduit_config(ccfg(**patch)))


def test_template_holds_user_decisions_with_sources():
    t = json.loads((ROOT / "docs" / "examples" / "conduit_hanger_config.template.json").read_text(encoding="utf-8"))
    assert C.validate_conduit_config(t) == []
    assert t["allowed_trade_sizes_mm"] == [75] and t["cross_member"]["value_mm"] == 300 and t["conduits_per_hanger"] == 2
    src = t["_value_sources"]
    for k in ("allowed_trade_sizes_mm", "cross_member.value_mm", "conduits_per_hanger"):
        assert "2026-10-17" in src[k], k
    s = (ROOT / "docs" / "schemas" / "conduit_hanger_config.schema.json").read_text(encoding="utf-8")
    assert '"default"' not in s
    json.loads(s)


# ---------- 命令註冊（原始碼檢查） ----------
def test_conduit_command_registered_consistently():
    addins = ET.parse(SRC / "MepTray.addin").getroot().findall("AddIn")
    a = next(x for x in addins if x.findtext("FullClassName") == "MepTray.Revit.AddConduitHangersCommand")
    assert a.findtext("Text") == "選取配管補吊架" and a.findtext("Assembly") == "MepTrayImport.dll"
    uuid.UUID(a.findtext("AddInId"))
    code = (SRC / "ConduitHangerRun.cs").read_text(encoding="utf-8")
    assert re.search(r"\[Transaction\(TransactionMode\.Manual\)\]\s*public class AddConduitHangersCommand\s*:\s*IExternalCommand", code)
    assert "TransactionGroup" in code and "Assimilate" in code and "RollBack" in code
    assert "BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM" in code and "BuiltInParameter.RBS_CONDUIT_OUTER_DIAM_PARAM" in code
    for forbidden in (".Delete(", "Conduit.Create", "MoveElement", ".Name.Contains", "Unblock"):
        assert forbidden not in code, forbidden
    placer = (SRC / "HangerPlacer.cs").read_text(encoding="utf-8")
    assert 'TrayToolVersion = "tray-hangers-v1"' in placer          # 橋架吊架追蹤資料不變


# ---------- C# 契約 ----------
def contract_cases() -> dict:
    cases = []

    def add(name, conduits, cc, base=None):
        case = {"name": name, "ccfg": cc, "conduits": conduits, "config_errors": C.error_fields(C.validate_conduit_config(cc))}
        if not case["config_errors"]:
            res = C.pair_conduits(conduits, cc)
            case["expect"] = res
            if base is not None:
                lay = S.layout(C.conduit_pieces(res["pairs"], res["singles"]), [], base)
                sid = {x["id"] for x in res["singles"]}
                case["base_cfg"] = base
                case["layout"] = lay
                case["placements"] = [C.conduit_placement(sp, base, cc, sp["piece_id"] in sid) for sp in lay["supports"]]
        cases.append(case)

    th = math.radians(0.5)
    add("pair", [K("1", (0, 0, 3000), (6095.483, 0, 3000)), K("2", (0, 150, 3000), (6095.483, 150, 3000))], ccfg(), bcfg())
    add("diag_pair", [K("11", (100, 200, 2500), (3100, 4200, 2500)), K("12", (220, 110, 2500), (3220, 4110, 2500))], ccfg(),
        bcfg(insertion_to_bearing_mm=40.0, orientation_correction_deg=90.0))
    add("partial", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (6500, 150, 0), (1000, 150, 0))], ccfg(), bcfg())
    add("skew_ok", [K("1", (0, 0, 0), (3000, 0, 0)), K("2", (0, 100, 0), (3000 * math.cos(th), 100 + 3000 * math.sin(th), 0))], ccfg(), bcfg())
    add("measured", [K("1", (0, 0, 0), (3000, 0, 0)), K("2", (0, 150, 0), (3000, 150, 0))],
        ccfg(cross_member={"value_mm": 300.0, "basis": "measured_length"}), bcfg())
    add("single", [K("1", (0, 0, 0), (6000, 0, 0))], ccfg())
    add("not_parallel", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 0), (6000 * math.cos(0.035), 150 + 6000 * math.sin(0.035), 0))], ccfg())
    add("elevation", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 5), (6000, 150, 5))], ccfg())
    add("diameter", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 0), (6000, 150, 0), ts=50.0, od=50.0), K("3", (0, 0, 0), (1, 0, 0), ts=None)], ccfg())
    add("too_wide", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 230, 0), (6000, 230, 0))], ccfg())
    add("too_close", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 50, 0), (6000, 50, 0))], ccfg())
    add("no_overlap", [K("1", (0, 0, 0), (3000, 0, 0)), K("2", (4000, 150, 0), (6000, 150, 0))], ccfg())
    add("ambiguous", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 200, 0), (6000, 200, 0))], ccfg())
    add("two_pairs", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 150, 0), (6000, 150, 0)),
                      K("3", (0, 5000, 0), (6000, 5000, 0)), K("4", (0, 5150, 0), (6000, 5150, 0))], ccfg(), bcfg())
    add("bad_geom", [K("S", (0, 0, 0), (6000, 0, 100)), K("V", (0, 0, 0), (0, 0, 3000)), K("Z", (1, 1, 1), (1, 1, 1))], ccfg())
    add("od_fallback", [K("1", (0, 0, 0), (6000, 0, 0), od=None), K("2", (0, 150, 0), (6000, 150, 0), od=None)], ccfg(), bcfg())
    sc = ccfg(cross_member_single=SINGLE)
    add("single_lone", [K("1", (0, 0, 3000), (6095.483, 0, 3000))], sc, bcfg())
    add("single_measured", [K("1", (0, 0, 0), (3000, 0, 0))], ccfg(cross_member_single={"value_mm": 300.0, "basis": "measured_length"}), bcfg())
    add("single_pair_mix", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 400, 0), (6000, 400, 0)),
                            K("4", (0, 5000, 0), (3000, 3000 + 5000, 0))], sc, bcfg())
    add("single_conflict_pair", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 200, 0), (6000, 200, 0), od=80.0)], sc)
    add("single_ambiguous", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 200, 0), (6000, 200, 0))], sc)
    add("single_too_close", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 50, 0), (6000, 50, 0))], sc)
    add("single_wide", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 230, 0), (6000, 230, 0))], sc, bcfg())
    add("single_partial", [K("1", (0, 0, 0), (6000, 0, 0)), K("2", (6500, 150, 0), (1000, 150, 0)), K("3", (0, 3000, 0), (2000, 3000, 0)),
                           K("5", (0, 0, 0), (0, 0, 3000))], sc, bcfg())
    add("cfg_single_bad", [], ccfg(cross_member_single={"value_mm": -1}))
    add("cfg_single_type", [], ccfg(cross_member_single=200))
    add("cfg_missing", [], {"units": "mm"})
    add("cfg_bad", [], ccfg(conduits_per_hanger=3, cross_member={"value_mm": 0}, pairing={"angle_tolerance_deg": 95}))
    return {"conduit_cases": cases}


def write_contract(path) -> None:
    path.write_text(json.dumps(contract_cases(), ensure_ascii=False, allow_nan=False), encoding="utf-8")


@pytest.mark.revit
@pytest.mark.skipif(DOTNET is None, reason="本機無 .NET SDK")
def test_csharp_conduit_contract(tmp_path):
    from tests.test_revit_dotnet import _build_model
    _, mp, ep = _build_model(tmp_path)
    cp = tmp_path / "conduit_contract.json"
    write_contract(cp)
    proj = ROOT / "revit" / "MepTray.Core.SelfTest"
    b = run([DOTNET, "build", str(proj), "-c", "Release", "-nologo"], timeout=600)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-1000:]
    os.environ["MEPTRAY_CONDUIT_CONTRACT"] = str(cp)
    try:
        r = run([DOTNET, "run", "--project", str(proj), "-c", "Release", "--no-build", "--", str(mp), str(ep)], timeout=120)
    finally:
        os.environ.pop("MEPTRAY_CONDUIT_CONTRACT", None)
    assert r.returncode == 0 and "ALL PASS" in r.stdout, r.stdout[-4000:] + r.stderr[-1000:]
    assert r.stdout.count("配管契約[") >= 60


def test_level_placement_corrects_z_and_still_verifies_readback():
    """實測 2026-10-18：Level 標高 −3600 mm 時 NewFamilyInstance(XYZ, symbol, Level) 落點低 3600 mm → 讀回驗證失敗。
    level 模式建立後必須讀回 LocationPoint 並垂直校正（只動剛建立的實例），之後仍保留原讀回驗證。"""
    placer = (SRC / "HangerPlacer.cs").read_text(encoding="utf-8")
    m = re.search(r"public static FamilyInstance PlaceInstance\(.*?\n    }\n", placer, flags=re.S)
    assert m, "PlaceInstance"
    body = m.group(0)
    lvl = body[body.index('if (mode == "level")'):body.index("else")]
    i_new = lvl.index("NewFamilyInstance(ins, sym, lvl")
    i_move = lvl.index("ElementTransformUtils.MoveElement(doc, fi.Id, new XYZ(0, 0, dz))")
    assert i_new < lvl.index("lp0.Point.Z") < i_move
    assert "zNotes" in body and "PlaceInstance(doc, sym, cfg.PlacementMode, ins, it.RotationDeg, planes, levels, zNotes)" in placer
    assert "讀回插入點" in placer and "ReadbackTolMm" in placer                     # 讀回驗證仍在
    assert ".Delete(" not in body

# ---------- 單管（使用者決定 2026-10-18） ----------
def test_single_absent_keeps_previous_behaviour():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0))], ccfg())
    assert r["singles"] == [] and codes(r) == [("unpaired", "1")]


def test_single_lone_conduit_hung_full_length_on_axis():
    r = C.pair_conduits([K("1", (0, 0, 3000), (6095.483, 0, 3000))], ccfg(cross_member_single=SINGLE))
    assert r["pairs"] == [] and r["issues"] == []
    sg = r["singles"][0]
    assert sg["id"] == "1" and sg["start"] == [0, 0, 3000] and sg["end"] == [6095.483, 0, 3000] and sg["bearing_z_mm"] == 2962.5
    lay = S.layout(C.conduit_pieces(r["pairs"], r["singles"]), [], bcfg())
    assert [s["station_mm"] for s in lay["supports"]] == [0, 1500, 3000, 4500, 6000]
    assert [(i["level"], i["code"]) for i in lay["issues"]] == [("pending", "fixed_remainder")]
    assert all(s["point_mm"][1] == 0 and s["bearing_z_mm"] == 2962.5 for s in lay["supports"])   # 吊架中心在管軸，承托面 = 管底
    p = C.conduit_placement(lay["supports"][1], bcfg(), ccfg(cross_member_single=SINGLE), single=True)
    assert p["cross_member_param_mm"] == 200 and p["cross_member_length_mm"] == 300 and p["insertion_point_mm"] == [1500, 0, 2962.5]
    pp = C.conduit_placement(lay["supports"][1], bcfg(), ccfg(cross_member_single=SINGLE))
    assert pp["cross_member_param_mm"] == 300                                     # 配對仍為 300


def test_single_pairs_unchanged_and_far_conduit_becomes_single():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 400, 0), (6000, 400, 0))],
                        ccfg(cross_member_single=SINGLE))
    assert [p["id"] for p in r["pairs"]] == ["1+2"] and [x["id"] for x in r["singles"]] == ["3"] and r["issues"] == []


def test_single_conflicts_with_neighbouring_pair_hanger_pending():
    # 配對中線 y=50；第 3 支外徑不同（無法配對）在 y=200：距中線 150 < (200+300)/2 → 橫擔可能重疊 → 待確認
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 200, 0), (6000, 200, 0), od=80.0)],
                        ccfg(cross_member_single=SINGLE))
    assert [p["id"] for p in r["pairs"]] == ["1+2"] and r["singles"] == [] and codes(r) == [("single_conflict", "3")]
    assert "1+2" in r["issues"][0]["detail"]


def test_single_ambiguous_not_converted():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 100, 0), (6000, 100, 0)), K("3", (0, 200, 0), (6000, 200, 0))],
                        ccfg(cross_member_single=SINGLE))
    assert r["pairs"] == [] and r["singles"] == [] and [c for c, _ in codes(r)] == ["ambiguous_pair"] * 3


def test_single_too_close_and_wide_cases():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 50, 0), (6000, 50, 0))], ccfg(cross_member_single=SINGLE))
    assert r["singles"] == [] and codes(r) == [("too_close", "1+2"), ("single_conflict", "1"), ("single_conflict", "2")]
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 230, 0), (6000, 230, 0))], ccfg(cross_member_single=SINGLE))
    assert [x["id"] for x in r["singles"]] == ["1", "2"] and r["issues"] == []    # 兩支各自單管（230 ≥ 200，橫擔不重疊）


def test_single_far_parallel_runs_both_hung():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (0, 5000, 0), (6000, 5000, 0))], ccfg(cross_member_single=SINGLE))
    assert [x["id"] for x in r["singles"]] == ["1", "2"] and r["issues"] == []


def test_single_does_not_hang_pair_tails_or_bad_conduits():
    r = C.pair_conduits([K("1", (0, 0, 0), (6000, 0, 0)), K("2", (6500, 150, 0), (1000, 150, 0)),
                         K("9", (0, 3000, 0), (6000, 3000, 0), ts=50.0, od=50.0), K("S", (0, 0, 0), (6000, 0, 100))],
                        ccfg(cross_member_single=SINGLE))
    assert r["singles"] == [] and len(r["pairs"]) == 1
    assert [c for c, _ in codes(r)] == ["diameter_not_allowed", "sloped", "unsupported_tail", "unsupported_tail"]


def test_single_config_validation():
    assert C.validate_conduit_config(ccfg(cross_member_single=SINGLE)) == []
    assert C.error_fields(C.validate_conduit_config(ccfg(cross_member_single={"value_mm": -1}))) == \
        ["cross_member_single.value_mm", "cross_member_single.basis"]
    assert C.error_fields(C.validate_conduit_config(ccfg(cross_member_single=200))) == ["cross_member_single"]


def test_dedupe_keys_and_cross_combination_cover():
    pk, sk = C.pair_key("B", "A"), C.single_key("A")
    assert pk == "conduit-pair:A|B" and sk == "conduit-single:A" and C.key_conduits(pk) == ["A", "B"] and C.key_conduits("tray-uid") == []
    ex = [{"id": "h1", "source": "tool", "tray_key": pk}, {"id": "h2", "source": "tool", "tray_key": sk},
          {"id": "h3", "source": "other", "tray_key": None}, {"id": "h4", "source": "tool", "tray_key": "conduit-single:C"},
          {"id": "h5", "source": "tool", "tray_key": "some-tray-uid"}]
    assert C.covering_hangers(sk, ex) == ["h1"]                  # 單管計畫，但該管已有配對吊架 → 不重複
    assert C.covering_hangers(pk, ex) == ["h2"]                  # 配對計畫，但 A 已有單管吊架 → 不重複
    assert C.covering_hangers(C.pair_key("A", "C"), ex) == ["h1", "h2", "h4"]
    assert C.covering_hangers("some-tray-uid", ex) == []         # 橋架鍵不受影響


def test_template_has_single_setting_sourced():
    t = json.loads((ROOT / "docs" / "examples" / "conduit_hanger_config.template.json").read_text(encoding="utf-8"))
    assert t["cross_member_single"] == {"value_mm": 200, "basis": "parameter"}
    assert "2026-10-18" in t["_value_sources"]["cross_member_single"]
    s = json.loads((ROOT / "docs" / "schemas" / "conduit_hanger_config.schema.json").read_text(encoding="utf-8"))
    assert "cross_member_single" in s["properties"] and "cross_member_single" not in s["required"]
    code = (SRC / "ConduitHangerRun.cs").read_text(encoding="utf-8")
    assert "ConduitHangers.CoveringHangers" in code and "ConduitHangers.SingleKey" in code
