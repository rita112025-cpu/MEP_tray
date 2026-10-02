import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("EZDXF_DISABLE_C_EXT", "1")
import ezdxf  # noqa: E402

from mep_tray import export_dxf as X  # noqa: E402
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
    return i, r, reps, X.export_dxf(i, r, reps, ["測試揭露"], run_id, convert_dwg=False, **kw)


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


def test_findings_are_annotated_with_basis_and_disclosure():
    i, r, reps, res = build(with_clash=True)
    doc = ezdxf.readfile(res.dxf)
    nfind = sum(len(x.findings) for x in reps)
    assert nfind >= 1 and len(ents(doc, "CIRCLE")) >= nfind        # 另有 block 內圓不計入 modelspace
    text = "\n".join(m.text for m in ents(doc, "MTEXT"))
    assert "衝突" in text and "建議" in text and "RUN t1" in text and "測試揭露" in text


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
