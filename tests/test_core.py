import pytest

from mep_tray.geometry import Box, segment_box
from mep_tray.router import (Obstacle, RoutingError, check_bend_legs,
                             place_hangers, route_tray)
from mep_tray.rules import fill_ratio, merge_strictest, recommend_width

ROOM = Box((0, 0, 0), (12, 6, 4))


def test_strictest_picks_max_for_min_and_min_for_max():
    g = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "TW_WIRING"])
    assert g["span_max_m"].value == 2.0 and g["span_max_m"].code != "NEC"
    assert g["fill_max"].value == 0.40
    assert g["clear_water_mm"].value == 300
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
    return merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "TW_WIRING"])


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
