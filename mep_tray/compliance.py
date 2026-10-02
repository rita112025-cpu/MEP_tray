"""規範驗算（非幾何碰撞）：吊架間距、填充率、轉彎/分支邊長。

與 clash.py 分開：clash 只管幾何；本模組管「設計值 vs 規範值」。
吊架檢查針對實際 route.hangers（可為使用者手動調整後的結果，不假設由 place_hangers 產生）。
"""
from __future__ import annotations

from .findings import Report, from_check
from .model import Inputs
from .router import Route, check_bend_legs
from .rules import Governing, evaluate, fill_ratio, recommend_width


def max_hanger_spacing(route: Route) -> tuple[float, tuple]:
    """各段相鄰吊架最大間距；整段無吊架者以段長計（不會被判符合）。"""
    worst, wloc = 0.0, route.segments[0][0] if route.segments else (0.0, 0.0, 0.0)
    for a, b in route.segments:
        ax = next((i for i in range(3) if abs(a[i] - b[i]) > 1e-9), None)
        if ax is None:
            continue
        hs = sorted((p for p in route.hangers
                     if all(abs(p[i] - a[i]) < 1e-6 for i in range(3) if i != ax)
                     and min(a[ax], b[ax]) - 1e-6 <= p[ax] <= max(a[ax], b[ax]) + 1e-6),
                    key=lambda p: p[ax])
        pairs = list(zip(hs, hs[1:])) if hs else [(a, b)]
        for p1, p2 in pairs:
            d = abs(p2[ax] - p1[ax])
            if d > worst:
                worst, wloc = d, tuple((x + y) / 2 for x, y in zip(p1, p2))
    return worst, wloc


def check_compliance(inp: Inputs, route: Route, gov: dict[str, Governing]) -> Report:
    rep = Report()
    loc0 = route.segments[0][0] if route.segments else (0.0, 0.0, 0.0)

    if "span_max_m" in gov:
        if not route.hangers:
            rep.design["note_hangers"] = "尚未配置吊架，吊架間距未驗算"
        else:
            worst, wloc = max_hanger_spacing(route)
            chk = evaluate(gov["span_max_m"], worst)
            tip = (f"最大吊架間距 {worst:.2f} m；增設吊架使間距 ≤ {gov['span_max_m'].value:.2f} m"
                   if chk.indicative == "FAIL" else "")
            rep.push(from_check("hanger_span", "吊架", wloc, chk, tip))

    cables = inp.effective_cables()
    if "fill_max" in gov:
        ratio = fill_ratio(cables, inp.tray_w_mm, inp.tray_h_mm)
        chk = evaluate(gov["fill_max"], ratio)
        tip = ""
        if chk.indicative == "FAIL":
            rec = recommend_width(cables, inp.tray_h_mm, gov["fill_max"].value)
            tip = (f"填充率 {ratio:.1%} 超過上限；建議橋架寬改為 {rec} mm" if rec
                   else "填充率超過上限，且標準寬度皆不足，請分設橋架或減少電纜")
        rep.push(from_check("fill", "電纜填充", loc0, chk, tip))
    if inp.cables_defaulted:
        rep.design["cables_defaulted"] = "未輸入電纜資料，填充率以預設電纜 Ø20mm×10 條計算"

    fit = gov["fitting_radius_mm"].value if "fitting_radius_mm" in gov else 0.0
    od = max(c["od_mm"] for c in cables)
    cab = gov["bend_radius_factor"].value * od if "bend_radius_factor" in gov else 0.0
    radius = max(fit, cab)
    rep.design["fitting_radius_mm_used"] = radius
    if radius and "fitting_radius_mm" in gov:
        need = Governing(**{**gov["fitting_radius_mm"].__dict__, "value": radius})
        for p, leg_m in check_bend_legs(route, radius / 1000):
            chk = evaluate(need, leg_m * 1000)
            rep.push(from_check("bend", "轉彎/分支", p, chk,
                                f"轉彎處邊長 {leg_m * 1000:.0f} mm 小於配件半徑 {radius:.0f} mm；"
                                f"延長相鄰直段或減少轉彎"))
    rep.findings.sort(key=lambda f: (f.kind, f.subject, f.location))
    return rep
