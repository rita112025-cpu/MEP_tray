import copy
import json
import re
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mep_tray import pipeline as P
from mep_tray import versioning as V
from mep_tray.model import Inputs
from mep_tray.paths import RUN_ID_RE, output_root
from mep_tray.rules import load_rules
from tests.test_pipeline import ALL, isolate, sample  # noqa: F401  (isolate 為 autouse fixture)

def mk(run_id, inp=None, codes=ALL, *, rules=None, created_at="2026-01-01T00:00:00Z", type_name=None,
       basis="UNSPECIFIED", notes=()):
    """執行管線（manifest 由管線在暫存資料夾內寫入）；回傳 (RunResult, manifest)。"""
    now = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    r = P.run(inp or sample(), codes, run_id, make_dwg=False, rules=rules, type_name=type_name, basis=basis,
              notes=notes, now=now)
    assert r.ok, r.error
    return r, r.manifest


def with_rules(fn):
    rules = load_rules()
    fn(rules)
    return rules


# ───────────── 雜湊正規化 ─────────────
def test_canonical_hash_ignores_key_order_negative_zero_integral_floats_and_tiny_float_noise():
    assert V.hash_obj({"a": 1, "b": [1, 2]}) == V.hash_obj({"b": [1, 2], "a": 1})
    assert V.hash_obj({"x": -0.0}) == V.hash_obj({"x": 0}) == V.hash_obj({"x": 0.0})
    assert V.hash_obj({"x": 300}) == V.hash_obj({"x": 300.0})
    assert V.hash_obj({"x": 0.1 + 0.2}) == V.hash_obj({"x": 0.3})
    assert V.hash_obj({"x": 0.3}) != V.hash_obj({"x": 0.31})
    for bad in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            V.hash_obj({"x": bad})
    with pytest.raises(TypeError):
        V.hash_obj({"x": object()})


def test_segments_are_normalised_independent_of_direction_order_and_numbering():
    S = types.SimpleNamespace
    a = S(segments=[((1, 1, 3), (6, 1, 3)), ((6, 1, 3), (10, 1, 3)), ((6, 1, 3), (6, 5, 3))])
    b = S(segments=[((6, 5, 3), (6, 1, 3)), ((10, 1, 3), (6, 1, 3)), ((6, 1, 3), (1, 1, 3))])
    assert V.normalized_segments(a) == V.normalized_segments(b)
    assert V.normalized_segments(a)[0] == [[1000.0, 1000.0, 3000.0], [6000.0, 1000.0, 3000.0]]
    c = S(segments=[((1, 1, 3), (6, 1, 3)), ((6, 1, 3), (10, 1, 3))])
    assert V.normalized_segments(a) != V.normalized_segments(c)


def test_input_hash_ignores_ends_obstacle_cable_order_but_not_codes_order():
    obs = [{"name": "A", "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 2, 3]},
           {"name": "B", "kind": "duct", "lo": [8, 2, 0], "hi": [8.5, 6, 3]}]
    cab = [{"od_mm": 20, "count": 5, "kind": "power"}, {"od_mm": 30, "count": 2, "kind": "power"}]
    i1 = Inputs(ends=[(10, 1, 3), (6, 5, 3)], obstacles=obs, cables=cab)
    i2 = Inputs(ends=[(6, 5, 3), (10, 1, 3)], obstacles=list(reversed(obs)), cables=list(reversed(cab)))
    assert V.input_hash(i1, ["CNS", "IEC"]) == V.input_hash(i2, ["CNS", "IEC"])
    assert V.input_hash(i1, ["CNS", "IEC"]) != V.input_hash(i1, ["IEC", "CNS"])   # 順序影響並列時的勝出歸屬
    assert V.input_hash(i1, ["CNS"], basis="INTERNAL_ORIGIN") != V.input_hash(i1, ["CNS"])
    assert V.input_hash(i1, ["CNS"], type_name="X") != V.input_hash(i1, ["CNS"])
    assert V.input_hash(Inputs(tray_w_mm=300), ["CNS"]) == V.input_hash(Inputs(tray_w_mm=300.0), ["CNS"])


# ───────────── 確定性（相同輸入 → 相同雜湊）─────────────
def strip_volatile(m):
    m = copy.deepcopy(m)
    for k in ("run_id", "created_at", "files", "disclosures", "dwg_note"):
        m.pop(k)
    return m


def test_same_input_and_rules_give_identical_hashes_across_run_ids_and_roots(isolate, monkeypatch, tmp_path):
    _, a = mk("det-a", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "other"))
    _, b = mk("det-b", created_at="2026-06-30T12:00:00Z")
    assert a["hashes"] == b["hashes"] and strip_volatile(a) == strip_volatile(b)
    assert a["files"]["revit_json"]["sha256"] != b["files"]["revit_json"]["sha256"]     # 檔案內嵌 run_id，本來就不同


def test_same_run_id_in_different_roots_gives_identical_file_hashes(isolate, monkeypatch, tmp_path):
    _, a = mk("same")
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "other"))
    _, b = mk("same")
    assert a["files"] == b["files"]


def test_result_hash_does_not_depend_on_dxf_bytes_or_environment(isolate, monkeypatch, tmp_path):
    from mep_tray import acad, export_dxf as X
    from mep_tray.acad import AcadResult
    _, plain = mk("env1")
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "cad"))
    fake_exe = tmp_path / "AutoCAD 2027" / "accoreconsole.exe"
    monkeypatch.setattr(acad, "find_accore", lambda: fake_exe)
    monkeypatch.setattr(acad, "audit_and_convert",
                        lambda dxf, dwg_out=None, accore=None, **k: AcadResult(0, None, "AutoCAD 2027", "fake"))
    inp = sample()
    r = P.run(inp, ALL, "env1", make_dwg=True)
    assert r.ok and r.acad_audit == 0
    m = V.build_manifest(run_id="env1", created_at="2026-01-01T00:00:00Z", inp=r.inputs, codes=r.codes,
                         gov=r.gov, rules=load_rules(), route=r.route, reports=r.reports, joints=r.stats["joints"],
                         span_m=r.stats["hanger_span_m"], span_source=r.stats["hanger_span_source"],
                         files=r.files, environment={"acad_available": True, "acad_version": "AutoCAD 2027",
                                                     "oda_available": False})
    assert m["files"]["dxf"]["sha256"] != plain["files"]["dxf"]["sha256"]                # 圖面多了稽核行
    assert m["hashes"] == plain["hashes"]                                                # 結果雜湊不受影響
    assert m["environment"] != plain["environment"]


# ───────────── 差異來源 ─────────────
def test_only_rules_change_has_source_rules_and_lists_the_changed_value(isolate):
    mk("base")
    r2 = with_rules(lambda r: r["codes"]["MRT_APPX_C"]["values"].update(span_max_m=0.8))      # 勝出者（最嚴）
    mk("rules", rules=r2)
    d = V.compare("base", "rules")
    assert d.ok and d.source == "rules" and not d.results_equal
    assert V._is_empty(d.inputs)
    ch = d.rules["changed"]["span_max_m"]
    assert ch["value"] == [1.0, 0.8] and d.rules["effective_changed"] and d.rules["hash_changed"]
    assert d.result["counts"]["hanger_count"]["delta"] > 0


def test_non_governing_rule_change_is_hash_only_and_not_a_rules_change(isolate):
    mk("base")
    r2 = with_rules(lambda r: r["codes"]["NEC"]["values"].update(span_max_m=2.9))     # NEC 的 3.0 本來就沒勝出
    mk("hashonly", rules=r2)
    d = V.compare("base", "hashonly")
    assert d.ok and d.rules["hash_only_change"] is True and not d.rules["effective_changed"]
    assert d.source == "none" and d.results_equal


def test_verified_toggle_is_reported_in_rules_diff(isolate):
    mk("base")
    r2 = with_rules(lambda r: r["codes"]["MRT_APPX_C"].update(verified=True))
    mk("ver", rules=r2)
    d = V.compare("base", "ver")
    assert d.source == "rules" and d.rules["changed"]["span_max_m"]["verified"] == [False, True]
    assert d.result["counts"]["unverified_checks"]["delta"] < 0


def test_only_input_change_has_source_inputs_and_empty_rules_diff(isolate):
    mk("base")
    mk("wider", inp=sample(tray_w_mm=400))
    d = V.compare("base", "wider")
    assert d.ok and d.source == "inputs" and not d.rules["effective_changed"] and not d.rules["hash_changed"]
    assert d.inputs["changed"]["inputs.tray_w_mm"] == [300, 400]


def test_both_changed(isolate):
    mk("base")
    r2 = with_rules(lambda r: r["codes"]["MRT_APPX_C"]["values"].update(span_max_m=0.8))
    mk("both", inp=sample(tray_w_mm=400), rules=r2)
    assert V.compare("base", "both").source == "both"


def test_options_and_codes_changes_are_input_changes(isolate):
    mk("base")
    mk("opt", basis="INTERNAL_ORIGIN", type_name="Ladder Cable Tray")
    d = V.compare("base", "opt")
    assert d.source == "inputs" and d.inputs["changed"]["options.basis"] == ["UNSPECIFIED", "INTERNAL_ORIGIN"]
    mk("codes", codes=["CNS"])
    c = V.compare("base", "codes")
    assert sorted(c.inputs["removed"]) == ["codes[1]", "codes[2]", "codes[3]", "codes[4]"]
    assert c.rules["effective_changed"] and c.source == "both"          # 少了規範 → 生效的規範值也變了


def test_moving_one_obstacle_changes_segments(isolate):
    mk("base")
    # 原本的牆（y 0~4）擋住直線，路徑繞到 y≈5；把牆移到 y 2~6 後，y=1 的直線已暢通
    moved = sample(obstacles=[{"name": "W", "kind": "water", "lo": [5, 2, 0], "hi": [5.3, 6, 3.2]}])
    mk("moved", inp=moved)
    d = V.compare("base", "moved")
    assert d.source == "inputs" and d.result["segments"]["added"] and d.result["segments"]["removed"]
    assert d.inputs["changed"]["inputs.obstacles[0].lo[1]"] == [0, 2]
    assert not d.results_equal and d.result["counts"]["length_m"]["delta"] != 0


def test_finding_changes_are_keyed_and_aggregated(isolate):
    mk("base")
    mk("more", inp=sample(cables=[{"od_mm": 30, "count": 80, "kind": "power"}]))
    d = V.compare("base", "more")
    fills = [c for c in d.result["findings"]["changed"] if c["kind"] == "fill"]
    assert len(fills) == 1 and fills[0]["before"]["values"] != fills[0]["after"]["values"]
    mk("none", inp=sample(cables=[{"od_mm": 5, "count": 2, "kind": "power"}]))
    gone = V.compare("base", "none")
    assert any(f["kind"] == "fill" for f in gone.result["findings"]["resolved"])
    back = V.compare("none", "base")
    assert any(f["kind"] == "fill" for f in back.result["findings"]["added"])


def test_identical_versions_compare_as_none_and_equal(isolate):
    mk("a")
    mk("b")
    d = V.compare("a", "b")
    assert d.ok and d.source == "none" and d.results_equal and V._is_empty(d.inputs)
    assert not d.result["segments"]["added"] and not d.result["findings"]["changed"]


def test_engine_and_unexplained_sources_with_synthetic_manifests(isolate):
    _, m = mk("base")

    def variant(edit_engine=False):
        n = copy.deepcopy(m)
        n["result"]["segment_count"] += 1
        n["hashes"]["result_sha256"] = V.hash_obj(n["result"])
        if edit_engine:
            n["engine"] = {"version": "9.9.9", "result_algo": 99}
        return n
    assert V.compare_manifests(m, variant(True)).source == "engine"
    assert V.compare_manifests(m, variant(False)).source == "unexplained"
    assert V.compare_manifests(m, copy.deepcopy(m)).source == "none"


# ───────────── 完整性與列出 ─────────────
def flip_one_byte(p: Path):
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0x01
    p.write_bytes(bytes(b))


def test_valid_version_loads_and_verifies_files(isolate):
    mk("ok1")
    r = V.load_version("ok1")
    assert r.ok and r.error is None and set(r.manifest["files"]) == {"dxf", "revit_json", "report"}


def test_tampering_one_byte_of_an_output_file_is_detected_and_blocks_compare(isolate):
    r, _ = mk("t1")
    mk("t2")
    flip_one_byte(r.files["dxf"])
    lr = V.load_version("t1")
    assert not lr.ok and lr.error.code == "file_tampered"
    d = V.compare("t1", "t2")
    assert d.ok is False and "A" in d.error.message and "t1" in d.error.message
    d2 = V.compare("t2", "t1")
    assert d2.ok is False and "B" in d2.error.message and d2.error.code == "file_tampered"


def test_missing_output_file_is_detected(isolate):
    r, _ = mk("mf")
    r.files["revit_json"].unlink()
    assert V.load_version("mf").error.code == "missing_file"


def test_tampered_manifest_sections_are_detected_by_hash_recomputation(isolate):
    r, m = mk("hm")
    mp = r.files["dxf"].parent / V.MANIFEST_NAME
    for edit in (lambda d: d["inputs"].update(tray_w_mm=999),
                 lambda d: d["result"].update(segment_count=99),
                 lambda d: d["rules"]["snapshot"]["span_max_m"].update(value=0.1)):
        d = json.loads(mp.read_text(encoding="utf-8"))
        edit(d)
        mp.write_text(json.dumps(d), encoding="utf-8")
        assert V.load_version("hm").error.code == "hash_mismatch"
        mp.write_text(json.dumps(m, sort_keys=True), encoding="utf-8")                 # 還原
    assert V.load_version("hm").ok


@pytest.mark.parametrize("content,code", [("not json", "corrupt_manifest"), ("[]", "corrupt_manifest"),
                                          ("{}", "unsupported_schema"),
                                          ('{"schema_version": 2}', "unsupported_schema"),
                                          ('{"schema_version": 1}', "corrupt_manifest")])
def test_unreadable_manifests_give_structured_errors(isolate, content, code):
    r, _ = mk("bad")
    (r.files["dxf"].parent / V.MANIFEST_NAME).write_text(content, encoding="utf-8")
    lr = V.load_version("bad")
    assert lr.ok is False and lr.error.code == code and lr.manifest is None


def test_manifest_run_id_must_match_folder_name(isolate):
    r, m = mk("rid1")
    m2 = dict(m, run_id="other")
    (r.files["dxf"].parent / V.MANIFEST_NAME).write_text(json.dumps(m2), encoding="utf-8")
    assert V.load_version("rid1").error.code == "corrupt_manifest"


def test_missing_version_and_invalid_run_id_are_structured(isolate):
    assert V.load_version("nope").error.code == "missing_version"
    assert V.load_version("../x").error.code == "invalid_run_id"
    d = V.compare("nope", "nada")
    assert d.ok is False and d.error.code == "missing_version"


def test_missing_manifest_folder_is_not_a_version(isolate):
    r, _ = mk("nm")
    (r.files["dxf"].parent / V.MANIFEST_NAME).unlink()
    assert V.load_version("nm").error.code == "missing_manifest"
    assert [v.run_id for v in V.list_versions()] == []


def test_list_versions_scans_only_real_run_folders_sorted_and_flags_corrupt(isolate):
    mk("zz-late", created_at="2026-03-01T00:00:00Z")
    mk("aa-early", created_at="2026-01-01T00:00:00Z")
    mk("mm-tie-b", created_at="2026-02-01T00:00:00Z")
    mk("mm-tie-a", created_at="2026-02-01T00:00:00Z")
    r, _ = mk("broken", created_at="2026-04-01T00:00:00Z")
    (r.files["dxf"].parent / V.MANIFEST_NAME).write_text("garbage", encoding="utf-8")
    (isolate / (P.STAGE_PREFIX + "abc")).mkdir()
    (isolate / (P.STAGE_PREFIX + "abc") / V.MANIFEST_NAME).write_text("{}")
    (isolate / "emptyfolder").mkdir()
    (isolate / "stray-file").write_text("x")
    (isolate / "bad name!").mkdir()
    ids = [v.run_id for v in V.list_versions()]
    # 排序 = (created_at, run_id)；讀不到建立時間的損毀版本 created_at 為空字串，因此排在最前面（問題版本一眼可見）
    assert ids == ["broken", "aa-early", "mm-tie-a", "mm-tie-b", "zz-late"]
    infos = {v.run_id: v for v in V.list_versions()}
    assert infos["broken"].error.code == "corrupt_manifest" and infos["zz-late"].error is None


def test_list_versions_on_missing_output_root_is_empty(isolate):
    assert not output_root().exists() and V.list_versions() == []


def test_manifest_cannot_be_written_twice_and_existing_versions_are_immutable(isolate):
    r, m = mk("imm1")
    with pytest.raises(FileExistsError):
        V.write_manifest(r.files["dxf"].parent, m)
    before = (r.files["dxf"].parent / V.MANIFEST_NAME).read_bytes()
    again = P.run(sample(), ALL, "imm1", make_dwg=False)
    assert again.ok is False and again.error.code == "run_exists"
    assert (r.files["dxf"].parent / V.MANIFEST_NAME).read_bytes() == before


# ───────────── 脫敏 ─────────────
def string_leaves(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield from string_leaves(k)
            yield from string_leaves(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from string_leaves(v)


def test_manifest_strings_never_contain_home_tmp_or_absolute_paths(isolate):
    import tempfile
    home, tmp = str(Path.home()), tempfile.gettempdir()
    bs = chr(92)
    dirty_path = f"{home}{bs}secret{bs}x.dxf"
    inp = sample(obstacles=[{"name": f"pipe at {dirty_path}", "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}])
    r = P.run(inp, ALL, "san1", make_dwg=False)
    assert r.ok
    m = V.build_manifest(
        run_id="san1", created_at="2026-01-01T00:00:00Z", inp=r.inputs, codes=r.codes,
        notes=[f"see {dirty_path}", f"C:{bs}Program Files{bs}Autodesk{bs}AutoCAD 2027{bs}accoreconsole.exe"],
        gov=r.gov, rules=load_rules(), route=r.route, reports=r.reports, joints=r.stats["joints"],
        span_m=2.0, span_source="CNS", files=r.files, dwg_note=f"failed at {tmp}{bs}x{bs}y.dwg",
        disclosures=[f"note {dirty_path}"], environment={"path": dirty_path, "tmp": tmp})
    text = "\n".join(string_leaves(m))
    assert home.lower() not in text.lower() and tmp.lower() not in text.lower()
    assert not re.search(r"[A-Za-z]:[\\/]", text) and "Program Files" not in text
    # 管線實際寫出的 manifest 也一樣乾淨，且脫敏後仍自洽（載入時雜湊重算一致）
    pipe_text = "\n".join(string_leaves(r.manifest))
    assert home.lower() not in pipe_text.lower() and "<HOME>" in pipe_text      # 家目錄被替換；其下的子路徑尾段是使用者自己輸入的名稱
    assert V.load_version("san1").ok


# ───────────── new_run_id ─────────────
NOW = datetime(2026, 10, 2, 17, 25, 53, tzinfo=timezone.utc)


def test_new_run_id_format_determinism_and_input_sensitivity(isolate):
    a = V.new_run_id(sample(), ALL, now=NOW)
    assert RUN_ID_RE.match(a) and len(a) <= 40 and a.startswith("20261002-172553-") and len(a) == 24
    assert a == V.new_run_id(sample(), ALL, now=NOW)
    assert a != V.new_run_id(sample(tray_w_mm=400), ALL, now=NOW)
    assert a != V.new_run_id(sample(), ["CNS"], now=NOW)
    assert V.new_run_id(sample(), ALL, now=NOW.replace(second=54)).startswith("20261002-172554-")


def test_new_run_id_collisions_get_deterministic_suffixes(isolate):
    base = V.new_run_id(sample(), ALL, now=NOW)
    taken = {base, base + "-2"}
    assert V.new_run_id(sample(), ALL, now=NOW, taken=lambda r: r in taken) == base + "-3"
    assert RUN_ID_RE.match(base + "-3")
    with pytest.raises(RuntimeError):
        V.new_run_id(sample(), ALL, now=NOW, taken=lambda r: True)


def test_new_run_id_checks_the_real_output_root_by_default(isolate):
    first = V.new_run_id(sample(), ALL, now=NOW)
    r = P.run(sample(), ALL, first, make_dwg=False)
    assert r.ok
    assert V.new_run_id(sample(), ALL, now=NOW) == first + "-2"
