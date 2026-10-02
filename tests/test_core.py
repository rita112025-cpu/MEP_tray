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
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules())
    assert len(r.waypoints[0]) == 2 and r.length_m == pytest.approx(9)


def test_route_avoids_obstacle_with_clearance():
    wall = Obstacle("water", "water", Box((5, 0, 0), (5.3, 4, 3.2)))
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [wall], 0.3, 0.1, "power", _rules())
    need = 0.3 + 0.15           # 水管淨距 300mm + 橋架半寬
    from mep_tray.geometry import segment_box as sb
    for a, b in r.segments:
        assert sb(a, b, 0.3, 0.1).gap(wall.box) >= 0.3 - 1e-6
    assert r.length_m > 9 and need > 0


def test_route_blocked_raises():
    full = Obstacle("slab", "structure", Box((5, -1, -1), (5.3, 7, 5)))
    with pytest.raises(RoutingError):
        route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [full], 0.3, 0.1, "power", _rules())


def test_branches_share_trunk_and_hangers_respect_span():
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (6, 5, 3)], [], 0.3, 0.1, "power", _rules())
    assert len(r.waypoints) == 2
    assert r.length_m == pytest.approx(13.0)
    hs = place_hangers(r, 2.0)
    for a, b in r.segments:
        on = sorted([h for h in hs if _on(h, a, b)], key=lambda h: sum(h))
        for h1, h2 in zip(on, on[1:]):
            assert sum(abs(x - y) for x, y in zip(h1, h2)) <= 2.0 + 1e-6


def _on(h, a, b):
    return all(min(a[i], b[i]) - 1e-6 <= h[i] <= max(a[i], b[i]) + 1e-6 for i in range(3)) and \
        sum(abs(h[i] - a[i]) > 1e-6 and 1 or 0 for i in range(3)) <= 3


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
    chk = evaluate(g["span_max_m"], 1.0, "max")
    assert chk.status == UNVERIFIED and chk.indicative == PASS
    assert evaluate(g["span_max_m"], 9.0, "max").indicative == FAIL
    v = copy.replace(g["span_max_m"], verified=True) if hasattr(copy, "replace") else None
    if v:
        assert evaluate(v, 1.0, "max").status == PASS
        assert evaluate(v, 9.0, "max").status == FAIL


def test_route_deterministic_and_minimal_bends():
    obs = [Obstacle("w", "water", Box((5, 0, 0), (5.3, 4, 3.2))),
           Obstacle("d", "duct", Box((8, 2, 0), (8.5, 6, 3.2)))]
    runs = [route_tray(ROOM, (1, 1, 3), [(10, 1, 3), (6, 5, 3)], obs, 0.3, 0.1, "power", _rules())
            for _ in range(3)]
    assert runs[0].waypoints == runs[1].waypoints == runs[2].waypoints
    straight = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules())
    assert straight.bends == []


def test_try_route_returns_explicit_error():
    full = Obstacle("slab", "structure", Box((5, -1, -1), (5.3, 7, 5)))
    res = try_route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [full], 0.3, 0.1, "power", _rules())
    assert res.ok is False and res.route is None and "路徑" in res.error
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


def test_snap_error_reported_and_zero_on_aligned_coords():
    r = route_tray(ROOM, (1, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules())
    assert r.snap_error_m == 0
    r2 = route_tray(ROOM, (1.03, 1, 3), [(10, 1, 3)], [], 0.3, 0.1, "power", _rules())
    assert 0 < r2.snap_error_m <= r2.cell_m
