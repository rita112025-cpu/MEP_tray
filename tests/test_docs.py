"""文件守門：三份文件存在、含固定揭露文字與限制關鍵字、連結與指令範例沒有過期。"""
import configparser
import re
from pathlib import Path

import pytest

from mep_tray import disclosure as D
from mep_tray import webui as W
from mep_tray.model import OBSTACLE_KINDS

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
NAMES = ("INSTALL.md", "MANUAL.md", "ARCHITECTURE.md")


def text(name):
    return (DOCS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", NAMES)
def test_doc_exists_and_carries_the_fixed_disclosure(name):
    t = text(name)
    assert D.BANNER_FIXED in t and D.SCOPE_NOTE in t


def test_manual_states_the_limitations_it_must_not_omit():
    t = text("MANUAL.md")
    for kw in ("未驗證", "UNVERIFIED", "HUMAN TEST PENDING", "不是 .rfa", "Revit 2027", "AutoCAD 2027", "Shared Coordinates",
               "無法取消", "不會被改寫", "被略過"):
        assert kw in t, kw
    assert D.ENGINE_CHANGED_NOTE in t


@pytest.mark.parametrize("name", NAMES)
def test_relative_links_point_at_existing_files(name):
    for target in re.findall(r"\]\(([^)#\s]+)\)", text(name)):
        if re.match(r"[a-z]+://", target):
            continue
        assert (DOCS / target).resolve().exists(), (name, target)


@pytest.mark.parametrize("name", NAMES)
def test_pytest_markers_in_commands_exist_in_pytest_ini(name):
    ini = configparser.ConfigParser()
    ini.read(ROOT / "pytest.ini", encoding="utf-8")
    declared = {ln.split(":")[0].strip() for ln in ini["pytest"]["markers"].splitlines() if ln.strip()}
    for m in re.findall(r"pytest\b[^\n`]*? -m (\w+)", text(name)):
        assert m in declared, (name, m)
    assert {"autocad", "revit", "slow"} <= declared


def test_install_commands_are_powershell_5_1_safe():
    t = text("INSTALL.md")
    for block in re.findall(r"```powershell\n(.*?)```", t, re.S):
        assert "&&" not in block and "||" not in block
    assert "requirements.txt" in t and "MEP_OUTPUT_ROOT" in t and ".stage-" in t


def test_manual_error_table_covers_every_web_error_code():
    t = text("MANUAL.md")
    for (stage, code) in W.HUMAN:
        assert f"{stage}／`{code}`" in t, (stage, code)


def test_manual_obstacle_schema_matches_the_code():
    t = text("MANUAL.md")
    for k in OBSTACLE_KINDS:
        assert f"`{k}`" in t, k
    assert str(W.MAX_OBSTACLES) in t and f"{W.MAX_OBSTACLE_BYTES // 1024} KB" in t
    for c in W.CELLS:
        assert f"{c:g}" in t


def test_architecture_mentions_every_module_and_existing_tests():
    t = text("ARCHITECTURE.md")
    for f in sorted((ROOT / "mep_tray").glob("*.py")):
        if f.stem in ("__init__", "__main__"):
            continue
        assert f.stem in t, f.stem
    for ref in set(re.findall(r"`(tests/[\w./]+\.py)`", t)):
        assert (ROOT / ref).exists(), ref
