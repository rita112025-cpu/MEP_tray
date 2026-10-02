"""碰撞偵測與規範驗算：輸出位置 + 規範依據 + 修正建議。

幾何事實（實體重疊）一律為「衝突」，不受規範 verified 影響；
規範淨距/間距/填充率等走 rules.evaluate 三態（符合/不符合/規範值未驗證）。
橋架實體用 geometry.segment_box 精確外包盒；與 router 的近似矛盾時以此為準。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .geometry import Box, Vec, nearest_points, segment_box
from .model import Inputs
from .router import Route, check_bend_legs, rule_key
from .rules import FAIL, Governing, evaluate, fill_ratio, recommend_width

CLASH = "衝突"
AXIS = "XYZ"


@dataclass
class Finding:
    kind: str                 # clash|clearance|structure|headroom|hanger_span|fill|bend
    status: str               # 衝突 | 不符合 | 規範值未驗證 | 符合
    indicative: str           # 與規範值直接比較之結果（衝突時為「衝突」）
    subject: str              # 對象（障礙物名稱/橋架段/房間）
    location: Vec             # 問題位置 (m)
    actual: float
    required: float
    unit: str
    code: str                 # 決定要求的規範代號（衝突為空）
    clause: str
    verified: bool
    suggestion: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["location"] = [round(v, 3) for v in self.location]
        return d


@dataclass
class ClashReport:
    findings: list = field(default_factory=list)   # 需處理：衝突 + indicative=不符合
    checks: list = field(default_factory=list)     # 全部已驗算項（含符合/未驗證），供報告合規表
    design: dict = field(default_factory=dict)     # 採用的設計值（配件半徑…）與揭露事項

    @property
    def has_clash(self) -> bool:
        return any(f.kind == "clash" for f in self.findings)

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings],
                "checks": [f.to_dict() for f in self.checks], "design": self.design}


def _mk(kind, subject, loc, chk, suggestion, unit_mul=1.0) -> Finding:
    return Finding(kind, chk.status, chk.indicative, subject, loc, chk.actual, chk.required,
                   chk.unit, chk.code, chk.clause, chk.verified, suggestion)


def _push(rep: ClashReport, f: Finding) -> None:
    rep.checks.append(f)
    if f.indicative == FAIL or f.kind == "clash":
        rep.findings.append(f)


def _move_hint(seg_box: Box, ob: Box, need_mm: float) -> str:
    """建議位移：沿「兩盒中心距相對於半尺寸和」最大的軸，遠離障礙物。"""
    ca, cb = seg_box.center(), ob.center()
    best, ax = -1.0, 0
    for i in range(3):
        half = (seg_box.hi[i] - seg_box.lo[i] + ob.hi[i] - ob.lo[i]) / 2
        r = abs(ca[i] - cb[i]) / half if half else 0
        if r > best:
            best, ax = r, i
    sign = "+" if ca[ax] >= cb[ax] else "-"
    return f"沿 {sign}{AXIS[ax]} 方向移開至少 {need_mm:.0f} mm"


def check_route(inp: Inputs, route: Route, gov: dict[str, Governing]) -> ClashReport:
    rep = ClashReport()
    w, h = inp.tray_w_mm / 1000, inp.tray_h_mm / 1000
    boxes = [(a, b, segment_box(a, b, w, h)) for a, b in route.segments]

    # 1) 障礙物：衝突 / 淨距（每個障礙物取最小間距的一段，避免重複）
    for ob in sorted(inp.obstacle_objs(), key=lambda o: o.name):
        a, b, sb = min(boxes, key=lambda t: (t[2].gap(ob.box), t[0], t[1]))
        gap = sb.gap(ob.box)
        key = rule_key(ob.kind, inp.tray_type, gov)
        g = gov.get(key)
        pa, pb = nearest_points(sb, ob.box)
        loc = tuple((x + y) / 2 for x, y in zip(pa, pb))
        seg_txt = f"橋架段 {a}→{b}"
        if gap <= 1e-9:
            req = (g.value if g else 0.0)
            pen = min(min(sb.hi[i], ob.box.hi[i]) - max(sb.lo[i], ob.box.lo[i]) for i in range(3)) * 1000
            f = Finding("clash", CLASH, CLASH, f"{ob.name}（{ob.kind}）", loc, 0.0, req,
                        "mm", g.code if g else "", g.clause if g else "", g.verified if g else True,
                        f"{seg_txt} 與 {ob.name} 實體重疊；{_move_hint(sb, ob.box, pen + req)}，"
                        f"或重新佈設路徑繞開")
            _push(rep, f)
        elif g:
            chk = evaluate(g, gap * 1000)
            hint = ""
            if chk.indicative == FAIL:
                hint = f"{seg_txt} 與 {ob.name} 淨距不足；{_move_hint(sb, ob.box, g.value - gap * 1000)}"
            _push(rep, _mk("clearance", f"{ob.name}（{ob.kind}）", loc, chk, hint))

    # 2) 房間：天花上方維護淨空、牆/結構淨距（取各段最小值）
    room = inp.room_box()
    if boxes:
        top_gap = min(room.hi[2] - sb.hi[2] for _, _, sb in boxes) * 1000
        side_gap = min(min(sb.lo[0] - room.lo[0], room.hi[0] - sb.hi[0], sb.lo[1] - room.lo[1],
                           room.hi[1] - sb.hi[1], sb.lo[2] - room.lo[2]) for _, _, sb in boxes) * 1000
        worst_top = min(boxes, key=lambda t: room.hi[2] - t[2].hi[2])
        loc = (*worst_top[2].center()[:2], worst_top[2].hi[2])
        if "headroom_mm" in gov:
            chk = evaluate(gov["headroom_mm"], top_gap)
            _push(rep, _mk("headroom", "天花/頂板", loc, chk,
                           f"橋架頂距天花僅 {top_gap:.0f} mm；下移至少 {gov['headroom_mm'].value - top_gap:.0f} mm"
                           if chk.indicative == FAIL else ""))
        if "structure_clear_mm" in gov:
            chk = evaluate(gov["structure_clear_mm"], min(side_gap, top_gap))
            _push(rep, _mk("structure", "牆/地板/結構", loc, chk,
                           "橋架離牆/結構過近，請內縮路徑" if chk.indicative == FAIL else ""))

    # 3) 吊架間距（以各段相鄰吊架之最大間距）
    if "span_max_m" in gov:
        worst, wloc = 0.0, route.segments[0][0] if route.segments else (0, 0, 0)
        for a, b in route.segments:
            ax = next((i for i in range(3) if abs(a[i] - b[i]) > 1e-9), None)
            if ax is None:
                continue
            hs = sorted((p for p in route.hangers
                         if all(abs(p[i] - a[i]) < 1e-6 for i in range(3) if i != ax)
                         and min(a[ax], b[ax]) - 1e-6 <= p[ax] <= max(a[ax], b[ax]) + 1e-6),
                        key=lambda p: p[ax])
            for p1, p2 in zip(hs, hs[1:]):
                if p2[ax] - p1[ax] > worst:
                    worst, wloc = p2[ax] - p1[ax], tuple((x + y) / 2 for x, y in zip(p1, p2))
        if not route.hangers:
            rep.design["note_hangers"] = "尚未配置吊架，吊架間距未驗算"
        else:
            chk = evaluate(gov["span_max_m"], worst)
            _push(rep, _mk("hanger_span", "吊架", wloc, chk,
                           f"最大吊架間距 {worst:.2f} m；增設吊架使間距 ≤ {gov['span_max_m'].value:.2f} m"
                           if chk.indicative == FAIL else ""))

    # 4) 填充率
    if "fill_max" in gov:
        cables = inp.effective_cables()
        ratio = fill_ratio(cables, inp.tray_w_mm, inp.tray_h_mm)
        chk = evaluate(gov["fill_max"], ratio)
        rec = recommend_width(cables, inp.tray_h_mm, gov["fill_max"].value)
        tip = ""
        if chk.indicative == FAIL:
            tip = (f"填充率 {ratio:.1%} 超過上限；建議橋架寬改為 {rec} mm" if rec
                   else "填充率超過上限，且標準寬度皆不足，請分設橋架或減少電纜")
        _push(rep, _mk("fill", "電纜填充", route.segments[0][0] if route.segments else (0, 0, 0), chk, tip))
        if inp.cables_defaulted:
            rep.design["cables_defaulted"] = "未輸入電纜資料，填充率以預設電纜 Ø20mm×10 條計算"

    # 5) 轉彎／分支邊長 vs 配件半徑（取規範值與電纜彎曲半徑倍數之大者）
    fit = gov["fitting_radius_mm"].value if "fitting_radius_mm" in gov else 0.0
    od = max(c["od_mm"] for c in inp.effective_cables())
    cab = gov["bend_radius_factor"].value * od if "bend_radius_factor" in gov else 0.0
    radius = max(fit, cab)
    rep.design["fitting_radius_mm_used"] = radius
    if radius and "fitting_radius_mm" in gov:
        g = gov["fitting_radius_mm"]
        need = Governing(**{**g.__dict__, "value": radius})
        for p, leg_m in check_bend_legs(route, radius / 1000):
            chk = evaluate(need, leg_m * 1000)
            _push(rep, _mk("bend", "轉彎/分支", p, chk,
                           f"轉彎處邊長 {leg_m * 1000:.0f} mm 小於配件半徑 {radius:.0f} mm；"
                           f"延長相鄰直段或減少轉彎"))
    rep.findings.sort(key=lambda f: (f.kind != "clash", f.kind, f.subject, f.location))
    return rep
