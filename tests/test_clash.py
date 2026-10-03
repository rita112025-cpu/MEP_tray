import dataclasses
import json
import random

import pytest

from mep_tray.clash import check_route
from mep_tray.model import Inputs
from mep_tray.router import Route, RoutingError, route_tray
from mep_tray.rules import CLASH, FAIL, LABELS, PASS, UNVERIFIED, merge_strictest

ALL = ["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"]
GOV = merge_strictest(ALL)
GEOM_KINDS = ("clash", "clearance", "headroom", "structure")


def ob(name, kind, lo, hi):
    return {"name": name, "kind": kind, "lo": lo, "hi": hi}


def manual(*pts):
    wp = list(pts)
    return Route([wp], list(zip(wp, wp[1:])), wp[1:-1], 0.0)


def inp(obstacles=(), **kw):
    return Inputs(start=(1, 1, 3), ends=[(10, 1, 3)], obstacles=list(obstacles), **kw)


STRAIGHT = manual((1, 1, 3), (10, 1, 3))


def shift(route, axis, sign, mm):
    d = [0, 0, 0]
    d["XYZ".index(axis)] = (1 if sign == "+" else -1) * mm / 1000
    mv = lambda p: tuple(p[i] + d[i] for i in range(3))
    return manual(*[mv(p) for p in route.waypoints[0]])


# ---------- 衝突 / 淨距 ----------
def test_overlap_is_clash_with_location_and_machine_fix():
    i = inp([ob("pipe", "water", [5, 0.5, 2.5], [5.4, 1.5, 3.5])])
    rep = check_route(i, STRAIGHT, GOV)
    f = [x for x in rep.findings if x.kind == "clash"]
    assert len(f) == 1 and f[0].status == CLASH and rep.has_clash
    assert 5.0 <= f[0].location[0] <= 5.4            # 相交取重疊區中心
    assert f[0].fix["axis"] in "XYZ" and f[0].fix["move_mm"] > 0 and "pipe" in f[0].subject


def test_verified_false_clash_stays_clash_and_clearance_is_unverified_with_indicative():
    clash = inp([ob("pipe", "water", [5, 0.5, 2.5], [5.4, 1.5, 3.5])])
    assert check_route(clash, STRAIGHT, GOV).findings[0].status == CLASH
    near = inp([ob("water1", "water", [4, 1.35, 2.0], [6, 1.6, 3.6])])   # 淨距 200 < 400
    f = [x for x in check_route(near, STRAIGHT, GOV).findings if x.kind == "clearance"][0]
    assert (f.actual, f.required, f.code) == (pytest.approx(200), 400, "MRT_APPX_C")
    assert f.status == UNVERIFIED and f.indicative == FAIL and f.clause
    ver = dict(GOV, clear_water_mm=dataclasses.replace(GOV["clear_water_mm"], verified=True))
    f2 = [x for x in check_route(near, STRAIGHT, ver).findings if x.kind == "clearance"][0]
    assert f2.status == FAIL


def test_pass_is_recorded_in_checks_but_unverified_never_pass():
    i = inp([ob("water1", "water", [4, 2.0, 2.0], [6, 2.3, 3.6])])
    rep = check_route(i, STRAIGHT, GOV)
    assert not [x for x in rep.findings if x.kind == "clearance"]
    c = [x for x in rep.checks if x.kind == "clearance"][0]
    assert c.indicative == PASS and c.status == UNVERIFIED


def test_boundary_exact_clearance_not_reported_one_mm_short_is_reported():
    exact = inp([ob("w", "water", [4, 1.55, 2.0], [6, 1.9, 3.6])])     # 邊緣 1.15 → 淨距 400mm
    assert not [f for f in check_route(exact, STRAIGHT, GOV).findings if f.kind == "clearance"]
    short = inp([ob("w", "water", [4, 1.549, 2.0], [6, 1.9, 3.6])])    # 399mm
    f = [f for f in check_route(short, STRAIGHT, GOV).findings if f.kind == "clearance"]
    assert len(f) == 1 and f[0].actual == pytest.approx(399)


def test_one_finding_per_obstacle_even_if_many_segments():
    i = inp([ob("pipe", "water", [3, 0.5, 2.5], [3.4, 1.5, 3.5])])
    r = manual((1, 1, 3), (2, 1, 3), (2, 5, 3), (9, 5, 3), (9, 1, 3), (10, 1, 3))
    assert len([x for x in check_route(i, r, GOV).checks if x.subject.startswith("pipe")]) == 1


def test_same_type_tray_uses_parallel_rule_and_other_type_uses_separation():
    near = ob("trayB", "tray_power", [1, 1.45, 2.95], [10, 1.75, 3.05])      # 淨距 300 < 600
    f = [x for x in check_route(inp([near], tray_type="power"), STRAIGHT, GOV).findings
         if x.subject.startswith("trayB")][0]
    assert f.required == 600
    sig = ob("trayS", "tray_signal", [1, 1.35, 2.95], [10, 1.65, 3.05])      # 淨距 200 < 300
    f2 = [x for x in check_route(inp([sig], tray_type="power"), STRAIGHT, GOV).findings
          if x.subject.startswith("trayS")][0]
    assert f2.required == 500                 # ALL 含 MRT_APPX_C（C-048 未附屏蔽蓋 500）；CNS/NEC 為 300


# ---------- 房間 ----------
def test_headroom_and_wall_faces():
    f = [x for x in check_route(inp(), manual((1, 1, 3.9), (10, 1, 3.9)), GOV).findings
         if x.kind == "headroom"][0]
    assert f.actual == pytest.approx(50) and f.required == 300 and f.fix["axis"] == "Z"
    wall = check_route(inp(), manual((0.03, 1, 3), (10, 1, 3)), GOV)      # 貼 X- 牆
    s = [x for x in wall.findings if x.kind == "structure"][0]
    assert s.subject == "X- 面" and s.location[0] == 0 and s.fix["sign"] == "+"


# ---------- 建議可驗證 ----------
@pytest.mark.parametrize("o", [ob("w", "water", [4, 1.35, 2.0], [6, 1.6, 3.6]),     # 淨距不足
                               ob("pipe", "water", [5, 0.5, 2.5], [5.4, 1.5, 3.5]),  # 實體重疊
                               ob("h", "heat", [4, 1.2, 2.5], [6, 1.4, 3.5])])
def test_suggested_shift_removes_the_finding(o):
    i = inp([o])
    f = [x for x in check_route(i, STRAIGHT, GOV).findings if x.kind in ("clash", "clearance")][0]
    moved = shift(STRAIGHT, f.fix["axis"], f.fix["sign"], f.fix["move_mm"])
    again = [x for x in check_route(i, moved, GOV).findings if x.kind in ("clash", "clearance")]
    assert not again


# ---------- 與 router 一致、自身接點 ----------
def _scene(seed):
    rnd = random.Random(seed)
    obs = []
    for k in range(rnd.randint(1, 4)):
        kind = rnd.choice(["water", "duct", "heat", "structure", "tray_signal", "tray_power"])
        lo = [rnd.uniform(2, 9), rnd.uniform(0, 4.5), rnd.uniform(0, 3)]
        hi = [lo[0] + rnd.uniform(0.3, 2), lo[1] + rnd.uniform(0.3, 1.5), lo[2] + rnd.uniform(0.3, 1.5)]
        obs.append(ob(f"o{k}", kind, [round(v, 2) for v in lo], [round(v, 2) for v in hi]))
    return Inputs(start=(1, 1, 3), ends=[(10, 5, 3), (6, 1, 3)], obstacles=obs)


def test_router_success_implies_no_geometric_findings_property():
    ok = 0
    for seed in range(25):
        i = _scene(seed)
        try:
            r = route_tray(i.room_box(), i.start, i.ends, i.obstacle_objs(), 0.3, 0.1, "power",
                           GOV, cell=0.25)
        except RoutingError:
            continue
        ok += 1
        bad = [f for f in check_route(i, r, GOV).findings if f.kind in GEOM_KINDS]
        assert not bad, (seed, [(f.kind, f.subject, f.actual, f.required) for f in bad])
    assert ok >= 12      # 確保此 property 真的有跑到足夠多組


def test_own_bends_and_branch_junctions_never_reported_as_clash():
    for ends in ([(10, 5, 3)], [(10, 1, 3), (6, 5, 3)]):
        i = Inputs(start=(1, 1, 3), ends=ends, obstacles=[ob("far", "water", [11, 5, 0], [11.5, 5.5, 1])])
        r = route_tray(i.room_box(), i.start, i.ends, [], 0.3, 0.1, "power", GOV, cell=0.25)
        assert r.bends
        assert not [f for f in check_route(i, r, GOV).findings if f.kind in GEOM_KINDS]


def test_exact_box_is_less_conservative_than_router_approximation():
    i = inp([ob("duct", "duct", [4, 0, 3.25], [6, 2, 3.6])])   # 橋架上方 200mm，風管需 150mm
    c = [x for x in check_route(i, STRAIGHT, GOV).checks if x.kind == "clearance"][0]
    assert c.actual == pytest.approx(200) and c.indicative == PASS


# ---------- 輸出契約 ----------
def test_to_dict_stable_codes_schema_json_and_determinism():
    i = inp([ob("a", "water", [5, 0.5, 2.5], [5.4, 1.5, 3.5]), ob("b", "heat", [7, 1.3, 2], [8, 1.6, 4])])
    d1 = check_route(i, STRAIGHT, GOV).to_dict()
    d2 = check_route(i, STRAIGHT, GOV).to_dict()
    s1 = json.dumps(d1, ensure_ascii=False, allow_nan=False)
    assert s1 == json.dumps(d2, ensure_ascii=False, allow_nan=False)
    assert d1["schema_version"] == 1
    for f in d1["findings"] + d1["checks"]:
        assert f["status"] in LABELS and f["indicative"] in LABELS
        assert f["status_label"] == LABELS[f["status"]] and f["kind"].isascii()
        assert all(isinstance(v, float) for v in f["location"])


def test_empty_route_start_equals_end_does_not_raise_and_is_disclosed():
    i = Inputs(start=(1, 1, 3), ends=[(1, 1, 3)], obstacles=[ob("w", "water", [5, 0, 0], [5.3, 4, 3.2])])
    r = route_tray(i.room_box(), i.start, i.ends, i.obstacle_objs(), 0.3, 0.1, "power", GOV, cell=0.25)
    assert r.segments == []
    rep = check_route(i, r, GOV)
    assert rep.findings == [] and "note_empty_route" in rep.design
    from mep_tray.compliance import check_compliance
    check_compliance(i, r, GOV)          # 亦不得拋例外
