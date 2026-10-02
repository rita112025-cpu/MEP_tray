import os
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


NL = chr(10)


def fake_dxf(tmp_path):
    p = tmp_path / "x.dxf"
    p.write_text("0" + NL + "EOF" + NL)
    return p


class FakeProc:
    pid = 4242

    def __init__(self, args, rc=0, hang=False):
        self.args, self.rc, self.hang, self.killed, self.waits = args, rc, hang, False, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.rc

    def kill(self):
        self.killed = True

    def poll(self):
        return None if self.hang and not self.killed else self.rc


def popen_factory(behavior=None, rc=0, hang=False, boom=None):
    made = []

    def popen(args, **kw):
        if boom:
            raise boom
        p = FakeProc(args, rc, hang)
        p.kw = kw
        made.append(p)
        if behavior:
            behavior(args, kw)
        return p
    popen.made = made
    return popen


def fake_exe(tmp_path, name="accoreconsole.exe"):
    f = tmp_path / name
    f.write_text("x")
    return f


def test_audit_and_convert_arg_list_no_shell_ascii_script(tmp_path):
    dxf, fake = fake_dxf(tmp_path), fake_exe(tmp_path)
    seen = {}

    def behavior(args, kw):
        seen["scr"] = Path(args[args.index("/s") + 1]).read_text(encoding="ascii")
        out = [ln for ln in seen["scr"].splitlines() if ln.endswith("out.dwg")][0]
        Path(out).write_bytes(b"AC1032")
        kw["stdout"].write("Total errors found 0 fixed 0".encode("utf-16"))

    popen = popen_factory(behavior)
    dwg_out = tmp_path / "x.dwg"
    r = audit_and_convert(dxf, dwg_out, accore=fake, popen=popen)
    a, kw = popen.made[0].args, popen.made[0].kw
    assert isinstance(a, list) and a[0] == str(fake) and a[1] == "/i" and "/s" in a and "/l" in a
    assert kw["shell"] is False and kw["stdin"] == subprocess.DEVNULL
    assert popen.made[0].waits == [90]
    assert Path(kw["cwd"]).resolve() != Path.cwd().resolve()          # 不以專案目錄為工作目錄
    assert "_.AUDIT" in seen["scr"] and "_.SAVEAS" in seen["scr"] and "_.QUIT" in seen["scr"]
    assert r.audit_errors == 0 and r.dwg == dwg_out and dwg_out.read_bytes() == b"AC1032"
    src = a[a.index("/i") + 1]
    assert src != str(dxf) and Path(src).name == "in.dxf"        # 只處理暫存副本，不直接動原檔


def test_audit_only_has_no_saveas(tmp_path):
    dxf, fake = fake_dxf(tmp_path), fake_exe(tmp_path, "a.exe")
    seen = {}

    def behavior(args, kw):
        seen["scr"] = Path(args[args.index("/s") + 1]).read_text(encoding="ascii")
        kw["stdout"].write(b"Total errors found 2")

    r = audit_and_convert(dxf, None, accore=fake, popen=popen_factory(behavior))
    assert "SAVEAS" not in seen["scr"] and r.audit_errors == 2 and r.dwg is None


def test_timeout_kills_whole_process_tree_and_reports(tmp_path):
    dxf, fake = fake_dxf(tmp_path), fake_exe(tmp_path, "AutoCAD 2027_accoreconsole.exe")
    popen = popen_factory(hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        for p in popen.made:
            p.killed = True          # 模擬行程樹被終止，之後 wait 可返回

    r = audit_and_convert(dxf, tmp_path / "o.dwg", accore=fake, popen=popen, killer=killer, timeout=5)
    assert killed == [4242]
    assert popen.made[0].waits == [5, 10]                       # 逾時後再 wait 一次確認已結束
    assert r.audit_errors is None and r.dwg is None and "逾時" in r.note and "行程樹" in r.note


def test_timeout_kill_failure_still_reports_and_falls_back_to_kill(tmp_path):
    dxf, fake = fake_dxf(tmp_path), fake_exe(tmp_path)
    popen = popen_factory(hang=True)

    def bad_killer(pid):
        raise OSError("taskkill missing")

    r = audit_and_convert(dxf, None, accore=fake, popen=popen, killer=bad_killer, timeout=1)
    assert "逾時" in r.note and r.audit_errors is None
    assert popen.made[0].waits == [1, 10, 10] and popen.made[0].killed is True
    assert "taskkill missing" in r.note and "未確認" in r.note


def test_crashed_process_does_not_publish_partial_dwg(tmp_path):
    def behavior(args, kw):
        script = Path(args[args.index("/s") + 1]).read_text(encoding="ascii")
        Path(next(line for line in script.splitlines() if line.endswith("out.dwg"))).write_bytes(b"partial")
        kw["stdout"].write(b"Total errors found 0")
    popen = popen_factory(behavior, rc=1)
    output = tmp_path / "o.dwg"
    result = audit_and_convert(fake_dxf(tmp_path), output, accore=fake_exe(tmp_path), popen=popen)
    assert result.audit_errors is None and result.dwg is None and not output.exists()
    assert "returncode=1" in result.note
    assert not Path(popen.made[0].kw["cwd"]).exists()


def test_wait_exception_cleans_recorded_process_and_temp_files(tmp_path):
    class WaitError(FakeProc):
        def wait(self, timeout=None):
            if not self.killed:
                raise OSError("wait failed")
            return 0
    made = []
    def launch(args, **kw):
        process = WaitError(args, hang=True)
        made.append((process, Path(kw["cwd"])))
        return process
    killed = []
    def killer(pid):
        killed.append(pid)
        made[0][0].killed = True
    result = audit_and_convert(fake_dxf(tmp_path), accore=fake_exe(tmp_path), popen=launch, killer=killer)
    assert "wait failed" in result.note and killed == [4242]
    assert made[0][0].poll() == 0 and not made[0][1].exists()


def test_kill_tree_uses_taskkill_arg_list_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(acad.os, "name", "nt")
    monkeypatch.setattr(acad.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    acad.kill_tree(4242)
    args, kw = calls[0]
    assert args == ["taskkill", "/F", "/T", "/PID", "4242"] and kw["shell"] is False


@pytest.mark.parametrize("boom", [OSError("boom"), subprocess.SubprocessError("x")])
def test_start_failures_degrade_without_raising(tmp_path, boom):
    dxf, fake = fake_dxf(tmp_path), fake_exe(tmp_path, "a.exe")
    r = audit_and_convert(dxf, tmp_path / "o.dwg", accore=fake, popen=popen_factory(boom=boom))
    assert r.audit_errors is None and r.dwg is None and "失敗" in r.note


def test_unparseable_log_is_unknown_not_zero(tmp_path):
    dxf, fake = fake_dxf(tmp_path), fake_exe(tmp_path, "a.exe")
    r = audit_and_convert(dxf, None, accore=fake, popen=popen_factory())
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


@pytest.mark.autocad
@pytest.mark.skipif(acad.find_accore() is None or os.environ.get("MEP_SKIP_ACAD") == "1",
                    reason="本機未安裝 AutoCAD（accoreconsole.exe），或設定了 MEP_SKIP_ACAD=1")
def test_real_autocad_opens_audits_and_converts():
    """整合測試：真實 AutoCAD 開啟本工具輸出的 DXF，稽核 0 錯誤並轉存 DWG。"""
    i, r, reps, _ = build("real1", with_clash=True)
    res = X.export_dxf(i, r, reps, GOV, "real2", convert_dwg=True)
    assert res.acad_audit == 0, res.dwg_note
    assert res.dwg is not None and res.dwg.read_bytes()[:6] == b"AC1032"
    assert "已以 AutoCAD" in _note(res)
