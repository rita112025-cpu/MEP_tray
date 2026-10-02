"""3D 軸對齊盒（AABB）基本運算。單位：公尺。"""
from __future__ import annotations

from dataclasses import dataclass

Vec = tuple[float, float, float]


@dataclass(frozen=True)
class Box:
    lo: Vec
    hi: Vec

    def inflate(self, d: float) -> "Box":
        return Box(tuple(v - d for v in self.lo), tuple(v + d for v in self.hi))

    def intersects(self, o: "Box") -> bool:
        return all(self.lo[i] < o.hi[i] and o.lo[i] < self.hi[i] for i in range(3))

    def gap(self, o: "Box") -> float:
        """兩盒最短間距；相交回傳 0。"""
        s = 0.0
        for i in range(3):
            d = max(self.lo[i] - o.hi[i], o.lo[i] - self.hi[i], 0.0)
            s += d * d
        return s ** 0.5

    def center(self) -> Vec:
        return tuple((a + b) / 2 for a, b in zip(self.lo, self.hi))


def segment_box(p: Vec, q: Vec, width_m: float, height_m: float) -> Box:
    """正交橋架段的外包盒（p→q 為中心線；寬度沿水平垂直方向，高度沿 Z）。"""
    hw = width_m / 2
    hh = height_m / 2
    ext = [hw, hw, hh]
    if p[2] != q[2]:           # 垂直段：截面為 width × width
        ext = [hw, hw, 0.0]
    elif p[0] != q[0]:         # 沿 X：寬度在 Y
        ext = [0.0, hw, hh]
    elif p[1] != q[1]:         # 沿 Y：寬度在 X
        ext = [hw, 0.0, hh]
    lo = tuple(min(p[i], q[i]) - ext[i] for i in range(3))
    hi = tuple(max(p[i], q[i]) + ext[i] for i in range(3))
    return Box(lo, hi)


def dist(a: Vec, b: Vec) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def nearest_points(a: Box, b: Box) -> tuple[Vec, Vec]:
    """兩盒間最近的一對點（相交時為重疊區中心，兩點相同）。"""
    pa, pb = [], []
    for i in range(3):
        lo, hi = max(a.lo[i], b.lo[i]), min(a.hi[i], b.hi[i])
        if lo <= hi:
            pa.append((lo + hi) / 2)
            pb.append((lo + hi) / 2)
        elif a.hi[i] < b.lo[i]:
            pa.append(a.hi[i])
            pb.append(b.lo[i])
        else:
            pa.append(a.lo[i])
            pb.append(b.hi[i])
    return tuple(pa), tuple(pb)
