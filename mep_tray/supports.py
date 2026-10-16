"""吊架支撐點核心（純邏輯，單位 mm）。

與 C# MepTray.Core.Supports 行為一致（tests/test_supports.py 產生契約案例，由 MepTray.Core.SelfTest 比對）。
Web 預覽、router.place_hangers、export_revit 的 hanger_supports 與 Revit 端「放置吊架」共用同一套規則：
- 只處理水平直段；斜段／垂直段／零長度段回報 unsupported/failed 並附位置，不放置。
- 共線且以 union 連接（或端點相接）的段合併為一條連續段計算間距；段與段之間的空隙（接頭本體）不放吊架。
- 每條連續段：起點內縮 start_offset、終點內縮 end_offset，中間等分且每一間距 ≤ max_spacing（不會因尾數超過）。
- 端距衝突（段長 < 起+終內縮）依 short_run_policy：midpoint＝置中 1 支並警告；skip＝不放並列為 failed。
- 接頭附近加強支撐只有在 near_fitting.enabled=true 且給定 max_distance_mm 時才加。
- 去重只在「同一被支撐物件＋相容方向」時成立；只是點靠近不算重複（其他／手動吊架靠近 → 待確認）。
本模組不含任何規範數值預設；所有間距與內縮必須由使用者設定，缺漏即拒絕並指出欄位。
"""
from __future__ import annotations

import math

GEOM_TOL_MM = 0.5            # 幾何判定容差（水平/共線/相接）；非規範值
EPS = 1e-9
SHORT_POLICIES = ("midpoint", "skip")
SPACING_MODES = ("even", "fixed")             # 未填 = even
PLACEMENT_MODES = ("level", "workplane")      # face/host-based 族 v1 不支援（預檢列為 failed）
SCOPES = ("type", "instance")
ROD_MODES = ("none", "fixed")
SUPPORTED_OBJECTS = ("cable_tray",)            # conduit 之後擴充；核心與物件種類無關


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _r3(v: float) -> float:
    return round(float(v), 3) + 0.0


def _p3(p) -> list[float]:
    return [_r3(v) for v in p]


# ---------------------------------------------------------------- 設定驗證
def validate_layout_config(cfg) -> list[str]:
    """支撐點佈置所需欄位。回傳錯誤（每筆以「欄位：原因」開頭）；空 = 合格。"""
    if not isinstance(cfg, dict):
        return ["config：必須是 JSON 物件"]
    e: list[str] = []
    if cfg.get("units") != "mm":
        e.append("units：必須為 \"mm\"")
    s = cfg.get("max_spacing_mm")
    if s is None:
        e.append("max_spacing_mm：缺少（最大支撐間距，工具不提供預設）")
    elif not _num(s) or s <= 0:
        e.append("max_spacing_mm：需為 > 0 的有限數")
    for k in ("start_offset_mm", "end_offset_mm"):
        v = cfg.get(k)
        if v is None:
            e.append(f"{k}：缺少（工具不提供預設）")
        elif not _num(v) or v < 0:
            e.append(f"{k}：需為 ≥ 0 的有限數")
    p = cfg.get("short_run_policy")
    if p is None:
        e.append("short_run_policy：缺少（midpoint 或 skip）")
    elif p not in SHORT_POLICIES:
        e.append("short_run_policy：需為 midpoint 或 skip")
    if "spacing_mode" in cfg and cfg["spacing_mode"] not in SPACING_MODES:
        e.append("spacing_mode：需為 even 或 fixed（未填 = even）")
    nf = cfg.get("near_fitting")
    if nf is None:
        e.append("near_fitting：缺少（不加強時填 {\"enabled\": false}）")
    elif not isinstance(nf, dict) or not isinstance(nf.get("enabled"), bool):
        e.append("near_fitting.enabled：需為 true/false")
    elif nf["enabled"]:
        d = nf.get("max_distance_mm")
        if d is None:
            e.append("near_fitting.max_distance_mm：啟用時必填")
        elif not _num(d) or d <= 0:
            e.append("near_fitting.max_distance_mm：需為 > 0 的有限數")
        elif _num(s) and s > 0 and d > s:
            e.append("near_fitting.max_distance_mm：不得大於 max_spacing_mm")
    return e


def _param_ref_errors(v, field: str) -> list[str]:
    if not isinstance(v, dict):
        return [f"{field}：缺少（需 {{\"name\": 族參數名稱, \"scope\": type|instance}}，取自族盤點）"]
    e = []
    if not _str(v.get("name")):
        e.append(f"{field}.name：缺少")
    if v.get("scope") not in SCOPES:
        e.append(f"{field}.scope：需為 type 或 instance")
    return e


def validate_placement_config(cfg) -> list[str]:
    """實際放置所需的全部欄位（含佈置欄位）。"""
    e = validate_layout_config(cfg)
    if not isinstance(cfg, dict):
        return e
    if cfg.get("supported_object") not in SUPPORTED_OBJECTS:
        e.append("supported_object：v1 只支援 cable_tray")
    fam = cfg.get("family")
    if not isinstance(fam, dict):
        e.append("family：缺少（family_name、type_name）")
    else:
        for k in ("family_name", "type_name"):
            if not _str(fam.get(k)):
                e.append(f"family.{k}：缺少")
    for k in ("widen_left_mm", "widen_right_mm"):
        v = cfg.get(k)
        if v is None:
            e.append(f"{k}：缺少（相對橋架寬度的加寬量，可為 0，但需明確填寫）")
        elif not _num(v) or v < 0:
            e.append(f"{k}：需為 ≥ 0 的有限數")
    v = cfg.get("insertion_to_bearing_mm")
    if v is None:
        e.append("insertion_to_bearing_mm：缺少（族插入點到承托面的垂直距離，需由族實測）")
    elif not _num(v):
        e.append("insertion_to_bearing_mm：需為有限數")
    v = cfg.get("orientation_correction_deg")
    if v is None:
        e.append("orientation_correction_deg：缺少（族橫擔軸向相對橋架方向的修正角，需由族實測）")
    elif not _num(v):
        e.append("orientation_correction_deg：需為有限數")
    if cfg.get("placement_mode") not in PLACEMENT_MODES:
        e.append("placement_mode：需為 level 或 workplane（需與族的放置方式一致）")
    pm = cfg.get("parameter_map")
    if not isinstance(pm, dict):
        e.append("parameter_map：缺少")
    else:
        e += _param_ref_errors(pm.get("cross_member_length"), "parameter_map.cross_member_length")
    rod = cfg.get("rod_length")
    if not isinstance(rod, dict) or rod.get("mode") not in ROD_MODES:
        e.append("rod_length.mode：需為 none 或 fixed")
    elif rod["mode"] == "fixed":
        v = rod.get("value_mm")
        if v is None:
            e.append("rod_length.value_mm：fixed 時必填")
        elif not _num(v) or v <= 0:
            e.append("rod_length.value_mm：需為 > 0 的有限數")
        e += _param_ref_errors(rod.get("param"), "rod_length.param")
    dd = cfg.get("dedupe")
    if not isinstance(dd, dict):
        e.append("dedupe：缺少（tolerance_mm、angle_tolerance_deg）")
    else:
        v = dd.get("tolerance_mm")
        if not _num(v) or v <= 0:
            e.append("dedupe.tolerance_mm：需為 > 0 的有限數")
        v = dd.get("angle_tolerance_deg")
        if not _num(v) or v < 0 or v >= 90:
            e.append("dedupe.angle_tolerance_deg：需為 0 ≤ 值 < 90")
    e += _calibration_errors(cfg)
    return e


def _calibration_errors(cfg: dict) -> list[str]:
    """校正區段（由 Revit「校正目標吊架」寫入）；放置前 Revit 端另核對族檔 sha256 與專案中族的簽章。"""
    cal = cfg.get("calibration")
    if not isinstance(cal, dict):
        return ["calibration：缺少（請在 Revit 依序執行「讀取目標吊架族」「校正目標吊架」）"]
    e = []
    ff = cal.get("family_file")
    if not isinstance(ff, dict) or not _str(ff.get("path")) or not _str(ff.get("sha256")) or not _num(ff.get("size")):
        e.append("calibration.family_file：缺少或不完整（path、size、sha256）")
    if not _str(cal.get("project_signature")):
        e.append("calibration.project_signature：缺少")
    cm = cal.get("cross_member_model")
    if not isinstance(cm, dict) or not (_num(cm.get("ratio")) and abs(cm["ratio"]) > 1e-6) or not _num(cm.get("offset_mm")):
        e.append("calibration.cross_member_model：缺少或無效（ratio≠0、offset_mm）")
    fam = cfg.get("family")
    if isinstance(fam, dict) and (cal.get("family_name") != fam.get("family_name") or cal.get("type_name") != fam.get("type_name")):
        e.append("calibration.family_name：與 family 不符，需重新校正")
    return e


def error_fields(errors: list[str]) -> list[str]:
    """契約比對用：只取「欄位」部分。"""
    return [x.split("：", 1)[0] for x in errors]


# ---------------------------------------------------------------- 佈置
def _issue(level, code, piece_id, loc, detail):
    return {"level": level, "code": code, "piece_id": piece_id, "location_mm": _p3(loc), "detail": detail}


def _norm360(d: float) -> float:
    d = math.fmod(d, 360.0)
    if d < 0:
        d += 360.0
    d = round(d, 6)
    return 0.0 if d >= 360.0 else d + 0.0


def layout(pieces: list[dict], links: list[dict], cfg: dict) -> dict:
    """pieces: [{id, start[mm], end[mm], width_mm, height_mm, start_kind, end_kind}]；kind = free|fitting。
    links: [{a, b, kind}]，kind=union 時嘗試把 a、b 合併為同一連續段（需共線）。
    回傳 {supports: [...], issues: [...]}；supports 依輸入段順序、沿連續段方向排序。"""
    errs = validate_layout_config(cfg)
    if errs:
        raise ValueError("; ".join(errs))
    s = float(cfg["max_spacing_mm"])
    a_off = float(cfg["start_offset_mm"])
    b_off = float(cfg["end_offset_mm"])
    policy = cfg["short_run_policy"]
    fixed = cfg.get("spacing_mode", "even") == "fixed"
    nf = cfg["near_fitting"]
    nf_d = float(nf["max_distance_mm"]) if nf["enabled"] else None
    issues: list[dict] = []
    ok: dict[str, dict] = {}
    order: dict[str, int] = {}
    for idx, p in enumerate(pieces):
        st, en = [float(v) for v in p["start"]], [float(v) for v in p["end"]]
        d = [en[i] - st[i] for i in range(3)]
        h = math.hypot(d[0], d[1])
        L = math.sqrt(h * h + d[2] * d[2])
        mid = [(st[i] + en[i]) / 2 for i in range(3)]
        if L <= GEOM_TOL_MM:
            issues.append(_issue("failed", "zero_length", p["id"], st, "長度為 0，無法放置"))
            continue
        if abs(d[2]) > GEOM_TOL_MM:
            code = "vertical" if h <= GEOM_TOL_MM else "sloped"
            issues.append(_issue("unsupported", code, p["id"], mid,
                                 "垂直段 v1 不支援" if code == "vertical" else f"斜段 v1 不支援（高差 {abs(d[2]):.1f} mm）"))
            continue
        w, ht = p.get("width_mm"), p.get("height_mm")
        if not (_num(w) and w > 0 and _num(ht) and ht > 0):
            issues.append(_issue("failed", "invalid_size", p["id"], mid, "橋架寬度/高度無效"))
            continue
        ok[p["id"]] = {"id": p["id"], "start": st, "end": en, "len": h, "u": [d[0] / h, d[1] / h],
                       "w": float(w), "h": float(ht), "z": st[2],
                       "start_kind": p.get("start_kind", "free"), "end_kind": p.get("end_kind", "free")}
        order[p["id"]] = idx

    # 共線 union → 連續段（union-find）
    parent = {k: k for k in ok}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ln in links:
        if ln.get("kind") != "union" or ln.get("a") not in ok or ln.get("b") not in ok:
            continue
        pa, pb = ok[ln["a"]], ok[ln["b"]]
        cross = pa["u"][0] * pb["u"][1] - pa["u"][1] * pb["u"][0]
        lat = abs((pb["start"][0] - pa["start"][0]) * -pa["u"][1] + (pb["start"][1] - pa["start"][1]) * pa["u"][0])
        if abs(cross) > 1e-4 or lat > GEOM_TOL_MM or abs(pb["z"] - pa["z"]) > GEOM_TOL_MM:
            issues.append(_issue("warning", "union_not_collinear", pa["id"], pa["end"],
                                 f"{pa['id']} 與 {pb['id']} 以 union 相接但不共線，分開計算"))
            continue
        ra, rb = find(pa["id"]), find(pb["id"])
        if ra != rb:
            if order[ra] < order[rb]:
                parent[rb] = ra
            else:
                parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for k in sorted(ok, key=lambda x: order[x]):
        groups.setdefault(find(k), []).append(k)
    chains = sorted(groups.values(), key=lambda g: order[g[0]])

    supports: list[dict] = []
    for ci, members in enumerate(chains, 1):
        cid = f"C{ci:03d}"
        ref = ok[members[0]]
        u, O = ref["u"], ref["start"]

        def proj(pt):
            return (pt[0] - O[0]) * u[0] + (pt[1] - O[1]) * u[1]

        iv = []
        for m in members:
            pm = ok[m]
            t0, t1 = proj(pm["start"]), proj(pm["end"])
            iv.append((min(t0, t1), max(t0, t1), m, t0 <= t1, t0))
        iv.sort(key=lambda x: (x[0], order[x[2]]))
        bad = False
        gaps = []
        for x, y in zip(iv, iv[1:]):
            if y[0] < x[1] - GEOM_TOL_MM:
                issues.append(_issue("failed", "overlap", y[2], ok[y[2]]["start"], f"{x[2]} 與 {y[2]} 重疊，整條連續段不放置"))
                bad = True
            elif y[0] - x[1] > GEOM_TOL_MM:
                gaps.append((x[1], y[0]))
        if bad:
            continue
        T0 = iv[0][0]
        T1 = max(x[1] for x in iv)
        L = T1 - T0
        first, last = iv[0], max(iv, key=lambda x: (x[1], -order[x[2]]))
        k0 = ok[first[2]]["start_kind"] if first[3] else ok[first[2]]["end_kind"]
        k1 = ok[last[2]]["end_kind"] if last[3] else ok[last[2]]["start_kind"]
        gaps_rel = [(g0 - T0, g1 - T0) for g0, g1 in gaps]

        def at(t):
            return [O[0] + u[0] * (T0 + t), O[1] + u[1] * (T0 + t), ref["z"]]

        def snap(t):
            for g0, g1 in gaps_rel:
                if g0 + EPS < t < g1 - EPS:
                    return (g0 if t - g0 <= g1 - t else g1), True
            return t, False

        pts: dict[float, str] = {}

        def add(t, reason):
            t2, moved = snap(t)
            key = round(t2, 6)
            if moved:
                issues.append(_issue("info", "moved_out_of_fitting", first[2], at(t2),
                                     f"{cid} 支撐點落在接頭本體內，已移到接頭邊緣（移動 {abs(t2 - t):.1f} mm）"))
            if key not in pts:
                pts[key] = reason

        if a_off > s + EPS:
            issues.append(_issue("warning", "start_offset_exceeds_spacing", first[2], at(0), "起點內縮大於最大間距（懸臂段）"))
        if b_off > s + EPS:
            issues.append(_issue("warning", "end_offset_exceeds_spacing", last[2], at(L), "終點內縮大於最大間距（懸臂段）"))
        for g0, g1 in gaps_rel:
            if g1 - g0 > s + EPS:
                issues.append(_issue("failed", "fitting_gap_exceeds_spacing", first[2], at(g0),
                                     f"{cid} 接頭本體長 {g1 - g0:.1f} mm 大於最大間距，無法滿足間距"))
        usable = L - a_off - b_off
        if usable < -EPS:
            if policy == "midpoint":
                add(L / 2, "short_midpoint")
                issues.append(_issue("warning", "short_run", first[2], at(L / 2),
                                     f"{cid} 長 {L:.1f} mm 小於起終點內縮和 {a_off + b_off:.1f} mm，置中 1 支"))
            else:
                issues.append(_issue("failed", "short_run", first[2], at(L / 2),
                                     f"{cid} 長 {L:.1f} mm 小於起終點內縮和 {a_off + b_off:.1f} mm，未放置"))
                continue
        elif fixed and usable > EPS:
            n = math.floor(usable / s + 1e-9)
            for i in range(n + 1):
                add(a_off + s * i, "spacing")
            rem = usable - s * n
            if rem > 1e-6:
                x0, x1 = a_off + s * n, L - b_off
                issues.append(_issue("pending", "fixed_remainder", last[2], at(x0),
                                     f"{cid} 固定間距 {s:.1f} mm 排至終點剩餘 {rem:.3f} mm（位置 {x0:.1f}–{x1:.1f} mm），終點未放支撐，待確認（人工決定是否補點）"))
        else:
            n = 0 if usable <= EPS else max(1, math.ceil(usable / s - 1e-9))
            if n == 0:
                add(a_off, "spacing")
            else:
                for i in range(n + 1):
                    add(a_off + usable * i / n, "spacing")
        if nf_d is not None:
            ts = sorted(pts)
            if k0 == "fitting" and ts and ts[0] > nf_d + EPS:
                add(nf_d, "near_fitting")
            if k1 == "fitting" and ts and L - ts[-1] > nf_d + EPS:
                add(L - nf_d, "near_fitting")
        for _ in range(50):          # 移出接頭後若間距超過上限，補點（補點也不得落在接頭內）
            ts = sorted(pts)
            before = len(ts)
            for x, y in zip(ts, ts[1:]):
                if y - x > s + 1e-6:
                    k = math.ceil((y - x) / s - 1e-9)
                    for j in range(1, k):
                        add(x + (y - x) * j / k, "gap_fix")
            if len(pts) == before:
                break
        ts = sorted(pts)
        for x, y in zip(ts, ts[1:]):
            if y - x > s + 1e-6:
                issues.append(_issue("failed", "spacing_not_met", first[2], at(x), f"{cid} 有間距 {y - x:.1f} mm 超過最大間距"))
        for t in ts:
            T = T0 + t
            host = next((x for x in iv if x[0] - 1e-6 <= T <= x[1] + 1e-6), None)
            if host is None:
                issues.append(_issue("failed", "no_piece", first[2], at(t), "支撐點不在任何橋架上"))
                continue
            pm = ok[host[2]]
            station = (T - host[4]) if host[3] else (host[4] - T)
            station = min(max(station, 0.0), pm["len"])
            pt = [pm["start"][0] + pm["u"][0] * station, pm["start"][1] + pm["u"][1] * station, pm["z"]]
            supports.append({
                "piece_id": pm["id"], "chain_id": cid, "station_mm": _r3(station), "point_mm": _p3(pt),
                "direction_deg": _norm360(math.degrees(math.atan2(pm["u"][1], pm["u"][0]))),
                "tray_width_mm": _r3(pm["w"]), "tray_height_mm": _r3(pm["h"]),
                "bearing_z_mm": _r3(pm["z"] - pm["h"] / 2), "reason": pts[t],
            })
    for i, sp in enumerate(supports, 1):
        sp["id"] = f"H{i:03d}"
    return {"supports": supports, "issues": issues}


# ---------------------------------------------------------------- 放置參數（族座標）
def placement(support: dict, cfg: dict) -> dict:
    """支撐點（橋架參考座標）→ 族插入點／旋轉／尺寸。cfg 必須已通過 validate_placement_config。
    insertion_to_bearing_mm：族插入點到承托面的垂直距離（插入點在承托面下方為正）。
    widen_left/right：相對橋架行進方向的左／右側加寬；不對稱時插入點往左（正）側平移 (left-right)/2。"""
    th = math.radians(support["direction_deg"])
    nx, ny = -math.sin(th), math.cos(th)
    left, right = float(cfg["widen_left_mm"]), float(cfg["widen_right_mm"])
    shift = (left - right) / 2
    px, py, _ = support["point_mm"]
    bz = support["bearing_z_mm"]
    rod = cfg["rod_length"]
    return {
        "bearing_point_mm": _p3([px + nx * shift, py + ny * shift, bz]),
        "insertion_point_mm": _p3([px + nx * shift, py + ny * shift, bz - float(cfg["insertion_to_bearing_mm"])]),
        "rotation_deg": _norm360(support["direction_deg"] + float(cfg["orientation_correction_deg"])),
        "cross_member_length_mm": _r3(support["tray_width_mm"] + left + right),
        "rod_length_mm": _r3(rod["value_mm"]) if rod["mode"] == "fixed" else None,
    }


# ---------------------------------------------------------------- 去重／重跑
def _angle_ok(a: float, b: float, tol: float) -> bool:
    d = abs(math.fmod(a - b, 180.0))
    return min(d, 180.0 - d) <= tol + 1e-9


def classify(planned: list[dict], existing: list[dict], tol_mm: float, tol_deg: float) -> list[dict]:
    """planned: [{tray_key, station_mm, point_mm, direction_deg}]；
    existing: [{id, source: tool|other, tray_key, station_mm, point_mm, direction_deg}]。
    回傳每個 planned 的 {status: new|exists|pending, match: [ids], reason}。
    - exists：同一橋架（tray_key）、同 station（≤ tol）、方向相容的「本工具」吊架恰好 1 支。
    - pending（待確認）：同橋架同位置但方向不相容／多於 1 支；或附近（水平距離 ≤ tol）有非本工具吊架。
    絕不因點靠近就判定重複或刪除；他人／手動吊架一律不修改。"""
    out = []
    for p in planned:
        tm = [e for e in existing if e.get("source") == "tool" and e.get("tray_key") == p["tray_key"]
              and e.get("station_mm") is not None and abs(e["station_mm"] - p["station_mm"]) <= tol_mm + 1e-9]
        if len(tm) == 1 and tm[0].get("direction_deg") is not None and _angle_ok(tm[0]["direction_deg"], p["direction_deg"], tol_deg):
            out.append({"status": "exists", "match": [tm[0]["id"]], "reason": "本工具已放置"})
            continue
        if tm:
            why = "同位置有多支本工具吊架" if len(tm) > 1 else "同位置本工具吊架方向不相容"
            out.append({"status": "pending", "match": [e["id"] for e in tm], "reason": why})
            continue
        om = [e for e in existing if e.get("source") != "tool" and e.get("point_mm") is not None
              and math.hypot(e["point_mm"][0] - p["point_mm"][0], e["point_mm"][1] - p["point_mm"][1]) <= tol_mm + 1e-9]
        if om:
            out.append({"status": "pending", "match": [e["id"] for e in om], "reason": "附近有非本工具（手動）吊架"})
            continue
        out.append({"status": "new", "match": [], "reason": ""})
    return out


# ---------------------------------------------------------------- 由中介 JSON（未建接頭）取段
def pieces_from_model(model: dict) -> tuple[list[dict], list[dict]]:
    """tray_*.json → (pieces, links)。端點落在 elbow/tee/cross/unsupported 接點 → fitting 端；union → link。
    注意：這是建立接頭「之前」的路徑端點；Revit 端會以建立接頭後的實際橋架端點重算。"""
    w, h = model["tray"]["width_mm"], model["tray"]["height_mm"]
    pieces = [{"id": s["id"], "start": s["start"], "end": s["end"], "width_mm": w, "height_mm": h,
               "start_kind": "free", "end_kind": "free"} for s in model["segments"]]
    by_id = {p["id"]: p for p in pieces}
    links = []
    for j in model.get("joints") or []:
        if j["kind"] == "union" and len(j["segments"]) == 2:
            links.append({"a": j["segments"][0], "b": j["segments"][1], "kind": "union"})
            continue
        for sid in j["segments"]:
            p = by_id.get(sid)
            if p is None:
                continue
            for end in ("start", "end"):
                if math.dist(p[end], j["point"]) <= GEOM_TOL_MM:
                    p[end + "_kind"] = "fitting"
    return pieces, links
