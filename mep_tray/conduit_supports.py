"""配管（Conduit）吊架核心（純邏輯，單位 mm）——第一階段：兩支並排配管共用一支吊架。

與 C# MepTray.Core.ConduitHangers 行為一致（tests/test_conduit_supports.py 產生契約案例，由 MepTray.Core.SelfTest 比對）。
- 只有「兩支」配管、且同時滿足下列條件才配成一對（共用一支吊架）：
  水平直段、管徑（Revit「Diameter(Trade Size)」）在設定的允許清單內、平行（≤ pairing.angle_tolerance_deg）、
  同高程（中心高差 ≤ pairing.elevation_tolerance_mm）、外徑相同、沿管方向有重疊、並排不重疊（中心距 ≥ 外徑），
  且兩管外緣總寬（最大中心距 + 外徑）≤ 橫擔長度 cross_member.value_mm。
- 吊架線 = 兩管中心線的中線，長度 = 重疊段；不重疊的尾段列為待確認，不放置。
- 任一支配管可與多支配成對 → 不猜測，全部列為待確認（ambiguous_pair）；單管／不符條件者列為待確認（unpaired），不放置。
- 間距、起終內縮、固定間距與餘數待確認、去重、族與校正全部沿用橋架吊架設定（base_config），本模組不改變其行為。
- 本模組不含任何規範數值預設；設定缺漏即拒絕並指出欄位。
"""
from __future__ import annotations

import math

from mep_tray import supports as S

GEOM_TOL_MM = S.GEOM_TOL_MM          # 幾何判定容差（水平/零長/管徑比對/重疊）；非規範值
SUPPORTED_OBJECTS = ("conduit_pair",)
CROSS_BASES = ("parameter", "measured_length")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _r3(v: float) -> float:
    return round(float(v), 3) + 0.0


def _p3(p) -> list[float]:
    return [_r3(v) for v in p]


# ---------------------------------------------------------------- 設定驗證
def validate_conduit_config(cfg) -> list[str]:
    """配管吊架專用設定（conduit_hanger_config.json）。回傳錯誤（每筆以「欄位：原因」開頭）；空 = 合格。"""
    if not isinstance(cfg, dict):
        return ["config：必須是 JSON 物件"]
    e: list[str] = []
    if cfg.get("units") != "mm":
        e.append("units：必須為 \"mm\"")
    if cfg.get("supported_object") not in SUPPORTED_OBJECTS:
        e.append("supported_object：需為 conduit_pair")
    b = cfg.get("base_config")
    if not isinstance(b, str) or not b.strip():
        e.append("base_config：缺少（橋架吊架設定檔路徑；族、校正、間距、去重沿用該檔）")
    n = cfg.get("conduits_per_hanger")
    if n is None:
        e.append("conduits_per_hanger：缺少（v1 只支援 2）")
    elif not (isinstance(n, int) and not isinstance(n, bool) and n == 2):
        e.append("conduits_per_hanger：v1 只支援 2")
    ts = cfg.get("allowed_trade_sizes_mm")
    if ts is None:
        e.append("allowed_trade_sizes_mm：缺少（允許的配管管徑 Diameter(Trade Size)，工具不提供預設）")
    elif not isinstance(ts, list) or not ts or not all(_num(v) and v > 0 for v in ts):
        e.append("allowed_trade_sizes_mm：需為非空陣列，元素為 > 0 的有限數")
    cm = cfg.get("cross_member")
    if not isinstance(cm, dict):
        e.append("cross_member：缺少（{\"value_mm\": 橫擔長度, \"basis\": parameter|measured_length}）")
    else:
        v = cm.get("value_mm")
        if v is None:
            e.append("cross_member.value_mm：缺少")
        elif not _num(v) or v <= 0:
            e.append("cross_member.value_mm：需為 > 0 的有限數")
        if cm.get("basis") not in CROSS_BASES:
            e.append("cross_member.basis：需為 parameter（值直接寫入族的橫擔長度參數）或 measured_length（實測橫擔長度，經校正換算）")
    pr = cfg.get("pairing")
    if not isinstance(pr, dict):
        e.append("pairing：缺少（angle_tolerance_deg、elevation_tolerance_mm）")
    else:
        a = pr.get("angle_tolerance_deg")
        if not _num(a) or a < 0 or a >= 90:
            e.append("pairing.angle_tolerance_deg：需為 0 ≤ 值 < 90")
        t = pr.get("elevation_tolerance_mm")
        if not _num(t) or t <= 0:
            e.append("pairing.elevation_tolerance_mm：需為 > 0 的有限數")
    return e


def error_fields(errors: list[str]) -> list[str]:
    return S.error_fields(errors)


# ---------------------------------------------------------------- 配對
def _issue(level, code, cid, loc, detail):
    return {"level": level, "code": code, "piece_id": cid, "location_mm": _p3(loc), "detail": detail}


def _angle_deg(u, v) -> float:
    """兩方向（不分正反）的夾角，0..90。"""
    c = min(1.0, abs(u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(c))


def _check(a: dict, b: dict, ccfg: dict) -> dict:
    """a、b 是否可配對。回傳 {ok, code, detail, ...幾何}；依序檢查，第一個不符即回傳。"""
    pr = ccfg["pairing"]
    ang = _angle_deg(a["u"], b["u"])
    if ang > pr["angle_tolerance_deg"] + 1e-9:
        return {"ok": False, "code": "not_parallel", "detail": f"夾角 {ang:.2f}°"}
    dz = abs(a["z"] - b["z"])
    if dz > pr["elevation_tolerance_mm"] + 1e-9:
        return {"ok": False, "code": "elevation_mismatch", "detail": f"中心高差 {dz:.1f} mm"}
    if abs(a["od"] - b["od"]) > GEOM_TOL_MM:
        return {"ok": False, "code": "diameter_mismatch", "detail": f"外徑 {a['od']:.1f} / {b['od']:.1f} mm"}
    u, o = a["u"], a["start"]
    n = (-u[1], u[0])

    def proj(p):
        return (p[0] - o[0]) * u[0] + (p[1] - o[1]) * u[1]

    tb0, tb1 = proj(b["start"]), proj(b["end"])
    lo, hi = max(0.0, min(tb0, tb1)), min(a["len"], max(tb0, tb1))
    if hi - lo <= GEOM_TOL_MM:
        return {"ok": False, "code": "no_overlap", "detail": "沿管方向沒有重疊段"}

    def lat(t):
        f = (t - tb0) / (tb1 - tb0)
        px = b["start"][0] + (b["end"][0] - b["start"][0]) * f
        py = b["start"][1] + (b["end"][1] - b["start"][1]) * f
        return (px - o[0]) * n[0] + (py - o[1]) * n[1]

    dlo, dhi = lat(lo), lat(hi)
    dmin, dmax = min(abs(dlo), abs(dhi)), max(abs(dlo), abs(dhi))
    od = a["od"]
    if dlo * dhi < 0 or dmin < od - GEOM_TOL_MM:
        return {"ok": False, "code": "too_close", "detail": f"中心距 {dmin:.1f} mm 小於外徑 {od:.1f} mm（重疊／非並排）"}
    width = dmax + od
    cm = float(ccfg["cross_member"]["value_mm"])
    if width > cm + GEOM_TOL_MM:
        return {"ok": False, "code": "exceeds_crossbar",
                "detail": f"兩管外緣總寬 {width:.1f} mm（最大中心距 {dmax:.1f} + 外徑 {od:.1f}）超過橫擔 {cm:.1f} mm"}
    return {"ok": True, "code": "", "detail": "", "lo": lo, "hi": hi, "dlo": dlo, "dhi": dhi,
            "tb0": tb0, "tb1": tb1, "dmin": dmin, "dmax": dmax, "width": width}


def pair_conduits(conduits: list[dict], ccfg: dict) -> dict:
    """conduits: [{id, start, end, trade_size_mm, outer_diameter_mm}]（Revit 內部座標 mm，依輸入順序）。
    回傳 {pairs: [...], issues: [...]}；issues 的 piece_id 為配管 id 或配對 id。"""
    issues: list[dict] = []
    ok: list[dict] = []
    allowed = [float(v) for v in ccfg["allowed_trade_sizes_mm"]]
    for c in conduits:
        st, en = c["start"], c["end"]
        d = [en[0] - st[0], en[1] - st[1], en[2] - st[2]]
        h = math.hypot(d[0], d[1])
        L = math.sqrt(h * h + d[2] * d[2])
        mid = [(st[i] + en[i]) / 2 for i in range(3)]
        if L <= GEOM_TOL_MM:
            issues.append(_issue("failed", "zero_length", c["id"], st, "長度為 0，未處理"))
            continue
        if abs(d[2]) > GEOM_TOL_MM:
            code = "vertical" if h <= GEOM_TOL_MM else "sloped"
            issues.append(_issue("unsupported", code, c["id"], mid,
                                 "垂直配管 v1 不支援" if code == "vertical" else f"斜配管 v1 不支援（高差 {abs(d[2]):.1f} mm）"))
            continue
        ts = c.get("trade_size_mm")
        if not _num(ts) or ts <= 0:
            issues.append(_issue("failed", "no_diameter", c["id"], mid, "讀不到管徑 Diameter(Trade Size)"))
            continue
        if not any(abs(ts - a) <= GEOM_TOL_MM for a in allowed):
            issues.append(_issue("pending", "diameter_not_allowed", c["id"], mid,
                                 f"管徑 {ts:.1f} mm 不在允許清單 {allowed}，未放置"))
            continue
        od = c.get("outer_diameter_mm")
        if not _num(od) or od <= 0:
            issues.append(_issue("info", "outer_diameter_fallback", c["id"], mid,
                                 f"讀不到外徑 Outside Diameter，外緣檢查改用管徑 {ts:.1f} mm"))
            od = ts
        ok.append({"id": c["id"], "start": list(map(float, st)), "end": list(map(float, en)), "len": h,
                   "u": (d[0] / h, d[1] / h), "z": float(st[2]), "od": float(od), "ts": float(ts), "mid": mid})

    cands = []
    fails = []
    reasons: dict[str, list[str]] = {c["id"]: [] for c in ok}
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            a, b = ok[i], ok[j]
            r = _check(a, b, ccfg)
            if r["ok"]:
                cands.append((i, j, r))
                continue
            reasons[a["id"]].append(f"{b['id']}:{r['code']}")
            reasons[b["id"]].append(f"{a['id']}:{r['code']}")
            if r["code"] in ("too_close", "exceeds_crossbar"):
                fails.append((i, j, r))
    cnt = [0] * len(ok)
    for i, j, _ in cands:
        cnt[i] += 1
        cnt[j] += 1
    accepted = [(i, j, r) for i, j, r in cands if cnt[i] == 1 and cnt[j] == 1]
    paired = {k for i, j, _ in accepted for k in (i, j)}
    for i, j, r in fails:            # 只對兩支都沒配成對者回報（已各自配對的遠處配管不必提示）
        if i in paired or j in paired:
            continue
        a, b = ok[i], ok[j]
        loc = [(a["mid"][k] + b["mid"][k]) / 2 for k in range(3)]
        issues.append(_issue("pending", r["code"], f"{a['id']}+{b['id']}", loc, r["detail"] + "，未放置"))
    for k, c in enumerate(ok):
        if k in paired:
            continue
        partners = [ok[j if i == k else i]["id"] for i, j, _ in cands if k in (i, j)]
        if partners:
            issues.append(_issue("pending", "ambiguous_pair", c["id"], c["mid"],
                                 f"可與多支配管配對或對象已有其他配對（{', '.join(partners)}），不猜測，未放置"))
        elif len(ok) == 1:
            issues.append(_issue("pending", "unpaired", c["id"], c["mid"], "單支配管（需兩支並排共用吊架），未放置"))
        else:
            issues.append(_issue("pending", "unpaired", c["id"], c["mid"],
                                 "找不到可配對的配管（" + "; ".join(reasons[c["id"]]) + "），未放置"))
    pairs = []
    for i, j, r in accepted:
        a, b = ok[i], ok[j]
        u, o, n = a["u"], a["start"], (-a["u"][1], a["u"][0])
        z = (a["z"] + b["z"]) / 2
        lo, hi = r["lo"], r["hi"]
        s = [o[0] + u[0] * lo + n[0] * r["dlo"] / 2, o[1] + u[1] * lo + n[1] * r["dlo"] / 2, z]
        e = [o[0] + u[0] * hi + n[0] * r["dhi"] / 2, o[1] + u[1] * hi + n[1] * r["dhi"] / 2, z]
        pid = f"{a['id']}+{b['id']}"
        pairs.append({"id": pid, "a": a["id"], "b": b["id"], "start": _p3(s), "end": _p3(e),
                      "overlap_mm": _r3(math.dist(s, e)), "center_distance_min_mm": _r3(r["dmin"]),
                      "center_distance_max_mm": _r3(r["dmax"]), "outer_width_mm": _r3(r["width"]),
                      "outer_diameter_mm": _r3(a["od"]), "bearing_z_mm": _r3(z - a["od"] / 2)})
        bmin, bmax = min(r["tb0"], r["tb1"]), max(r["tb0"], r["tb1"])

        def at(c, t):
            if c is a:
                return [o[0] + u[0] * t, o[1] + u[1] * t, a["z"]]
            f = (t - r["tb0"]) / (r["tb1"] - r["tb0"])
            return [b["start"][k] + (b["end"][k] - b["start"][k]) * f for k in range(3)]

        for c, t0, t1 in ((a, 0.0, lo), (a, hi, a["len"]), (b, bmin, lo), (b, hi, bmax)):
            if t1 - t0 > GEOM_TOL_MM:
                issues.append(_issue("pending", "unsupported_tail", c["id"], at(c, (t0 + t1) / 2),
                                     f"{pid} 重疊段外的尾段 {t1 - t0:.1f} mm 未放吊架（單管段，待確認）"))
    return {"pairs": pairs, "issues": issues}


def conduit_pieces(pairs: list[dict]) -> list[dict]:
    """配對 → supports.layout 用的 pieces（寬 = 外緣總寬，高 = 外徑 → 承托面 = 中心 − 外徑/2）。"""
    return [{"id": p["id"], "start": list(p["start"]), "end": list(p["end"]), "width_mm": p["outer_width_mm"],
             "height_mm": p["outer_diameter_mm"], "start_kind": "free", "end_kind": "free"} for p in pairs]


def cross_member_values(base_cfg: dict, ccfg: dict) -> tuple[float, float]:
    """回傳 (實測橫擔長度 mm, 族橫擔參數值 mm)。依校正：實測 = ratio × 參數 + offset。"""
    cm = base_cfg["calibration"]["cross_member_model"]
    ratio, off = float(cm["ratio"]), float(cm["offset_mm"])
    v = float(ccfg["cross_member"]["value_mm"])
    if ccfg["cross_member"]["basis"] == "parameter":
        return _r3(ratio * v + off), _r3(v)
    return _r3(v), _r3((v - off) / ratio)


def conduit_placement(support: dict, base_cfg: dict, ccfg: dict) -> dict:
    """支撐點 → 族插入點／旋轉／尺寸。沿用橋架設定的族實測值（插入點、修正角、吊桿），但不加寬、橫擔長度固定。"""
    p = S.placement(support, dict(base_cfg, widen_left_mm=0.0, widen_right_mm=0.0))
    length, param = cross_member_values(base_cfg, ccfg)
    p["cross_member_length_mm"] = length
    p["cross_member_param_mm"] = param
    return p
