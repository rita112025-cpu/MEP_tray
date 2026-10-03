"""versioning 的不可信輸入加固、掃描、引擎判定、finding 鍵穩定性與管線整合測試。"""
import copy
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mep_tray import acad
from mep_tray import export_dxf as X
from mep_tray import pipeline as P
from mep_tray import versioning as V
from mep_tray.acad import AcadResult
from tests.test_pipeline import ALL, isolate, sample  # noqa: F401  (isolate 為 autouse fixture)
from tests.test_versioning import mk


def manifest_path(r):
    return r.files["dxf"].parent / V.MANIFEST_NAME


def rewrite_manifest(r, edit):
    mp = manifest_path(r)
    m = json.loads(mp.read_text(encoding="utf-8"))
    edit(m)
    mp.write_text(json.dumps(m), encoding="utf-8")


# ───────────── 語意與措辭 ─────────────
def test_determinism_labels_state_that_they_hold_for_the_same_run_id(isolate):
    _, m = mk("lab1")
    assert m["files"]["revit_json"]["determinism"] == "same-run-id"
    assert m["files"]["dxf"]["determinism"] == "same-run-id-no-cad"
    assert V.DETERMINISM["dwg"] == "never"


def test_integrity_error_wording_does_not_claim_signature_or_verification(isolate):
    r, _ = mk("wd1")
    b = bytearray(r.files["dxf"].read_bytes())
    b[len(b) // 2] ^= 1
    r.files["dxf"].write_bytes(bytes(b))
    msg = V.load_version("wd1").error.message
    assert "完整性檢查" in msg and "不防刻意竄改" in msg and "簽章" not in msg and "驗證通過" not in msg


# ───────────── 不可信輸入：路徑穿越 ─────────────
@pytest.mark.parametrize("evil", ["../../x.dxf", "..", "a/b.dxf", "C:/x.dxf", "x.exe", ".hidden.dxf",
                                  "a b.dxf", "", None, 5, "x" * 200 + ".dxf"])
def test_tampered_file_name_in_manifest_is_rejected_without_touching_other_files(isolate, evil):
    r, _ = mk("tn1")
    rewrite_manifest(r, lambda m: m["files"]["dxf"].update(name=evil))
    lr = V.load_version("tn1")
    assert lr.ok is False and lr.error.code == "corrupt_manifest"


def test_file_entry_with_unknown_key_or_non_object_is_rejected(isolate):
    r, _ = mk("tk1")
    rewrite_manifest(r, lambda m: m["files"].update(evil={"name": "x.dxf", "sha256": "0", "bytes": 1}))
    assert V.load_version("tk1").error.code == "corrupt_manifest"
    r2, _ = mk("tk2")
    rewrite_manifest(r2, lambda m: m["files"].update(dxf="not-an-object"))
    assert V.load_version("tk2").error.code == "corrupt_manifest"


@pytest.mark.parametrize("rid", ["../x", "..", "a/b", "", "x" * 41, "中文", "a b", "/abs", None])
def test_run_id_arguments_are_validated_in_load_and_compare(isolate, rid):
    assert V.load_version(rid).error.code == "invalid_run_id"
    mk("ok-a")
    assert V.compare(rid, "ok-a").ok is False and V.compare("ok-a", rid).error.code == "invalid_run_id"


def make_link(link: Path, target: Path):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        rc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, shell=False) if os.name == "nt" else None
        if rc is None or rc.returncode != 0:
            pytest.skip("此環境無法建立 symlink/junction")


def test_version_folder_that_is_a_junction_to_outside_the_root_is_rejected(isolate, tmp_path):
    outside = tmp_path / "outside_version"
    r, _ = mk("src1")                                      # 取得一份完整版本，複製到根目錄之外
    import shutil
    shutil.copytree(r.files["dxf"].parent, outside)
    make_link(isolate / "esc-ver", outside)
    lr = V.load_version("esc-ver")
    assert lr.ok is False and lr.error.code in ("invalid_run_id", "missing_version")
    d = V.compare("src1", "esc-ver")
    assert d.ok is False
    infos = {v.run_id for v in V.list_versions()}
    assert "esc-ver" not in infos                          # 列表也不把它當版本


# ───────────── 巨大 / 深層 manifest ─────────────
def test_oversized_manifest_is_rejected_before_being_read(isolate, monkeypatch):
    r, _ = mk("big1")
    manifest_path(r).write_bytes(b"0" * (V.MAX_MANIFEST_BYTES + 1))
    called = []
    monkeypatch.setattr(V.json, "loads", lambda *a, **k: called.append(1))
    lr = V.load_version("big1")
    assert lr.ok is False and lr.error.code == "corrupt_manifest" and not called     # 沒有走到解析


@pytest.mark.parametrize("depth", [2000, 200000])
def test_deeply_nested_manifest_is_a_structured_error_not_a_crash(isolate, depth):
    r, _ = mk("deep1")
    manifest_path(r).write_text("[" * depth + "]" * depth, encoding="utf-8")
    lr = V.load_version("deep1")
    assert lr.ok is False and lr.error.code == "corrupt_manifest"


def test_deep_nesting_inside_a_valid_looking_manifest_is_structured(isolate):
    r, m = mk("deep2")
    m2 = copy.deepcopy(m)
    m2["inputs"]["evil"] = "@@DEEP@@"
    text = json.dumps(m2).replace('"@@DEEP@@"', "[" * 5000 + "]" * 5000)      # 用字串拼，避免測試端 dumps 自己遞迴爆掉
    manifest_path(r).write_text(text, encoding="utf-8")
    lr = V.load_version("deep2")
    assert lr.ok is False and lr.error.code == "corrupt_manifest"


# ───────────── 掃描 ─────────────
def test_scan_reports_ignored_folders_and_stage_leftovers_and_never_opens_output_files(isolate, monkeypatch):
    mk("v1")
    (isolate / "revit2025_gui").mkdir()                    # 既有的非版本資料夾（例如人工驗收輸出）
    (isolate / "stray.txt").write_text("x")
    (isolate / "bad name!").mkdir()
    (isolate / (P.STAGE_PREFIX + "a")).mkdir()
    (isolate / (P.STAGE_PREFIX + "b")).mkdir()
    monkeypatch.setattr(V, "file_sha256", lambda *a, **k: (_ for _ in ()).throw(AssertionError("列表不得雜湊產出檔")))
    scan = V.scan_versions()
    assert [v.run_id for v in scan.versions] == ["v1"]
    assert sorted(scan.ignored) == ["bad name!", "revit2025_gui", "stray.txt"]
    assert scan.stage_leftovers == 2
    assert V.list_versions() == scan.versions or [v.run_id for v in V.list_versions()] == ["v1"]
    json.dumps(scan.to_dict(), allow_nan=False)


def test_list_succeeds_even_when_an_output_file_is_unreadable_or_missing(isolate):
    r, _ = mk("miss1")
    r.files["dxf"].unlink()
    assert [v.run_id for v in V.list_versions()] == ["miss1"]                    # 列表只讀 manifest
    assert V.load_version("miss1").error.code == "missing_file"                  # 完整性檢查按需執行


# ───────────── 引擎判定 ─────────────
def test_code_hash_ignores_line_endings_but_tracks_content_and_names(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    base = V._code_hash(tmp_path, ("a.py",))
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")
    assert V._code_hash(tmp_path, ("a.py",)) == base                              # CRLF 與 LF 等價
    (tmp_path / "a.py").write_bytes(b"x = 1\ny = 3\n")
    assert V._code_hash(tmp_path, ("a.py",)) != base                              # 內容改變
    (tmp_path / "b.py").write_bytes(b"x = 1\ny = 2\n")
    assert V._code_hash(tmp_path, ("b.py",)) != V._code_hash(tmp_path, ("a.py",)) # 檔名也進雜湊


def test_manifest_records_code_hash_and_engine_modules_exist(isolate):
    _, m = mk("eng0")
    assert m["engine"]["code_sha256"] == V.code_sha256() and len(m["engine"]["code_sha256"]) == 64
    base = Path(V.__file__).resolve().parent
    files = V.engine_files(base)
    assert {"router.py", "clash.py", "compliance.py", "rules.py", "geometry.py", "export_revit.py",
            "model.py", "pipeline.py", "findings.py"} <= set(files)           # 含預設值所在檔案
    assert not (set(files) & V.ENGINE_EXCLUDED)


def test_code_hash_change_makes_source_engine_and_same_engine_makes_it_unexplained(isolate):
    _, m = mk("eng1")

    def variant(code=None):
        n = copy.deepcopy(m)
        n["result"]["segment_count"] += 1
        n["hashes"]["result_sha256"] = V.hash_obj(n["result"])
        if code:
            n["engine"]["code_sha256"] = code
        return n
    assert V.compare_manifests(m, variant("0" * 64)).source == "engine"
    assert V.compare_manifests(m, variant()).source == "unexplained"
    n = copy.deepcopy(m)
    n["engine"]["version"] = "9.9.9"
    n["result"]["segment_count"] += 1
    n["hashes"]["result_sha256"] = V.hash_obj(n["result"])
    assert V.compare_manifests(m, n).source == "engine"


def test_comparing_a_version_with_itself_is_none_and_everything_is_empty(isolate):
    mk("self1")
    d = V.compare("self1", "self1")
    assert d.ok and d.source == "none" and d.results_equal
    assert V._is_empty(d.inputs) and not d.rules["effective_changed"] and not d.rules["hash_changed"]
    assert d.result["segments"] == {"added": [], "removed": []} and d.result["counts"] == {}
    assert d.result["findings"] == {"added": [], "resolved": [], "changed": []}


# ───────────── finding 鍵穩定性（平移）─────────────
def shifted(dx):
    return sample(start=(1 + dx, 1, 3), ends=[(10 + dx, 1, 3), (6 + dx, 5, 3)],
                  obstacles=[{"name": "W", "kind": "water", "lo": [5 + dx, 0, 0], "hi": [5.3 + dx, 4, 3.2]}])


def test_translating_the_whole_layout_changes_segments_but_not_finding_keys(isolate):
    _, a = mk("tr-a")
    _, b = mk("tr-b", inp=shifted(1))
    d = V.compare("tr-a", "tr-b")
    assert d.ok and d.source == "inputs"
    assert d.result["segments"]["added"] and d.result["segments"]["removed"]          # 路徑確實平移了
    f = d.result["findings"]
    assert f["added"] == [] and f["resolved"] == []                                   # 鍵沒變：沒有「新增/已解決」假象
    keys = lambda m: {(x["kind"], x["subject"], x["code"]) for x in m["result"]["findings"]}
    assert keys(a) == keys(b) and keys(a)


def test_finding_subjects_do_not_embed_coordinates_or_serial_numbers(isolate):
    import re
    inp = sample(obstacles=[], cables=[{"od_mm": 60, "count": 30, "kind": "power"}], room=((0, 0, 0), (12, 6, 4)),
                 start=(1, 1, 3), ends=[(1.25, 1.25, 3)])             # 短腿 → 彎頭 finding；重電纜 → 填充 finding
    _, m = mk("subj1", inp=inp)
    kinds = {f["kind"] for f in m["result"]["findings"]}
    assert {"fill", "bend"} <= kinds
    for f in m["result"]["findings"]:
        assert not re.search(r"\d+\.\d+|S\d{3}|\(\s*-?\d", f["subject"].replace("12×", "")), f["subject"]


# ───────────── 管線整合 ─────────────
def test_pipeline_writes_manifest_last_and_it_loads_clean(isolate):
    now = datetime(2026, 5, 6, 7, 8, 9, 123456, tzinfo=timezone.utc)
    r = P.run(sample(), ALL, "pm1", make_dwg=False, now=now)
    assert r.ok and r.files["manifest"] == isolate / "pm1" / "manifest.json"
    assert r.manifest["created_at"] == "2026-05-06T07:08:09Z"                        # 微秒被截掉、固定格式
    lr = V.load_version("pm1")
    assert lr.ok and lr.manifest == json.loads(r.files["manifest"].read_text(encoding="utf-8"))
    assert r.to_summary_dict()["hashes"]["result_sha256"] == r.manifest["hashes"]["result_sha256"]
    assert sorted(p.name for p in (isolate / "pm1").iterdir()) == sorted(
        ["manifest.json", "report_pm1.html", "tray_pm1.dxf", "tray_pm1.json"])


def test_failed_runs_never_leave_a_manifest_or_a_folder(isolate, monkeypatch):
    monkeypatch.setattr(P.V, "write_manifest", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "disk full")))
    r = P.run(sample(), ALL, "pm2", make_dwg=False)
    assert r.ok is False and r.error.code == "os_error" and not (isolate / "pm2").exists()
    assert V.list_versions() == [] and V.scan_versions().stage_leftovers == 0


def test_environment_is_not_probed_when_cad_is_forbidden_and_probed_when_allowed(isolate, monkeypatch, tmp_path):
    def forbidden(*a, **k):
        raise AssertionError("make_dwg=False 不得探測 CAD")
    monkeypatch.setattr(acad, "find_accore", forbidden)
    monkeypatch.setattr(X, "find_oda", forbidden)
    off = P.run(sample(), ALL, "env-off", make_dwg=False)
    assert off.ok and off.manifest["environment"]["acad_available"] is None
    assert off.manifest["environment"]["oda_available"] is None
    fake_exe = tmp_path / "AutoCAD 2027" / "accoreconsole.exe"
    monkeypatch.setattr(acad, "find_accore", lambda: fake_exe)
    monkeypatch.setattr(X, "find_oda", lambda: None)
    def fake_convert(dxf, dwg_out=None, accore=None, **k):
        if dwg_out is not None:
            Path(dwg_out).write_bytes(b"AC1032")               # 真實的 acad 只在檔案存在時才回傳 dwg 路徑
        return AcadResult(0, dwg_out, "AutoCAD 2027", "ok")
    monkeypatch.setattr(acad, "audit_and_convert", fake_convert)
    on = P.run(sample(), ALL, "env-on", make_dwg=True)
    assert on.ok
    env = on.manifest["environment"]
    assert env["acad_available"] is True and env["acad_version"] == "AutoCAD 2027" and env["oda_available"] is False
    assert on.manifest["hashes"]["result_sha256"] == off.manifest["hashes"]["result_sha256"]


def test_manifest_ordering_is_stable_and_file_is_strict_json(isolate):
    r, _ = mk("js1")
    text = manifest_path(r).read_text(encoding="utf-8")
    assert json.loads(text, parse_constant=lambda c: pytest.fail("NaN/Infinity")) == r.manifest
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)


# ───────────── 補充：engine_changed 獨立於 source ─────────────
def test_engine_change_is_reported_next_to_inputs_source(isolate):
    _, a = mk("ec-a")
    _, b = mk("ec-b", inp=sample(tray_w_mm=400))
    b = copy.deepcopy(b)
    b["engine"]["code_sha256"] = "0" * 64                    # 兩版引擎程式碼不同
    d = V.compare_manifests(a, b)
    assert d.source == "inputs" and d.engine_changed is True                      # 不被「輸入變了」掩蓋
    assert d.engine == {"code_sha256": [a["engine"]["code_sha256"], "0" * 64]}
    assert d.to_dict()["engine_changed"] is True and d.to_dict()["engine"]["code_sha256"][1] == "0" * 64
    n = copy.deepcopy(b)
    n["engine"]["version"] = "9.9.9"
    assert set(V.compare_manifests(a, n).engine) == {"code_sha256", "version"}


def test_same_inputs_and_rules_with_different_engine_is_source_engine_and_flagged(isolate):
    _, a = mk("ee-a")
    b = copy.deepcopy(a)
    b["engine"]["code_sha256"] = "f" * 64
    b["result"]["segment_count"] += 1
    b["hashes"]["result_sha256"] = V.hash_obj(b["result"])
    d = V.compare_manifests(a, b)
    assert d.source == "engine" and d.engine_changed is True


def test_engine_changed_is_false_when_engines_match(isolate):
    mk("en-a")
    mk("en-b", inp=sample(tray_w_mm=400))
    d = V.compare("en-a", "en-b")
    assert d.source == "inputs" and d.engine_changed is False and d.engine == {}


# ───────────── 補充：原始碼雜湊的範圍（fail-safe）─────────────
def package_copy(tmp_path):
    import shutil
    dst = tmp_path / "pkg"
    dst.mkdir()
    for p in Path(V.__file__).resolve().parent.glob("*.py"):
        shutil.copy2(p, dst / p.name)
    return dst


def hash_of(pkg):
    return V._code_hash(pkg, V.engine_files(pkg))


def test_code_hash_changes_when_default_cable_or_default_span_source_changes(tmp_path):
    pkg = package_copy(tmp_path)
    base = hash_of(pkg)
    model = pkg / "model.py"
    src = model.read_text(encoding="utf-8")
    assert '"od_mm": 20.0' in src
    model.write_text(src.replace('"od_mm": 20.0', '"od_mm": 25.0'), encoding="utf-8")           # DEFAULT_CABLE
    assert hash_of(pkg) != base
    model.write_text(src, encoding="utf-8")
    assert hash_of(pkg) == base
    pipe = pkg / "pipeline.py"
    psrc = pipe.read_text(encoding="utf-8")
    assert "DEFAULT_SPAN_M = 2.0" in psrc
    pipe.write_text(psrc.replace("DEFAULT_SPAN_M = 2.0", "DEFAULT_SPAN_M = 2.5"), encoding="utf-8")
    assert hash_of(pkg) != base
    pipe.write_text(psrc, encoding="utf-8")
    findings = pkg / "findings.py"
    fsrc = findings.read_text(encoding="utf-8")
    findings.write_text(fsrc + "\n# changed\n", encoding="utf-8")
    assert hash_of(pkg) != base


def test_code_hash_includes_new_modules_by_default_but_not_explicitly_excluded_ones(tmp_path):
    pkg = package_copy(tmp_path)
    base = hash_of(pkg)
    (pkg / "brand_new_module.py").write_text("X = 1\n", encoding="utf-8")                          # 不在排除清單 → 納入
    with_new = hash_of(pkg)
    assert with_new != base and "brand_new_module.py" in V.engine_files(pkg)
    for name in ("report.py", "webui.py"):                                                         # 排除清單內（尚未存在也可建立）
        (pkg / name).write_text("Y = 2\n", encoding="utf-8")
    assert hash_of(pkg) == with_new
    for name in ("sanitize.py", "versioning.py", "acad.py", "export_dxf.py", "paths.py", "disclosure.py", "__init__.py"):
        p = pkg / name
        p.write_text(p.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    assert hash_of(pkg) == with_new


def test_excluded_list_is_explicit_and_contains_only_non_result_modules():
    assert V.ENGINE_EXCLUDED == {"__init__.py", "sanitize.py", "versioning.py", "acad.py", "export_dxf.py",
                                 "paths.py", "disclosure.py", "report.py", "webui.py"}
    assert not ({"model.py", "pipeline.py", "findings.py", "router.py"} & V.ENGINE_EXCLUDED)
