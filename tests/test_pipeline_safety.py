"""pipeline 的安全性、競態、錯誤分界、確定性測試（基本成功/失敗路徑見 test_pipeline.py）。"""
import dataclasses
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from mep_tray import export_dxf as X
from mep_tray import export_revit as R
from mep_tray import pipeline as P
from mep_tray.model import Inputs
from mep_tray.rules import load_rules
from tests.test_pipeline import ALL, digest, isolate, sample, tree_digest  # noqa: F401  (isolate 為 autouse fixture)


def stages(root):
    return sorted(p.name for p in root.glob(P.STAGE_PREFIX + "*")) if root.exists() else []


def make_link(link: Path, target: Path):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        rc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, shell=False) if os.name == "nt" else None
        if rc is None or rc.returncode != 0:
            pytest.skip("此環境無法建立 symlink/junction")


def boom(exc):
    def _raise(*a, **k):
        raise exc
    return _raise


# ---------- 摘要與脫敏 ----------
def test_summary_dict_is_strict_json_relative_paths_and_no_local_paths(isolate, tmp_path):
    r = P.run(sample(), ALL, "sum1", make_dwg=False)
    d = r.to_summary_dict()
    text = json.dumps(d, ensure_ascii=False, allow_nan=False)
    assert d["files"] == {"dxf": "sum1/tray_sum1.dxf", "dwg": None, "revit_json": "sum1/tray_sum1.json",
                          "manifest": "sum1/manifest.json"}
    assert set(d["hashes"]) == {"input_sha256", "rules_sha256", "rules_snapshot_sha256", "result_sha256"}
    for leak in (str(tmp_path), str(Path.home()), tempfile.gettempdir()):
        assert leak.lower() not in text.lower()


def test_error_messages_do_not_leak_local_paths(isolate, monkeypatch, tmp_path):
    monkeypatch.setattr(P.R, "write_model", boom(OSError(5, f"cannot write {tmp_path}/x and {Path.home()}/y")))
    r = P.run(sample(), ALL, "leak1", make_dwg=False)
    m = r.error.message
    assert r.ok is False and str(tmp_path) not in m and str(Path.home()) not in m and "<" in m


# ---------- 暫存資料夾清理安全 ----------
def test_remove_stage_only_removes_own_direct_child_with_stage_prefix(isolate, tmp_path):
    isolate.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    assert P._remove_stage(outside) is not None and (outside / "keep.txt").exists()      # 不在輸出根目錄
    assert P._remove_stage(isolate) is not None and isolate.exists()                     # 根目錄本身
    wrong = isolate / "notstage"
    wrong.mkdir()
    assert P._remove_stage(wrong) is not None and wrong.exists()                         # 前綴不符
    nested = isolate / (P.STAGE_PREFIX + "x") / "sub"
    nested.mkdir(parents=True)
    assert P._remove_stage(nested) is not None and nested.exists()                       # 非直接子層
    own = isolate / (P.STAGE_PREFIX + "own")
    own.mkdir()
    (own / "f").write_text("1")
    assert P._remove_stage(own) is None and not own.exists()
    assert P._remove_stage(own) is None                                                  # 已不存在 → 視為乾淨


def test_remove_stage_refuses_junction_or_symlink_and_leaves_target(isolate, tmp_path):
    isolate.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("data")
    link = isolate / (P.STAGE_PREFIX + "lnk")
    make_link(link, victim)
    assert P._remove_stage(link) is not None
    assert (victim / "important.txt").read_text() == "data"


def test_remove_stage_failure_is_reported_not_swallowed(isolate, monkeypatch):
    isolate.mkdir(parents=True)
    st = isolate / (P.STAGE_PREFIX + "stuck")
    st.mkdir()
    monkeypatch.setattr(P.shutil, "rmtree", boom(PermissionError("locked")))
    note = P._remove_stage(st)
    assert note and "殘留" in note and "locked" in note and st.exists()


def test_cleanup_failure_after_structured_error_reports_residue_sanitised(isolate, monkeypatch, tmp_path):
    monkeypatch.setattr(P.R, "write_model", boom(OSError(28, "disk full")))
    monkeypatch.setattr(P.shutil, "rmtree", boom(PermissionError("locked")))
    r = P.run(sample(), ALL, "cl1", make_dwg=False)
    assert r.ok is False and r.error.code == "os_error"
    assert r.error.cleanup_failed is True
    assert "殘留資料夾未能清除: " + P.STAGE_PREFIX in r.error.message and str(tmp_path) not in r.error.message
    assert len(stages(isolate)) == 1                          # 殘留確實存在，而且有被回報
    assert r.to_summary_dict()["error"]["cleanup_failed"] is True


def test_cleanup_failed_flag_is_false_when_cleanup_succeeds(isolate, monkeypatch):
    monkeypatch.setattr(P.R, "write_model", boom(OSError(28, "disk full")))
    r = P.run(sample(), ALL, "cl0", make_dwg=False)
    assert r.ok is False and r.error.cleanup_failed is False and "殘留" not in r.error.message
    assert stages(isolate) == []


def test_cleanup_failure_on_propagating_exception_adds_note(isolate, monkeypatch):
    monkeypatch.setattr(P.R, "write_model", boom(RuntimeError("bug")))
    monkeypatch.setattr(P.shutil, "rmtree", boom(PermissionError("locked")))
    with pytest.raises(RuntimeError) as ei:
        P.run(sample(), ALL, "cl2", make_dwg=False)
    assert any("殘留" in n for n in ei.value.__notes__)


def test_successful_and_failed_runs_leave_no_stage(isolate):
    assert P.run(sample(), ALL, "okstage", make_dwg=False).ok
    assert stages(isolate) == []
    assert P.run(sample(start=(1.03, 1, 3)), ALL, "bad", make_dwg=False).ok is False
    assert stages(isolate) == [] and not (isolate / "bad").exists()


def test_junction_named_like_run_id_is_rejected_and_target_untouched(isolate, tmp_path):
    isolate.mkdir(parents=True)
    outside = tmp_path / "victim2"
    outside.mkdir()
    (outside / "important.txt").write_text("data")
    make_link(isolate / "esc1", outside)
    r = P.run(sample(), ALL, "esc1", make_dwg=False)
    assert r.ok is False and r.error.code == "invalid_run_id"
    assert (outside / "important.txt").read_text() == "data" and len(list(outside.iterdir())) == 1


# ---------- 競態：認領 / rename ----------
def test_rename_destination_appearing_mid_run_is_refused_and_left_untouched(isolate, monkeypatch):
    real = os.rename

    def racing_rename(src, dst):
        Path(dst).mkdir()                                     # 別人在檢查之後、rename 之前剛好建好
        (Path(dst) / "winner.txt").write_text("theirs")
        return real(src, dst)                                 # 對非空目的地，Windows/POSIX 都會失敗

    monkeypatch.setattr(P.os, "rename", racing_rename)
    r = P.run(sample(), ALL, "race1", make_dwg=False)
    assert r.ok is False and (r.error.stage, r.error.code) == ("output", "run_exists")
    assert [p.name for p in (isolate / "race1").iterdir()] == ["winner.txt"]
    assert (isolate / "race1" / "winner.txt").read_text() == "theirs"
    assert stages(isolate) == []


def test_concurrent_runs_with_same_run_id_exactly_one_wins_and_winner_is_complete(isolate, monkeypatch, tmp_path):
    ref_root = tmp_path / "ref"
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(ref_root))
    assert P.run(sample(), ALL, "same1", make_dwg=False).ok          # 單獨跑一次當基準
    reference = tree_digest(ref_root / "same1")
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(isolate))
    barrier, out = threading.Barrier(2), []

    def work():
        barrier.wait()
        out.append(P.run(sample(), ALL, "same1", make_dwg=False))

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    oks, bad = [r for r in out if r.ok], [r for r in out if not r.ok]
    assert len(oks) == 1 and len(bad) == 1
    assert (bad[0].error.stage, bad[0].error.code) == ("output", "run_exists")
    assert tree_digest(isolate / "same1") == reference               # 勝出者資料夾完整，且與單獨執行逐位元組相同
    assert stages(isolate) == []


def test_existing_run_dir_is_never_modified_by_a_failed_or_refused_run(isolate):
    assert P.run(sample(), ALL, "keep1", make_dwg=False).ok
    snap = tree_digest(isolate / "keep1")
    P.run(sample(start=(2, 1, 3)), ["CNS"], "keep1", make_dwg=False)             # 被拒
    P.run(sample(start=(1.03, 1, 3)), ALL, "keep1", make_dwg=False)              # 另一種失敗
    assert tree_digest(isolate / "keep1") == snap and stages(isolate) == []


# ---------- 錯誤分界 ----------
@pytest.mark.parametrize("target", ["check_route", "check_compliance"])
def test_value_error_outside_validation_calls_propagates_unconverted(isolate, monkeypatch, target):
    monkeypatch.setattr(P, target, boom(ValueError("deep bug")))
    with pytest.raises(ValueError, match="deep bug"):
        P.run(sample(), ALL, "ve1", make_dwg=False)
    assert not (isolate / "ve1").exists() and stages(isolate) == []


def test_value_error_from_build_model_propagates(isolate, monkeypatch):
    monkeypatch.setattr(P.R, "build_model", boom(ValueError("model bug")))
    with pytest.raises(ValueError, match="model bug"):
        P.run(sample(), ALL, "ve2", make_dwg=False)


def test_malformed_inputs_are_validation_errors_not_leaked_key_or_type_errors(isolate):
    bad_obstacles = ([{"name": "x", "kind": "water"}], ["not-a-dict"],
                     [{"name": "x", "kind": "water", "lo": [0, 0], "hi": [1, 1, 1]}],
                     [{"name": "", "kind": "water", "lo": [0, 0, 0], "hi": [1, 1, 1]}],
                     [{"name": "x", "kind": "water", "lo": [0, 0, float("nan")], "hi": [1, 1, 1]}])
    for obs in bad_obstacles:
        r = P.run(sample(obstacles=obs), ALL, "inv1", make_dwg=False)
        assert r.ok is False and r.error.code == "invalid_input", obs
    for kw in ({"tray_w_mm": float("inf")}, {"ends": []}, {"start": (1, 1)}, {"cables": [{"od_mm": 0}]},
               {"cables": [{"od_mm": 10, "count": 1.5}]}, {"room": ((0, 0, 0), (0, 5, 5))}, {"cell_m": -1}):
        r = P.run(sample(**kw), ALL, "inv2", make_dwg=False)
        assert r.ok is False and r.error.code == "invalid_input", kw


# ---------- codes / basis / type_name ----------
def test_codes_are_deduplicated_preserving_order_and_not_case_normalised(isolate):
    r = P.run(sample(), ["IEC", "CNS", "IEC", "CNS"], "dd1", make_dwg=False)
    assert r.ok and r.codes == ["IEC", "CNS"] and r.inputs.codes == ["IEC", "CNS"]
    low = P.run(sample(), ["cns"], "dd2", make_dwg=False)
    assert low.ok is False and (low.error.stage, low.error.code) == ("rules", "invalid_rules")
    for bad in ("CNS", None, [1, 2], ["CNS", None]):
        r2 = P.run(sample(), bad, "dd3", make_dwg=False)
        assert r2.ok is False and r2.error.code == "invalid_codes"


@pytest.mark.parametrize("kw", [
    {"basis": "world"}, {"basis": ""}, {"basis": None},
    {"type_name": "a" + chr(0) + "b"}, {"type_name": "a" + chr(10) + "b"}, {"type_name": chr(127)},
    {"type_name": "x" * 201}, {"type_name": "   "}, {"type_name": 5},
])
def test_basis_and_type_name_validated_before_anything_is_written(isolate, kw):
    r = P.run(sample(), ALL, "bt1", make_dwg=False, **kw)
    assert r.ok is False and r.error.stage == "validate" and r.error.code in ("invalid_basis", "invalid_type_name")
    assert not (isolate / "bt1").exists() and stages(isolate) == []


# ---------- 吊架預設值不入規範值 ----------
def test_default_hanger_span_is_not_a_governing_value_and_is_not_checked_as_code(isolate):
    d = P.run(Inputs(cell_m=0.25), ["TW_BUILDING"], "hs1", make_dwg=False)
    assert d.ok and "span_max_m" not in d.gov
    assert d.stats["hanger_span_source"] == "default" and d.stats["hanger_span_m"] == P.DEFAULT_SPAN_M
    assert all(c.kind != "hanger_span" for c in d.reports["compliance"].checks)    # 不拿預設值當規範去驗算
    c = P.run(Inputs(cell_m=0.25), ["CNS"], "hs2", make_dwg=False)
    assert c.stats["hanger_span_source"] == "CNS" and c.stats["hanger_span_m"] == 2.0
    assert any(x.kind == "hanger_span" for x in c.reports["compliance"].checks)


# ---------- 確定性與等價 ----------
def manual_outputs(inp, codes, run_id):
    """不經 pipeline、依相同順序手動呼叫各模組，產出到目前的 MEP_OUTPUT_ROOT。"""
    from mep_tray.clash import check_route
    from mep_tray.compliance import check_compliance
    from mep_tray.router import place_hangers, try_route_tray
    from mep_tray.rules import merge_strictest
    inp = dataclasses.replace(inp, codes=list(codes))
    gov = merge_strictest(codes, load_rules())
    res = try_route_tray(inp.room_box(), inp.start, inp.ends, inp.obstacle_objs(),
                         inp.tray_w_mm / 1000, inp.tray_h_mm / 1000, inp.tray_type, gov, cell=inp.cell_m)
    route = res.route
    place_hangers(route, gov["span_max_m"].value)
    reps = [check_route(inp, route, gov), check_compliance(inp, route, gov)]
    dxf = X.export_dxf(inp, route, reps, gov, run_id, notes=[], convert_dwg=False)
    model = R.build_model(inp, route, reps, gov, run_id, type_name=None, basis="UNSPECIFIED", notes=[])
    return dxf.dxf, R.write_model(model, run_id)


def test_pipeline_output_is_byte_identical_to_manual_calls(isolate, monkeypatch, tmp_path):
    inp = sample()
    r = P.run(inp, ALL, "eq1", make_dwg=False)
    assert r.ok
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "manual"))
    dxf, js = manual_outputs(inp, ALL, "eq1")
    assert r.files["dxf"].read_bytes() == dxf.read_bytes()
    assert r.files["revit_json"].read_bytes() == js.read_bytes()


def test_same_input_and_run_id_in_different_roots_is_byte_identical_despite_time_passing(isolate, monkeypatch, tmp_path):
    a = P.run(sample(), ALL, "det1", make_dwg=False)
    time.sleep(1.1)                                           # 時間戳若沒被固定，這裡必然不同
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "other"))
    b = P.run(sample(), ALL, "det1", make_dwg=False)
    assert a.files["dxf"].read_bytes() == b.files["dxf"].read_bytes()
    assert a.files["revit_json"].read_bytes() == b.files["revit_json"].read_bytes()


def test_dxf_guids_follow_input_and_rules_not_wall_clock(isolate):
    import ezdxf
    g = lambda r: ezdxf.readfile(r.files["dxf"]).header["$FINGERPRINTGUID"]
    a = P.run(sample(), ALL, "gd1", make_dwg=False)
    b = P.run(sample(), ALL, "gd2", make_dwg=False)
    c = P.run(sample(tray_w_mm=400), ALL, "gd3", make_dwg=False)
    d = P.run(sample(), ["CNS"], "gd4", make_dwg=False)
    assert g(a) == g(b) != g(c) and g(a) != g(d)
    assert g(a) != "{00000000-0000-0000-0000-000000000000}"


def test_dxf_still_audits_clean_after_deterministic_rewrite(isolate):
    import ezdxf
    r = P.run(sample(), ALL, "au1", make_dwg=False)
    doc = ezdxf.readfile(r.files["dxf"])
    assert not doc.audit().has_errors and doc.header["$INSUNITS"] == 4


def test_export_restores_global_ezdxf_option_even_when_save_fails(isolate, monkeypatch):
    import ezdxf
    from ezdxf.document import Drawing
    assert ezdxf.options.write_fixed_meta_data_for_testing is False
    monkeypatch.setattr(Drawing, "saveas", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk")))
    r = P.run(sample(), ALL, "opt1", make_dwg=False)
    assert r.ok is False and r.error.code == "os_error"
    assert ezdxf.options.write_fixed_meta_data_for_testing is False


# ---------- 格距覆寫（UI 不必改 Inputs）----------
def test_cell_override_parameter_wins_over_inputs_and_does_not_mutate_them(isolate):
    base = Inputs(cell_m=0.1)
    r = P.run(base, ALL, "cm1", make_dwg=False, cell_m=0.25)
    assert r.ok and r.route.cell_m == 0.25 and r.inputs.cell_m == 0.25 and base.cell_m == 0.1
    r2 = P.run(Inputs(cell_m=0.25), ALL, "cm2", make_dwg=False)               # None → 沿用 inputs.cell_m
    assert r2.ok and r2.route.cell_m == 0.25
    bad = P.run(Inputs(), ALL, "cm3", make_dwg=False, cell_m=-1)
    assert bad.ok is False and bad.error.code == "invalid_input" and not (isolate / "cm3").exists()


# ---------- 遞迴脫敏 ----------
def test_scrub_is_recursive_over_dicts_and_lists_and_leaves_numbers_alone():
    home = str(Path.home())
    out = P._scrub({"a": [home + "/x", {"b": "ok " + home + "/y"}], "n": 1, "t": ("p", home), "none": None})
    assert home not in json.dumps(out, ensure_ascii=False) and out["n"] == 1 and out["none"] is None
    assert "<HOME>" in out["a"][0] and isinstance(out["t"], list)


# ---------- ezdxf 選項防護與降級重匯入 ----------
def test_missing_ezdxf_option_raises_instead_of_silently_losing_determinism(tmp_path):
    import types
    fake_ezdxf = types.SimpleNamespace(options=object(), __version__="0")
    fake_doc = types.SimpleNamespace(ezdxf_metadata=lambda: {}, saveas=lambda p: pytest.fail("不應存檔"))
    orig = X.ezdxf
    X.ezdxf = fake_ezdxf
    try:
        with pytest.raises(RuntimeError, match="write_fixed_meta_data_for_testing"):
            X._save_deterministic(fake_doc, tmp_path / "x.dxf", "seed")
    finally:
        X.ezdxf = orig


def test_determinism_holds_after_ezdxf_fallback_reimport(isolate, monkeypatch, tmp_path):
    import importlib
    import sys
    monkeypatch.delenv("EZDXF_DISABLE_C_EXT", raising=False)       # 讓降級路徑設定的變數於測試後被還原
    saved = {k: v for k, v in sys.modules.items() if k == "ezdxf" or k.startswith("ezdxf.")}
    old_mod, calls = X.ezdxf, []

    def importer(name):
        calls.append(1)
        if len(calls) == 1:
            raise ImportError("DLL load failed while importing matrix44")
        return importlib.import_module(name)

    try:
        new_mod = X.load_ezdxf(importer)
        assert new_mod is not old_mod and new_mod.options is not old_mod.options     # 確實是重新匯入的另一份模組
        monkeypatch.setattr(X, "ezdxf", new_mod)                                     # export_dxf 實際使用的模組物件
        a = P.run(sample(), ALL, "fb1", make_dwg=False)
        time.sleep(1.1)
        monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "fb_other"))
        b = P.run(sample(), ALL, "fb1", make_dwg=False)
        assert a.ok and b.ok and a.files["dxf"].read_bytes() == b.files["dxf"].read_bytes()
        assert new_mod.options.write_fixed_meta_data_for_testing is False            # 在「使用的那份」上還原
        assert old_mod.options.write_fixed_meta_data_for_testing is False
    finally:
        for k in [k for k in sys.modules if k == "ezdxf" or k.startswith("ezdxf.")]:
            del sys.modules[k]
        sys.modules.update(saved)
