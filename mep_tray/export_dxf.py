"""DXF/DWG 匯出（AutoCAD / SCADA 可用）。

- DXF R2010、單位 mm（座標 = m × 1000）。圖層與屬性塊見常數。
- 3DFACE 盒為表面幾何，不是 3DSOLID（ezdxf 不產生 ACIS 實體）。
- DWG：僅在偵測到 ODA File Converter 時轉檔（subprocess 引數陣列、shell=False）；
  否則只輸出 DXF 並回傳原因。本機未實測真轉檔。
- 無任何網路呼叫。
"""
from __future__ import annotations

import glob
import importlib
import os
import sys
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path



def load_ezdxf(importer=importlib.import_module):
    """先正常匯入（保留 C 擴充）；僅在 ImportError（如 Windows 'DLL load failed'）時
    改設 EZDXF_DISABLE_C_EXT=1 並重試一次，仍失敗才拋出明確錯誤。"""
    try:
        return importer("ezdxf")
    except ImportError as first:
        for k in [k for k in sys.modules if k == "ezdxf" or k.startswith("ezdxf.")]:
            del sys.modules[k]                     # 清掉半載入狀態
        os.environ["EZDXF_DISABLE_C_EXT"] = "1"
        try:
            return importer("ezdxf")
        except ImportError as second:
            raise ImportError(f"無法載入 ezdxf：{first}；停用 C 擴充後仍失敗：{second}。"
                              f"請參閱 INSTALL.md「ezdxf 匯入失敗」") from second


ezdxf = load_ezdxf()

from .geometry import Box, segment_box  # noqa: E402
from .model import Inputs  # noqa: E402
from . import acad  # noqa: E402
from .paths import check_run_id, out_path  # noqa: E402
from .router import Route  # noqa: E402
from .rules import LABELS  # noqa: E402

S = 1000.0                       # m → mm
BLOCK = "TRAY_SEG"
TRAY_SEG_TAGS = ("ID", "WIDTH_MM", "HEIGHT_MM", "TYPE", "LENGTH_MM")   # 固定 ASCII；SCADA 端依此讀屬性，勿改
CLASH_R_MM = 300.0
LAYER_COLOR = {"MEP-TRAY-PWR": 6, "MEP-TRAY-SIG": 3, "MEP-TRAY-BODY": 8, "MEP-HANGER": 2, "MEP-CLASH": 1,
               "MEP-NOTE": 7, "MEP-OBST-water": 5, "MEP-OBST-duct": 4, "MEP-OBST-heat": 30,
               "MEP-OBST-structure": 8, "MEP-OBST-tray_power": 6, "MEP-OBST-tray_signal": 3,
               "MEP-OBST-other": 9}


@dataclass
class DxfResult:
    dxf: Path
    dwg: Path | None
    dwg_note: str
    acad_audit: int | None = None     # AutoCAD 稽核錯誤數；None=未稽核/無法解析
    acad_version: str = ""


def _pt(p) -> tuple:
    return tuple(round(v * S, 3) for v in p)


def _box_faces(b: Box):
    (x0, y0, z0), (x1, y1, z1) = b.lo, b.hi
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    for f in ((0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)):
        yield [_pt(c[i]) for i in f]


def _setup(doc, kinds) -> None:
    for name, color in LAYER_COLOR.items():
        doc.layers.add(name, color=color)
    st = doc.styles.add("MEP", font="msjh.ttc")   # 繁中 TrueType（預設 txt.shx 無 CJK 字形會變 ???）
    st.set_extended_font_data("Microsoft JhengHei")
    doc.header["$INSUNITS"] = 4                 # mm
    doc.header["$MEASUREMENT"] = 1
    blk = doc.blocks.new(BLOCK)
    blk.add_circle((0, 0), 50, dxfattribs={"layer": "MEP-NOTE"})
    for i, tag in enumerate(TRAY_SEG_TAGS):
        blk.add_attdef(tag, (0, -150 * (i + 1)), dxfattribs={"height": 80, "invisible": 1, "layer": "MEP-NOTE"})


def _esc(text: str) -> str:
    """跳脫 MTEXT 控制碼（反斜線、大括號），使用者輸入/規範文字不會被當成格式碼。"""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _mtext(msp, lines, at, layer: str, h: float = 120.0, width: float = 3000.0) -> None:
    """lines: 字串或字串清單（每項跳脫後以 MTEXT 換行碼 反斜線+P 串接）。"""
    lines = [lines] if isinstance(lines, str) else list(lines)
    msp.add_mtext("\\P".join(_esc(x) for x in lines), dxfattribs={
        "layer": layer, "style": "MEP", "char_height": h, "insert": at, "width": width})


def disclosure_notes(inp: Inputs, gov: dict, reports=(), extra: list[str] = (),
                     acad_audit: int | None = None, acad_version: str = "") -> list[str]:
    """圖面揭露文字：座標約定、規範值未驗證清單、預設電纜、未實機驗證。"""
    notes = ["座標 = 公尺×1000 (mm)，原點 = 輸入座標系原點（不平移）"]
    bad = sorted({g.code for g in gov.values() if not g.verified})
    n_unv = sum(1 for rep in reports for f in rep.checks if f.status == "UNVERIFIED")
    if n_unv:     # 自 checks（不只 findings）統計：零 finding 的「乾淨」圖也必須揭露
        notes.append(f"本圖有 {n_unv} 項檢查所依規範值尚未核對條文（verified=false），不得視為合規")
    if bad:
        notes.append("規範值未驗證 (verified=false)：" + ", ".join(bad) + "；結果不得視為合規依據")
    if inp.cables_defaulted:
        notes.append("未輸入電纜資料，填充率以預設電纜計算")
    if acad_audit is None:
        notes.append("本圖未於 AutoCAD 實機驗證（僅以 ezdxf 回讀與稽核）")
    elif acad_audit == 0:
        notes.append(f"本圖已以 {acad_version} 開啟並稽核：0 個錯誤（不代表規範合規）")
    else:
        notes.append(f"{acad_version} 稽核發現 {acad_audit} 個錯誤，請勿直接採用")
    return notes + list(extra)


def _build_doc(inp: Inputs, route: Route, reports: list, gov: dict, run_id: str,
               notes: list[str], acad_audit: int | None, acad_version: str):
    notes = disclosure_notes(inp, gov, reports, notes, acad_audit, acad_version)
    doc = ezdxf.new("R2010", setup=False)
    _setup(doc, None)
    msp = doc.modelspace()
    tray_layer = "MEP-TRAY-SIG" if inp.tray_type == "signal" else "MEP-TRAY-PWR"
    w, h = inp.tray_w_mm / S, inp.tray_h_mm / S

    for wp in route.waypoints:                                    # 中心線
        if len(wp) >= 2:
            msp.add_polyline3d([_pt(p) for p in wp], dxfattribs={"layer": tray_layer})
    for i, (a, b) in enumerate(route.segments, 1):                # 橋架實體外框 + 屬性塊
        for face in _box_faces(segment_box(a, b, w, h)):        # 外框盒獨立圖層，可凍結/關閉
            msp.add_3dface(face, dxfattribs={"layer": "MEP-TRAY-BODY"})
        mid = tuple((x + y) / 2 for x, y in zip(a, b))
        length = sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
        ref = msp.add_blockref(BLOCK, _pt(mid), dxfattribs={"layer": tray_layer})
        ref.add_auto_attribs({            # 屬性直接掛在 INSERT（SCADA/AutoCAD 可讀）；不用 add_auto_blockref 的外殼區塊
            "ID": f"SEG-{i:03d}", "WIDTH_MM": f"{inp.tray_w_mm:g}", "HEIGHT_MM": f"{inp.tray_h_mm:g}",
            "TYPE": inp.tray_type, "LENGTH_MM": f"{length * S:.1f}"})
    for p in route.hangers:
        msp.add_point(_pt(p), dxfattribs={"layer": "MEP-HANGER"})
    for ob in inp.obstacle_objs():
        layer = f"MEP-OBST-{ob.kind}"
        for face in _box_faces(ob.box):
            msp.add_3dface(face, dxfattribs={"layer": layer})
        _mtext(msp, ob.name, _pt(ob.box.center()), layer, h=100, width=1500)

    n = 0
    for rep in reports:                                           # 問題標註：位置＋規範依據＋建議
        for f in rep.findings:
            n += 1
            loc = _pt(f.location)
            msp.add_circle(loc, CLASH_R_MM, dxfattribs={"layer": "MEP-CLASH"})
            lines = [f"#{n} {f.status} {f.kind} {f.code}  {LABELS[f.status]}：{f.subject}",   # ASCII 代碼在前，字型缺失仍可辨識
                     f"實際 {f.actual:.0f} / 要求 {f.required:.0f} {f.unit}",
                     f"依據: {f.code or '—'} {f.clause}" + ("" if f.verified else "（規範值未驗證）"),
                     f"建議: {f.suggestion}"]
            _mtext(msp, lines, (loc[0] + CLASH_R_MM, loc[1] + CLASH_R_MM, loc[2]), "MEP-CLASH")
    lo = _pt(inp.room_box().lo)
    _mtext(msp, [f"RUN {run_id}", *notes], (lo[0], lo[1] - 600, lo[2]), "MEP-NOTE", h=150, width=8000)

    return doc


def export_dxf(inp: Inputs, route: Route, reports: list, gov: dict, run_id: str,
               notes: list[str] = (), name: str | None = None, convert_dwg: bool = True,
               overwrite: bool = False, accore: Path | None = None) -> DxfResult:
    """reports: [clash.Report, compliance.Report…]；gov: merge_strictest 結果（用於揭露文字）。
    run_id 由本函式自行驗證；目標檔已存在時預設拒絕覆寫（FileExistsError）。
    convert_dwg=True 且偵測到 AutoCAD：先以草稿稽核，將結果寫進圖面揭露後再定稿並轉存 DWG；
    否則退而用 ODA File Converter；都沒有則只輸出 DXF（回報原因）。"""
    check_run_id(run_id)
    path = out_path(run_id, name or f"tray_{run_id}.dxf")
    if path.exists() and not overwrite:
        raise FileExistsError(f"輸出檔已存在，拒絕覆寫: {path.name}")
    acad_exe = (accore or acad.find_accore()) if convert_dwg else None
    audit, ver = None, ""
    if acad_exe is not None:                              # 第 1 趟：草稿 → AutoCAD 稽核
        with tempfile.TemporaryDirectory() as tmp:
            draft = Path(tmp) / "draft.dxf"
            _build_doc(inp, route, reports, gov, run_id, notes, None, "").saveas(draft)
            r1 = acad.audit_and_convert(draft, None, accore=acad_exe)
            audit, ver = r1.audit_errors, r1.version
    _build_doc(inp, route, reports, gov, run_id, notes, audit, ver).saveas(path)
    dwg, note = (None, "未要求 DWG")
    if convert_dwg:
        if acad_exe is not None:                          # 第 2 趟：定稿 → 轉存 DWG（並再稽核定稿）
            r2 = acad.audit_and_convert(path, path.with_suffix(".dwg"), accore=acad_exe)
            audit, ver = r2.audit_errors, r2.version
            dwg, note = r2.dwg, r2.note
        if dwg is None:
            dwg, note2 = to_dwg(path)
            note = note2 if acad_exe is None else f"{note}；ODA：{note2}"
    return DxfResult(path, dwg, note, audit, ver)


def find_oda() -> Path | None:
    env = os.environ.get("MEP_ODA_PATH")
    if env and Path(env).is_file():
        return Path(env)
    for pat in ("C:/Program Files/ODA/ODAFileConverter*/ODAFileConverter.exe",
                "C:/Program Files (x86)/ODA/ODAFileConverter*/ODAFileConverter.exe"):
        hits = sorted(glob.glob(pat))
        if hits:
            return Path(hits[-1])
    return None


def to_dwg(dxf: Path, oda: Path | None = None, runner=subprocess.run) -> tuple[Path | None, str]:
    """以 ODA File Converter 轉 DWG；找不到或失敗則回 (None, 原因)，不拋例外。"""
    oda = oda or find_oda()
    if oda is None:
        return None, "未偵測到 ODA File Converter（設定環境變數 MEP_ODA_PATH 可啟用 DWG 轉檔）；僅輸出 DXF"
    dxf = Path(dxf).resolve()
    dwg = dxf.with_suffix(".dwg")
    with tempfile.TemporaryDirectory() as tin, tempfile.TemporaryDirectory() as tout:
        shutil.copy2(dxf, Path(tin) / dxf.name)
        args = [str(oda), tin, tout, "ACAD2018", "DWG", "0", "1", dxf.name]   # 引數陣列，shell=False
        try:
            r = runner(args, shell=False, timeout=60, capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"ODA 轉檔執行失敗: {e}"
        produced = Path(tout) / dwg.name
        if getattr(r, "returncode", 1) != 0 or not produced.is_file():
            return None, f"ODA 轉檔失敗 (returncode={getattr(r, 'returncode', '?')})；僅輸出 DXF"
        shutil.copy2(produced, dwg)
    return dwg, "已由 ODA File Converter 轉為 DWG（ACAD2018）"
