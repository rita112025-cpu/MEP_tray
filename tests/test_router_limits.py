import math
import random
import time

import pytest

from mep_tray import pipeline as P
from mep_tray.geometry import Box
from mep_tray.model import Inputs
from mep_tray.router import (MAX_OBSTACLE_WORK, Obstacle, RoutingError, estimate_obstacle_work, grid_cells,
                             grid_size, obstacle_work, route_tray, try_route_tray)
from mep_tray.rules import merge_strictest
from tests.test_pipeline import ALL, isolate  # noqa: F401  (isolate 為 autouse fixture)

GOV = merge_strictest(ALL)
BIG_ROOM = Box((0, 0, 0), (30, 15, 5))


def ob(name, lo, hi, kind="structure"):
    return Obstacle(name, kind, Box(lo, hi))


def whole_room(name="whole"):
    return ob(name, (-1, -1, -1), (31, 16, 6))


# ───────────── 障礙物工作量預算 ─────────────
def test_whole_room_obstacle_is_refused_instantly_with_a_stable_code():
    t0 = time.perf_counter()
    res = try_route_tray(BIG_ROOM, (1, 7, 3), [(29, 7, 3)], [whole_room()], 0.3, 0.1, "power", GOV, cell=0.1)
    dt = time.perf_counter() - t0
    assert res.ok is False and res.code == "obstacle_work_limit" and "估算工作量" in res.error
    assert dt < 2.0, dt                                   # 過去要 46 秒才被擋；現在是 O(障礙物數)


def test_several_big_obstacles_are_refused_by_their_sum():
    one = ob("o", (0.2, 0.2, 0.2), (7.0, 14.0, 4.6))
    w1 = estimate_obstacle_work(BIG_ROOM, [one], 0.3, 0.1, "power", GOV, 0.1)
    assert w1 < MAX_OBSTACLE_WORK                          # 單個在預算內
    many = [ob(f"o{i}", (0.2, 0.2, 0.2), (7.0, 14.0, 4.6)) for i in range(4)]
    t0 = time.perf_counter()
    res = try_route_tray(BIG_ROOM, (1, 7, 3), [(29, 7, 3)], many, 0.3, 0.1, "power", GOV, cell=0.1)
    assert res.ok is False and res.code == "obstacle_work_limit" and time.perf_counter() - t0 < 2.0


def test_normal_scenes_are_far_below_the_budget_and_unaffected():
    room = Box((0, 0, 0), (12, 6, 4))
    wall = ob("w", (5, 0, 0), (5.3, 4, 3.2), "water")
    for cell in (0.25, 0.1):
        w = estimate_obstacle_work(room, [wall], 0.3, 0.1, "power", GOV, cell)
        assert 0 < w < MAX_OBSTACLE_WORK * 0.2, (cell, w)
    r = route_tray(room, (1, 1, 3), [(10, 1, 3)], [wall], 0.3, 0.1, "power", GOV, cell=0.25)
    assert r.length_m > 9
    twelve = [ob(f"o{i}", (2 + i * 0.8, 4.5, 0), (2.2 + i * 0.8, 5, 1), "other") for i in range(12)]
    assert estimate_obstacle_work(Box((0, 0, 0), (20, 12, 6)), twelve, 0.3, 0.1, "power", GOV, 0.25) < MAX_OBSTACLE_WORK


def test_budget_is_a_parameter_and_checked_before_any_marking():
    wall = ob("w", (5, 0, 0), (5.3, 4, 3.2), "water")
    room = Box((0, 0, 0), (12, 6, 4))
    w = estimate_obstacle_work(room, [wall], 0.3, 0.1, "power", GOV, 0.25)
    with pytest.raises(RoutingError) as ei:
        route_tray(room, (1, 1, 3), [(10, 1, 3)], [wall], 0.3, 0.1, "power", GOV, cell=0.25, max_obstacle_work=w - 1)
    assert ei.value.code == "obstacle_work_limit"
    route_tray(room, (1, 1, 3), [(10, 1, 3)], [wall], 0.3, 0.1, "power", GOV, cell=0.25, max_obstacle_work=w)


def test_estimate_matches_an_independent_count_for_a_simple_box():
    room = Box((0, 0, 0), (10, 10, 10))
    cell = 0.5
    n = grid_size(room, cell)
    inf = Box((2, 2, 2), (4, 4, 4))
    # 獨立計算：各軸格點索引範圍 [floor(lo/c), ceil(hi/c)]；邊範圍再往低側多一格
    rng = [(math.floor(2 / cell), math.ceil(4 / cell))] * 3
    nodes = math.prod(b - a + 1 for a, b in rng)
    e0, e1 = max(0, math.floor(2 / cell) - 1), min(n[0] - 2, math.ceil(4 / cell))
    edges = 3 * (e1 - e0 + 1) * (rng[0][1] - rng[0][0] + 1) ** 2
    assert obstacle_work(inf, room.lo, cell, n) == nodes + edges


def test_estimate_is_monotone_clipped_to_the_grid_and_zero_outside():
    room = Box((0, 0, 0), (10, 10, 10))
    n = grid_size(room, 0.5)
    ws = [obstacle_work(Box((1, 1, 1), (1 + d, 1 + d, 1 + d)), room.lo, 0.5, n) for d in (0.5, 1, 2, 4)]
    assert ws == sorted(ws) and len(set(ws)) == len(ws)
    huge = obstacle_work(Box((-1e6, -1e6, -1e6), (1e6, 1e6, 1e6)), room.lo, 0.5, n)
    assert huge <= 4 * n[0] * n[1] * n[2]                 # 被格網裁切，不會因座標很大而爆炸
    assert obstacle_work(Box((100, 100, 100), (101, 101, 101)), room.lo, 0.5, n) == 0
    assert obstacle_work(Box((-50, -50, -50), (-40, -40, -40)), room.lo, 0.5, n) == 0


def test_no_obstacles_means_zero_work():
    assert estimate_obstacle_work(BIG_ROOM, [], 0.3, 0.1, "power", GOV, 0.1) == 0


# ───────────── pipeline 層 ─────────────
def test_pipeline_returns_structured_error_for_whole_room_obstacle_and_leaves_nothing(isolate):
    inp = Inputs(room=((0, 0, 0), (30, 15, 5)), start=(1, 7, 3), ends=[(29, 7, 3)], cell_m=0.1,
                 obstacles=[{"name": "whole", "kind": "structure", "lo": [-1, -1, -1], "hi": [31, 16, 6]}])
    t0 = time.perf_counter()
    r = P.run(inp, ALL, "ow1", make_dwg=False)
    assert time.perf_counter() - t0 < 2.0
    assert r.ok is False and (r.error.stage, r.error.code) == ("route", "obstacle_work_limit")
    assert not (isolate / "ow1").exists()


# ───────────── grid_size 單一來源 ─────────────
def test_grid_size_formula_and_cells():
    room = Box((0, 0, 0), (12, 6, 4))
    assert grid_size(room, 0.25) == (49, 25, 17) and grid_cells(room, 0.25) == 49 * 25 * 17
    assert grid_size(Box((1, 1, 1), (1.05, 1.05, 1.05)), 0.1) == (1, 1, 1)
    # 浮點：0.3/0.1 = 2.9999999999999996 → floor=2 → 3 格（既有行為；router 與 webui 共用同一公式，所以兩邊一致）
    assert grid_size(Box((0, 0, 0), (0.3, 0.3, 0.3)), 0.1) == (3, 3, 3)


def test_router_and_grid_helper_agree_on_the_max_cells_boundary_for_random_rooms():
    rnd = random.Random(7)
    for _ in range(60):
        room = Box((0, 0, 0), (round(rnd.uniform(0.5, 40), 2), round(rnd.uniform(0.5, 40), 2),
                               round(rnd.uniform(0.5, 10), 2)))
        cell = rnd.choice([0.25, 0.2, 0.1, 0.05])
        total = grid_cells(room, cell)
        lo = try_route_tray(room, room.lo, [room.lo], [], 0.3, 0.1, "power", GOV, cell=cell, max_cells=total - 1)
        assert lo.ok is False and lo.code == "grid_too_large", (room, cell, total)
        ok = try_route_tray(room, room.lo, [room.lo], [], 0.3, 0.1, "power", GOV, cell=cell, max_cells=total)
        assert ok.code != "grid_too_large", (room, cell, total)             # 恰在上限不被 grid 規則擋下
