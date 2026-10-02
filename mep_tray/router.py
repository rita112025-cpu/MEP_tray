"""3D 正交 A* 橋架路徑佈設：避障（含規範淨距）、轉彎懲罰、分支共幹、吊架配置。單位：公尺。"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .geometry import Box, Vec, dist
from .rules import Governing

DIRS = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]

# 障礙物類型 → 規則鍵
OBSTACLE_RULE = {
    "water": "clear_water_mm",
    "duct": "clear_duct_mm",
    "heat": "clear_heat_mm",
    "structure": "structure_clear_mm",
}


NICE_CELLS = [0.25, 0.2, 0.1, 0.05]


class RoutingError(RuntimeError):
    pass


@dataclass
class Obstacle:
    name: str
    kind: str          # water|duct|heat|structure|tray_power|tray_signal|other
    box: Box


@dataclass
class Route:
    waypoints: list[list[Vec]]            # 每個終點一條折線（從接入點到終點）
    segments: list[tuple[Vec, Vec]]       # 全部直線段
    bends: list[Vec]                      # 轉彎/分支節點
    length_m: float
    hangers: list[Vec] = field(default_factory=list)
    cell_m: float = 0.0
    snap_error_m: float = 0.0             # 起訖點吸附到格點的最大位移


def clearance_m(kind: str, tray_type: str, rules: dict[str, Governing]) -> float:
    """障礙物類型對應之規範淨距（公尺）；規範未規定則採側向作業淨空或 0。"""
    if kind in ("tray_power", "tray_signal"):
        other = kind.split("_")[1]
        key = "sep_power_signal_mm" if other != tray_type else "side_clear_mm"
    else:
        key = OBSTACLE_RULE.get(kind, "side_clear_mm")
    g = rules.get(key)
    return (g.value if g else 0.0) / 1000.0


@dataclass
class RouteResult:
    """路徑規劃結果：ok=False 時 route=None 並帶明確 error（不丟例外、不回空 list）。"""
    ok: bool
    route: "Route | None" = None
    error: str = ""
    cell_m: float = 0.0


def auto_cell(tray_w_m: float, rules: dict[str, Governing], max_cell: float = 0.25) -> float:
    """格距需 ≤ 最小非零淨距與橋架寬/2，避免窄通道被誤判無路；下限 0.05m。"""
    cands = [max_cell, tray_w_m / 2]
    cands += [g.value / 1000 for k, g in rules.items() if k.endswith("_mm") and 0 < g.value]
    limit = min(cands)
    # 只取易整除常見座標(5cm 倍數)的格距，降低端點吸附誤差
    return next((c for c in NICE_CELLS if c <= limit + 1e-9), NICE_CELLS[-1])


def try_route_tray(*a, **kw) -> RouteResult:
    """route_tray 的非例外包裝，給 UI/報告流程使用。"""
    try:
        r = route_tray(*a, **kw)
        return RouteResult(True, r, "", r.cell_m)
    except RoutingError as e:
        return RouteResult(False, None, str(e))


def route_tray(room: Box, start: Vec, ends: list[Vec], obstacles: list[Obstacle],
               tray_w_m: float, tray_h_m: float, tray_type: str,
               rules: dict[str, Governing], cell: float | None = None,
               bend_penalty: float = 4.0, vertical_penalty: float = 1.3) -> Route:
    if cell is None:
        cell = auto_cell(tray_w_m, rules)
    half = max(tray_w_m, tray_h_m) / 2
    sc = rules.get("structure_clear_mm")
    wall = (sc.value if sc else 0.0) / 1000.0 + half
    origin = room.lo
    n = tuple(int(math.floor((room.hi[i] - room.lo[i]) / cell)) + 1 for i in range(3))

    def to_cell(p: Vec):
        return tuple(int(round((p[i] - origin[i]) / cell)) for i in range(3))

    def to_pos(c) -> Vec:
        return tuple(round(origin[i] + c[i] * cell, 6) for i in range(3))

    # 可行範圍（中心線需離牆/結構一定距離）
    def in_bounds(c) -> bool:
        p = to_pos(c)
        return all(room.lo[i] + wall - 1e-9 <= p[i] <= room.hi[i] - wall + 1e-9 for i in range(3))

    blocked: set = set()
    for ob in obstacles:
        inf = ob.box.inflate(clearance_m(ob.kind, tray_type, rules) + half)
        rng = []
        for i in range(3):
            a = max(0, int(math.floor((inf.lo[i] - origin[i]) / cell)))
            b = min(n[i] - 1, int(math.ceil((inf.hi[i] - origin[i]) / cell)))
            rng.append(range(a, b + 1))
        for x in rng[0]:
            for y in rng[1]:
                for z in rng[2]:
                    p = to_pos((x, y, z))
                    if all(inf.lo[k] < p[k] < inf.hi[k] for k in range(3)):
                        blocked.add((x, y, z))

    def free(c) -> bool:
        return all(0 <= c[i] < n[i] for i in range(3)) and in_bounds(c) and c not in blocked

    s_cell = to_cell(start)
    if not free(s_cell):
        raise RoutingError(f"起點 {start} 落在障礙/淨距範圍或牆邊禁區內")
    network: set = {s_cell}
    waypoints: list[list[Vec]] = []
    junctions: list[Vec] = []

    # 由遠到近逐一接入網路：先成形長幹線，近端分支再接入，共幹較省
    for end in sorted(ends, key=lambda e: (-dist(start, e), e)):
        t_cell = to_cell(end)
        if not free(t_cell):
            raise RoutingError(f"終點 {end} 落在障礙/淨距範圍或牆邊禁區內")
        path = _astar(network, t_cell, free, bend_penalty, vertical_penalty)
        if path is None:
            raise RoutingError(f"找不到通往終點 {end} 的可行路徑（淨距/障礙過嚴？）")
        if path[0] != s_cell and len(network) > 1:
            junctions.append(to_pos(path[0]))
        network.update(path)
        waypoints.append(_simplify([to_pos(c) for c in path]))

    segs: list[tuple[Vec, Vec]] = []
    bends: list[Vec] = list(junctions)
    for wp in waypoints:
        for a, b in zip(wp, wp[1:]):
            segs.append((a, b))
        bends.extend(wp[1:-1])
    length = sum(dist(a, b) for a, b in segs)
    snap = max(dist(p, to_pos(to_cell(p))) for p in [start, *ends])
    return Route(waypoints, segs, _uniq(bends), length, cell_m=cell, snap_error_m=snap)


def _astar(sources: set, goal, free, bend_penalty, vertical_penalty):
    def h(c):
        return abs(c[0] - goal[0]) + abs(c[1] - goal[1]) + abs(c[2] - goal[2])

    open_: list = []
    best: dict = {}
    parent: dict = {}
    for s in sorted(sources):
        st = (s, -1)
        best[st] = 0.0
        heapq.heappush(open_, (h(s), 0.0, s, -1))
    while open_:
        _, g, c, d = heapq.heappop(open_)
        if g > best.get((c, d), 1e18):
            continue
        if c == goal:
            out = [c]
            st = (c, d)
            while st in parent:
                st = parent[st]
                out.append(st[0])
            out.reverse()
            return out
        for nd, v in enumerate(DIRS):
            nc = (c[0] + v[0], c[1] + v[1], c[2] + v[2])
            if not free(nc):
                continue
            step = vertical_penalty if v[2] else 1.0
            if d != -1 and d != nd:
                step += bend_penalty
            ng = g + step
            if ng < best.get((nc, nd), 1e18):
                best[(nc, nd)] = ng
                parent[(nc, nd)] = (c, d)
                heapq.heappush(open_, (ng + h(nc), ng, nc, nd))
    return None


def _simplify(pts: list[Vec]) -> list[Vec]:
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        d1 = tuple(round(b[k] - a[k], 6) for k in range(3))
        d2 = tuple(round(c[k] - b[k], 6) for k in range(3))
        n1 = [x / (abs(x) or 1) for x in d1]
        n2 = [x / (abs(x) or 1) for x in d2]
        if n1 != n2:
            out.append(b)
    out.append(pts[-1])
    return out


def _uniq(pts: list[Vec]) -> list[Vec]:
    seen, out = set(), []
    for p in pts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def place_hangers(route: Route, span_max_m: float, near_bend_m: float = 0.3) -> list[Vec]:
    """每段：兩端內縮 near_bend_m 設吊架，中間等分且間距 ≤ span_max_m。"""
    hs: list[Vec] = []
    for a, b in route.segments:
        L = dist(a, b)
        if L <= 1e-9:
            continue
        off = min(near_bend_m, L / 2)
        usable = L - 2 * off
        k = max(1, math.ceil(usable / span_max_m)) if usable > 1e-9 else 0
        ts = [off / L]
        ts += [(off + usable * i / k) / L for i in range(1, k + 1)] if k else []
        for t in ts:
            hs.append(tuple(round(a[i] + (b[i] - a[i]) * t, 6) for i in range(3)))
    route.hangers = _uniq(hs)
    return route.hangers


def check_bend_legs(route: Route, radius_m: float) -> list[tuple[Vec, float]]:
    """轉彎處相鄰兩邊長若小於配件半徑則回報 (節點, 較短邊長)。"""
    bad = []
    for wp in route.waypoints:
        for i in range(1, len(wp) - 1):
            leg = min(dist(wp[i - 1], wp[i]), dist(wp[i], wp[i + 1]))
            if leg + 1e-9 < radius_m:
                bad.append((wp[i], leg))
    return bad
