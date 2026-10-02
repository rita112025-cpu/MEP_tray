import os
import subprocess
from pathlib import Path

import pytest

from mep_tray import export_dxf as X  # noqa: E402

ezdxf = X.ezdxf
from mep_tray.clash import check_route  # noqa: E402
from mep_tray.compliance import check_compliance  # noqa: E402
from mep_tray.model import Inputs  # noqa: E402
from mep_tray.paths import check_run_id, out_path  # noqa: E402
from mep_tray.router import place_hangers, route_tray  # noqa: E402
from mep_tray.rules import merge_strictest  # noqa: E402

GOV = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])


@pytest.fixture(autouse=True)
def out_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path))
    return tmp_path


def scene(with_clash=False):
    obs = [{"name": "W1", "kind": "water", "lo": [5, 0, 0], "hi": [5.3, 4, 3.2]}]
    i = Inputs(start=(1, 1, 3), ends=[(10, 1, 3), (6, 5, 3)], obstacles=obs)
    r = route_tray(i.room_box(), i.start, i.ends, i.obstacle_objs(), 0.3, 0.1, "power", GOV, cell=0.25)
    if with_clash:      # router 會避障；要有衝突需再放一支「事後才出現」的障礙物（模擬設計變更）
        i.obstacles.append({"name": "PIPE", "kind": "duct", "lo": [9.8, 0.5, 2.5], "hi": [10.2, 1.5, 3.5]})
    place_hangers(r, GOV["span_max_m"].value)
    return i, r


def build(run_id="t1", with_clash=False, **kw):
    i, r = scene(with_clash)
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    return i, r, reps, X.export_dxf(i, r, reps, GOV, run_id, notes=["測試揭露"], convert_dwg=False, **kw)


def ents(doc, t):
    return [e for e in doc.modelspace() if e.dxftype() == t]


def test_dxf_roundtrip_audit_layers_counts_units():
    i, r, reps, res = build()
    doc = ezdxf.readfile(res.dxf)
    assert not doc.audit().has_errors
    assert doc.dxfversion == "AC1024" and doc.header["$INSUNITS"] == 4
    for layer in ("MEP-TRAY-PWR", "MEP-HANGER", "MEP-CLASH", "MEP-NOTE", "MEP-OBST-water"):
        assert layer in doc.layers
    assert len(ents(doc, "POLYLINE")) == len(r.waypoints)
    assert len(ents(doc, "3DFACE")) == 6 * (len(r.segments) + len(i.obstacles))
    body = [e for e in ents(doc, "3DFACE") if e.dxf.layer == "MEP-TRAY-BODY"]
    assert len(body) == 6 * len(r.segments)
    assert len(ents(doc, "POINT")) == len(r.hangers)
    assert len(ents(doc, "INSERT")) == len(r.segments)


def test_coordinates_are_millimetres_and_attribs_present():
    i, r, reps, res = build()
    doc = ezdxf.readfile(res.dxf)
    poly = ents(doc, "POLYLINE")[0]
    first = tuple(poly.vertices[0].dxf.location)
    assert first == pytest.approx(tuple(v * 1000 for v in r.waypoints[0][0]))
    ins = ents(doc, "INSERT")[0]
    vals = {a.dxf.tag: a.dxf.text for a in ins.attribs}
    assert vals["WIDTH_MM"] == "300" and vals["TYPE"] == "power" and vals["ID"].startswith("SEG-")
    assert set(vals) == set(X.TRAY_SEG_TAGS) == {"ID", "WIDTH_MM", "HEIGHT_MM", "TYPE", "LENGTH_MM"}
    a, b = r.segments[0]
    assert float(vals["LENGTH_MM"]) == pytest.approx(sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5 * 1000, abs=0.1)
    got = {x.dxf.get("insert") and tuple(x.dxf.insert) for x in ents(doc, "INSERT")}
    assert len(got) == len(r.segments)


def test_findings_are_annotated_with_basis_and_disclosure():
    i, r, reps, res = build(with_clash=True)
    doc = ezdxf.readfile(res.dxf)
    nfind = sum(len(x.findings) for x in reps)
    assert nfind >= 1 and len(ents(doc, "CIRCLE")) == nfind        # block 內的圓不在 modelspace
    text = "\n".join(m.text for m in ents(doc, "MTEXT"))
    assert "衝突" in text and "建議" in text and "RUN t1" in text and "測試揭露" in text
    assert "CLASH clash" in text and "UNVERIFIED" in text           # ASCII 代碼在前


def test_unverified_basis_is_disclosed_on_drawing():
    i, r, reps, res = build(with_clash=True)
    doc = ezdxf.readfile(res.dxf)
    text = "\n".join(m.text for m in ents(doc, "MTEXT"))
    assert any(f.status == "UNVERIFIED" for rep in reps for f in rep.findings) == ("規範值未驗證" in text)


def test_export_is_deterministic_apart_from_run_id():
    def sig(res):
        d = ezdxf.readfile(res.dxf)
        out = []
        for e in d.modelspace():
            if e.dxftype() == "MTEXT":
                continue
            a = {k: v for k, v in e.dxf.all_existing_dxf_attribs().items() if k not in ("handle", "owner")}
            if e.dxftype() == "POLYLINE":
                a["v"] = [tuple(v.dxf.location) for v in e.vertices]
            if e.dxftype() == "INSERT":
                a["attribs"] = [(x.dxf.tag, x.dxf.text) for x in e.attribs if x.dxf.tag != "ID"]
            out.append((e.dxftype(), str(sorted(a.items(), key=lambda kv: kv[0]))))
        return out
    a = build("ra")[3]
    b = build("rb")[3]
    assert sig(a) == sig(b)


@pytest.mark.parametrize("bad", ["../x", "a b", "中文", "CON/..", "a/b", "", "x" * 41, "a.b", "..", "/abs"])
def test_run_id_rejected(bad):
    with pytest.raises(ValueError):
        check_run_id(bad)


@pytest.mark.parametrize("bad", ["../a.dxf", "a b.dxf", "中文.dxf", ".hidden.dxf", "a..b.dxf",
                                 "x.exe", "x.dxf/../../y.dxf", "C:\a.dxf", "a/b.dxf", ""])
def test_filename_rejected(bad):
    with pytest.raises(ValueError):
        out_path("ok1", bad)


def test_output_stays_inside_root(out_root):
    p = out_path("ok1", "tray_ok1.dxf")
    assert out_root.resolve() in p.parents


def test_dwg_gracefully_skipped_when_oda_missing(monkeypatch):
    monkeypatch.delenv("MEP_ODA_PATH", raising=False)
    monkeypatch.setattr(X, "find_oda", lambda: None)
    i, r, reps, res = build()
    dwg, note = X.to_dwg(res.dxf)
    assert dwg is None and "ODA" in note


def test_oda_called_with_arg_list_no_shell(tmp_path):
    i, r, reps, res = build()
    fake = tmp_path / "ODAFileConverter.exe"
    fake.write_text("x")
    calls = []

    def runner(args, **kw):
        calls.append((args, kw))
        (Path(args[2]) / Path(args[7]).with_suffix(".dwg").name).write_bytes(b"DWG")
        return subprocess.CompletedProcess(args, 0)

    dwg, note = X.to_dwg(res.dxf, oda=fake, runner=runner)
    args, kw = calls[0]
    assert isinstance(args, list) and kw["shell"] is False and kw["timeout"] == 60
    assert args[0] == str(fake) and args[3:7] == ["ACAD2018", "DWG", "0", "1"] and args[7] == res.dxf.name
    assert dwg == res.dxf.with_suffix(".dwg") and dwg.read_bytes() == b"DWG"


def test_oda_failure_degrades(tmp_path):
    i, r, reps, res = build()
    fake = tmp_path / "oda.exe"
    fake.write_text("x")
    dwg, note = X.to_dwg(res.dxf, oda=fake, runner=lambda a, **k: subprocess.CompletedProcess(a, 3))
    assert dwg is None and "失敗" in note
    def boom(a, **k):
        raise subprocess.TimeoutExpired(a, 60)
    assert X.to_dwg(res.dxf, oda=fake, runner=boom)[0] is None


def test_text_style_is_cjk_truetype_and_mtext_uses_it():
    i, r, reps, res = build(with_clash=True)
    doc = ezdxf.readfile(res.dxf)
    st = doc.styles.get("MEP")
    assert not st.dxf.font.lower().endswith(".shx") and "txt" not in st.dxf.font.lower()
    assert all(m.dxf.style == "MEP" for m in ents(doc, "MTEXT"))


def test_exact_millimetre_coordinates_no_float_noise():
    i = Inputs(start=(1.1, 1, 3), ends=[(6.1, 1, 3)])
    from mep_tray.router import Route
    r = Route([[(1.1, 1.0, 3.0), (6.1, 1.0, 3.0)]], [((1.1, 1.0, 3.0), (6.1, 1.0, 3.0))], [], 5.0)
    res = X.export_dxf(i, r, [], GOV, "exact", convert_dwg=False)
    poly = ents(ezdxf.readfile(res.dxf), "POLYLINE")[0]
    assert [tuple(v.dxf.location) for v in poly.vertices] == [(1100.0, 1000.0, 3000.0), (6100.0, 1000.0, 3000.0)]


def test_empty_route_and_zero_findings_still_valid_dxf_with_disclosures():
    i = Inputs(start=(1, 1, 3), ends=[(1, 1, 3)])
    r = route_tray(i.room_box(), i.start, i.ends, [], 0.3, 0.1, "power", GOV, cell=0.25)
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    res = X.export_dxf(i, r, reps, GOV, "empty1", convert_dwg=False)
    doc = ezdxf.readfile(res.dxf)
    assert not doc.audit().has_errors
    text = "\n".join(m.text for m in ents(doc, "MTEXT"))
    assert "規範值未驗證" in text and "未於 AutoCAD 實機驗證" in text and "原點" in text
    assert not ents(doc, "CIRCLE") and not ents(doc, "INSERT")


def test_unverified_gov_disclosure_absent_when_all_verified():
    import dataclasses
    ver = {k: dataclasses.replace(g, verified=True) for k, g in GOV.items()}
    notes = X.disclosure_notes(Inputs(cables=[{"od_mm": 20, "count": 1, "kind": "power"}]), ver)
    assert not any("規範值未驗證" in n for n in notes) and any("實機" in n for n in notes)


def test_existing_output_is_not_silently_overwritten():
    build("dup1")
    with pytest.raises(FileExistsError):
        build("dup1")
    i, r, reps, _ = build("dup2")
    X.export_dxf(i, r, reps, GOV, "dup2", convert_dwg=False, overwrite=True)    # 明確允許才可覆寫


def test_export_validates_run_id_itself():
    i, r = scene()
    for bad in ("../evil", "", "x" * 41, "中文"):
        with pytest.raises(ValueError):
            X.export_dxf(i, r, [], GOV, bad, convert_dwg=False)


def test_symlink_escape_rejected(out_root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    link = out_root / "esc1"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows 無權限建 symlink 時改用 junction（不需系統管理員）
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                           capture_output=True, shell=False) if os.name == "nt" else None
        if r is None or r.returncode != 0:
            pytest.skip("此環境無法建立 symlink/junction")
    with pytest.raises(ValueError):
        out_path("esc1", "tray_esc1.dxf")
    assert not list(outside.iterdir())


def test_oda_uses_folder_interface_with_single_dxf_input(tmp_path):
    i, r, reps, res = build("oda1")
    (res.dxf.parent / "other.json").write_text("{}")             # 同資料夾的其他檔不得被送去轉
    fake = tmp_path / "ODAFileConverter.exe"
    fake.write_text("x")
    seen = {}

    def runner(args, **kw):
        tin, tout = Path(args[1]), Path(args[2])
        assert tin.is_dir() and tout.is_dir() and tin != res.dxf.parent and tout != res.dxf.parent
        seen["in"] = sorted(p.name for p in tin.iterdir())
        seen["out_before"] = sorted(p.name for p in tout.iterdir())
        (tout / res.dxf.with_suffix(".dwg").name).write_bytes(b"DWG")
        return subprocess.CompletedProcess(args, 0)

    dwg, _ = X.to_dwg(res.dxf, oda=fake, runner=runner)
    assert seen["in"] == [res.dxf.name] and seen["out_before"] == []
    assert dwg is not None and sorted(p.name for p in res.dxf.parent.iterdir()) ==         sorted([res.dxf.name, dwg.name, "other.json"])


def test_oda_timeout_degrades_with_reason(tmp_path):
    i, r, reps, res = build("oda2")
    fake = tmp_path / "oda.exe"
    fake.write_text("x")

    def hang(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    dwg, note = X.to_dwg(res.dxf, oda=fake, runner=hang)
    assert dwg is None and "失敗" in note


@pytest.fixture
def keep_ezdxf_modules():
    """降級路徑會清 sys.modules 的 ezdxf*；測試後必須還原，否則後續匯入會得到第二份 ezdxf（類別身分錯亂）。"""
    import sys
    saved = {k: v for k, v in sys.modules.items() if k == "ezdxf" or k.startswith("ezdxf.")}
    yield
    for k in [k for k in sys.modules if k == "ezdxf" or k.startswith("ezdxf.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def test_ezdxf_import_falls_back_only_after_failure(monkeypatch, keep_ezdxf_modules):
    calls = []

    class Fake:
        __version__ = "fake"

    def importer(name):
        calls.append(os.environ.get("EZDXF_DISABLE_C_EXT"))
        if len(calls) == 1:
            raise ImportError("DLL load failed while importing matrix44")
        return Fake

    monkeypatch.delenv("EZDXF_DISABLE_C_EXT", raising=False)
    assert X.load_ezdxf(importer) is Fake
    assert calls == [None, "1"]                                   # 第一次未設環境變數，失敗後才設
    monkeypatch.delenv("EZDXF_DISABLE_C_EXT", raising=False)
    calls.clear()
    assert X.load_ezdxf(lambda n: Fake) is Fake and os.environ.get("EZDXF_DISABLE_C_EXT") is None
    with pytest.raises(ImportError, match="INSTALL.md"):
        X.load_ezdxf(lambda n: (_ for _ in ()).throw(ImportError("boom")))


def _clean_scene():
    i = Inputs(start=(1, 1, 3), ends=[(10, 1, 3)])
    r = route_tray(i.room_box(), i.start, i.ends, [], 0.3, 0.1, "power", GOV, cell=0.25)
    place_hangers(r, GOV["span_max_m"].value)
    return i, r


def _note_text(res):
    doc = ezdxf.readfile(res.dxf)
    return "\n".join(m.text for m in ents(doc, "MTEXT") if m.dxf.layer == "MEP-NOTE")


def test_clean_drawing_with_zero_findings_still_discloses_unverified_count():
    import dataclasses
    i, r = _clean_scene()
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    assert not [f for rep in reps for f in rep.findings]                    # 看似乾淨
    n = sum(1 for rep in reps for f in rep.checks if f.status == "UNVERIFIED")
    assert n >= 3
    res = X.export_dxf(i, r, reps, GOV, "clean1", convert_dwg=False)
    text = _note_text(res)
    assert f"本圖有 {n} 項檢查所依規範值尚未核對條文" in text and "不得視為合規" in text
    ver = {k: dataclasses.replace(g, verified=True) for k, g in GOV.items()}
    reps2 = [check_route(i, r, ver), check_compliance(i, r, ver)]
    text2 = _note_text(X.export_dxf(i, r, reps2, ver, "clean2", convert_dwg=False))
    assert "尚未核對條文" not in text2 and "未於 AutoCAD 實機驗證" in text2


def test_mtext_control_codes_in_user_text_are_escaped():
    BS = chr(92)
    name = "{" + BS + "C1;}x" + BS + "Ptail"
    i, r = _clean_scene()
    i.obstacles.append({"name": name, "kind": "other", "lo": [5, 3, 0], "hi": [5.2, 3.2, 1]})
    res = X.export_dxf(i, r, [], GOV, "esc2", convert_dwg=False)
    doc = ezdxf.readfile(res.dxf)
    m = [e for e in ents(doc, "MTEXT") if e.dxf.layer == "MEP-OBST-other"][0]
    assert BS + "{" in m.text and BS + "}" in m.text and (BS * 2 + "C1;") in m.text   # 已跳脫
    assert m.plain_text() == name                                                      # 還原為字面字串
