"""極簡本機網頁介面（標準庫 http.server；只綁 127.0.0.1；單頁、四塊：尺寸輸入／規範勾選／執行／結果下載）。

安全設計摘要（細節見各函式）：
- 只綁 127.0.0.1（拒絕其他位址）；每次啟動隨機 token，放在 URL 路徑前綴 /t/<token>/（不用 cookie：cookie 不按埠隔離，
  會被同機其他 127.0.0.1 服務收到）。錯誤/缺少 token 與不存在的路由回應完全相同（404）。
- Host 必須精確為 127.0.0.1:PORT 或 localhost:PORT（擋 DNS rebinding）；POST 必須帶相符的 Origin；
  Sec-Fetch-Site 為 cross-site 一律拒絕。
- 每個回應 Connection: close（不做 keep-alive 狀態機）；Transfer-Encoding 一律 400、Expect 417、Content-Length 嚴格解析。
- 同時處理的連線有上限；每連線 socket 逾時；同時只跑一個工作（忙碌→409）；沒有取消（有格點與障礙物工作量預算保護）。
- 下載只走 manifest 白名單（kind 白名單 + 已驗證的安全檔名），不接受使用者提供的路徑，不列目錄。
- 前端只用 textContent；UI 頁 CSP 不含 unsafe-inline；報告以 sandbox CSP 提供。
"""
from __future__ import annotations

import argparse
import hmac
import html
import json
import math
import re
import secrets
import socket
import sys
import threading
import webbrowser
from collections import OrderedDict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import pipeline as P
from . import export_revit as ER
from . import report as RP
from . import versioning as V
from .disclosure import BANNER_FIXED, SCOPE_NOTE
from .model import OBSTACLE_KINDS, Inputs
from .paths import RUN_ID_RE, check_run_id, output_root
from .router import MAX_OBSTACLE_WORK, Obstacle, estimate_obstacle_work, grid_cells
from .rules import load_rules, merge_strictest
from .sanitize import sanitize_text

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 1_048_576                  # POST 本文上限 1 MB
MAX_OBSTACLE_BYTES = 262_144          # 障礙物整體序列化上限 256 KB
MAX_OBSTACLES = 200
MAX_END_POINTS = 3
MAX_JSON_DEPTH = 20
MAX_CONNECTIONS = 16
SOCKET_TIMEOUT_S = 15
MAX_GRID_CELLS = 3_000_000            # 與 router.route_tray 的 max_cells 預設一致
FINE_CELL_MAX_GRID = 500_000          # 0.05 m 只在網格 <= 此值時可選
CELLS = (0.25, 0.2, 0.1, 0.05)
JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")
KIND_ORDER = ("dxf", "dwg", "revit_json", "report", "manifest")
KIND_LABEL = {"dxf": "DXF 圖面", "dwg": "DWG 圖面", "revit_json": "Revit 匯入 JSON", "report": "HTML 報告",
              "manifest": "版本資訊與雜湊（manifest）"}
KIND_TYPE = {"dxf": "application/dxf", "dwg": "application/acad", "revit_json": "application/json",
             "manifest": "application/json", "report": "text/html; charset=utf-8"}
_CTRL = RP._CTRL                      # 與報告共用的控制/雙向字元集合

UI_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; "
          "form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
DOWNLOAD_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field

    def body(self) -> dict:
        return {"error": {"code": self.code, "field": self.field, "message": self.message}}


# ───────────────────────── 請求驗證 ─────────────────────────
def _reject_constant(name):
    raise ValueError(f"非法的 JSON 常數 {name}")


def _no_dup_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"重複的鍵: {k}")
        out[k] = v
    return out


def _depth(o, d=0) -> int:
    if d > MAX_JSON_DEPTH:
        return d
    if isinstance(o, dict):
        return max([_depth(v, d + 1) for v in o.values()] + [d + 1])
    if isinstance(o, list):
        return max([_depth(v, d + 1) for v in o] + [d + 1])
    return d


def parse_json_body(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_dup_pairs)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ApiError(400, "bad_json", f"請求內容不是合法的 JSON（{type(e).__name__}）")
    if _depth(obj) > MAX_JSON_DEPTH:
        raise ApiError(400, "bad_json", "JSON 巢狀過深")
    if not isinstance(obj, dict):
        raise ApiError(400, "bad_json", "請求內容必須是 JSON 物件")
    return obj


def _num(v, field, lo, hi, what) -> float:
    """明確拒絕 bool（Python 的 True 是 int）與字串型數字；有限數；四捨五入到 0.001 後再驗證。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ApiError(400, "invalid_number", f"{what} 必須是數字", field)
    if not math.isfinite(v):
        raise ApiError(400, "invalid_number", f"{what} 必須是有限的數字", field)
    r = round(float(v), 3) + 0.0
    if not (lo <= r <= hi):
        raise ApiError(400, "out_of_range", f"{what} 必須介於 {lo:g} 與 {hi:g} 之間", field)
    return r


def _int(v, field, lo, hi, what) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ApiError(400, "invalid_number", f"{what} 必須是整數", field)
    if not (lo <= v <= hi):
        raise ApiError(400, "out_of_range", f"{what} 必須介於 {lo} 與 {hi} 之間", field)
    return v


def _obj(v, field, keys, required=True):
    if not isinstance(v, dict):
        raise ApiError(400, "invalid_type", f"{field} 必須是物件", field)
    extra = set(v) - set(keys)
    if extra:
        raise ApiError(400, "unknown_field", f"{field} 含有未知欄位: {sorted(extra)[:3]}", field)
    miss = [k for k in keys if required and k not in v]
    if miss:
        raise ApiError(400, "missing_field", f"{field} 缺少欄位: {miss[0]}", f"{field}.{miss[0]}")
    return v


def _point(v, field, room, what) -> tuple:
    o = _obj(v, field, ("x", "y", "z"))
    p = tuple(_num(o[a], f"{field}.{a}", 0, room[i], f"{what} {a}") for i, a in enumerate("xyz"))
    return p


def validate_request(body: dict, rules: dict | None = None):
    """回傳 (Inputs, codes)。任何不合法一律 ApiError(400)。UI 與管線共用同一組上限常數與 router 的網格公式。"""
    rules = rules or load_rules()
    top = _obj(body, "request", ("room", "tray", "start", "ends", "codes", "cell_m", "cable", "obstacles"),
               required=False)
    for k in ("room", "tray", "start", "ends", "codes"):
        if k not in top:
            raise ApiError(400, "missing_field", f"缺少欄位: {k}", k)
    r = _obj(top["room"], "room", ("x", "y", "z"))
    room = tuple(_num(r[a], f"room.{a}", 0.5, 200, f"房間{a.upper()}長度 (m)") for a in "xyz")
    t = _obj(top["tray"], "tray", ("width_mm", "height_mm", "kind"))
    w = _num(t["width_mm"], "tray.width_mm", 50, 2000, "橋架寬 (mm)")
    h = _num(t["height_mm"], "tray.height_mm", 20, 500, "橋架高 (mm)")
    if t["kind"] not in ("power", "signal"):
        raise ApiError(400, "invalid_choice", "橋架類型必須是 power 或 signal", "tray.kind")
    start = _point(top["start"], "start", room, "起點")
    if not isinstance(top["ends"], list) or not (1 <= len(top["ends"]) <= MAX_END_POINTS):
        raise ApiError(400, "invalid_type", f"終點需 1～{MAX_END_POINTS} 個", "ends")
    ends = [_point(e, f"ends[{i}]", room, f"終點{i + 1}") for i, e in enumerate(top["ends"])]
    cables = []
    if top.get("cable") is not None:
        c = _obj(top["cable"], "cable", ("od_mm", "count"))
        cables = [{"od_mm": _num(c["od_mm"], "cable.od_mm", 1, 200, "電纜外徑 (mm)"),
                   "count": _int(c["count"], "cable.count", 1, 1000, "電纜條數"), "kind": t["kind"]}]
    codes = top["codes"]
    if not isinstance(codes, list) or not codes or not all(isinstance(x, str) for x in codes):
        raise ApiError(400, "invalid_type", "請至少勾選一套規範", "codes")
    codes = list(dict.fromkeys(codes))
    unknown = [c for c in codes if c not in rules["codes"]]
    if unknown:
        raise ApiError(400, "unknown_code", f"未知的規範代號: {unknown[0][:40]!r}", "codes")
    cell = top.get("cell_m", 0.25)
    cell = _num(cell, "cell_m", 0.01, 10, "格距 (m)")
    if cell not in CELLS:
        raise ApiError(400, "invalid_choice", f"格距必須是 {list(CELLS)} 之一", "cell_m")
    from .geometry import Box
    box = Box((0, 0, 0), room)
    total = grid_cells(box, cell)                                  # 與 router 共用同一個公式
    if total > MAX_GRID_CELLS:
        raise ApiError(400, "grid_too_large", f"房間在格距 {cell:g} m 下有 {total:,} 格，超過上限 {MAX_GRID_CELLS:,}；"
                       f"請縮小房間或放大格距", "cell_m")
    if cell == 0.05 and total > FINE_CELL_MAX_GRID:
        raise ApiError(400, "cell_too_fine", f"格距 0.05 m 只在網格 ≤ {FINE_CELL_MAX_GRID:,} 格時可選"
                       f"（目前 {total:,} 格）；請改用 0.1 m 以上或縮小房間", "cell_m")
    obstacles = []
    raw_obs = top.get("obstacles") or []
    if not isinstance(raw_obs, list):
        raise ApiError(400, "invalid_type", "障礙物必須是清單", "obstacles")
    if len(raw_obs) > MAX_OBSTACLES:
        raise ApiError(400, "too_many_obstacles", f"障礙物最多 {MAX_OBSTACLES} 個（目前 {len(raw_obs)}）", "obstacles")
    if len(json.dumps(raw_obs, ensure_ascii=False)) > MAX_OBSTACLE_BYTES:
        raise ApiError(400, "obstacles_too_large", f"障礙物資料超過 {MAX_OBSTACLE_BYTES // 1024} KB", "obstacles")
    for i, o in enumerate(raw_obs):
        f = f"obstacles[{i}]"
        o = _obj(o, f, ("name", "kind", "lo", "hi"))
        name = o["name"]
        if not isinstance(name, str) or not (1 <= len(name) <= 100) or not name.strip() or _CTRL.search(name):
            raise ApiError(400, "invalid_name", "障礙物名稱需為 1～100 字元且不含控制字元", f"{f}.name")
        if o["kind"] not in OBSTACLE_KINDS:
            raise ApiError(400, "invalid_choice", f"障礙物類型必須是 {sorted(OBSTACLE_KINDS)} 之一", f"{f}.kind")
        pts = {}
        for key in ("lo", "hi"):
            v = o[key]
            if not isinstance(v, list) or len(v) != 3:
                raise ApiError(400, "invalid_type", f"{key} 必須是 3 個數字", f"{f}.{key}")
            pts[key] = [_num(x, f"{f}.{key}[{j}]", -1000, 1000, f"障礙物 {key}") for j, x in enumerate(v)]
        if any(l >= hh for l, hh in zip(pts["lo"], pts["hi"])):
            raise ApiError(400, "invalid_box", "障礙物的 lo 每個分量都必須小於 hi", f)
        obstacles.append({"name": name, "kind": o["kind"], "lo": pts["lo"], "hi": pts["hi"]})
    inp = Inputs(tray_w_mm=w, tray_h_mm=h, tray_type=t["kind"], room=((0, 0, 0), room), start=start, ends=ends,
                 obstacles=obstacles, cables=cables, codes=codes, cell_m=cell)
    try:
        inp.validate()
    except ValueError as e:                                       # 防禦：不應發生（上面已逐欄位驗證）
        raise ApiError(400, "invalid_input", f"輸入驗證失敗: {e}")
    if obstacles:                                                 # 與管線相同的估算，進入工作前先擋
        gov = merge_strictest(codes, rules)
        work = estimate_obstacle_work(box, [Obstacle(o["name"], o["kind"], Box(tuple(o["lo"]), tuple(o["hi"])))
                                            for o in obstacles], w / 1000, h / 1000, t["kind"], gov, cell)
        if work > MAX_OBSTACLE_WORK:
            raise ApiError(400, "obstacle_work_limit", f"障礙物範圍過大或過多（估算工作量 {work:,} 超過 {MAX_OBSTACLE_WORK:,}），"
                           f"請縮小障礙物、減少數量或放大格距", "obstacles")
    return inp, codes


# ───────────────────────── 錯誤 → 人話 ─────────────────────────
HUMAN = {
    ("route", "no_route"): ("找不到可行路徑", "障礙物與規範淨距把通道封住了；請移動障礙物，或確認起訖點之間有足夠空間。"),
    ("route", "misaligned"): ("起訖座標未對齊格距", "座標必須是格距的整數倍（例如格距 0.25 m 時用 1.25、3.5）。"),
    ("route", "search_limit"): ("搜尋範圍太大", "請放大格距或縮小房間後再試。"),
    ("route", "grid_too_large"): ("房間在這個格距下太大", "請放大格距或縮小房間。"),
    ("route", "obstacle_work_limit"): ("障礙物範圍過大或過多", "請縮小障礙物、減少數量，或放大格距。"),
    ("route", "endpoint_blocked"): ("起點或終點落在障礙物淨距範圍內", "請移動起訖點或障礙物，或確認未貼近牆面與天花。"),
    ("validate", "invalid_input"): ("輸入不合法", "請檢查各欄位的數值與範圍。"),
    ("validate", "no_codes"): ("請至少勾選一套規範", "規範勾選區需要至少一項。"),
    ("rules", "invalid_rules"): ("規範設定有問題", "請檢查 rules.json 或勾選的規範代號。"),
    ("output", "run_exists"): ("版本編號衝突", "請再按一次執行。"),
    ("io", "os_error"): ("寫入輸出資料夾失敗", "請確認磁碟空間與資料夾權限後再試。"),
}


PREVIEW_CAPTION = "示意圖，非施工圖"
PREVIEW_SIZE = (640, 280)
_PREVIEW_MARGIN = 24
_PREVIEW_BAR_SPACE = 36
PREVIEW_MAX_SEGMENTS = 5000                    # 超過就不送示意（避免完成結果的 JSON 過大）
PREVIEW_MAX_OBSTACLES = MAX_OBSTACLES          # 與請求上限一致（請求本來就不會超過）
PREVIEW_MAX_HANGERS = 1000                     # 圖層筆數上限：超過只畫前 N 個並附註（JSON 保持小）
PREVIEW_MAX_JOINTS = 500
PREVIEW_UNAVAILABLE = "無法產生平面示意圖；請以下載檔與報告為準。"
PREVIEW_JOINT_KINDS = ("elbow", "tee", "cross", "union", "unsupported")
_LAYER_LABEL = {"obstacles": "障礙物", "hangers": "吊架", "joints": "接頭"}


def _nice_scale_m(max_m: float) -> float:
    """不大於 max_m 的 1–2–5×10^n 長度（公尺）。"""
    if not math.isfinite(max_m) or max_m <= 0:
        return 1.0
    exp = math.floor(math.log10(max_m))
    for k in range(exp, exp - 8, -1):
        base = 10 ** k
        for n in (5, 2, 1):
            v = n * base
            if v <= max_m + 1e-12:
                return v
    return max_m


def _r3(v) -> float:
    return round(float(v), 3) + 0.0


def xy_preview(room, segments, size=PREVIEW_SIZE, start=None, ends=(), obstacles=(), hangers=(), joints=()) -> dict:
    """房間 XY 平面的 Canvas 座標（Y 向下；模型 +Y 朝上，原點在房間框左下）。

    輸入為模型座標（m）；輸出已換算成畫布像素，前端只需照畫。只在工作成功時計算一次並存在工作結果中。
    圖層（皆為俯視投影，不分高度）：
    - obstacles：[{kind, lo, hi}] → [{kind, rect:[x, y, w, h]}]，裁切到房間框內；完全在房外的略過；不送名稱。
    - hangers：[(x, y, z)] → [[x, y]]，俯視重疊（垂直段）只留一個。
    - joints：[{kind, point}] → [{kind, pt:[x, y]}]，kind 不在白名單一律 unsupported。
    每層超過上限只保留前 N 個，並在 notes 附註；counts 為原始筆數。
    資料不合理（非有限數、房間尺寸非正、線段過多）一律拋 ValueError。"""
    lo, hi = room[0], room[1]
    rx, ry = hi[0] - lo[0], hi[1] - lo[1]
    if not all(math.isfinite(float(v)) for v in (lo[0], lo[1], hi[0], hi[1])):
        raise ValueError("房間座標必須為有限數")
    if rx <= 0 or ry <= 0:
        raise ValueError("房間平面尺寸必須為正")
    segments = list(segments)
    if len(segments) > PREVIEW_MAX_SEGMENTS:
        raise ValueError("線段過多，不產生示意圖")
    cw, ch = size
    inner_w = cw - 2 * _PREVIEW_MARGIN
    inner_h = ch - _PREVIEW_MARGIN - _PREVIEW_BAR_SPACE
    scale = min(inner_w / rx, inner_h / ry)
    rw, rh = rx * scale, ry * scale
    x = _PREVIEW_MARGIN + (inner_w - rw) / 2
    y = _PREVIEW_MARGIN + (inner_h - rh) / 2

    def to_px(pt):
        px, py = float(pt[0]), float(pt[1])
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError("座標必須為有限數")
        return (_r3(x + (px - lo[0]) * scale), _r3(y + (hi[1] - py) * scale))

    segs = []
    for a, b in segments:
        x0, y0 = to_px(a)
        x1, y1 = to_px(b)
        segs.append([x0, y0, x1, y1])

    notes = []
    obstacles, hangers, joints = list(obstacles), list(hangers), list(joints)
    obs_px = []
    for o in obstacles:
        olo, ohi = o["lo"], o["hi"]
        vals = [float(v) for v in (olo[0], olo[1], ohi[0], ohi[1])]
        if not all(math.isfinite(v) for v in vals):
            raise ValueError("障礙物座標必須為有限數")
        if len(obs_px) >= PREVIEW_MAX_OBSTACLES:
            continue
        x0, x1 = max(min(vals[0], vals[2]), lo[0]), min(max(vals[0], vals[2]), hi[0])
        y0, y1 = max(min(vals[1], vals[3]), lo[1]), min(max(vals[1], vals[3]), hi[1])
        if x1 <= x0 or y1 <= y0:                               # 俯視投影完全在房外
            continue
        px0, py_top = to_px((x0, y1))
        px1, py_bot = to_px((x1, y0))
        kind = o.get("kind") if o.get("kind") in OBSTACLE_KINDS else "other"
        obs_px.append({"kind": kind, "rect": [px0, py_top, _r3(px1 - px0), _r3(py_bot - py_top)]})
    if len(obstacles) > PREVIEW_MAX_OBSTACLES:
        notes.append(f"障礙物共 {len(obstacles)} 個，僅顯示前 {PREVIEW_MAX_OBSTACLES} 個。")

    hang_px, seen = [], set()
    for h in hangers:
        pt = to_px(h)
        if pt in seen or len(hang_px) >= PREVIEW_MAX_HANGERS:
            continue
        seen.add(pt)
        hang_px.append(list(pt))
    if len(hang_px) >= PREVIEW_MAX_HANGERS and len(hangers) > PREVIEW_MAX_HANGERS:
        notes.append(f"吊架共 {len(hangers)} 個，僅顯示前 {PREVIEW_MAX_HANGERS} 個。")

    joint_px = []
    for jt in joints:
        pt = to_px(jt["point"])
        if len(joint_px) >= PREVIEW_MAX_JOINTS:
            continue
        kind = jt.get("kind") if jt.get("kind") in PREVIEW_JOINT_KINDS else "unsupported"
        joint_px.append({"kind": kind, "pt": list(pt)})
    if len(joints) > PREVIEW_MAX_JOINTS:
        notes.append(f"接頭共 {len(joints)} 個，僅顯示前 {PREVIEW_MAX_JOINTS} 個。")

    metres = _nice_scale_m(0.35 * rx)
    length_px = metres * scale
    return {
        "caption": PREVIEW_CAPTION,
        "size": [cw, ch],
        "room": [_r3(x), _r3(y), _r3(rw), _r3(rh)],
        "segments": segs,
        "start": list(to_px(start)) if start is not None else None,
        "ends": [list(to_px(e)) for e in ends],
        "obstacles": obs_px,
        "hangers": hang_px,
        "joints": joint_px,
        "counts": {"obstacles": len(obstacles), "hangers": len(hangers), "joints": len(joints)},
        "notes": notes,
        "scale_bar": {"x": _r3(x), "y": _r3(y + rh + 14), "length_px": _r3(length_px),
                      "label": f"{metres:g} m"},
    }


def preview_joints(route) -> list:
    """接頭與 Revit 匯入 JSON 同一個來源（export_revit.build_joints；分支接入點先切段），座標 mm → m。"""
    return [{"kind": j["kind"], "point": [v / 1000 for v in j["point"]]}
            for j in ER.build_joints(ER.split_segments(route))]


def safe_preview(res) -> tuple:
    """(preview, preview_error)。示意圖只是附加資訊：任何失敗都不能讓已成功的工作變成錯誤。

    圖層各自取資料（障礙物＝送出的請求、吊架＝route.hangers、接頭＝preview_joints）；取不到的圖層只略過並附註。
    帶圖層換算失敗時退回只畫基本示意（房間、路徑、起訖點）；連基本示意都失敗才回 None + 文字。"""
    try:
        inp = res.inputs
        layers, broken = {}, []
        sources = (("obstacles", lambda: list(inp.obstacles or [])),
                   ("hangers", lambda: list(res.route.hangers or [])),
                   ("joints", lambda: preview_joints(res.route)))
        for name, get in sources:
            try:
                layers[name] = get()
            except Exception as e:                             # 單一圖層失敗不影響其他圖層
                broken.append(name)
                sys.stderr.write(f"[webui] preview layer {name} skipped: {type(e).__name__}\n")
        try:
            pv = xy_preview(inp.room, res.route.segments, start=inp.start, ends=inp.ends, **layers)
        except Exception as e:
            sys.stderr.write(f"[webui] preview layers skipped: {type(e).__name__}\n")
            pv = xy_preview(inp.room, res.route.segments, start=inp.start, ends=inp.ends)
            pv["notes"].append("圖層資料異常，僅顯示房間、路徑與起訖點。")
            return pv, None
        if broken:
            pv["notes"].append("無法顯示的圖層：" + "、".join(_LAYER_LABEL[b] for b in broken) + "。")
        return pv, None
    except Exception as e:                                     # 不洩漏細節，只記類型
        sys.stderr.write(f"[webui] preview skipped: {type(e).__name__}\n")
        return None, PREVIEW_UNAVAILABLE


def human_error(err: P.PipelineError) -> dict:
    title, hint = HUMAN.get((err.stage, err.code), ("執行失敗", "請查看下方的錯誤代碼與說明。"))
    if err.cleanup_failed:
        hint += " 另有暫存資料夾（.stage- 開頭）未能清除，可在輸出資料夾中手動刪除。"
    return {"stage": err.stage, "code": err.code, "title": title, "hint": hint,
            "message": err.message, "cleanup_failed": err.cleanup_failed}


# ───────────────────────── 工作管理（同時只跑一個） ─────────────────────────
class JobManager:
    def __init__(self, token: str, make_dwg: bool = True, keep: int = 20):
        self.token, self.make_dwg, self.keep = token, make_dwg, keep
        self._lock = threading.Lock()
        self._running: str | None = None
        self.jobs: OrderedDict = OrderedDict()
        self._thread: threading.Thread | None = None

    def submit(self, inp: Inputs, codes: list) -> str:
        with self._lock:
            if self._running is not None:
                raise ApiError(409, "busy", "已有一個工作在執行，請等它完成後再試。")
            jid = secrets.token_hex(8)
            self._running = jid
            self.jobs[jid] = {"state": "running"}
            while len(self.jobs) > self.keep:
                self.jobs.popitem(last=False)
        self._thread = threading.Thread(target=self._work, args=(jid, inp, codes), daemon=True)
        self._thread.start()
        return jid

    def _work(self, jid: str, inp: Inputs, codes: list) -> None:
        try:
            res = None
            for _ in range(3):
                run_id = V.new_run_id(inp, codes)
                res = P.run(inp, codes, run_id, make_dwg=self.make_dwg)
                if res.ok or res.error.code != "run_exists":
                    break
            out = self._view(res)
        except Exception as e:                                     # 程式錯誤：不洩漏細節，只給類型
            out = {"state": "error", "error": {"stage": "internal", "code": "internal_error",
                                               "title": "內部錯誤", "hint": "請查看伺服器終端機輸出。",
                                               "message": type(e).__name__, "cleanup_failed": False}}
            sys.stderr.write(f"[webui] job {jid} crashed: {type(e).__name__}\n")
        with self._lock:
            self.jobs[jid] = out
            self._running = None

    def _view(self, res: P.RunResult) -> dict:
        if not res.ok:
            return {"state": "error", "error": human_error(res.error)}
        m = res.manifest
        base = f"/t/{self.token}"
        files = []
        for kind in KIND_ORDER:
            ent = m["files"].get(kind) if kind != "manifest" else {"name": V.MANIFEST_NAME, "bytes": None, "sha256": ""}
            if ent:
                files.append({"kind": kind, "label": KIND_LABEL[kind], "name": ent["name"],
                              "href": f"{base}/download/{res.run_id}/{kind}", "sha8": ent["sha256"][:8]})
        prev = previous_version(res.run_id)
        preview, preview_error = safe_preview(res)
        return {"state": "done", "run_id": res.run_id, "summary": res.to_summary_dict(), "files": files,
                "banners": [BANNER_FIXED, SCOPE_NOTE] if res.stats.get("unverified_checks") or
                any(not g.verified for g in res.gov.values()) else [SCOPE_NOTE],
                "diff_href": f"{base}/diff/{prev}/{res.run_id}" if prev else None,
                "preview": preview, "preview_error": preview_error}

    def status(self, jid: str):
        with self._lock:
            return self.jobs.get(jid)

    def running(self) -> str | None:
        with self._lock:
            return self._running

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)


def previous_version(run_id: str) -> str | None:
    """依 (created_at, run_id) 排序，回傳緊鄰目前版本之前、且 manifest 可用的版本。"""
    vs = [v for v in V.list_versions() if v.error is None]
    ids = [v.run_id for v in vs]
    if run_id not in ids:
        return None
    i = ids.index(run_id)
    return ids[i - 1] if i > 0 else None


def recent_versions(limit: int = 10) -> list:
    out = []
    for v in reversed(V.list_versions()):
        if len(out) >= limit:
            break
        out.append(v.to_dict())
    return out


# ───────────────────────── 靜態資源 ─────────────────────────
APP_CSS = """
*{box-sizing:border-box}
body{font-family:"Microsoft JhengHei","PingFang TC","Noto Sans CJK TC",sans-serif;margin:0 auto;max-width:960px;
padding:16px;line-height:1.5;color:#111;background:#fff}
h1{font-size:1.4rem;margin:.2em 0 .6em}h2{font-size:1.1rem;border-bottom:2px solid #444;padding-bottom:.2em}
section{margin:1.4em 0}fieldset{border:1px solid #888;margin:.8em 0;padding:.6em .9em;min-width:0}
legend{font-weight:bold;padding:0 .3em}
.row{display:flex;flex-wrap:wrap;gap:.6em 1em;align-items:flex-end}.field{display:flex;flex-direction:column;min-width:0}
.field label,.choice label{font-size:.9rem}.choice{display:inline-flex;gap:.4em;align-items:center;margin:.15em .8em .15em 0}
input,select,button{font:inherit;padding:.35em .5em;min-width:0;max-width:100%}
input[type=number]{width:7.5em}input[aria-invalid=true]{border:2px solid #000;background:#ffe9e9}
.err{font-weight:bold;margin:.3em 0}.err::before{content:"✖ "}.hint{font-size:.85rem;color:#333}
.banner{border:3px solid #000;border-left-width:14px;padding:8px 12px;margin:.8em 0;background:#fff8dc;font-weight:bold}
button[type=submit]{font-weight:bold;padding:.6em 1.6em;border:2px solid #000;background:#eee;cursor:pointer}
button[disabled]{opacity:.6;cursor:progress}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{border:1px solid #888;padding:3px 6px;text-align:left;
word-break:break-word}
.mono{font-family:Consolas,"Courier New",monospace;font-size:.85rem}ul.files{padding-left:1.2em}
canvas{display:block;max-width:100%;border:1px solid #888;background:#fafafa;margin:.6em 0}
@media(max-width:480px){input[type=number]{width:6em}}
""".strip()

APP_JS = r"""
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var obstacles = null, obstaclesBad = false, busy = false;
  var FIELD_MAP = {codes: "codes", cell_m: "cell_m", obstacles: "obstacle_file", "tray.kind": "tray_kind_power"};

  function text(tag, str, cls) { var e = document.createElement(tag); e.textContent = str; if (cls) e.className = cls; return e; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function num(id) { var v = $(id).value.trim(); return v === "" ? null : Number(v); }
  function fid(field) {
    if (!field) return null;
    if (field.indexOf("obstacles") === 0) return "obstacle_file";
    if (field.indexOf("codes") === 0) return "codes";
    if (FIELD_MAP[field]) return FIELD_MAP[field];
    return field.replace(/\[(\d+)\]/g, "$1").replace(/\./g, "_");
  }
  function clearErrors() {
    clear($("form-errors"));
    var bad = document.querySelectorAll("[aria-invalid=true]");
    for (var i = 0; i < bad.length; i++) { bad[i].removeAttribute("aria-invalid"); bad[i].removeAttribute("aria-describedby"); }
  }
  function showError(message, field) {
    var box = $("form-errors");
    var p = text("p", message, "err"); p.id = "err-" + box.childNodes.length; box.appendChild(p);
    var el = field ? $(fid(field)) : null;
    if (el) { el.setAttribute("aria-invalid", "true"); el.setAttribute("aria-describedby", p.id); if (el.focus) el.focus(); }
  }
  function status(msg) { $("status").textContent = msg; }

  function buildRequest() {
    var req = {
      room: {x: num("room_x"), y: num("room_y"), z: num("room_z")},
      tray: {width_mm: num("tray_width_mm"), height_mm: num("tray_height_mm"),
             kind: document.querySelector("input[name=tray_kind]:checked").value},
      start: {x: num("start_x"), y: num("start_y"), z: num("start_z")},
      ends: [], codes: [], cell_m: Number($("cell_m").value)
    };
    for (var i = 0; i < 3; i++) {
      var x = num("ends" + i + "_x"), y = num("ends" + i + "_y"), z = num("ends" + i + "_z");
      if (x === null && y === null && z === null) continue;
      req.ends.push({x: x, y: y, z: z});
    }
    var od = num("cable_od_mm"), cnt = num("cable_count");
    if (od !== null || cnt !== null) req.cable = {od_mm: od, count: cnt};
    var cs = document.querySelectorAll("input[name=code]:checked");
    for (var j = 0; j < cs.length; j++) req.codes.push(cs[j].value);
    if (obstacles) req.obstacles = obstacles;
    return req;
  }

  function finite(v) { return typeof v === "number" && isFinite(v); }
  function finiteList(a, n) {
    if (!Array.isArray(a) || a.length !== n) return false;
    for (var i = 0; i < n; i++) if (!finite(a[i])) return false;
    return true;
  }
  function previewOk(pv) {
    if (!pv || typeof pv !== "object" || typeof pv.caption !== "string") return false;
    if (!finiteList(pv.size, 2) || pv.size[0] <= 0 || pv.size[1] <= 0 || pv.size[0] > 4000 || pv.size[1] > 4000) return false;
    if (!finiteList(pv.room, 4) || !Array.isArray(pv.segments)) return false;
    for (var i = 0; i < pv.segments.length; i++) if (!finiteList(pv.segments[i], 4)) return false;
    if (pv.start !== null && pv.start !== undefined && !finiteList(pv.start, 2)) return false;
    if (pv.ends !== undefined && !Array.isArray(pv.ends)) return false;
    for (var j = 0; pv.ends && j < pv.ends.length; j++) if (!finiteList(pv.ends[j], 2)) return false;
    var sb = pv.scale_bar;
    return !!sb && finite(sb.x) && finite(sb.y) && finite(sb.length_px) && typeof sb.label === "string";
  }
  var OBS_STYLE = {structure: ["#444", "結構"], water: ["#1a8fc9", "水管"], duct: ["#777", "風管"],
    heat: ["#d97a00", "熱源"], heat_bare: ["#a0461e", "無保溫熱源"], tray_power: ["#7b3fa0", "電力橋架"],
    tray_signal: ["#c055b8", "訊號橋架"], other: ["#999", "其他"]};
  var JOINT_LABEL = {elbow: "彎頭◇", tee: "三通△", cross: "四通⊞", union: "直接頭○", unsupported: "未支援接頭⊠"};
  var LAYER_CAP = 1000;
  function layer(a, ok) {
    var out = [];
    if (!Array.isArray(a)) return out;
    for (var i = 0; i < a.length && out.length < LAYER_CAP; i++) if (ok(a[i])) out.push(a[i]);
    return out;
  }
  function obstacleOk(o) { return !!o && typeof o.kind === "string" && finiteList(o.rect, 4) && o.rect[2] > 0 && o.rect[3] > 0; }
  function jointOk(o) { return !!o && typeof o.kind === "string" && finiteList(o.pt, 2); }
  function pointOk(p) { return finiteList(p, 2); }
  function drawObstacles(ctx, list) {
    for (var i = 0; i < list.length; i++) {
      var r = list[i].rect, st = OBS_STYLE[list[i].kind] || OBS_STYLE.other, k;
      ctx.save();
      ctx.fillStyle = "rgba(160,160,160,0.18)"; ctx.fillRect(r[0], r[1], r[2], r[3]);
      ctx.beginPath(); ctx.rect(r[0], r[1], r[2], r[3]); ctx.clip();
      ctx.strokeStyle = st[0]; ctx.lineWidth = 1; ctx.beginPath();
      for (k = -r[3]; k < r[2]; k += 6) { ctx.moveTo(r[0] + k, r[1] + r[3]); ctx.lineTo(r[0] + k + r[3], r[1]); }
      ctx.stroke(); ctx.restore();
      ctx.strokeStyle = st[0]; ctx.lineWidth = 1.5; ctx.strokeRect(r[0], r[1], r[2], r[3]);
    }
  }
  function drawJoint(ctx, j) {
    var x = j.pt[0], y = j.pt[1], s = 5;
    ctx.lineWidth = 1.5; ctx.strokeStyle = "#111"; ctx.fillStyle = "#fff"; ctx.beginPath();
    if (j.kind === "elbow") { ctx.moveTo(x, y - s); ctx.lineTo(x + s, y); ctx.lineTo(x, y + s); ctx.lineTo(x - s, y); ctx.closePath(); ctx.fill(); ctx.stroke(); }
    else if (j.kind === "tee") { ctx.moveTo(x, y - s); ctx.lineTo(x + s, y + s); ctx.lineTo(x - s, y + s); ctx.closePath(); ctx.fill(); ctx.stroke(); }
    else if (j.kind === "cross") {
      ctx.rect(x - s, y - s, 2 * s, 2 * s); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - s, y); ctx.lineTo(x + s, y); ctx.moveTo(x, y - s); ctx.lineTo(x, y + s); ctx.stroke();
    } else if (j.kind === "union") { ctx.arc(x, y, s - 1, 0, 2 * Math.PI); ctx.fill(); ctx.stroke(); }
    else {
      ctx.strokeStyle = "#b40000"; ctx.rect(x - s, y - s, 2 * s, 2 * s); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - s, y - s); ctx.lineTo(x + s, y + s); ctx.moveTo(x + s, y - s); ctx.lineTo(x - s, y + s); ctx.stroke();
    }
  }
  function legendText(obs, hs, jt) {
    var parts = ["圖例：藍線＝橋架路徑（俯視，垂直段顯示為點）", "綠方塊＝起點", "紅圓點＝終點"], seen = {}, kinds = [], i;
    if (obs.length) {
      for (i = 0; i < obs.length; i++) {
        var st = OBS_STYLE[obs[i].kind] || OBS_STYLE.other;
        if (!seen[st[1]]) { seen[st[1]] = true; kinds.push(st[1]); }
      }
      parts.push("斜線框＝障礙物俯視投影（不分高度；依類型著色：" + kinds.join("、") + "）");
    }
    if (hs.length) parts.push("黑色 ×＝吊架");
    if (jt.length) {
      seen = {}; kinds = [];
      for (i = 0; i < jt.length; i++) {
        var lb = JOINT_LABEL[jt[i].kind] || JOINT_LABEL.unsupported;
        if (!seen[lb]) { seen[lb] = true; kinds.push(lb); }
      }
      parts.push("接頭：" + kinds.join("、"));
    }
    return parts.join("；") + "；比例尺單位 m。";
  }
  function drawPreview(box, d) {
    var pv = d.preview;
    box.appendChild(text("h3", "XY 平面示意"));
    if (!previewOk(pv)) {
      box.appendChild(text("p", typeof d.preview_error === "string" ? d.preview_error : "無法顯示平面示意圖；請以下載檔與報告為準。", "hint"));
      return;
    }
    box.appendChild(text("p", d.preview.caption, "hint"));
    var obs = layer(pv.obstacles, obstacleOk), hs = layer(pv.hangers, pointOk), jt = layer(pv.joints, jointOk);
    var cv = document.createElement("canvas");
    cv.id = "preview"; cv.width = pv.size[0]; cv.height = pv.size[1];
    cv.setAttribute("role", "img"); cv.setAttribute("aria-label", "XY 平面示意（" + pv.caption + "）");
    var ctx = null;
    try { ctx = cv.getContext("2d"); } catch (e) { ctx = null; }
    if (!ctx) { box.appendChild(text("p", "此瀏覽器無法繪製示意圖；請以下載檔與報告為準。", "hint")); return; }
    try {
      var rm = pv.room, sg = pv.segments, sb = pv.scale_bar, k;
      ctx.strokeStyle = "#111"; ctx.fillStyle = "#111"; ctx.lineWidth = 1; ctx.strokeRect(rm[0], rm[1], rm[2], rm[3]);
      drawObstacles(ctx, obs);
      ctx.strokeStyle = "#0050b4"; ctx.lineWidth = 3; ctx.lineCap = "round";
      ctx.fillStyle = "#0050b4";
      for (k = 0; k < sg.length; k++) {
        ctx.beginPath();
        if (sg[k][0] === sg[k][2] && sg[k][1] === sg[k][3]) { ctx.arc(sg[k][0], sg[k][1], 3, 0, 2 * Math.PI); ctx.fill(); continue; }
        ctx.moveTo(sg[k][0], sg[k][1]); ctx.lineTo(sg[k][2], sg[k][3]); ctx.stroke();
      }
      ctx.strokeStyle = "#111"; ctx.lineWidth = 1.5; ctx.lineCap = "butt"; ctx.beginPath();
      for (k = 0; k < hs.length; k++) {
        ctx.moveTo(hs[k][0] - 3, hs[k][1] - 3); ctx.lineTo(hs[k][0] + 3, hs[k][1] + 3);
        ctx.moveTo(hs[k][0] + 3, hs[k][1] - 3); ctx.lineTo(hs[k][0] - 3, hs[k][1] + 3);
      }
      ctx.stroke();
      for (k = 0; k < jt.length; k++) drawJoint(ctx, jt[k]);
      ctx.font = "12px sans-serif"; ctx.lineWidth = 1; ctx.strokeStyle = "#111";
      if (pv.start) {
        ctx.fillStyle = "#0a7a2f"; ctx.fillRect(pv.start[0] - 5, pv.start[1] - 5, 10, 10);
        ctx.fillStyle = "#111"; ctx.fillText("起點", pv.start[0] + 7, pv.start[1] - 7);
      }
      for (k = 0; pv.ends && k < pv.ends.length; k++) {
        ctx.fillStyle = "#b40000"; ctx.beginPath(); ctx.arc(pv.ends[k][0], pv.ends[k][1], 5, 0, 2 * Math.PI); ctx.fill();
        ctx.fillStyle = "#111"; ctx.fillText("終點" + (k + 1), pv.ends[k][0] + 7, pv.ends[k][1] + 14);
      }
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(sb.x, sb.y); ctx.lineTo(sb.x + sb.length_px, sb.y);
      ctx.moveTo(sb.x, sb.y - 4); ctx.lineTo(sb.x, sb.y + 4);
      ctx.moveTo(sb.x + sb.length_px, sb.y - 4); ctx.lineTo(sb.x + sb.length_px, sb.y + 4); ctx.stroke();
      ctx.fillText(sb.label, sb.x, sb.y + 16);
      ctx.fillText(pv.caption, pv.size[0] - 8 - ctx.measureText(pv.caption).width, pv.size[1] - 8);
    } catch (e2) {
      box.appendChild(text("p", "繪製示意圖時發生問題；請以下載檔與報告為準。", "hint"));
      return;
    }
    box.appendChild(cv);
    box.appendChild(text("p", legendText(obs, hs, jt), "hint"));
    var notes = Array.isArray(pv.notes) ? pv.notes : [];
    for (var n = 0; n < notes.length && n < 10; n++) if (typeof notes[n] === "string") box.appendChild(text("p", notes[n], "hint"));
  }

  function renderResult(d) {
    var box = $("results"); clear(box);
    for (var i = 0; i < d.banners.length; i++) box.appendChild(text("div", d.banners[i], "banner"));
    var s = d.summary.stats;
    var t = document.createElement("table"), rows = [
      ["版本編號", d.run_id], ["線段數", s.segments], ["總長 (m)", s.length_m], ["接頭數", s.joints],
      ["吊架數", s.hangers], ["轉彎數", s.bends], ["規範值未驗證的檢查項", s.unverified_checks]];
    for (var r = 0; r < rows.length; r++) {
      var tr = document.createElement("tr"); tr.appendChild(text("th", rows[r][0])); tr.appendChild(text("td", String(rows[r][1]))); t.appendChild(tr);
    }
    box.appendChild(t);
    drawPreview(box, d);
    var h = text("h3", "下載"); box.appendChild(h);
    var ul = document.createElement("ul"); ul.className = "files";
    for (var k = 0; k < d.files.length; k++) {
      var li = document.createElement("li"), a = document.createElement("a");
      a.href = d.files[k].href; a.textContent = d.files[k].label + "（" + d.files[k].name + "）";
      li.appendChild(a); if (d.files[k].sha8) li.appendChild(text("span", " SHA256 " + d.files[k].sha8 + "…", "mono"));
      ul.appendChild(li);
    }
    box.appendChild(ul);
    if (d.diff_href) {
      var p = document.createElement("p"), da = document.createElement("a");
      da.href = d.diff_href; da.textContent = "與前一版差異報告"; p.appendChild(da); box.appendChild(p);
    }
    status("完成：" + d.run_id);
    loadVersions();
  }

  function renderJobError(e) {
    var box = $("results"); clear(box);
    box.appendChild(text("p", "✖ " + e.title, "err"));
    box.appendChild(text("p", e.hint, "hint"));
    box.appendChild(text("p", "錯誤代碼：" + e.stage + "/" + e.code + (e.message ? "（" + e.message + "）" : ""), "mono"));
    status("失敗：" + e.title);
  }

  function poll(jid, n) {
    if (n > 600) { setBusy(false); status("等待逾時（10 分鐘）；工作可能仍在伺服器執行，請稍後重新整理。"); return; }
    fetch("api/jobs/" + jid, {headers: {"Accept": "application/json"}}).then(function (r) { return r.json(); }).then(function (d) {
      if (d.state === "running") { status("計算中…（" + n + " 秒）"); setTimeout(function () { poll(jid, n + 1); }, 1000); return; }
      setBusy(false);
      if (d.state === "done") renderResult(d); else renderJobError(d.error);
    }).catch(function () { setBusy(false); status("無法取得工作狀態"); });
  }

  function setBusy(b) { busy = b; var btn = $("run"); btn.disabled = b; btn.setAttribute("aria-busy", b ? "true" : "false"); }

  function loadVersions() {
    fetch("api/versions").then(function (r) { return r.json(); }).then(function (d) {
      var ul = $("versions"); clear(ul);
      for (var i = 0; i < d.versions.length; i++) {
        var v = d.versions[i], li = document.createElement("li");
        li.appendChild(text("span", v.run_id, "mono"));
        li.appendChild(text("span", v.error ? "（無法讀取：" + v.error.code + "）" : "（" + v.created_at + "）"));
        ul.appendChild(li);
      }
      $("versions-empty").hidden = d.versions.length > 0;
    }).catch(function () {});
  }

  $("obstacle_file").addEventListener("change", function (ev) {
    var f = ev.target.files[0], out = $("obstacle_status"); obstacles = null; obstaclesBad = false; out.textContent = "";
    ev.target.removeAttribute("aria-invalid");
    if (!f) return;
    if (f.size > 262144) { obstaclesBad = true; out.textContent = "✖ 檔案超過 256 KB"; ev.target.setAttribute("aria-invalid", "true"); return; }
    var rd = new FileReader();
    rd.onload = function () {
      try {
        var j = JSON.parse(rd.result);
        if (j && !Array.isArray(j) && Array.isArray(j.obstacles)) j = j.obstacles;
        if (!Array.isArray(j)) throw new Error("需為清單");
        if (j.length > 200) throw new Error("最多 200 個障礙物");
        obstacles = j; out.textContent = "已載入 " + j.length + " 個障礙物";
      } catch (e) { obstaclesBad = true; out.textContent = "✖ 不是合法的障礙物 JSON：" + e.message; ev.target.setAttribute("aria-invalid", "true"); }
    };
    rd.onerror = function () { obstaclesBad = true; out.textContent = "✖ 讀取檔案失敗"; ev.target.setAttribute("aria-invalid", "true"); };
    rd.readAsText(f);
  });

  $("form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (busy) return;
    clearErrors(); clear($("results"));
    // 用狀態變數判斷（clearErrors 已清掉 aria-invalid；壞檔案不得被靜默忽略後照常執行）
    if (obstaclesBad) { showError("障礙物檔案有問題，請更正或移除後再執行（目前不會送出）。", "obstacles"); return; }
    var req = buildRequest();
    if (!req.codes.length) { showError("請至少勾選一套規範。", "codes"); return; }
    setBusy(true); status("送出中…");
    fetch("api/run", {method: "POST", headers: {"Content-Type": "application/json", "Accept": "application/json"},
                      body: JSON.stringify(req)})
      .then(function (r) { return r.json().then(function (d) { return {status: r.status, d: d}; }); })
      .then(function (x) {
        if (x.status === 202) { poll(x.d.job_id, 1); return; }
        setBusy(false); status("未執行");
        var e = x.d.error || {message: "未知錯誤"}; showError(e.message, e.field);
      }).catch(function () { setBusy(false); status("無法連線到伺服器"); });
  });
  loadVersions();
})();
""".strip()


def render_index(token: str, rules: dict) -> str:
    """頁面。所有控制項都用明確的 <label for=id> 對應（不用「包住輸入框」的標籤），且名稱唯一，
    螢幕閱讀器才不會唸出一堆重複的「X (m)」或只有值的名稱（瀏覽器實測抓到的問題）。"""
    def esc(s):
        return html.escape(str(s), quote=True)

    def field(i, label, control):
        return f'<span class="field"><label for="{i}">{esc(label)}</label>{control}</span>'

    def num(i, label, val, step="any"):
        return field(i, label, f'<input id="{i}" name="{i}" type="number" step="{step}" inputmode="decimal" value="{esc(val)}">')

    def xyz(prefix, title, vals):
        cells = "".join(num(f"{prefix}_{a}", f"{title} {a.upper()} (m)", v) for a, v in zip("xyz", vals))
        return f'<div class="row" role="group" aria-label="{esc(title)}">{cells}</div>'

    codes = "".join(
        f'<div class="choice"><input type="checkbox" id="code_{esc(k)}" name="code" value="{esc(k)}" checked>'
        f'<label for="code_{esc(k)}">{esc(k)}：{esc(v["name"])}</label></div>'
        for k, v in rules["codes"].items())
    ends = "".join(f'<p class="hint">終點 {i + 1}{"（必填）" if i == 0 else "（選填，留空略過）"}</p>'
                   + xyz(f"ends{i}", f"終點 {i + 1}", ((10, 1, 3) if i == 0 else ("", "", "")))
                   for i in range(MAX_END_POINTS))
    cells = "".join(f'<option value="{c:g}"{" selected" if c == 0.25 else ""}>{c:g} m</option>' for c in CELLS)
    kinds = ('<span class="choice"><input type="radio" name="tray_kind" id="tray_kind_power" value="power" checked>'
             '<label for="tray_kind_power">電力橋架</label></span>'
             '<span class="choice"><input type="radio" name="tray_kind" id="tray_kind_signal" value="signal">'
             '<label for="tray_kind_signal">訊號橋架</label></span>')
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MEP 電纜橋架智慧設計工具</title><link rel="stylesheet" href="app.css"></head>
<body><h1>MEP 電纜橋架智慧設計與衝突偵測</h1>
<noscript><p class="err">此頁面需要啟用 JavaScript 才能執行。</p></noscript>
<form id="form" novalidate>
<section aria-labelledby="h-size"><h2 id="h-size">① 尺寸輸入</h2>
<div id="form-errors" role="alert"></div>
<fieldset><legend>房間（公尺）</legend>{xyz("room", "房間", (12, 6, 4))}</fieldset>
<fieldset><legend>橋架</legend><div class="row">{num("tray_width_mm", "橋架寬 (mm)", 300)}{num("tray_height_mm", "橋架高 (mm)", 100)}
<div role="radiogroup" aria-label="橋架類型" class="row">{kinds}</div></div></fieldset>
<fieldset><legend>起點與終點（公尺）</legend><p class="hint">起訖座標需為格距的整數倍。</p>
<p class="hint">起點</p>{xyz("start", "起點", (1, 1, 3))}{ends}</fieldset>
<fieldset><legend>電纜（選填；不填則以預設電纜 Ø20 mm × 10 條計算並在報告揭露）</legend><div class="row">
{num("cable_od_mm", "電纜外徑 (mm)", "")}{num("cable_count", "電纜條數", "", step="1")}</div></fieldset>
<fieldset><legend>格距與障礙物</legend><div class="row">
{field("cell_m", "格距", f'<select id="cell_m" name="cell_m">{cells}</select>')}
{field("obstacle_file", "障礙物 JSON 檔（選填；不上傳＝空房間）", '<input id="obstacle_file" type="file" accept=".json,application/json" aria-describedby="obstacle_status">')}</div>
<p id="obstacle_status" class="hint" role="status"></p>
<p class="hint">0.25 m 約數秒；0.1 m 可能需數十秒；0.05 m 僅小房間可選。</p></fieldset></section>
<section aria-labelledby="h-codes"><h2 id="h-codes">② 規範勾選</h2>
<fieldset id="codes" tabindex="-1"><legend>採用的規範（自動採用最嚴格條件）</legend>{codes}</fieldset>
<p class="banner">{esc(BANNER_FIXED)}</p></section>
<section aria-labelledby="h-run"><h2 id="h-run">③ 執行</h2>
<button type="submit" id="run" aria-busy="false">執行</button>
<p id="status" role="status" aria-live="polite">尚未執行</p></section></form>
<section aria-labelledby="h-res"><h2 id="h-res">④ 結果下載</h2>
<div id="results" aria-live="polite"></div>
<h3>最近的版本</h3><ul id="versions"></ul><p id="versions-empty">尚無版本。</p></section>
<script src="app.js"></script></body></html>
"""


# ───────────────────────── HTTP 伺服器 ─────────────────────────
class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, token: str, make_dwg: bool = True, host: str = HOST):
        if host != HOST:
            raise ValueError("只允許綁定 127.0.0.1（本機）")
        super().__init__((host, port), Handler)
        self.port = self.server_address[1]
        self.token = token
        self.jobs = JobManager(token, make_dwg)
        self.rules = load_rules()
        self.index = render_index(token, self.rules).encode("utf-8")
        self._sem = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.log_lines: deque = deque(maxlen=500)
        self.allowed_hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{HOST}:{self.port}", f"http://localhost:{self.port}"}

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/t/{self.token}/"

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):                  # 連線數上限：超過直接 503 並關閉
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                # 先半關閉寫入端並短暫讀掉客戶端還在送的資料，再關閉：直接 close 在 Windows 會送 RST，
                # 503 可能在被客戶端讀到之前就被丟棄（整套測試負載下實測偶發）。
                request.shutdown(socket.SHUT_WR)
                request.settimeout(0.3)
                try:
                    request.recv(65536)
                except OSError:
                    pass
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()

    _serving = False

    def serve_forever(self, poll_interval: float = 0.5):
        self._serving = True
        try:
            super().serve_forever(poll_interval)
        finally:
            self._serving = False

    def close(self) -> str | None:
        """關閉伺服器並回傳仍在執行的工作 id（結果不會保留）。serve_forever 尚未開始時 shutdown() 會永遠等待，
        所以只在服務迴圈確實在跑時才呼叫它。"""
        running = self.jobs.running()
        if self._serving:
            self.shutdown()
        self.server_close()
        return running


class Handler(BaseHTTPRequestHandler):
    server: WebServer
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT_S
    server_version = "MEPTray"
    sys_version = ""

    # ── 日誌：不含 token、不含 body、不含查詢字串 ──
    def log_message(self, fmt, *args):
        line = (fmt % args)
        tok = self.server.token
        self.server.log_lines.append(line.replace(tok, "<token>"))

    def log_request(self, code="-", size="-"):
        path = urlsplit(self.path).path.replace(self.server.token, "<token>")
        self.server.log_lines.append(f"{self.command} {path[:120]} {code}")

    # ── 回應 ──
    def _send(self, status, body: bytes, ctype="application/json; charset=utf-8", csp=None, extra=None,
              head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", csp or UI_CSP)
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _error(self, e: ApiError):
        self._json(e.status, e.body())

    def _not_found(self):
        self._send(404, b'{"error":{"code":"not_found","field":null,"message":"not found"}}')

    # ── 通用檢查 ──
    def _guard(self, is_post: bool) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.allowed_hosts:
            self._json(403, {"error": {"code": "bad_host", "field": None, "message": "Host 不被允許"}})
            return False
        sfs = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if sfs == "cross-site":
            self._json(403, {"error": {"code": "cross_site", "field": None, "message": "不允許跨站請求"}})
            return False
        origin = self.headers.get("Origin")
        if is_post and origin is None or (origin is not None and origin not in self.server.allowed_origins):
            self._json(403, {"error": {"code": "bad_origin", "field": None, "message": "Origin 不被允許"}})
            return False
        return True

    def _route(self):
        """回傳 token 之後的路徑片段 list，或 None（已回 404）。token 錯誤與不存在的路由回應相同。"""
        path = urlsplit(self.path).path
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "" or parts[1] != "t" or not hmac.compare_digest(
                parts[2].encode("utf-8"), self.server.token.encode("utf-8")):
            self._not_found()
            return None
        return parts[3:]

    def _reject_method(self):
        self._send(405, b'{"error":{"code":"method_not_allowed","field":null,"message":"method not allowed"}}',
                   extra={"Allow": "GET, POST"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = lambda self: self._reject_method()

    # ── GET ──
    def do_GET(self):
        if not self._guard(False):
            return
        rest = self._route()
        if rest is None:
            return
        try:
            if rest in ([""], []):
                return self._send(200, self.server.index, "text/html; charset=utf-8")
            if rest == ["app.js"]:
                return self._send(200, APP_JS.encode("utf-8"), "application/javascript; charset=utf-8")
            if rest == ["app.css"]:
                return self._send(200, APP_CSS.encode("utf-8"), "text/css; charset=utf-8")
            if rest == ["api", "versions"]:
                scan = V.scan_versions()
                return self._json(200, {"versions": recent_versions(), "ignored": len(scan.ignored),
                                        "stage_leftovers": scan.stage_leftovers})
            if len(rest) == 3 and rest[:2] == ["api", "jobs"] and JOB_ID_RE.match(rest[2]):
                st = self.server.jobs.status(rest[2])
                return self._json(200, st) if st is not None else self._not_found()
            if len(rest) == 3 and rest[0] == "download":
                return self._download(unquote(rest[1]), unquote(rest[2]))
            if len(rest) == 3 and rest[0] == "diff":
                return self._diff(unquote(rest[1]), unquote(rest[2]))
        except ApiError as e:
            return self._error(e)
        self._not_found()

    def _download(self, run_id: str, kind: str):
        if kind not in KIND_ORDER or not RUN_ID_RE.match(run_id):
            return self._not_found()
        lr = V.load_version(run_id, verify_files=False)
        if not lr.ok:
            return self._not_found()
        ent = lr.manifest["files"].get(kind) if kind != "manifest" else {"name": V.MANIFEST_NAME}
        if not ent:
            return self._not_found()
        d = output_root() / run_id
        fp = d / ent["name"]
        if not fp.is_file() or fp.is_symlink() or fp.resolve().parent != d.resolve():
            return self._not_found()
        data = fp.read_bytes()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", ent["name"])
        disp = f'{"inline" if kind == "report" else "attachment"}; filename="{safe}"'
        self._send(200, data, KIND_TYPE[kind], DOWNLOAD_CSP, {"Content-Disposition": disp})

    def _diff(self, a: str, b: str):
        if not (RUN_ID_RE.match(a) and RUN_ID_RE.match(b)):
            return self._not_found()
        diff = V.compare(a, b)
        doc = RP.render_diff_report(diff, a, b).encode("utf-8")
        self._send(200, doc, "text/html; charset=utf-8", DOWNLOAD_CSP,
                   {"Content-Disposition": 'inline; filename="diff.html"'})

    # ── POST ──
    def do_POST(self):
        if not self._guard(True):
            return
        rest = self._route()
        if rest is None:
            return
        if rest != ["api", "run"]:
            return self._not_found()
        try:
            body = self._read_body()
            inp, codes = validate_request(parse_json_body(body), self.server.rules)
            jid = self.server.jobs.submit(inp, codes)
        except ApiError as e:
            return self._error(e)
        self._json(202, {"job_id": jid})

    def _read_body(self) -> bytes:
        h = self.headers
        if h.get("Transfer-Encoding") is not None:
            raise ApiError(400, "bad_request", "不支援 Transfer-Encoding")
        if h.get("Expect") is not None:
            raise ApiError(417, "expectation_failed", "不支援 Expect")
        ctype = (h.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "unsupported_media_type", "Content-Type 必須是 application/json")
        cls = h.get_all("Content-Length") or []
        if not cls:
            raise ApiError(411, "length_required", "需要 Content-Length")
        if len(cls) != 1 or not re.fullmatch(r"[0-9]{1,9}", cls[0].strip()):
            raise ApiError(400, "bad_content_length", "Content-Length 不合法")
        n = int(cls[0])
        if n > MAX_BODY:
            raise ApiError(413, "payload_too_large", f"請求本文超過 {MAX_BODY // 1024} KB")
        try:
            data = self.rfile.read(n)
        except (TimeoutError, socket.timeout, OSError):
            raise ApiError(400, "body_timeout", "讀取請求本文逾時")
        if len(data) != n:
            raise ApiError(400, "short_body", "請求本文比 Content-Length 短")
        return data                                               # 多出的位元組不會被當成下一個請求（每連線只處理一個請求）


# ───────────────────────── 啟動 ─────────────────────────
def make_server(port: int | None = None, make_dwg: bool = True, host: str = HOST) -> WebServer:
    token = secrets.token_urlsafe(32)
    if port == 0:
        return WebServer(0, token, make_dwg, host)
    try:
        return WebServer(DEFAULT_PORT if port is None else port, token, make_dwg, host)
    except OSError:                                               # 埠被占用 → 讓 OS 指定空埠
        return WebServer(0, token, make_dwg, host)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MEP 橋架工具的本機網頁介面（只綁 127.0.0.1）")
    ap.add_argument("--port", type=int, default=None, help=f"偏好埠（預設 {DEFAULT_PORT}；被占用則自動改用空埠）")
    ap.add_argument("--open", action="store_true", help="啟動後用預設瀏覽器開啟（預設不開，避免 token 進入非預期的瀏覽器）")
    a = ap.parse_args(argv)
    srv = make_server(a.port)
    left = V.scan_versions().stage_leftovers
    print(f"MEP 橋架工具已啟動（僅本機）：{srv.url}")
    print("Ctrl+C 結束。輸出資料夾：", sanitize_text(str(output_root())))
    if left:
        print(f"注意：輸出資料夾有 {left} 個 .stage- 暫存殘留（通常是被中斷的執行），可手動刪除。")
    if a.open:
        webbrowser.open(srv.url)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        running = srv.close()
        print("\n已關閉。" + (f" 仍有工作在執行（{running}），結果不會保留。" if running else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
