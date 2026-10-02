import tempfile
from pathlib import Path

import pytest

from mep_tray.paths import output_root
from mep_tray.sanitize import relative_to_output, sanitize_text


@pytest.fixture(autouse=True)
def out_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path / "out"))
    return tmp_path / "out"


def test_output_root_tmp_and_home_are_replaced_with_labels(out_root):
    root = output_root()
    s = f"failed writing {root}\\sub\\a.dxf and {str(root).replace(chr(92), '/')}/b.json"
    t = sanitize_text(s)
    assert str(root) not in t and "<OUT>" in t and t.count("<OUT>") == 2
    home = str(Path.home())
    assert "<HOME>" in sanitize_text(f"cfg at {home}\\x.ini") and home not in sanitize_text(home + "/x")
    tmp = tempfile.gettempdir()
    assert "<TMP>" in sanitize_text(tmp + "\\scratch.dxf")


def test_matching_is_case_insensitive_and_covers_both_separators():
    home = str(Path.home())
    assert home.lower() not in sanitize_text(home.upper() + "\\Foo").lower()
    assert home.lower() not in sanitize_text(home.replace(chr(92), "/").lower() + "/foo").lower()


def test_other_absolute_paths_become_generic_placeholder():
    t = sanitize_text(r"cannot start 'C:\Program Files\Autodesk\AutoCAD 2027\accoreconsole.exe' (WinError 2)")
    assert "Program Files" not in t and "accoreconsole" not in t and "<PATH>" in t and "WinError 2" in t
    u = sanitize_text(r"read \\fileserver\share\drawings\a.dxf failed")
    assert "fileserver" not in u and "<PATH>" in u


def test_plain_text_labels_and_nonstrings_are_untouched():
    for s in ("規範值未驗證", "座標 = 公尺×1000 (mm)", "已由 AutoCAD 2027 稽核：0 個錯誤", "<OUT>/x stays", ""):
        assert sanitize_text(s) == s
    assert sanitize_text(None) is None and sanitize_text(5) == 5


def test_sanitize_is_idempotent(out_root):
    s = f"{output_root()}\\a and C:\\Windows\\System32\\x.dll and {Path.home()}\\y"
    once = sanitize_text(s)
    assert sanitize_text(once) == once


def test_relative_to_output(out_root):
    f = output_root() / "run1" / "tray_run1.dxf"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert relative_to_output(f) == "run1/tray_run1.dxf"
    assert relative_to_output(None) is None
    outside = Path(tempfile.gettempdir()) / "somewhere" / "evil.dxf"
    assert relative_to_output(outside) == "evil.dxf"            # 不在輸出根目錄：只留檔名，不洩漏路徑


def test_paths_with_spaces_quoted_repr_and_unquoted_are_fully_removed():
    BS = chr(92)
    pf = "C:" + BS + "Program Files" + BS + "Autodesk" + BS + "AutoCAD 2027" + BS + "accoreconsole.exe"
    for text in (f"cannot start '{pf}' (WinError 2)",
                 f"repr 'C:{BS * 2}Program Files{BS * 2}Autodesk{BS * 2}x.exe' failed",
                 f"unquoted {pf} failed to start",
                 f"in dir C:{BS}Program Files{BS}Common Files{BS}x y z.dxf now"):
        out = sanitize_text(text)
        for frag in ("Program Files", "Autodesk", "AutoCAD", "accoreconsole", "Common Files", "x y z"):
            assert frag not in out, (text, out)
        assert "<PATH>" in out
    assert sanitize_text(f"cannot start '{pf}' (WinError 2)").endswith("(WinError 2)")      # 其餘說明保留
