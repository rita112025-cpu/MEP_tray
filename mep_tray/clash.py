"""幾何類檢查：實體衝突、障礙淨距、房間牆/地板/天花淨距、天花上方維護淨空。

- 實體重疊一律為 CLASH，不受規範 verified 影響（幾何事實）。
- 淨距走 rules.evaluate 三態；規範值 verified=false 時 status=UNVERIFIED、indicative 仍給出。
- 橋架實體用 geometry.segment_box 精確外包盒（router 用較保守的近似，故 router 成功的路徑
  不應被本模組報 clash/clearance；兩者矛盾即為 bug，見 tests）。
- 只比對「路徑 vs 輸入障礙/房間」；同一路徑內相鄰段、分支接點不互比（共用端點必相交）。
- 位置：相交取重疊區中心；不相交取最近點對的中點。
- 修正建議為單軸最小位移（fix: axis/sign/move_mm），移動橋架後該 finding 應消失。
"""
from __future__ import annotations

import math

from .findings import Finding, Report, from_check
from .geometry import Box, nearest_points, segment_box
from .model import Inputs
from .router import Route, rule_key
from .rules import CLASH, Governing, evaluate

AXIS = "XYZ"
EPS = 1e-9


def min_axis_shift(seg: Box, ob: Box, req_m: float) -> tuple[int, int, float]:
    """回傳 (軸, 方向±1, 位移 m)：沿單一軸移動橋架，使其與障礙物間距 ≥ req_m 的最小位移。"""
    best = None
    for ax in range(3):
        rest = math.sqrt(sum(max(seg.lo[j] - ob.hi[j], ob.lo[j] - seg.hi[j], 0.0) ** 2
                             for j in range(3) if j != ax))
        target = math.sqrt(req_m ** 2 - rest ** 2) if req_m > rest else 0.0
        for sign in (1, -1):
            sep = (seg.lo[ax] - ob.hi[ax]) if sign > 0 else (ob.lo[ax] - seg.hi[ax])
            cand = (max(target - sep, 0.0), ax, sign)
            if best is None or cand < best:
                best = cand
    d, ax, sign = best
    return ax, sign, d


def _fix_text(name: str, ax: int, sign: int, d_m: float) -> tuple[str, dict]:
    fix = {"axis": AXIS[ax], "sign": "+" if sign > 0 else "-", "move_mm": d_m * 1000}
    return (f"將橋架沿 {fix['sign']}{fix['axis']} 方向移動至少 {fix['move_mm']:.0f} mm（遠離 {name}），"
            f"或重新佈設路徑", fix)


def check_route(inp: Inputs, route: Route, gov: dict[str, Governing]) -> Report:
    rep = Report()
    w, h = inp.tray_w_mm / 1000, inp.tray_h_mm / 1000
    boxes = [(a, b, segment_box(a, b, w, h)) for a, b in route.segments]
    if not boxes:
        rep.design["note_empty_route"] = "路徑無任何線段（起點等於終點？），未做障礙物/房間檢查"
        return rep

    # 1) 障礙物：衝突 / 淨距（每個障礙物取最小間距的一段，避免重複）
    for ob in sorted(inp.obstacle_objs(), key=lambda o: o.name):
        a, b, sb = min(boxes, key=lambda t: (t[2].gap(ob.box), t[0], t[1]))
        gap = sb.gap(ob.box)
        g = gov.get(rule_key(ob.kind, inp.tray_type, gov))
        pa, pb = nearest_points(sb, ob.box)
        loc = tuple((x + y) / 2 for x, y in zip(pa, pb))
        subj = f"{ob.name}（{ob.kind}）"
        req_m = (g.value if g else 0.0) / 1000
        if gap <= EPS:
            txt, fix = _fix_text(ob.name, *min_axis_shift(sb, ob.box, req_m))
            rep.push(Finding("clash", CLASH, CLASH, subj, loc, 0.0, req_m * 1000, "mm",
                             g.code if g else "", g.clause if g else "", g.verified if g else True,
                             f"橋架段 {a}→{b} 與 {ob.name} 實體重疊；{txt}", fix))
        elif g:
            chk = evaluate(g, gap * 1000)
            txt, fix = "", None
            if chk.indicative == "FAIL":
                txt, fix = _fix_text(ob.name, *min_axis_shift(sb, ob.box, req_m))
                txt = f"橋架段 {a}→{b} 與 {ob.name} 淨距不足；{txt}"
            rep.push(from_check("clearance", subj, loc, chk, txt, fix))

    # 2) 房間六面：天花上方維護淨空；牆/地板/天花之結構淨距（與 router 同一規則鍵）
    room = inp.room_box()
    faces = []   # (名稱, 最小間距 m, 該面上的位置, 軸, 方向)
    for ax in range(3):
        for sign in (-1, 1):
            name = {(2, 1): "天花/頂板", (2, -1): "地板"}.get((ax, sign), f"{AXIS[ax]}{'+' if sign > 0 else '-'} 面")
            gp, sb = min((((sb.lo[ax] - room.lo[ax]) if sign < 0 else (room.hi[ax] - sb.hi[ax]), sb)
                          for _, _, sb in boxes), key=lambda t: t[0])
            c = list(sb.center())
            c[ax] = room.lo[ax] if sign < 0 else room.hi[ax]
            faces.append((name, gp, tuple(c), ax, sign))

    ceiling = next(f for f in faces if f[0] == "天花/頂板")
    if "headroom_mm" in gov:
        g = gov["headroom_mm"]
        chk = evaluate(g, ceiling[1] * 1000)
        txt, fix = "", None
        if chk.indicative == "FAIL":
            d = g.value - ceiling[1] * 1000
            txt = f"橋架頂距天花僅 {ceiling[1] * 1000:.0f} mm；將橋架沿 -Z 下移至少 {d:.0f} mm"
            fix = {"axis": "Z", "sign": "-", "move_mm": d}
        rep.push(from_check("headroom", "天花/頂板", ceiling[2], chk, txt, fix))
    if "structure_clear_mm" in gov:
        g = gov["structure_clear_mm"]
        name, gp, loc, ax, sign = min(faces, key=lambda f: f[1])
        chk = evaluate(g, gp * 1000)
        txt, fix = "", None
        if chk.indicative == "FAIL":
            d = g.value - gp * 1000
            fix = {"axis": AXIS[ax], "sign": "+" if sign < 0 else "-", "move_mm": d}
            txt = f"橋架離 {name} 僅 {gp * 1000:.0f} mm；將橋架沿 {fix['sign']}{fix['axis']} 移開至少 {d:.0f} mm"
        rep.push(from_check("structure", name, loc, chk, txt, fix))

    rep.findings.sort(key=lambda f: (f.kind != "clash", f.kind, f.subject, f.location))
    return rep
