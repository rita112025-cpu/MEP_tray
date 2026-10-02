import subprocess
from pathlib import Path

import pytest

from mep_tray import acad
from mep_tray import export_dxf as X
from mep_tray.acad import AcadResult, audit_and_convert, parse_audit
from tests.test_export_dxf import GOV, build, ents, ezdxf, out_root, scene  # noqa: F401
from mep_tray.clash import check_route
from mep_tray.compliance import check_compliance


def test_parse_audit_english_and_chinese_and_unknown():
    assert parse_audit("Total errors found 0 fixed 0") == 0
    assert parse_audit("blah\nTotal errors found 3 fixed 1") == 3
    assert parse_audit("總共發現 0 個錯誤，修復 0 個") == 0
    assert parse_audit("總共發現 12 個錯誤") == 12
    assert parse_audit("nothing useful") is None


def fake_dxf(tmp_path):
    p = tmp_path / "x.dxf"
    p.write_text("0\nEOF\n")
    return p


def test_audit_and_convert_arg_list_no_shell_ascii_script(tmp_path):
    dxf, fake = fake_dxf(tmp_path), tmp_path / "accoreconsole.exe"
    fake.write_text("x")
    seen = {}

    def runner(args, **kw):
        seen["args"], seen["kw"] = args, kw
        scr = Path(args[args.index("/s") + 1])
        seen["scr"] = scr.read_text(encoding="ascii")
        out = [ln for ln in seen["scr"].splitlines() if ln.endswith("out.dwg")][0]
        Path(out).write_bytes(b"AC1032")
        kw["stdout"].write("Total errors found 0 fixed 0".encode("utf-16"))
        return subprocess.CompletedProcess(args, 0)

    dwg_out = tmp_path / "x.dwg"
    r = audit_and_convert(dxf, dwg_out, accore=fake, runner=runner)
    a, kw = seen["args"], seen["kw"]
    assert isinstance(a, list) and a[0] == str(fake) and a[1] == "/i" and "/s" in a and "/l" in a
    assert kw["shell"] is False and kw["timeout"] == 90 and kw["stdin"] == subprocess.DEVNULL
    assert Path(kw["cwd"]).resolve() != Path.cwd().resolve()          # 不以專案目錄為工作目錄
    assert "_.AUDIT" in seen["scr"] and "_.SAVEAS" in seen["scr"] and "_.QUIT" in seen["scr"]
    assert r.audit_errors == 0 and r.dwg == dwg_out and dwg_out.read_bytes() == b"AC1032"
    src = a[a.index("/i") + 1]
    assert src != str(dxf) and Path(src).name == "in.dxf"        # 只處理暫存副本，不直接動原檔


def test_audit_only_has_no_saveas(tmp_path):
    dxf, fake = fake_dxf(tmp_path), tmp_path / "a.exe"
    fake.write_text("x")
    seen = {}

    def runner(args, **kw):
        seen["scr"] = Path(args[args.index("/s") + 1]).read_text(encoding="ascii")
        kw["stdout"].write(b"Total errors found 2")
        return subprocess.CompletedProcess(args, 0)

    r = audit_and_convert(dxf, None, accore=fake, runner=runner)
    assert "SAVEAS" not in seen["scr"] and r.audit_errors == 2 and r.dwg is None


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("x", 90), OSError("boom")])
def test_failures_degrade_without_raising(tmp_path, exc):
    dxf, fake = fake_dxf(tmp_path), tmp_path / "a.exe"
    fake.write_text("x")

    def runner(args, **kw):
        raise exc
    r = audit_and_convert(dxf, tmp_path / "o.dwg", accore=fake, runner=runner)
    assert r.audit_errors is None and r.dwg is None and "失敗" in r.note


def test_unparseable_log_is_unknown_not_zero(tmp_path):
    dxf, fake = fake_dxf(tmp_path), tmp_path / "a.exe"
    fake.write_text("x")
    r = audit_and_convert(dxf, None, accore=fake,
                          runner=lambda a, **k: subprocess.CompletedProcess(a, 0))
    assert r.audit_errors is None and "無法解析" in r.note


def test_missing_accore_degrades(monkeypatch, tmp_path):
    monkeypatch.delenv("MEP_ACCORE_PATH", raising=False)
    monkeypatch.setattr(acad, "find_accore", lambda: None)
    r = audit_and_convert(fake_dxf(tmp_path), None)
    assert r.audit_errors is None and "未偵測到 AutoCAD" in r.note


def _note(res):
    doc = ezdxf.readfile(res.dxf)
    return "\n".join(m.text for m in ents(doc, "MTEXT") if m.dxf.layer == "MEP-NOTE")


@pytest.mark.parametrize("audit,expect", [(0, "0 個錯誤"), (4, "稽核發現 4 個錯誤"), (None, "未於 AutoCAD 實機驗證")])
def test_two_pass_flow_writes_audit_result_into_final_drawing(monkeypatch, audit, expect, tmp_path):
    calls = []
    fake_exe = tmp_path / "AutoCAD 2027" / "accoreconsole.exe"

    def fake(dxf, dwg_out=None, accore=None, **kw):
        calls.append((Path(dxf).name, dwg_out))
        if dwg_out is not None:
            Path(dwg_out).write_bytes(b"AC1032")
        return AcadResult(audit, dwg_out, "AutoCAD 2027", f"fake audit={audit}")

    monkeypatch.setattr(acad, "audit_and_convert", fake)
    i, r = scene()
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    res = X.export_dxf(i, r, reps, GOV, "two1", accore=fake_exe)
    assert calls[0][0] == "draft.dxf" and calls[0][1] is None            # 第1趟：草稿、不轉檔
    assert calls[1][0] == res.dxf.name and calls[1][1] == res.dxf.with_suffix(".dwg")
    assert expect in _note(res)
    assert res.dwg == res.dxf.with_suffix(".dwg") and res.acad_audit == audit


def test_no_acad_no_oda_only_dxf(monkeypatch):
    monkeypatch.setattr(acad, "find_accore", lambda: None)
    monkeypatch.setattr(X, "find_oda", lambda: None)
    i, r = scene()
    res = X.export_dxf(i, r, [], GOV, "none1")
    assert res.dxf.exists() and res.dwg is None and res.acad_audit is None and "ODA" in res.dwg_note


@pytest.mark.skipif(acad.find_accore() is None, reason="本機未安裝 AutoCAD（accoreconsole.exe）")
def test_real_autocad_opens_audits_and_converts():
    """整合測試：真實 AutoCAD 開啟本工具輸出的 DXF，稽核 0 錯誤並轉存 DWG。"""
    i, r, reps, _ = build("real1", with_clash=True)
    res = X.export_dxf(i, r, reps, GOV, "real2", convert_dwg=True)
    assert res.acad_audit == 0, res.dwg_note
    assert res.dwg is not None and res.dwg.read_bytes()[:6] == b"AC1032"
    assert "已以 AutoCAD" in _note(res)
