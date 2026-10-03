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
    """路徑規劃的可預期失敗。code 為穩定的機器可讀代碼（供管線/UI 使用，勿比對中文訊息）：
    misaligned | grid_too_large | obstacle_work_limit | endpoint_blocked | no_route | search_limit | route_error"""

    def __init__(self, message: str, code: str = "route_error"):
        super().__init__(message)
        self.code = code


@dataclass
class Obstacle:
    name: str
    kind: str          # water|duct|heat|heat_bare|structure|tray_power|tray_signal|other
    box: Box


@dataclass
class Route:
    waypoints: list[list[Vec]]            # 每個終點一條折線（從接入點到終點）
    segments: list[tuple[Vec, Vec]]       # 全部直線段
    bends: list[Vec]                      # 轉彎/分支節點
    length_m: float
    hangers: list[Vec] = field(default_factory=list)
    cell_m: float = 0.0
    junctions: list = field(default_factory=list)   # [(點, 分支索引)]


def rule_key(kind: str, tray_type: str, rules: dict[str, Governing]) -> str:
    """障礙物類型 → 適用的規則鍵。同類橋架優先用 tray_parallel_mm（若有規範給值），否則側向淨空。"""
    if kind in ("tray_power", "tray_signal"):
        if kind.split("_")[1] != tray_type:
            return "sep_power_signal_mm"
        return "tray_parallel_mm" if "tray_parallel_mm" in rules else "side_clear_mm"
    if kind == "heat_bare":                       # 無保溫熱源：有規範給值才用專用鍵，否則退回一般熱源淨距
        return "clear_heat_bare_mm" if "clear_heat_bare_mm" in rules else "clear_heat_mm"
    return OBSTACLE_RULE.get(kind, "side_clear_mm")


def clearance_m(kind: str, tray_type: str, rules: dict[str, Governing]) -> float:
    """障礙物對應之規範淨距（公尺）；規範未規定則為 0。"""
    g = rules.get(rule_key(kind, tray_type, rules))
    return (g.value if g else 0.0) / 1000.0


@dataclass
class RouteResult:
    """路徑規劃結果：ok=False 時 route=None 並帶明確 error（不丟例外、不回空 list）。"""
    ok: bool
    route: "Route | None" = None
    error: str = ""
    cell_m: float = 0.0
    code: str = ""


def _aligned(points, origin, cell: float) -> bool:
    return all(abs((p[i] - origin[i]) / cell - round((p[i] - origin[i]) / cell)) * cell < 1e-6
               for p in points for i in range(3))


def auto_cell(tray_w_m: float, rules: dict[str, Governing], points=(), origin=(0.0, 0.0, 0.0),
              max_cell: float = 0.25) -> float:
    """格距需 ≤ 最小非零淨距與橋架寬/2（避免窄通道誤判無路），且須整除所有起訖座標。
    只從 NICE_CELLS 挑；若連 0.05m 都對不齊，回傳 0.05（route_tray 會明確報錯）。"""
    cands = [max_cell, tray_w_m / 2]
    cands += [g.value / 1000 for k, g in rules.items() if k.endswith("_mm") and 0 < g.value]
    limit = min(cands)
    for c in NICE_CELLS:
        if c <= limit + 1e-9 and _aligned(points, origin, c):
            return c
    return NICE_CELLS[-1]


def try_route_tray(*a, **kw) -> RouteResult:
    """route_tray 的非例外包裝，給 UI/報告流程使用。"""
    try:
        r = route_tray(*a, **kw)
        return RouteResult(True, r, "", r.cell_m)
    except RoutingError as e:
        return RouteResult(False, None, str(e), code=e.code)


# 「標記被阻擋的格點與邊」發生在 A* 之前，max_expansions 擋不住它：單一涵蓋整個大房間的障礙物在 0.1 m 格距下
# 實測要數十秒。因此在建格後、標記前，以確定性的工作量估算（格次）設預算，超過就直接拒絕（不用牆鐘）。
MAX_OBSTACLE_WORK = 3_000_000


def grid_size(room: Box, cell: float) -> tuple[int, int, int]:
    """網格各軸格數 floor(L/cell)+1。router 與 webui 共用同一個公式，避免「UI 放行、管線拒絕」的漂移。"""
    return tuple(int(math.floor((room.hi[i] - room.lo[i]) / cell)) + 1 for i in range(3))


def grid_cells(room: Box, cell: float) -> int:
    n = grid_size(room, cell)
    return n[0] * n[1] * n[2]


def _ranges(inf: Box, origin, cell: float, n) -> list:
    out = []
    for i in range(3):
        a = max(0, int(math.floor((inf.lo[i] - origin[i]) / cell)))
        b = min(n[i] - 1, int(math.ceil((inf.hi[i] - origin[i]) / cell)))
        out.append(range(a, b + 1))
    return out


def obstacle_work(inf: Box, origin, cell: float, n) -> int:
    """單一（已膨脹）障礙物的標記工作量 = 格點迴圈次數 + 三個軸向的邊檢查迴圈次數（與標記迴圈同形，O(1) 計算）。"""
    rng = _ranges(inf, origin, cell, n)
    nodes = len(rng[0]) * len(rng[1]) * len(rng[2])
    edges = 0
    for ax in range(3):
        oth = [k for k in range(3) if k != ax]
        e0 = max(0, int(math.floor((inf.lo[ax] - origin[ax]) / cell)) - 1)
        e1 = min(n[ax] - 2, int(math.ceil((inf.hi[ax] - origin[ax]) / cell)))
        edges += max(0, e1 - e0 + 1) * len(rng[oth[0]]) * len(rng[oth[1]])
    return nodes + edges


def estimate_obstacle_work(room: Box, obstacles: list, tray_w_m: float, tray_h_m: float, tray_type: str,
                           rules: dict, cell: float) -> int:
    """route_tray 在標記阻擋格前會做的同一個估算（webui 用它在建立工作前先 400）。"""
    half = max(tray_w_m, tray_h_m) / 2
    n = grid_size(room, cell)
    return sum(obstacle_work(ob.box.inflate(clearance_m(ob.kind, tray_type, rules) + half), room.lo, cell, n)
               for ob in obstacles)


def route_tray(room: Box, start: Vec, ends: list[Vec], obstacles: list[Obstacle],
               tray_w_m: float, tray_h_m: float, tray_type: str,
               rules: dict[str, Governing], cell: float | None = None,
               bend_penalty: float = 4.0, vertical_penalty: float = 1.3,
               max_expansions: int = 300_000, max_cells: int = 3_000_000,
               max_obstacle_work: int = MAX_OBSTACLE_WORK) -> Route:
    origin = room.lo
    if cell is None:
        cell = auto_cell(tray_w_m, rules, [start, *ends], origin)
    if not _aligned([start, *ends], origin, cell):
        raise RoutingError(
            f"起訖座標未對齊格距 {cell} m（相對房間原點需為其整數倍）；請調整座標或指定可整除的格距",
            "misaligned")
    half = max(tray_w_m, tray_h_m) / 2
    sc = rules.get("structure_clear_mm")
    hr = rules.get("headroom_mm")
    wall = (sc.value if sc else 0.0) / 1000.0 + half
    top = max(sc.value if sc else 0.0, hr.value if hr else 0.0) / 1000.0 + half   # 天花：結構淨距與上方維護淨空取大
    n = grid_size(room, cell)
    total = n[0] * n[1] * n[2]
    if total > max_cells:    # 確定性防護：建格前即報錯，不必跑 A*
        raise RoutingError(f"格點總數 {total:,} 超過上限 {max_cells:,}，請放大格距或縮小場景", "grid_too_large")
    infl = [ob.box.inflate(clearance_m(ob.kind, tray_type, rules) + half) for ob in obstacles]
    work = sum(obstacle_work(inf, origin, cell, n) for inf in infl)
    if work > max_obstacle_work:     # 確定性預算：在標記迴圈（可能跑數十秒）之前就拒絕
        raise RoutingError(f"障礙物範圍過大或過多（估算工作量 {work:,} 超過上限 {max_obstacle_work:,}），"
                           f"請縮小障礙物、減少數量或放大格距", "obstacle_work_limit")

    def to_cell(p: Vec):
        return tuple(int(round((p[i] - origin[i]) / cell)) for i in range(3))

    def to_pos(c) -> Vec:
        return tuple(round(origin[i] + c[i] * cell, 6) for i in range(3))

    # 可行範圍（中心線需離牆/結構一定距離）
    def in_bounds(c) -> bool:
        p = to_pos(c)
        hi = (room.hi[0] - wall, room.hi[1] - wall, room.hi[2] - top)
        return all(room.lo[i] + wall - 1e-9 <= p[i] <= hi[i] + 1e-9 for i in range(3))

    blocked: set = set()
    blocked_edges: set = set()
    for inf in infl:
        rng = _ranges(inf, origin, cell, n)
        for x in rng[0]:
            for y in rng[1]:
                for z in rng[2]:
                    p = to_pos((x, y, z))
                    if all(inf.lo[k] < p[k] < inf.hi[k] for k in range(3)):
                        blocked.add((x, y, z))
        # 邊檢查：相鄰格點間線段穿過膨脹盒（薄障礙，兩端格點皆在盒外）也必須阻擋
        for ax in range(3):
            oth = [k for k in range(3) if k != ax]
            e0 = max(0, int(math.floor((inf.lo[ax] - origin[ax]) / cell)) - 1)
            e1 = min(n[ax] - 2, int(math.ceil((inf.hi[ax] - origin[ax]) / cell)))
            for i in range(e0, e1 + 1):
                lo_p = origin[ax] + i * cell
                if not (lo_p < inf.hi[ax] - 1e-9 and lo_p + cell > inf.lo[ax] + 1e-9):
                    continue
                for u in rng[oth[0]]:
                    if not inf.lo[oth[0]] < origin[oth[0]] + u * cell < inf.hi[oth[0]]:
                        continue
                    for v in rng[oth[1]]:
                        if not inf.lo[oth[1]] < origin[oth[1]] + v * cell < inf.hi[oth[1]]:
                            continue
                        c = [0, 0, 0]
                        c[ax], c[oth[0]], c[oth[1]] = i, u, v
                        blocked_edges.add((tuple(c), ax))

    def free(c) -> bool:
        return all(0 <= c[i] < n[i] for i in range(3)) and in_bounds(c) and c not in blocked

    def edge_free(c, nc) -> bool:
        ax = next(k for k in range(3) if c[k] != nc[k])
        lo = c if c[ax] < nc[ax] else nc
        return (lo, ax) not in blocked_edges

    s_cell = to_cell(start)
    if not free(s_cell):
        raise RoutingError(f"起點 {start} 落在障礙/淨距範圍或牆邊禁區內", "endpoint_blocked")
    network: set = {s_cell}
    waypoints: list[list[Vec]] = []
    junctions: list[tuple[Vec, int]] = []

    # 由遠到近逐一接入網路：先成形長幹線，近端分支再接入，共幹較省
    for end in sorted(ends, key=lambda e: (-dist(start, e), e)):
        t_cell = to_cell(end)
        if not free(t_cell):
            raise RoutingError(f"終點 {end} 落在障礙/淨距範圍或牆邊禁區內", "endpoint_blocked")
        path = _astar(network, t_cell, free, edge_free, bend_penalty, vertical_penalty,
                      max_expansions)
        if path is None:
            raise RoutingError(f"找不到通往終點 {end} 的可行路徑（淨距/障礙過嚴？）", "no_route")
        if path[0] != s_cell and len(network) > 1:
            junctions.append((to_pos(path[0]), len(waypoints)))
        network.update(path)
        waypoints.append(_simplify([to_pos(c) for c in path]))

    segs: list[tuple[Vec, Vec]] = []
    bends: list[Vec] = [j for j, _ in junctions]
    for wp in waypoints:
        for a, b in zip(wp, wp[1:]):
            segs.append((a, b))
        bends.extend(wp[1:-1])
    length = sum(dist(a, b) for a, b in segs)
    return Route(waypoints, segs, _uniq(bends), length, cell_m=cell, junctions=junctions)


def _astar(sources: set, goal, free, edge_free, bend_penalty, vertical_penalty, max_expansions):
    def h(c):
        return abs(c[0] - goal[0]) + abs(c[1] - goal[1]) + abs(c[2] - goal[2])

    open_: list = []
    best: dict = {}
    parent: dict = {}
    for s in sorted(sources):
        st = (s, -1)
        best[st] = 0.0
        heapq.heappush(open_, (h(s), 0.0, s, -1))
    expanded = 0
    while open_:
        _, g, c, d = heapq.heappop(open_)
        if g > best.get((c, d), 1e18):
            continue
        expanded += 1
        if expanded > max_expansions:
            raise RoutingError("超過搜尋上限，請放大格距或縮小場景", "search_limit")
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
            if not free(nc) or not edge_free(c, nc):
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
    """轉彎處與分支接入點相鄰邊長若小於配件半徑，回報 (節點, 較短邊長)。"""
    bad = []
    for wp in route.waypoints:
        for i in range(1, len(wp) - 1):
            leg = min(dist(wp[i - 1], wp[i]), dist(wp[i], wp[i + 1]))
            if leg + 1e-9 < radius_m:
                bad.append((wp[i], leg))
    for j, bi in route.junctions:
        legs = [dist(route.waypoints[bi][0], route.waypoints[bi][1])]
        for k, wp in enumerate(route.waypoints):
            if k == bi:
                continue
            for a, b in zip(wp, wp[1:]):
                if _on_segment(j, a, b) and j != a and j != b:
                    legs += [dist(j, a), dist(j, b)]
        leg = min(legs)
        if leg + 1e-9 < radius_m:
            bad.append((j, leg))
    return bad


def _on_segment(p: Vec, a: Vec, b: Vec) -> bool:
    """p 是否落在軸向線段 a-b 上。"""
    axes = [i for i in range(3) if abs(a[i] - b[i]) > 1e-9]
    if len(axes) != 1:
        return False
    ax = axes[0]
    on_line = all(abs(p[i] - a[i]) <= 1e-9 for i in range(3) if i != ax)
    return on_line and min(a[ax], b[ax]) - 1e-9 <= p[ax] <= max(a[ax], b[ax]) + 1e-9
