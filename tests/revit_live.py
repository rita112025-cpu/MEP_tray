"""Revit 實機測試的場景與執行器（由 tests/test_revit_live.py 使用，亦可手動執行）。

流程：建置匯入器 → 暫存 DLL → 臨時寫入 AutoRun .addin 清單（結束後移除）→ 啟動 Revit →
等待 autorun.done → 讀取各場景的 *.revit_report.json。
座標設定：setups() 中的場景會另寫 <名稱>.setup.json（例如 {"move_pbp_mm":[dx,dy,dz]}），AutoRun 於匯入前
以獨立 Transaction 套用，結果寫入 *.setup_result.json（失敗則 *.setup_error.txt，且不匯入該場景）。
注意：會啟動 Revit GUI；Revit 若出現「未簽署外掛」安全性對話框需由使用者按「Load Once/Always Load」。
"""
from __future__ import annotations

import json
import math
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
    s["j1_shared_translate"] = _routed_model([(10, 1, 3)], "j1_shared_translate", basis="SHARED_COORDINATES")
    s["j2_shared_rotated"] = _routed_model([(10, 1, 3)], "j2_shared_rotated", basis="SHARED_COORDINATES")
    s["k_unsupported"] = _manual_model([((0, 5, 3), a), (a, (5, 10, 3)), (a, (5, 5, 0))], "k_unsupported")
    s["i2_pbp_moved"] = _routed_model([(10, 1, 3)], "i2_pbp_moved", basis="PROJECT_BASE_POINT")
    return s


# 非零 PBP 位移（mm）；不動 Z，避免牽涉 Level 高程
I2_PBP_DELTA_MM = (5000.0, -3000.0, 0.0)
# 共用座標設定（ProjectLocation.SetProjectPosition(原點, …)）：東西、南北、高程平移與真北旋轉
J_SHARED_POS = {"ew_mm": 12000.0, "ns_mm": -7000.0, "elev_mm": 1500.0}
J2_ANGLE_DEG = 30.0
SETUP_KEYS = ("move_pbp_mm", "set_project_position")
PROJECT_POSITION_KEYS = ("ew_mm", "ns_mm", "elev_mm", "angle_deg")


def setups() -> dict[str, dict]:
    """各場景匯入前要套用的文件座標設定（檔名 <場景>.setup.json）。"""
    return {"i2_pbp_moved": {"move_pbp_mm": list(I2_PBP_DELTA_MM)},
            "j1_shared_translate": {"set_project_position": {**J_SHARED_POS, "angle_deg": 0.0}},
            "j2_shared_rotated": {"set_project_position": {**J_SHARED_POS, "angle_deg": J2_ANGLE_DEG}}}


def validate_setup(d) -> list[str]:
    """回傳錯誤清單（空 = 合法）；規則與 AutoRunApp.ApplySetup 一致。"""
    if not isinstance(d, dict) or not d:
        return ["setup 必須是非空物件"]
    errs = [f"未知的 setup 鍵 '{k}'" for k in d if k not in SETUP_KEYS]
    if "move_pbp_mm" in d:
        v = d["move_pbp_mm"]
        if not (isinstance(v, list) and len(v) == 3
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v)):
            errs.append("move_pbp_mm 必須是 3 個有限數值 [dx,dy,dz]（mm）")
    if "set_project_position" in d:
        v = d["set_project_position"]
        if not (isinstance(v, dict) and set(v) == set(PROJECT_POSITION_KEYS)
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v.values())):
            errs.append("set_project_position 必須恰有 ew_mm、ns_mm、elev_mm、angle_deg 四個有限數值")
    return errs


def shared_mismatches(model: dict, report: dict, tol_mm: float = 0.5) -> list[str]:
    """比對 Revit 以 GetProjectPosition 讀回的橋架端點共用座標與模型點（共用座標基準）。
    接頭處端點會被 fitting 修剪，故略過；找不到橋架算不符。"""
    trays = {t["Id"]: t for t in (report.get("Inspection") or {}).get("Trays", [])}
    created = dict(x.split("=", 1) for x in report.get("CreatedTrays", []))
    joint_pts = [j["point"] for j in model.get("joints", [])]
    bad = []
    for s in model["segments"]:
        t = trays.get(created.get(s["id"], ""))
        if t is None:
            bad.append(f"{s['id']}: 找不到讀回的橋架")
            continue
        for key, got_key in (("start", "SharedStartMm"), ("end", "SharedEndMm")):
            p = s[key]
            if any(math.dist(p, q) < 1.0 for q in joint_pts):
                continue
            d = math.dist(p, t[got_key])
            if d > tol_mm:
                bad.append(f"{s['id']}.{key}: 模型 {p} 共用座標讀回 {t[got_key]} 差 {d:.3f} mm")
    return bad


def xy_rotation_deg(model_seg: dict, tray: dict) -> float:
    """模型線段方向到 Revit 內部座標方向的 XY 平面旋轉角（度，逆時針為正，範圍 (-180, 180]）。"""
    mx, my = (model_seg["end"][i] - model_seg["start"][i] for i in (0, 1))
    tx, ty = (tray["EndMm"][i] - tray["StartMm"][i] for i in (0, 1))
    return math.degrees(math.atan2(mx * ty - my * tx, mx * tx + my * ty))


def endpoint_mismatches(model: dict, report: dict, offset_mm, tol_mm: float = 0.5) -> list[str]:
    """比對 Revit 讀回的橋架端點與「模型點 + offset」。接頭處端點會被 fitting 修剪，故只比對非接頭端點。
    回傳不符項目描述（空 = 全部相符）；找不到對應橋架也算不符。"""
    trays = {t["Id"]: t for t in (report.get("Inspection") or {}).get("Trays", [])}
    created = dict(x.split("=", 1) for x in report.get("CreatedTrays", []))
    joint_pts = [j["point"] for j in model.get("joints", [])]
    bad = []
    for s in model["segments"]:
        t = trays.get(created.get(s["id"], ""))
        if t is None:
            bad.append(f"{s['id']}: 找不到讀回的橋架")
            continue
        for key, got_key in (("start", "StartMm"), ("end", "EndMm")):
            p = s[key]
            if any(math.dist(p, q) < 1.0 for q in joint_pts):
                continue
            exp = [p[i] + offset_mm[i] for i in range(3)]
            d = math.dist(exp, t[got_key])
            if d > tol_mm:
                bad.append(f"{s['id']}.{key}: 期望 {exp} 讀回 {t[got_key]} 差 {d:.3f} mm")
    return bad


def write_scenarios(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    sc = scenarios()
    for name, m in sc.items():
        (d / f"{name}.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    for name, st in setups().items():
        if name not in sc:
            raise ValueError(f"setup 對應的場景不存在: {name}")
        errs = validate_setup(st)
        if errs:
            raise ValueError(f"{name}.setup.json: " + "; ".join(errs))
        (d / f"{name}.setup.json").write_text(json.dumps(st), encoding="utf-8")


def wait_revit_exit(timeout_s: float = 30.0) -> bool:
    """autorun.done 之後 Revit 呼叫 Environment.Exit，但程序消失需要數秒；輪詢直到結束或逾時。"""
    end = time.time() + timeout_s
    while revit_running():
        if time.time() > end:
            return False
        time.sleep(1)
    return True


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
    """回傳 {'version': str|None, 'reports': {scenario: dict}, 'models': {scenario: dict},
    'setups': {scenario: dict}, 'errors': {scenario 或 scenario.setup: str}, 'done': bool, 'dir': Path}"""
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
    res = {"version": None, "reports": {}, "models": {}, "setups": {}, "errors": {},
           "done": (models / "autorun.done").exists(), "dir": models}
    for name in scenarios():
        res["models"][name] = json.loads((models / f"{name}.json").read_text(encoding="utf-8"))
    for p in sorted(models.glob("*.json.setup_result.json")):
        res["setups"][p.name.replace(".json.setup_result.json", "")] = json.loads(p.read_text(encoding="utf-8"))
    for p in sorted(models.glob("*.json.setup_error.txt")):
        res["errors"][p.name.replace(".json.setup_error.txt", "") + ".setup"] = p.read_text(encoding="utf-8")
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
    # 可選參數：輸出資料夾（保留證據用）；預設為暫存資料夾
    w = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="meprevit_"))
    out = run_revit(dn, w)
    print(json.dumps({k: v for k, v in out.items() if k not in ("reports", "models")},
                     ensure_ascii=False, default=str, indent=1))
    for k, v in out["reports"].items():
        ins = v.get("Inspection") or {}
        pbp = ((ins.get("Coordinates") or {}).get("ProjectBasePoint") or {}).get("PositionMm")
        print(k, "committed=", v.get("Committed"), "abort=", v.get("Abort"), "trays=",
              len(ins.get("Trays", [])), "joints=",
              [(j["Kind"], j["Status"]) for j in v.get("Joints", [])], "pbp_mm=", pbp)
    sys.exit(0 if out["done"] else 1)
