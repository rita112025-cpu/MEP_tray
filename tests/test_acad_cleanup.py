"""Real owned-process cleanup checks; never targets an existing user session."""
import ctypes
import os
import subprocess
from pathlib import Path

import pytest

from mep_tray import acad
from tests.test_acad import fake_dxf


def pid_alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        code = ctypes.c_ulong()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


@pytest.mark.autocad
@pytest.mark.skipif(os.name != "nt" or acad.find_accore() is None, reason="requires Windows AutoCAD")
def test_real_autocad_timeout_cleans_only_owned_pid(tmp_path):
    made = []

    def launch(args, **kwargs):
        process = subprocess.Popen(args, **kwargs)
        made.append((process, Path(kwargs["cwd"])))
        return process

    output = tmp_path / "timeout.dwg"
    result = acad.audit_and_convert(fake_dxf(tmp_path), output, popen=launch, timeout=0.01)
    assert "逾時" in result.note, result.note
    assert result.dwg is None and result.audit_errors is None and not output.exists()
    assert len(made) == 1
    process, work = made[0]
    assert process.poll() is not None and not pid_alive(process.pid)
    assert not work.exists()  # own temporary DWG, lock files and output directory removed


@pytest.mark.autocad
@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill tree integration")
def test_real_owned_child_tree_timeout(tmp_path):
    import sys
    child_record = tmp_path / "child.pid"
    made = []
    code = (
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        "pathlib.Path('out.dwg').write_text('partial'); "
        "pathlib.Path('in.dwl').write_text('lock'); time.sleep(120)"
    )

    def launch(args, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", code, str(child_record)],
                                   creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        made.append((process, Path(kwargs["cwd"])))
        return process

    try:
        result = acad.audit_and_convert(fake_dxf(tmp_path), tmp_path / "result.dwg",
                                        accore=Path(sys.executable), popen=launch, timeout=3)
        assert "逾時" in result.note and "未確認" not in result.note
        assert child_record.exists(), "helper child was not spawned"
        child_pid = int(child_record.read_text())
        assert not pid_alive(made[0][0].pid) and not pid_alive(child_pid)
        assert not made[0][1].exists() and not (tmp_path / "result.dwg").exists()
    finally:
        # Only the process started by this test may be terminated.
        if made and made[0][0].poll() is None:
            acad.kill_tree(made[0][0].pid)
            made[0][0].wait(timeout=10)
        if child_record.exists():
            owned_child = int(child_record.read_text())
            if pid_alive(owned_child):
                acad.kill_tree(owned_child)


@pytest.mark.autocad
def test_real_owned_abnormal_exit_cleans_partial_files(tmp_path):
    import sys
    made = []
    code = ("import pathlib,sys; pathlib.Path('out.dwg').write_text('partial'); "
            "pathlib.Path('in.dwl').write_text('lock'); print('Total errors found 0'); sys.exit(3)")

    def launch(args, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", code], **kwargs)
        made.append((process, Path(kwargs["cwd"])))
        return process

    output = tmp_path / "crash.dwg"
    result = acad.audit_and_convert(fake_dxf(tmp_path), output, accore=Path(sys.executable), popen=launch)
    assert "returncode=3" in result.note and result.audit_errors is None and result.dwg is None
    assert made[0][0].poll() == 3 and not made[0][1].exists() and not output.exists()
