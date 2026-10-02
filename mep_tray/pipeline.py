"""單一入口管線：Inputs → 路徑 → 檢查 → DXF/DWG → Revit JSON。網頁、版本管理、報告共用。

只回資料（RunResult），不碰 UI。

錯誤處理分界：
- 轉成結構化錯誤（ok=False，不拋）：使用者可修正或環境類的預期失敗——輸入/參數驗證、規範代號與 rules 驗證、
  路徑規劃失敗（RoutingError）、run_id 不合法或已存在、輸出時的 OSError。
- 直接拋出：其餘一律視為程式錯誤（TypeError/KeyError/AssertionError…），不吞。
輸出資料夾：所有運算在記憶體完成後才以 mkdir（不允許已存在）原子「認領」output/<run_id>/；
本次呼叫認領的資料夾，在任何階段失敗（含直接拋出的例外）都整個移除；既有資料夾絕不碰（舊版本不可改）。
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import export_dxf as X
from . import export_revit as R
from .clash import check_route
from .compliance import check_compliance
from .disclosure import base_notes
from .model import Inputs
from .paths import check_run_id, output_root, run_dir
from .router import Route, place_hangers, try_route_tray
from .rules import load_rules, merge_strictest, validate_rules

DEFAULT_SPAN_M = 2.0
DEFAULT_SPAN_NOTE = "所選規範均未規定吊架間距，採預設 2.0 m（非規範值）"
MAX_TYPE_NAME = 200

# 同一行程內同時最多一個外部 CAD（AutoCAD/ODA）；上層（webui）另有「同時只跑一個工作」。
_DWG_LOCK = threading.Lock()


@dataclass(frozen=True)
class PipelineError:
    stage: str      # validate | rules | route | output | io
    code: str       # 穩定的機器可讀代碼
    message: str    # 給人看的說明

    def to_dict(self) -> dict:
        return {"stage": self.stage, "code": self.code, "message": self.message}


@dataclass
class RunResult:
    ok: bool
    run_id: str
    error: PipelineError | None
    inputs: Inputs
    codes: list
    gov: dict | None = None
    route: Route | None = None
    reports: dict = field(default_factory=dict)          # {"clash": Report, "compliance": Report}
    files: dict = field(default_factory=lambda: {"dxf": None, "dwg": None, "revit_json": None})
    dwg_note: str = ""
    acad_audit: int | None = None
    disclosures: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_summary_dict(self) -> dict:
        """可 json.dumps(allow_nan=False) 的摘要；檔案只給檔名（相對 output/<run_id>/），不洩漏本機絕對路徑。"""
        return {
            "ok": self.ok, "run_id": self.run_id,
            "error": None if self.error is None else self.error.to_dict(),
            "codes": list(self.codes),
            "files": {k: (None if v is None else Path(v).name) for k, v in self.files.items()},
            "dwg_note": self.dwg_note, "acad_audit": self.acad_audit,
            "disclosures": list(self.disclosures), "stats": self.stats,
        }


def _fail(run_id, inp, codes, stage, code, message, gov=None) -> RunResult:
    return RunResult(False, run_id, PipelineError(stage, code, message), inp, list(codes), gov)


def _remove_claimed(d: Path) -> bool:
    """移除本次呼叫認領的資料夾。只在 resolve 後位於輸出根目錄之內且不是根目錄本身時才刪。"""
    root = output_root()
    try:
        r = d.resolve()
        if r == root or not r.is_relative_to(root) or r.parent != root:
            return False
        shutil.rmtree(r)
        return True
    except OSError:
        return False


def _stats(route: Route, reports: dict, model: dict | None) -> dict:
    allf = [f for rep in reports.values() for f in rep.findings]
    allc = [c for rep in reports.values() for c in rep.checks]
    return {
        "segments": len(route.segments),
        "joints": len(model["joints"]) if model else 0,
        "length_m": round(route.length_m, 3),
        "hangers": len(route.hangers),
        "bends": len(route.bends),
        "findings_by_kind_status": dict(sorted(Counter(f"{f.kind}/{f.status}" for f in allf).items())),
        "unverified_checks": sum(1 for c in allc if c.status == "UNVERIFIED"),
    }


def run(inputs: Inputs, codes: Sequence[str], run_id: str, *, type_name: str | None = None,
        basis: str = "UNSPECIFIED", make_dwg: bool | None = True, rules: dict | None = None,
        notes: Sequence[str] = ()) -> RunResult:
    """執行完整管線。make_dwg: True/None=偵測到 AutoCAD/ODA 才轉 DWG；False=絕不啟動外部程式。"""
    codes = list(codes)
    inp = dataclasses.replace(inputs, codes=list(codes))      # 副本，不改呼叫端物件

    # ── validate（全在記憶體，尚未碰磁碟）──
    try:
        check_run_id(run_id)
        d = run_dir(run_id, create=False)
    except ValueError as e:
        return _fail(run_id, inp, codes, "validate", "invalid_run_id", str(e))
    if os.path.lexists(d):
        return _fail(run_id, inp, codes, "output", "run_exists", f"run_id 已存在，拒絕覆寫: {run_id}")
    if basis not in R.BASES:
        return _fail(run_id, inp, codes, "validate", "invalid_basis", f"basis 需為 {R.BASES}")
    if type_name is not None and (not isinstance(type_name, str) or not type_name.strip()
                                  or len(type_name) > MAX_TYPE_NAME
                                  or any(ord(c) < 32 for c in type_name)):
        return _fail(run_id, inp, codes, "validate", "invalid_type_name", "type_name 需為非空字串（<=200，無控制字元）")
    if not codes:
        return _fail(run_id, inp, codes, "validate", "no_codes", "至少需勾選一套規範")
    try:
        inp.validate()
    except (ValueError, KeyError, TypeError) as e:
        return _fail(run_id, inp, codes, "validate", "invalid_input", f"輸入驗證失敗: {e}")

    # ── rules ──
    try:
        rules_d = validate_rules(rules) if rules is not None else load_rules()
        gov = merge_strictest(codes, rules_d)
    except ValueError as e:
        return _fail(run_id, inp, codes, "rules", "invalid_rules", str(e))

    # ── route ──
    res = try_route_tray(inp.room_box(), inp.start, inp.ends, inp.obstacle_objs(),
                         inp.tray_w_mm / 1000, inp.tray_h_mm / 1000, inp.tray_type, gov, cell=inp.cell_m)
    if not res.ok:
        return _fail(run_id, inp, codes, "route", res.code or "route_error", res.error, gov)
    route = res.route

    extra = list(notes)
    span = gov["span_max_m"].value if "span_max_m" in gov else None
    if span is None:
        span = DEFAULT_SPAN_M
        extra.insert(0, DEFAULT_SPAN_NOTE)
    place_hangers(route, span)

    reports = {"clash": check_route(inp, route, gov), "compliance": check_compliance(inp, route, gov)}
    rep_list = [reports["clash"], reports["compliance"]]
    model = R.build_model(inp, route, rep_list, gov, run_id, type_name=type_name, basis=basis, notes=extra)
    disclosures = base_notes(inp, gov, rep_list, extra)

    # ── output：原子認領資料夾後才寫檔 ──
    claimed, ok = False, False
    try:
        try:
            d.mkdir(parents=True)                    # exist_ok=False：不存在才建，同時是跨行程的認領
        except FileExistsError:
            return _fail(run_id, inp, codes, "output", "run_exists", f"run_id 已存在，拒絕覆寫: {run_id}", gov)
        claimed = True
        convert = make_dwg is not False
        if convert:
            with _DWG_LOCK:
                dxf = X.export_dxf(inp, route, rep_list, gov, run_id, notes=extra, convert_dwg=True)
        else:
            dxf = X.export_dxf(inp, route, rep_list, gov, run_id, notes=extra, convert_dwg=False)
        jpath = R.write_model(model, run_id)
        ok = True
        return RunResult(True, run_id, None, inp, codes, gov, route, reports,
                         {"dxf": dxf.dxf, "dwg": dxf.dwg, "revit_json": jpath},
                         dxf.dwg_note, dxf.acad_audit, disclosures, _stats(route, reports, model))
    except FileExistsError as e:
        return _fail(run_id, inp, codes, "output", "file_exists", str(e), gov)
    except OSError as e:
        return _fail(run_id, inp, codes, "io", "os_error", f"{type(e).__name__}: {e}", gov)
    finally:
        if claimed and not ok:
            _remove_claimed(d)
