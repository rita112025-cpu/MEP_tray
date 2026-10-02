"""單一入口管線：Inputs → 路徑 → 檢查 → DXF/DWG → Revit JSON。網頁、版本管理、報告共用。

只回資料（RunResult），不碰 UI。

錯誤處理分界（只在具體呼叫點捕捉，不用大 try 包整段）：
- 轉成結構化錯誤（ok=False，不拋）：Inputs.validate() 的 ValueError、規範代號/rules 驗證的 ValueError、
  run_id 檢查的 ValueError、路徑規劃失敗（try_route_tray 已回結構化結果）、輸出階段的 OSError、
  run_id 已存在。
- 直接拋出：其餘一切（含非上述呼叫點拋出的 ValueError）都是程式錯誤，不吞。

輸出的原子性（取代「先檢查、後建立、失敗 rmtree」的競態做法）：
所有運算在記憶體完成 → 在輸出根目錄下以 mkdtemp 建立本次專屬的暫存資料夾 → 全部檔案寫進
<暫存>/<run_id>/ → 全部成功才 os.rename 成 output/<run_id>/。Windows 上目的地已存在時 rename 會失敗，
即為原子的「認領」；失敗時只刪自己的暫存資料夾（刪除前確認：真實目錄非 symlink/junction、resolve 後
是輸出根目錄的直接子層、名稱符合暫存前綴；失敗不靜默，殘留路徑回報在錯誤訊息或例外 note）。
既有的 output/<run_id>/ 絕不被修改或刪除。（POSIX 上 rename 會覆蓋同名空目錄，前面有 lexists 檢查縮小視窗。）
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
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
from .sanitize import relative_to_output, sanitize_text

DEFAULT_SPAN_M = 2.0
DEFAULT_SPAN_NOTE = "所選規範均未規定吊架間距，採預設 2.0 m（非規範值）"
MAX_TYPE_NAME = 200
STAGE_PREFIX = ".stage-"     # 以 . 開頭，不符合 run_id 規則，版本列舉時會被忽略

# 同一行程內同時最多一個外部 CAD（AutoCAD/ODA）；上層（webui）另有「同時只跑一個工作」。
_DWG_LOCK = threading.Lock()


@dataclass(frozen=True)
class PipelineError:
    stage: str      # validate | rules | route | output | io
    code: str       # 穩定的機器可讀代碼
    message: str    # 給人看的說明（已脫敏：不含本機路徑）
    cleanup_failed: bool = False   # True=暫存資料夾未能清除（殘留會佔用磁碟，但不影響同 run_id 重跑，因為只有 rename 成功才認領）

    def to_dict(self) -> dict:
        return {"stage": self.stage, "code": self.code, "message": self.message,
                "cleanup_failed": self.cleanup_failed}


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
        """可 json.dumps(allow_nan=False) 的摘要；檔案為相對輸出根目錄的路徑（<run_id>/檔名），不含本機絕對路徑。"""
        return {
            "ok": self.ok, "run_id": self.run_id,
            "error": None if self.error is None else self.error.to_dict(),
            "codes": list(self.codes),
            "files": {k: relative_to_output(v) for k, v in self.files.items()},
            "dwg_note": self.dwg_note, "acad_audit": self.acad_audit,
            "disclosures": list(self.disclosures), "stats": self.stats,
        }


def _scrub(obj):
    """遞迴脫敏：字串經 sanitize_text，容器逐項處理，其餘原樣。"""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _fail(run_id, inp, codes, stage, code, message, gov=None, residue: str | None = None) -> RunResult:
    msg = sanitize_text(message)
    if residue:
        msg += "；" + sanitize_text(residue)
    return RunResult(False, run_id, PipelineError(stage, code, msg, cleanup_failed=bool(residue)),
                     inp, list(codes), gov)


def _remove_stage(stage: Path) -> str | None:
    """移除本次呼叫專屬的暫存資料夾。回傳 None=已清乾淨（或本來就不存在）；否則回傳殘留說明（不靜默）。"""
    try:
        if not os.path.lexists(stage):
            return None
        if stage.is_symlink() or os.path.isjunction(stage) or not stage.is_dir():
            return f"殘留資料夾未能清除: {stage.name}（拒絕清理：不是一般目錄）"
        r = stage.resolve()
        if r.parent != output_root() or not r.name.startswith(STAGE_PREFIX):
            return f"殘留資料夾未能清除: {stage.name}（拒絕清理：不在預期位置）"
        shutil.rmtree(r)
    except OSError as e:
        return f"殘留資料夾未能清除: {stage.name}（{type(e).__name__}: {e}）"
    return None


def _publish(src: Path, final: Path) -> PipelineError | None:
    """把組裝好的資料夾改名為 output/<run_id>。目的地已存在 → run_exists（既有資料夾不被改動）。"""
    if os.path.lexists(final):
        return PipelineError("output", "run_exists", f"run_id 已存在，拒絕覆寫: {final.name}")
    try:
        os.rename(src, final)
    except OSError:
        if os.path.lexists(final):                       # 競態：別人剛好先認領
            return PipelineError("output", "run_exists", f"run_id 已存在，拒絕覆寫: {final.name}")
        raise
    return None


def _stats(route: Route, reports: dict, model: dict, span_m: float, span_source: str) -> dict:
    allf = [f for rep in reports.values() for f in rep.findings]
    allc = [c for rep in reports.values() for c in rep.checks]
    return {
        "segments": len(route.segments),
        "joints": len(model["joints"]),
        "length_m": round(route.length_m, 3),
        "hangers": len(route.hangers),
        "hanger_span_m": span_m,
        "hanger_span_source": span_source,          # 規範代號，或 "default"（所選規範均未規定）
        "bends": len(route.bends),
        "findings_by_kind_status": dict(sorted(Counter(f"{f.kind}/{f.status}" for f in allf).items())),
        "unverified_checks": sum(1 for c in allc if c.status == "UNVERIFIED"),
    }


def _dedupe(codes) -> list:
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def run(inputs: Inputs, codes: Sequence[str], run_id: str, *, type_name: str | None = None,
        basis: str = "UNSPECIFIED", make_dwg: bool | None = True, rules: dict | None = None,
        notes: Sequence[str] = (), cell_m: float | None = None) -> RunResult:
    """執行完整管線。make_dwg: True/None=偵測到 AutoCAD/ODA 才轉 DWG；False=絕不啟動外部程式。
    codes 去重並保序；大小寫不正規化（未知代號會被明確拒絕，如 "cns"）。
    cell_m 不為 None 時覆寫 inputs.cell_m（呼叫端如 UI 不必改 Inputs 就能控制格距）；None=沿用 inputs.cell_m
    （仍為 None 則依規範淨距自動，自動格距小場景約 5 秒/次；UI 建議預設 0.25）。"""
    if not isinstance(codes, (list, tuple)) or not all(isinstance(c, str) for c in codes):
        inp0 = dataclasses.replace(inputs)
        return _fail(run_id, inp0, [], "validate", "invalid_codes", "codes 需為字串清單")
    codes = _dedupe(codes)
    inp = dataclasses.replace(inputs, codes=list(codes))      # 副本，不改呼叫端物件
    if cell_m is not None:
        inp = dataclasses.replace(inp, cell_m=cell_m)

    # ── validate：只在具體呼叫點捕捉 ──
    try:
        check_run_id(run_id)
        final = run_dir(run_id, create=False)
    except ValueError as e:
        return _fail(run_id, inp, codes, "validate", "invalid_run_id", str(e))
    if os.path.lexists(final):
        return _fail(run_id, inp, codes, "output", "run_exists", f"run_id 已存在，拒絕覆寫: {run_id}")
    if basis not in R.BASES:
        return _fail(run_id, inp, codes, "validate", "invalid_basis", f"basis 需為 {R.BASES}")
    if type_name is not None and (not isinstance(type_name, str) or not type_name.strip()
                                  or len(type_name) > MAX_TYPE_NAME
                                  or any(ord(c) < 32 or ord(c) == 127 for c in type_name)):
        return _fail(run_id, inp, codes, "validate", "invalid_type_name",
                     "type_name 需為非空字串（<=200，無控制字元）")
    if not codes:
        return _fail(run_id, inp, codes, "validate", "no_codes", "至少需勾選一套規範")
    try:
        inp.validate()
    except ValueError as e:
        return _fail(run_id, inp, codes, "validate", "invalid_input", f"輸入驗證失敗: {e}")

    # ── rules：只包 rules 載入與合併 ──
    try:
        rules_d = validate_rules(rules) if rules is not None else load_rules()
        gov = merge_strictest(codes, rules_d)
    except ValueError as e:
        return _fail(run_id, inp, codes, "rules", "invalid_rules", str(e))

    # ── route（try_route_tray 已回結構化結果；其餘例外不捕捉）──
    res = try_route_tray(inp.room_box(), inp.start, inp.ends, inp.obstacle_objs(),
                         inp.tray_w_mm / 1000, inp.tray_h_mm / 1000, inp.tray_type, gov, cell=inp.cell_m)
    if not res.ok:
        return _fail(run_id, inp, codes, "route", res.code or "route_error", res.error, gov)
    route = res.route

    # 吊架間距：規範有給值就用規範；否則用「純數字預設」且只記錄/揭露，絕不合成 Governing
    # （合成的話 compliance 會把它當規範值驗算並引用條文）。
    extra = list(notes)
    if "span_max_m" in gov:
        span, span_source = gov["span_max_m"].value, gov["span_max_m"].code
    else:
        span, span_source = DEFAULT_SPAN_M, "default"
        extra.insert(0, DEFAULT_SPAN_NOTE)
    place_hangers(route, span)

    reports = {"clash": check_route(inp, route, gov), "compliance": check_compliance(inp, route, gov)}
    rep_list = [reports["clash"], reports["compliance"]]
    model = R.build_model(inp, route, rep_list, gov, run_id, type_name=type_name, basis=basis, notes=extra)
    disclosures = [sanitize_text(n) for n in base_notes(inp, gov, rep_list, extra)]

    # ── output：暫存資料夾組裝 → 全部成功才 rename ──
    root = output_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=root))
    except OSError as e:
        return _fail(run_id, inp, codes, "io", "os_error", f"{type(e).__name__}: {e}", gov)

    try:
        err = None
        try:
            if make_dwg is not False:
                with _DWG_LOCK:
                    dxf = X.export_dxf(inp, route, rep_list, gov, run_id, notes=extra, convert_dwg=True,
                                       base=stage)
            else:
                dxf = X.export_dxf(inp, route, rep_list, gov, run_id, notes=extra, convert_dwg=False,
                                   base=stage)
            R.write_model(model, run_id, base=stage)
            err = _publish(stage / run_id, final)
        except FileExistsError as e:
            err = PipelineError("output", "file_exists", str(e))
        except OSError as e:
            err = PipelineError("io", "os_error", f"{type(e).__name__}: {e}")
    except BaseException as exc:                          # 程式錯誤：清理後原樣拋出，殘留資訊附在 note
        residue = _remove_stage(stage)
        if residue:
            exc.add_note(sanitize_text(residue))
        raise

    residue = _remove_stage(stage)                        # 成功時 stage 已空；失敗時含半成品
    if err is not None:
        return _fail(run_id, inp, codes, err.stage, err.code, err.message, gov, residue)

    files = {"dxf": final / dxf.dxf.name, "dwg": (final / dxf.dwg.name) if dxf.dwg else None,
             "revit_json": final / f"tray_{run_id}.json"}
    result = RunResult(True, run_id, None, inp, codes, gov, route, reports, files,
                       sanitize_text(dxf.dwg_note), dxf.acad_audit, disclosures,
                       _scrub(_stats(route, reports, model, span, span_source)))
    if residue:                                           # 成功但暫存殘留：不靜默
        result.disclosures.append(sanitize_text(residue))
    return result
