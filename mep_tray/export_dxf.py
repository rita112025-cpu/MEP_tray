"""DXF/DWG 匯出（AutoCAD / SCADA 可用）。

- DXF R2010、單位 mm（座標 = m × 1000）。圖層與屬性塊見常數。
- 3DFACE 盒為表面幾何，不是 3DSOLID（ezdxf 不產生 ACIS 實體）。
- DWG：僅在偵測到 ODA File Converter 時轉檔（subprocess 引數陣列、shell=False）；
  否則只輸出 DXF 並回傳原因。本機未實測真轉檔。
- 無任何網路呼叫。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 本機部分環境 ezdxf 的 C 擴充 DLL 載入失敗；純 Python 路徑對本工具輸出量足夠快
os.environ.setdefault("EZDXF_DISABLE_C_EXT", "1")
import ezdxf  # noqa: E402

from .geometry import Box, segment_box  # noqa: E402
from .model import Inputs  # noqa: E402
from .paths import out_path  # noqa: E402
from .router import Route  # noqa: E402
from .rules import LABELS  # noqa: E402

S = 1000.0                       # m → mm
BLOCK = "TRAY_SEG"
CLASH_R_MM = 300.0
LAYER_COLOR = {"MEP-TRAY-PWR": 6, "MEP-TRAY-SIG": 3, "MEP-HANGER": 2, "MEP-CLASH": 1,
               "MEP-NOTE": 7, "MEP-OBST-water": 5, "MEP-OBST-duct": 4, "MEP-OBST-heat": 30,
               "MEP-OBST-structure": 8, "MEP-OBST-tray_power": 6, "MEP-OBST-tray_signal": 3,
               "MEP-OBST-other": 9}


@dataclass
class DxfResult:
    dxf: Path
    dwg: Path | None
    dwg_note: str


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
    doc.styles.add("MEP", font="msjh.ttc")      # 繁中字型（無字型時 AutoCAD 以預設替代）
    doc.header["$INSUNITS"] = 4                 # mm
    doc.header["$MEASUREMENT"] = 1
    blk = doc.blocks.new(BLOCK)
    blk.add_circle((0, 0), 50, dxfattribs={"layer": "MEP-NOTE"})
    for i, tag in enumerate(("ID", "WIDTH_MM", "HEIGHT_MM", "TYPE", "LENGTH_M")):
        blk.add_attdef(tag, (0, -150 * (i + 1)), dxfattribs={"height": 80, "invisible": 1, "layer": "MEP-NOTE"})


def _mtext(msp, text: str, at, layer: str, h: float = 120.0, width: float = 3000.0) -> None:
    msp.add_mtext(text, dxfattribs={"layer": layer, "style": "MEP", "char_height": h,
                                    "insert": at, "width": width})


def export_dxf(inp: Inputs, route: Route, reports: list, notes: list[str], run_id: str,
               name: str | None = None, convert_dwg: bool = True) -> DxfResult:
    """reports: [clash.Report, compliance.Report…]；notes: 要印在圖面的揭露文字。"""
    doc = ezdxf.new("R2010", setup=False)
    _setup(doc, None)
    msp = doc.modelspace()
    tray_layer = "MEP-TRAY-SIG" if inp.tray_type == "signal" else "MEP-TRAY-PWR"
    w, h = inp.tray_w_mm / S, inp.tray_h_mm / S

    for wp in route.waypoints:                                    # 中心線
        if len(wp) >= 2:
            msp.add_polyline3d([_pt(p) for p in wp], dxfattribs={"layer": tray_layer})
    for i, (a, b) in enumerate(route.segments, 1):                # 橋架實體外框 + 屬性塊
        for face in _box_faces(segment_box(a, b, w, h)):
            msp.add_3dface(face, dxfattribs={"layer": tray_layer})
        mid = tuple((x + y) / 2 for x, y in zip(a, b))
        length = sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
        ref = msp.add_blockref(BLOCK, _pt(mid), dxfattribs={"layer": tray_layer})
        ref.add_auto_attribs({            # 屬性直接掛在 INSERT（SCADA/AutoCAD 可讀）；不用 add_auto_blockref 的外殼區塊
            "ID": f"SEG-{i:03d}", "WIDTH_MM": f"{inp.tray_w_mm:g}", "HEIGHT_MM": f"{inp.tray_h_mm:g}",
            "TYPE": inp.tray_type, "LENGTH_M": f"{length:.3f}"})
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
            lines = [f"#{n} [{LABELS[f.status]}] {f.kind}: {f.subject}",
                     f"實際 {f.actual:.0f} / 要求 {f.required:.0f} {f.unit}",
                     f"依據: {f.code or '—'} {f.clause}" + ("" if f.verified else "（規範值未驗證）"),
                     f"建議: {f.suggestion}"]
            _mtext(msp, "\\P".join(lines), (loc[0] + CLASH_R_MM, loc[1] + CLASH_R_MM, loc[2]), "MEP-CLASH")
    lo = _pt(inp.room_box().lo)
    _mtext(msp, "\\P".join([f"RUN {run_id}", *notes]), (lo[0], lo[1] - 600, lo[2]), "MEP-NOTE", h=150, width=8000)

    path = out_path(run_id, name or f"tray_{run_id}.dxf")
    doc.saveas(path)
    dwg, note = (None, "未要求 DWG")
    if convert_dwg:
        dwg, note = to_dwg(path)
    return DxfResult(path, dwg, note)


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
