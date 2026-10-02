import pytest

from mep_tray.geometry import Box, segment_box
from mep_tray.router import (Obstacle, RoutingError, check_bend_legs,
                             place_hangers, route_tray)
from mep_tray.rules import fill_ratio, merge_strictest, recommend_width

ROOM = Box((0, 0, 0), (12, 6, 4))


def test_strictest_picks_max_for_min_and_min_for_max():
    g = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])
    assert g["span_max_m"].value == 2.0 and g["span_max_m"].code != "NEC"
    assert g["fill_max"].value == 0.40
    assert g["clear_water_mm"].value == 400 and g["clear_water_mm"].code == "MRT_APPX_C"
    assert g["headroom_mm"].value == 300
    assert "TW_BUILDING" not in g["fill_max"].all_values   # null 不參與


def test_single_code_and_errors():
    assert merge_strictest(["NEC"])["span_max_m"].value == 3.0
    with pytest.raises(ValueError):
        merge_strictest([])
    with pytest.raises(ValueError):
        merge_strictest(["XYZ"])


def test_fill_and_recommend():
    cables = [{"od_mm": 20, "count": 20}]
    assert fill_ratio(cables, 300, 100) == pytest.approx(0.2094, abs=1e-3)
    assert recommend_width(cables, 100, 0.10) == 750
    assert recommend_width([{"od_mm": 50, "count": 500}], 100, 0.1) is None


def test_box_gap_and_intersect():
    a, b = Box((0, 0, 0), (1, 1, 1)), Box((3, 0, 0), (4, 1, 1))
    assert a.gap(b) == 2 and not a.intersects(b)
    assert a.intersects(a.inflate(0.1)) and a.gap(a) == 0
    s = segment_box((0, 0, 3), (5, 0, 3), 0.3, 0.1)
    assert s.lo == (0, -0.15, 2.95) and s.hi == (5, 0.15, 3.05)


def _rules():
    return merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])


def test_route_straight_when_free():
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.25)
    assert len(r.waypoints[0]) == 2 and r.length_m == pytest.approx(9)


def test_route_avoids_obstacle_with_clearance():
    wall = Obstacle("water", "water", Box((5, 0, 0), (5.3, 4, 3.2)))
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [wall], 0.3, 0.1, "power", _rules(), cell=0.25)
    req = _rules()["clear_water_mm"].value / 1000
    for a, b in r.segments:
        assert segment_box(a, b, 0.3, 0.1).gap(wall.box) >= req - 1e-6
    assert r.length_m > 9


def test_route_blocked_raises():
    full = Obstacle("slab", "structure", Box((5, -1, -1), (5.3, 7, 5)))
    with pytest.raises(RoutingError):
        route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [full], 0.3, 0.1, "power", _rules(), cell=0.25)


def _hangers_on(hs, a, b):
    axis = next(i for i in range(3) if abs(a[i] - b[i]) > 1e-9)
    other = [i for i in range(3) if i != axis]
    on = [h for h in hs if all(abs(h[i] - a[i]) < 1e-6 for i in other)
          and min(a[axis], b[axis]) - 1e-6 <= h[axis] <= max(a[axis], b[axis]) + 1e-6]
    return sorted(on, key=lambda h: h[axis]), axis


def test_branches_share_trunk():
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (6, 5, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.25)
    assert len(r.waypoints) == 2
    assert r.length_m == pytest.approx(13.0)


def test_hangers_span_and_near_ends():
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (6, 5, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.25)
    hs = place_hangers(r, 2.0, near_bend_m=0.3)
    for a, b in r.segments:
        on, axis = _hangers_on(hs, a, b)
        assert len(on) >= 2
        for h1, h2 in zip(on, on[1:]):
            assert h2[axis] - h1[axis] <= 2.0 + 1e-6
        assert abs(on[0][axis] - min(a[axis], b[axis])) <= 0.3 + 1e-6
        assert abs(max(a[axis], b[axis]) - on[-1][axis]) <= 0.3 + 1e-6


def test_bend_leg_check():
    r = route_tray(ROOM, (1, 1, 3), [(1.2, 1.2, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.1)
    assert check_bend_legs(r, 0.3)


# ---- 審查意見補強 ----
import copy
import json

from mep_tray.model import Inputs
from mep_tray.router import auto_cell, try_route_tray
from mep_tray.rules import (FAIL, PASS, UNVERIFIED, evaluate, load_rules,
                            validate_rules)

ALL = ["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"]


def test_every_param_strictest_not_looser_than_any_single_code():
    rules = load_rules()
    g = merge_strictest(ALL, rules)
    for key, meta in rules["params"].items():
        for c in ALL:
            v = rules["codes"][c]["values"].get(key)
            if v is None or key not in g:
                continue
            if meta["direction"] == "min":
                assert g[key].value >= v
            else:
                assert g[key].value <= v
            assert g[key].code in g[key].all_values        # 來源規範可追溯
            assert g[key].all_values[g[key].code] == g[key].value


@pytest.mark.parametrize("key,code", [("span_max_m", "CNS"), ("fill_max", "CNS"),
                                      ("clear_water_mm", "MRT_APPX_C"),
                                      ("tray_parallel_mm", "MRT_APPX_C")])
def test_winner_code(key, code):
    g = merge_strictest(ALL)
    assert g[key].all_values[code] == g[key].value


def test_rules_validation_rejects_bad_files():
    good = load_rules()
    for mut in (lambda r: r["params"]["fill_max"].pop("direction"),
                lambda r: r["params"]["span_max_m"].update(unit="mm"),
                lambda r: r["codes"]["CNS"].pop("verified"),
                lambda r: r["codes"]["CNS"]["values"].update(bogus=1),
                lambda r: r["codes"]["CNS"]["values"].update(fill_max="x")):
        bad = copy.deepcopy(good)
        mut(bad)
        with pytest.raises(ValueError):
            validate_rules(bad)


def test_unverified_never_reported_as_pass():
    g = merge_strictest(ALL)
    chk = evaluate(g["span_max_m"], 1.0)
    assert chk.status == UNVERIFIED and chk.indicative == PASS
    assert evaluate(g["span_max_m"], 9.0).indicative == FAIL


def test_route_deterministic_and_minimal_bends():
    obs = [Obstacle("w", "water", Box((5, 0, 0), (5.3, 4, 3.2))),
           Obstacle("d", "duct", Box((8, 2, 0), (8.5, 6, 3.2)))]
    runs = [route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (6, 5, 3)], obs, 0.3, 0.1, "power", _rules(), cell=0.25)
            for _ in range(3)]
    assert runs[0].waypoints == runs[1].waypoints == runs[2].waypoints
    straight = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.25)
    assert straight.bends == []


def test_try_route_returns_explicit_error():
    full = Obstacle("slab", "structure", Box((5, -1, -1), (5.3, 7, 5)))
    res = try_route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [full], 0.3, 0.1, "power", _rules(), cell=0.25)
    assert res.ok is False and res.route is None and "找不到" in res.error
    ok = try_route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules())
    assert ok.ok and ok.route.cell_m > 0


def test_auto_cell_not_larger_than_smallest_clearance():
    g = _rules()
    c = auto_cell(0.3, g)
    assert c <= 0.15 + 1e-9 and c >= 0.05 and c in (0.1, 0.05)


def test_narrow_gap_found_with_auto_cell():
    # 兩牆間縫寬 0.9m：需 2*(0.3 淨距)+0.3 橋架寬=0.9 才剛好通過
    a = Obstacle("a", "water", Box((5, 0, 0), (5.3, 2.55, 4)))
    b = Obstacle("b", "water", Box((5, 3.45, 0), (5.3, 6, 4)))
    res = try_route_tray(ROOM, (1, 3, 3), [(10, 3, 3)], [a, b], 0.3, 0.1, "power",
                         merge_strictest(["TW_BUILDING"]))
    assert res.ok


def test_inputs_roundtrip_and_defaults_disclosed():
    i = Inputs()
    assert i.cables_defaulted and i.effective_cables()[0]["od_mm"] == 20
    j = Inputs.from_dict(json.loads(json.dumps(i.to_dict())))
    assert j.to_dict() == i.to_dict()
    with pytest.raises(ValueError):
        Inputs.from_dict({"obstacles": [{"name": "x", "kind": "nope", "lo": [0, 0, 0], "hi": [1, 1, 1]}]})


def test_endpoints_preserved_exactly_and_misaligned_rejected():
    r = route_tray(ROOM, (1.1, 1, 3), [(6.1, 5.05, 3)], [], 0.3, 0.1, "power", _rules())
    assert r.waypoints[0][0] == (1.1, 1.0, 3.0) and r.waypoints[0][-1] == (6.1, 5.05, 3.0)
    with pytest.raises(RoutingError, match="對齊"):
        route_tray(ROOM, (1.03, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules())
    with pytest.raises(RoutingError, match="對齊"):
        route_tray(ROOM, (1.1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.25)


def _thin(gap=None):
    obs = [Obstacle("plate", "other", Box((5.05, 0, 0), (5.10, 6, 4)))]
    if gap:
        obs = [Obstacle("p1", "other", Box((5.05, 0, 0), (5.10, gap[0], 4))),
               Obstacle("p2", "other", Box((5.05, gap[1], 0), (5.10, 6, 4)))]
    return obs


def test_thin_plate_through_room_cannot_be_crossed():
    g = merge_strictest(["TW_BUILDING"])
    with pytest.raises(RoutingError):
        route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], _thin(), 0.1, 0.1, "power", g, cell=0.25)


def test_thin_plate_with_gap_is_detoured():
    g = merge_strictest(["TW_BUILDING"])
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], _thin(gap=(3.0, 4.5)), 0.1, 0.1, "power", g, cell=0.25)
    assert r.length_m > 9
    for a, b in r.segments:
        for ob in _thin(gap=(3.0, 4.5)):
            assert not segment_box(a, b, 0.1, 0.1).intersects(ob.box)


def test_bend_check_covers_junction_legs():
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (6.1, 5, 3)], [], 0.3, 0.1, "power", _rules())
    assert r.junctions
    assert not check_bend_legs(r, 0.3)
    near = route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (1.1, 5, 3)], [], 0.3, 0.1, "power", _rules())
    assert any(abs(p[0] - 1.1) < 1e-6 for p, _ in check_bend_legs(near, 0.5))


def test_evaluate_reads_direction_from_governing():
    import dataclasses
    g = merge_strictest(ALL)
    assert g["span_max_m"].direction == "max" and g["headroom_mm"].direction == "min"
    for verified in (True, False):
        mx = dataclasses.replace(g["span_max_m"], verified=verified)   # 上限 2.0m
        mn = dataclasses.replace(g["headroom_mm"], verified=verified)  # 下限 300mm
        ok_mx, bad_mx = evaluate(mx, 1.5), evaluate(mx, 2.5)
        ok_mn, bad_mn = evaluate(mn, 350), evaluate(mn, 250)
        assert (ok_mx.indicative, bad_mx.indicative) == (PASS, FAIL)
        assert (ok_mn.indicative, bad_mn.indicative) == (PASS, FAIL)
        want = (PASS, FAIL) if verified else (UNVERIFIED, UNVERIFIED)
        assert (ok_mx.status, bad_mx.status) == want and (ok_mn.status, bad_mn.status) == want


def test_search_limit_returns_explicit_error():
    res = try_route_tray(ROOM, (1, 1, 3), [(10, 5, 1)], [], 0.3, 0.1, "power", _rules(),
                         cell=0.25, max_expansions=5)
    assert res.ok is False and "超過搜尋上限" in res.error


def test_governing_direction_is_required():
    from mep_tray.rules import Governing
    with pytest.raises(TypeError):
        Governing("k", "l", "mm", 1.0, "CNS", "c", True)           # 缺 direction
    g = Governing("k", "l", "mm", 1.0, "CNS", "c", True, "min")
    assert g.direction == "min" and g.all_values == {}


def test_grid_size_guard_is_checked_before_search():
    huge = Box((0, 0, 0), (100, 100, 10))
    res = try_route_tray(huge, (1, 1, 3), [(90, 90, 3)], [], 0.3, 0.1, "power", _rules(), cell=0.05)
    assert res.ok is False and "格點總數" in res.error
    res2 = try_route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules(),
                          cell=0.25, max_cells=10)
    assert res2.ok is False and "格點總數" in res2.error
