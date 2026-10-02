import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from mep_tray import acad
from mep_tray import export_dxf as X
from mep_tray import export_revit as R
from mep_tray import pipeline as P
from mep_tray.acad import AcadResult
from mep_tray.disclosure import REVIT_STATUS
from mep_tray.model import Inputs
from mep_tray.rules import load_rules

ALL = ["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"]


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """輸出到暫存根目錄；預設絕不啟動 AutoCAD/ODA（即使本機有裝）。需要時由測試自行覆寫。"""
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.delenv("MEP_ACCORE_PATH", raising=False)
    monkeypatch.delenv("MEP_ODA_PATH", raising=False)
    monkeypatch.setattr(acad, "find_accore", lambda: None)
    monkeypatch.setattr(X, "find_oda", lambda: None)
    return tmp_path / "out"


def sample(**kw):
    base = dict(start=(1, 1, 3), ends=[(10, 1, 3), (6, 5, 3)],
                obstacles=[{"name": "W", "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}],
                cables=[{"od_mm": 30, "count": 40, "kind": "power"}],      # 填充率超標 → 有 finding
                cell_m=0.25)       # 管線邏輯測試用粗格距；自動格距（0.1m）單次約 5 秒，另有專案測試覆蓋
    base.update(kw)
    return Inputs(**base)


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_digest(d: Path) -> dict:
    return {str(p.relative_to(d)): digest(p) for p in sorted(d.rglob("*")) if p.is_file()}


# ---------- 基本成功路徑 ----------
def test_success_returns_data_files_stats_and_does_not_mutate_inputs(isolate):
    inp = sample(codes=["CNS"])
    before = inp.to_dict()
    r = P.run(inp, ALL, "ok1", make_dwg=False)
    assert r.ok and r.error is None and r.codes == ALL
    assert inp.to_dict() == before and inp.codes == ["CNS"]          # 呼叫端物件不被修改
    assert r.inputs is not inp and r.inputs.codes == ALL             # 實際採用的是副本
    run_dir = isolate / "ok1"
    assert r.files["dxf"].parent == run_dir and r.files["revit_json"].parent == run_dir and r.files["dwg"] is None
    s = r.stats
    assert s["segments"] == 4 and s["joints"] == 3 and s["hangers"] > 0 and s["length_m"] > 0
    assert any(k.startswith("fill/") for k in s["findings_by_kind_status"])
    assert set(r.reports) == {"clash", "compliance"}


# ---------- 結構化錯誤 ----------
def bad_obstacle():
    return sample(obstacles=[{"name": "x", "kind": "water"}])


SEALED = [{"name": "slab", "kind": "structure", "lo": [5, -1, -1], "hi": [5.3, 7, 5]}]


@pytest.mark.parametrize("label,make,codes,kw,stage,code", [
    ("no codes", sample, [], {}, "validate", "no_codes"),
    ("unknown code", sample, ["XYZ"], {}, "rules", "invalid_rules"),
    ("negative width", lambda: sample(tray_w_mm=-5), ALL, {}, "validate", "invalid_input"),
    ("malformed obstacle", bad_obstacle, ALL, {}, "validate", "invalid_input"),
    ("bad basis", sample, ALL, {"basis": "WORLD"}, "validate", "invalid_basis"),
    ("bad type name", sample, ALL, {"type_name": "a" * 201}, "validate", "invalid_type_name"),
    ("misaligned", lambda: sample(start=(1.03, 1, 3)), ALL, {}, "route", "misaligned"),
    ("sealed, coarse grid", lambda: sample(obstacles=SEALED), ALL, {}, "route", "no_route"),
    ("sealed, fine grid hits search cap", lambda: sample(obstacles=SEALED, cell_m=None), ALL, {}, "route", "search_limit"),
    ("grid too large", lambda: sample(room=((0, 0, 0), (100, 100, 10)), ends=[(90, 90, 3)], cell_m=0.05),
     ALL, {}, "route", "grid_too_large"),
    ("endpoint blocked", lambda: sample(obstacles=[{"name": "b", "kind": "structure", "lo": [9, 0, 2],
                                                    "hi": [11, 2, 4]}], ends=[(10, 1, 3)], cell_m=0.25),
     ALL, {}, "route", "endpoint_blocked"),
])
def test_expected_failures_are_structured_and_leave_nothing(isolate, label, make, codes, kw, stage, code):
    r = P.run(make(), codes, "bad1", make_dwg=False, **kw)
    assert r.ok is False and r.error is not None, label
    assert (r.error.stage, r.error.code) == (stage, code), (label, r.error)
    assert r.error.message and all(v is None for v in r.files.values())
    assert not (isolate / "bad1").exists(), "失敗不得留下資料夾"
    json.dumps(r.to_summary_dict(), allow_nan=False)


@pytest.mark.parametrize("rid", ["../evil", "", "x" * 41, "中文", "a b", "..", "/abs"])
def test_invalid_run_id_rejected_without_touching_disk(isolate, rid):
    r = P.run(sample(), ALL, rid, make_dwg=False)
    assert r.ok is False and r.error.stage == "validate" and r.error.code == "invalid_run_id"
    assert not isolate.exists() or not any(isolate.iterdir())


def test_existing_run_is_refused_and_left_byte_identical(isolate):
    first = P.run(sample(), ALL, "dup1", make_dwg=False)
    assert first.ok
    snap = tree_digest(isolate / "dup1")
    again = P.run(sample(start=(2, 1, 3)), ["CNS"], "dup1", make_dwg=False)
    assert again.ok is False and (again.error.stage, again.error.code) == ("output", "run_exists")
    assert tree_digest(isolate / "dup1") == snap                       # 既有版本原封不動


def test_existing_stray_file_or_dir_named_like_run_is_refused(isolate):
    isolate.mkdir(parents=True)
    (isolate / "stray").write_text("x")
    r = P.run(sample(), ALL, "stray", make_dwg=False)
    assert r.ok is False and r.error.code == "run_exists" and (isolate / "stray").read_text() == "x"


# ---------- 程式錯誤直傳 + 清理 ----------
def test_unexpected_exception_propagates_and_claimed_dir_is_removed(isolate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(P.R, "write_model", boom)          # DXF 已寫出之後才失敗
    with pytest.raises(RuntimeError, match="bug"):
        P.run(sample(), ALL, "crash1", make_dwg=False)
    assert not (isolate / "crash1").exists()


def test_oserror_after_claim_becomes_io_error_and_dir_removed(isolate, monkeypatch):
    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(P.R, "write_model", disk_full)
    r = P.run(sample(), ALL, "io1", make_dwg=False)
    assert r.ok is False and (r.error.stage, r.error.code) == ("io", "os_error")
    assert not (isolate / "io1").exists()


def test_programming_error_before_claim_also_propagates(monkeypatch):
    monkeypatch.setattr(P, "check_route", lambda *a, **k: (_ for _ in ()).throw(KeyError("oops")))
    with pytest.raises(KeyError):
        P.run(sample(), ALL, "bug2", make_dwg=False)


# ---------- 揭露 ----------
def test_clean_all_unverified_run_still_discloses_and_revit_json_carries_status(isolate):
    r = P.run(Inputs(start=(1, 1, 3), ends=[(10, 1, 3)]), ALL, "clean1", make_dwg=False)
    assert r.ok and r.stats["findings_by_kind_status"] == {} and r.stats["unverified_checks"] >= 3
    assert any("項檢查所依規範值尚未核對" in n for n in r.disclosures)
    m = json.loads(r.files["revit_json"].read_text(encoding="utf-8"))
    assert REVIT_STATUS in m["disclosures"] and any("尚未核對" in n for n in m["disclosures"])
    assert m["coordinate_system"]["basis"] == "UNSPECIFIED"


def test_default_hanger_span_is_disclosed_only_when_no_selected_code_defines_it(isolate):
    none = P.run(Inputs(), ["TW_BUILDING"], "span1", make_dwg=False)
    assert none.ok and P.DEFAULT_SPAN_NOTE in none.disclosures
    m = json.loads(none.files["revit_json"].read_text(encoding="utf-8"))
    assert P.DEFAULT_SPAN_NOTE in m["disclosures"]
    some = P.run(Inputs(), ["CNS"], "span2", make_dwg=False)
    assert some.ok and P.DEFAULT_SPAN_NOTE not in some.disclosures
    assert some.stats["hangers"] > none.stats["hangers"] or some.stats["hangers"] >= 1


def test_basis_and_type_name_are_passed_to_revit_json_only(isolate):
    r = P.run(Inputs(), ALL, "rv1", make_dwg=False, type_name="Ladder Cable Tray", basis="INTERNAL_ORIGIN")
    m = json.loads(r.files["revit_json"].read_text(encoding="utf-8"))
    assert m["tray"]["type_name"] == "Ladder Cable Tray" and m["coordinate_system"]["basis"] == "INTERNAL_ORIGIN"


# ---------- rules 注入 ----------
def test_injected_rules_change_governing_values_and_invalid_rules_are_structured(isolate):
    base = P.run(Inputs(), ["NEC"], "rl1", make_dwg=False)
    custom = load_rules()
    custom["codes"]["NEC"]["values"]["span_max_m"] = 1.0
    changed = P.run(Inputs(), ["NEC"], "rl2", make_dwg=False, rules=custom)
    assert base.gov["span_max_m"].value == 3.0 and changed.gov["span_max_m"].value == 1.0
    assert changed.stats["hangers"] > base.stats["hangers"]
    bad = load_rules()
    bad["params"]["span_max_m"].pop("direction")
    r = P.run(Inputs(), ["NEC"], "rl3", make_dwg=False, rules=bad)
    assert r.ok is False and (r.error.stage, r.error.code) == ("rules", "invalid_rules")
    assert not (isolate / "rl3").exists()


# ---------- DWG ----------
def test_no_cad_detected_gives_dwg_none_with_note(isolate):
    r = P.run(sample(), ALL, "dwg0", make_dwg=True)
    assert r.ok and r.files["dwg"] is None and r.dwg_note and "ODA" in r.dwg_note


def test_make_dwg_false_never_touches_external_cad(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("不得呼叫外部 CAD")
    monkeypatch.setattr(acad, "find_accore", forbidden)
    monkeypatch.setattr(acad, "audit_and_convert", forbidden)
    monkeypatch.setattr(X, "find_oda", forbidden)
    monkeypatch.setattr(X, "to_dwg", forbidden)
    assert P.run(sample(), ALL, "nocad1", make_dwg=False).ok


def test_dwg_conversions_are_serialised_across_concurrent_runs(monkeypatch, tmp_path):
    active, peak, calls = [0], [0], []
    gate = threading.Lock()
    fake_exe = tmp_path / "AutoCAD 2027" / "accoreconsole.exe"

    def fake(dxf, dwg_out=None, accore=None, **kw):
        with gate:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        calls.append(dwg_out is not None)
        if dwg_out is not None:
            Path(dwg_out).write_bytes(b"AC1032")
        with gate:
            active[0] -= 1
        return AcadResult(0, dwg_out, "AutoCAD 2027", "fake")

    monkeypatch.setattr(acad, "find_accore", lambda: fake_exe)
    monkeypatch.setattr(acad, "audit_and_convert", fake)
    results = {}

    def work(rid):
        results[rid] = P.run(Inputs(), ALL, rid, make_dwg=True)

    ts = [threading.Thread(target=work, args=(f"cc{i}",)) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r.ok and r.files["dwg"] is not None for r in results.values())
    assert peak[0] == 1 and len(calls) == 6                    # 每個 run 兩趟，且從未同時執行
