"""Revit 實機測試的場景與執行器（由 tests/test_revit_live.py 使用，亦可手動執行）。

流程：建置匯入器 → 暫存 DLL → 臨時寫入 AutoRun .addin 清單（結束後移除）→ 啟動 Revit →
等待 autorun.done → 讀取各場景的 *.revit_report.json。
注意：會啟動 Revit GUI；Revit 若出現「未簽署外掛」安全性對話框需由使用者按「Load Once/Always Load」。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from mep_tray import export_revit as R
from mep_tray.clash import check_route
from mep_tray.compliance import check_compliance
from mep_tray.model import Inputs
from mep_tray.router import Route, place_hangers, route_tray
from mep_tray.rules import merge_strictest
from tests.dotnet_util import ROOT, run

GOV = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])
REVIT_DIR = Path(os.environ.get("MEP_REVIT_DIR", r"C:\Program Files\Autodesk\Revit 2025"))
TEMPLATE = Path(os.environ.get("MEP_REVIT_TEMPLATE",
                               r"C:\ProgramData\Autodesk\RVT 2025\Templates\English\Electrical-Default_Metric.rte"))
ADDINS_DIR = Path(os.environ["APPDATA"]) / "Autodesk" / "Revit" / "Addins" / REVIT_DIR.name.split()[-1] \
    if "APPDATA" in os.environ else None
MANIFEST_NAME = "MepTray.AutoRun.addin"


def _routed_model(ends, name, basis="INTERNAL_ORIGIN", type_name="__FIRST__"):
    i = Inputs(start=(1, 1, 3), ends=ends)
    r = route_tray(i.room_box(), i.start, i.ends, [], 0.3, 0.1, "power", GOV, cell=0.25)
    place_hangers(r, GOV["span_max_m"].value)
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    return R.build_model(i, r, reps, GOV, name, type_name=type_name, basis=basis)


def _manual_model(segs_m, name, basis="INTERNAL_ORIGIN", type_name="__FIRST__"):
    i = Inputs(start=segs_m[0][0], ends=[segs_m[0][1]])
    r = Route([[segs_m[0][0], segs_m[0][1]]], list(segs_m), [], 0.0)
    return R.build_model(i, r, [], GOV, name, type_name=type_name, basis=basis)


def scenarios() -> dict[str, dict]:
    s: dict[str, dict] = {}
    s["a_tee"] = _routed_model([(10, 1, 3), (6, 5, 3)], "a_tee")
    s["b_elbow"] = _routed_model([(10, 5, 3)], "b_elbow")
    s["c_unspecified"] = _routed_model([(10, 1, 3)], "c_unspecified", basis="UNSPECIFIED")
    s["ov_d_unspecified"] = _routed_model([(10, 1, 3)], "ov_d_unspecified", basis="UNSPECIFIED")
    s["e_missing_type"] = _routed_model([(10, 1, 3)], "e_missing_type", type_name="NoSuchType")
    s["f_rollback"] = _manual_model(
        [((1, 1, 3), (3, 1, 3)), ((3, 1, 3), (3, 4, 3)), ((3, 4, 3), (3.0003, 4, 3))], "f_rollback")
    a = (5, 5, 3)
    s["g_cross"] = _manual_model([((0, 5, 3), a), (a, (10, 5, 3)), ((5, 0, 3), a), (a, (5, 10, 3))], "g_cross")
    s["h_union"] = _manual_model([((1, 1, 3), (4, 1, 3)), ((4, 1, 3), (8, 1, 3))], "h_union")
    s["i_pbp"] = _routed_model([(10, 1, 3)], "i_pbp", basis="PROJECT_BASE_POINT")
    s["j_shared"] = _routed_model([(10, 1, 3)], "j_shared", basis="SHARED_COORDINATES")
    s["k_unsupported"] = _manual_model([((0, 5, 3), a), (a, (5, 10, 3)), (a, (5, 5, 0))], "k_unsupported")
    return s


def write_scenarios(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name, m in scenarios().items():
        (d / f"{name}.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")


def revit_running() -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Revit.exe", "/NH"], capture_output=True, text=True,
                       shell=False)
    return "Revit.exe" in r.stdout


def build_addin(dotnet: str) -> Path:
    proj = ROOT / "revit" / "MepTrayImport"
    b = run([dotnet, "build", str(proj), "-c", "Release", "-nologo", f"-p:RevitInstallDir={REVIT_DIR}\\"], timeout=900)
    if b.returncode != 0:
        raise RuntimeError("建置失敗:\n" + b.stdout[-3000:] + b.stderr[-1000:])
    outs = sorted((proj / "bin" / "Release").glob("*/MepTrayImport.dll"))
    if not outs:
        raise RuntimeError("找不到建置輸出")
    return outs[-1].parent


def run_revit(dotnet: str, work: Path, timeout_s: int = 420, basis_override: str = "INTERNAL_ORIGIN") -> dict:
    """回傳 {'version': str|None, 'reports': {scenario: dict}, 'errors': {scenario: str}, 'done': bool}"""
    if revit_running():
        raise RuntimeError("Revit 已在執行；請先關閉，避免新程序被併入既有實例")
    if ADDINS_DIR is None:
        raise RuntimeError("找不到 APPDATA")
    out = build_addin(dotnet)
    stage = work / "addin"
    shutil.copytree(out, stage, dirs_exist_ok=True)
    models = work / "models"
    write_scenarios(models)
    manifest = ADDINS_DIR / MANIFEST_NAME
    ADDINS_DIR.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "revit" / "MepTrayImport" / MANIFEST_NAME).read_text(encoding="utf-8")
    text = text.replace("<Assembly>MepTrayImport.dll</Assembly>", f"<Assembly>{stage / 'MepTrayImport.dll'}</Assembly>")
    manifest.write_text(text, encoding="utf-8")
    env = dict(os.environ, MEP_TRAY_AUTORUN_DIR=str(models), MEP_TRAY_TEMPLATE=str(TEMPLATE),
               MEP_TRAY_BASIS=basis_override)
    proc = None
    try:
        proc = subprocess.Popen([str(REVIT_DIR / "Revit.exe"), "/language", "ENU", "/nosplash"], env=env, shell=False)
        deadline = time.time() + timeout_s
        while time.time() < deadline and not (models / "autorun.done").exists():
            time.sleep(2)
            if proc.poll() is not None and not (models / "autorun.done").exists():
                break
    finally:
        try:
            manifest.unlink()
        except FileNotFoundError:
            pass
        if proc is not None and proc.poll() is None:
            time.sleep(3)
            if proc.poll() is None:
                proc.kill()
    res = {"version": None, "reports": {}, "errors": {}, "done": (models / "autorun.done").exists(),
           "dir": models}
    v = models / "revit_version.txt"
    if v.exists():
        res["version"] = v.read_text(encoding="utf-8").strip()
    for p in sorted(models.glob("*.revit_report.json")):
        res["reports"][p.name.replace(".json.revit_report.json", "")] = json.loads(p.read_text(encoding="utf-8"))
    for p in sorted(models.glob("*.revit_error.txt")):
        res["errors"][p.name.replace(".json.revit_error.txt", "")] = p.read_text(encoding="utf-8")
    for n in ("autorun.error.txt",):
        if (models / n).exists():
            res["errors"]["_autorun"] = (models / n).read_text(encoding="utf-8")
    return res


if __name__ == "__main__":
    import sys
    import tempfile

    from tests.dotnet_util import find_dotnet
    dn = find_dotnet()
    w = Path(tempfile.mkdtemp(prefix="meprevit_"))
    out = run_revit(dn, w)
    print(json.dumps({k: v for k, v in out.items() if k != "reports"}, ensure_ascii=False, default=str, indent=1))
    for k, v in out["reports"].items():
        print(k, "committed=", v.get("Committed"), "abort=", v.get("Abort"), "trays=",
              len((v.get("Inspection") or {}).get("Trays", [])), "joints=",
              [(j["Kind"], j["Status"]) for j in v.get("Joints", [])])
    sys.exit(0 if out["done"] else 1)
