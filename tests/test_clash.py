import dataclasses
import json

import pytest

from mep_tray.clash import CLASH, check_route
from mep_tray.geometry import Box
from mep_tray.model import Inputs
from mep_tray.router import Route, place_hangers, route_tray
from mep_tray.rules import FAIL, PASS, UNVERIFIED, merge_strictest

ALL = ["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"]
GOV = merge_strictest(ALL)


def ob(name, kind, lo, hi):
    return {"name": name, "kind": kind, "lo": lo, "hi": hi}


def manual(*pts):
    wp = list(pts)
    segs = list(zip(wp, wp[1:]))
    return Route([wp], segs, wp[1:-1], 0.0)


def inp(obstacles=(), **kw):
    return Inputs(start=(1, 1, 3), ends=[(10, 1, 3)], obstacles=list(obstacles), **kw)


def test_overlap_is_clash_with_location_and_suggestion():
    i = inp([ob("pipe", "water", [5, 0.5, 2.5], [5.4, 1.5, 3.5])])
    rep = check_route(i, manual((1, 1, 3), (10, 1, 3)), GOV)
    f = [x for x in rep.findings if x.kind == "clash"]
    assert len(f) == 1 and f[0].status == CLASH and rep.has_clash
    assert 5.0 <= f[0].location[0] <= 5.4 and "移開" in f[0].suggestion and "pipe" in f[0].subject


def test_clearance_fail_cites_code_and_is_unverified_by_default():
    # 橋架邊緣 y=1.15；水管 y 起 1.35 → 淨距 200mm < 400mm(MRT_APPX_C)
    i = inp([ob("water1", "water", [4, 1.35, 2.0], [6, 1.6, 3.6])])
    rep = check_route(i, manual((1, 1, 3), (10, 1, 3)), GOV)
    f = [x for x in rep.findings if x.kind == "clearance"][0]
    assert f.actual == pytest.approx(200) and f.required == 400 and f.code == "MRT_APPX_C"
    assert f.indicative == FAIL and f.status == UNVERIFIED and f.clause and "移開" in f.suggestion
    ver = dict(GOV, clear_water_mm=dataclasses.replace(GOV["clear_water_mm"], verified=True))
    f2 = [x for x in check_route(i, manual((1, 1, 3), (10, 1, 3)), ver).findings
          if x.kind == "clearance"][0]
    assert f2.status == FAIL


def test_compliant_clearance_recorded_in_checks_but_not_a_finding_and_never_pass_if_unverified():
    i = inp([ob("water1", "water", [4, 2.0, 2.0], [6, 2.3, 3.6])])
    rep = check_route(i, manual((1, 1, 3), (10, 1, 3)), GOV)
    assert not [x for x in rep.findings if x.kind == "clearance"]
    c = [x for x in rep.checks if x.kind == "clearance"][0]
    assert c.indicative == PASS and c.status == UNVERIFIED


def test_one_finding_per_obstacle_even_if_many_segments():
    i = inp([ob("pipe", "water", [3, 0.5, 2.5], [3.4, 1.5, 3.5])])
    r = manual((1, 1, 3), (2, 1, 3), (2, 5, 3), (9, 5, 3), (9, 1, 3), (10, 1, 3))
    rep = check_route(i, r, GOV)
    assert len([x for x in rep.checks if x.subject.startswith("pipe")]) == 1


def test_same_type_tray_uses_parallel_rule_and_other_type_uses_separation():
    near = ob("trayB", "tray_power", [1, 1.55, 2.95], [10, 1.85, 3.05])   # 淨距 ≈400mm
    rep = check_route(inp([near], tray_type="power"), manual((1, 1, 3), (10, 1, 3)), GOV)
    f = [x for x in rep.findings if x.subject.startswith("trayB")][0]
    assert f.required == 600
    sig = ob("trayS", "tray_signal", [1, 1.35, 2.95], [10, 1.65, 3.05])   # 淨距 200mm
    rep2 = check_route(inp([sig], tray_type="power"), manual((1, 1, 3), (10, 1, 3)), GOV)
    f2 = [x for x in rep2.findings if x.subject.startswith("trayS")][0]
    assert f2.required == 300 and f2.code in ("CNS", "NEC", "MRT_APPX_C")


def test_headroom_to_ceiling():
    rep = check_route(inp(), manual((1, 1, 3.9), (10, 1, 3.9)), GOV)
    f = [x for x in rep.findings if x.kind == "headroom"][0]
    assert f.actual == pytest.approx(50) and f.required == 300


def test_hanger_span_checked_and_missing_hangers_disclosed():
    r = manual((1, 1, 3), (10, 1, 3))
    assert "note_hangers" in check_route(inp(), r, GOV).design
    r.hangers = [(1.3, 1, 3), (9.7, 1, 3)]
    f = [x for x in check_route(inp(), r, GOV).findings if x.kind == "hanger_span"]
    assert f and f[0].actual == pytest.approx(8.4)
    place_hangers(r, GOV["span_max_m"].value)
    assert not [x for x in check_route(inp(), r, GOV).findings if x.kind == "hanger_span"]


def test_fill_fail_recommends_width_and_discloses_default_cables():
    i = inp(cables=[{"od_mm": 30, "count": 40, "kind": "power"}], tray_w_mm=300)
    rep = check_route(i, manual((1, 1, 3), (10, 1, 3)), GOV)
    f = [x for x in rep.findings if x.kind == "fill"][0]
    assert "建議橋架寬" in f.suggestion and f.code == "CNS"
    assert "cables_defaulted" in check_route(inp(), manual((1, 1, 3), (10, 1, 3)), GOV).design


def test_bend_leg_finding():
    r = manual((1, 1, 3), (1.2, 1, 3), (1.2, 5, 3))
    rep = check_route(inp(), r, GOV)
    assert [x for x in rep.findings if x.kind == "bend"]
    assert rep.design["fitting_radius_mm_used"] >= 300


def test_router_output_has_no_clash_or_clearance_failure():
    obs = [ob("w", "water", [5, 0, 0], [5.3, 4, 3.2]), ob("d", "duct", [8, 2, 0], [8.5, 6, 3.2])]
    i = inp(obs)
    r = route_tray(i.room_box(), i.start, i.ends, i.obstacle_objs(), 0.3, 0.1, "power", GOV, cell=0.25)
    place_hangers(r, GOV["span_max_m"].value)
    rep = check_route(i, r, GOV)
    assert not [f for f in rep.findings if f.kind in ("clash", "clearance", "headroom", "structure")]


def test_exact_box_is_less_conservative_than_router_approximation():
    # 橋架上方 200mm 處有風管(需 150mm)：精確外包盒(半高 50mm)判符合；
    # router 以 max(寬,高)/2=150mm 膨脹，會認為該高度不可走 → 以 clash 為準
    i = inp([ob("duct", "duct", [4, 0, 3.25], [6, 2, 3.6])])
    rep = check_route(i, manual((1, 1, 3), (10, 1, 3)), GOV)
    c = [x for x in rep.checks if x.kind == "clearance"][0]
    assert c.actual == pytest.approx(200) and c.indicative == PASS


def test_report_is_json_serialisable_and_deterministic():
    i = inp([ob("a", "water", [5, 0.5, 2.5], [5.4, 1.5, 3.5]), ob("b", "heat", [7, 1.3, 2], [8, 1.6, 4])])
    r = manual((1, 1, 3), (10, 1, 3))
    d1, d2 = check_route(i, r, GOV).to_dict(), check_route(i, r, GOV).to_dict()
    assert json.dumps(d1, ensure_ascii=False) == json.dumps(d2, ensure_ascii=False)
