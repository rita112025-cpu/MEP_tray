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


# ───────────── README、歷史紀錄橫幅、規範表 ─────────────
def test_readme_is_a_current_entry_point_with_disclosure_and_valid_links():
    t = (ROOT / "README.md").read_text(encoding="utf-8")
    assert D.BANNER_FIXED in t and D.SCOPE_NOTE in t and "python -m mep_tray.webui" in t
    assert "沒有 Web UI" not in t and "沒有 Web UI、API server" not in t
    for name in NAMES + ("VERIFICATION.md", "HANDOFF.md"):
        assert f"docs/{name}" in t, name
    for target in re.findall(r"\]\(([^)#\s]+)\)", t):
        assert (ROOT / target).exists(), target


@pytest.mark.parametrize("name", ("HANDOFF.md", "VERIFICATION.md"))
def test_historical_records_carry_the_superseded_banner_at_the_top(name):
    head = text(name)[:400]
    assert head.lstrip().startswith("> **歷史紀錄。**") and "MANUAL 第 8 節" in head and "A–E 輪" in head


def test_manual_rules_table_lists_exactly_the_codes_in_rules_json():
    import json
    codes = set(json.loads((ROOT / "mep_tray" / "rules.json").read_text(encoding="utf-8"))["codes"])
    sec = text("MANUAL.md").split("## 規範表")[1].split("\n## ")[0].split("\n### ")[0]
    rows = set(re.findall(r"^\| `(\w+)` \|", sec, re.M))
    assert rows == codes
    assert "回查原檔" in sec and "MRT_APPX_C" in text("MANUAL.md").split("## 8. 限制")[1]


def test_docs_do_not_claim_six_slow_tests_or_a_certain_symlink_skip():
    for name in NAMES:
        t = text(name)
        assert not re.search(r"6 個\s*slow|六個\s*slow", t), name
    assert "在不支援符號連結的機器上會有一個測試 skip" in text("INSTALL.md")


def test_manual_says_the_result_preview_is_schematic_not_a_construction_drawing():
    t = text("MANUAL.md")
    assert "示意圖，非施工圖" in t and "XY" in t


def test_manual_discloses_routing_and_clearance_scope_limits():
    t = text("MANUAL.md")
    for kw in ("greedy shared-trunk", "不保證全域最佳解", "JSON 軸對齊 box",
               "clear_water_mm", "單軸最小位移"):
        assert kw in t, kw


def test_manual_documents_the_mrt_appendix_c_decisions():
    t = text("MANUAL.md")
    for kw in ("C-041", "1000", "C-048", "500", "屏蔽", "檢修空間", "heat_bare", "C-050", "SHOULD", "大於 100", "易燃爆氣體管",
               "用電設備交越", "交叉 300", "PDF 頁碼"):
        assert kw in t, kw


def test_manual_documents_cable_tray_support_fill_and_layer_rules():
    t = text("MANUAL.md")
    for kw in ("1000", "255", "2400", "NEMA VE1", "第三層", "最多兩層", "1.15.2 (1) F", "p.1-57", "用戶用電設備裝置規則",
               "接地匯流排", "不是 Cable Tray 填充率", "尚未自動檢核", "不是本工程允許的托架安裝間距"):
        assert kw in t, kw
    assert "fill ratio 維持 null" in t


def test_manual_describes_the_preview_layers():
    t = text("MANUAL.md")
    for kw in ("障礙物", "吊架", "接頭"):
        assert kw in t.split("XY 平面示意")[1][:300], kw


def test_manual_describes_the_preview_view_switching():
    t = text("MANUAL.md")
    for kw in ("XZ", "YZ", "俯視", "前視", "側視", "切換"):
        assert kw in t, kw


# ───────────── CI 與發布流程 ─────────────
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_release_doc_covers_gate_versioning_signing_and_wdac():
    t = text("RELEASE.md")
    for kw in ("Pull Request", "develop", "master", "VERIFICATION.md", "未執行", "not executed", "CI",
               "v<major>.<minor>.<patch>", "v0.4.0", "Changelog", "MepTrayImport.dll", "MepTray.Core.dll",
               "signtool sign /fd SHA256 /tr", "/td SHA256", "Get-AuthenticodeSignature", "WDAC", "Unblock-File",
               "永遠不進 repo", "不在 CI 或 PR workflow 中簽章", "自簽憑證只用於測試", "系統管理員"):
        assert kw in t, kw
    for target in re.findall(r"\]\(([^)#\s]+)\)", t):
        if not re.match(r"[a-z]+://", target):
            assert (DOCS / target).resolve().exists(), target


def test_release_doc_is_linked_from_git_workflow_and_readme():
    assert "](RELEASE.md)" in text("GIT_WORKFLOW.md")
    assert "](docs/RELEASE.md)" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_ci_workflow_runs_the_gate_but_never_builds_the_revit_addin():
    t = WORKFLOW.read_text(encoding="utf-8")
    for kw in ("runs-on: windows-latest", "contents: read", "cancel-in-progress: true", 'python-version: "3.12"',
               "cache: pip", "python -m pytest -q", "python -m compileall -q mep_tray tests", "git diff --check",
               "dotnet build revit/MepTray.Core -c Release", "dotnet build revit/MepTray.Core.SelfTest -c Release",
               "_build_model", "MepTray.Core.SelfTest.dll"):
        assert kw in t, kw
    assert t.count("branches: [develop, master]") == 2
    for ln in t.splitlines():                      # MepTrayImport 只能出現在說明註解，不能被建置
        if "MepTrayImport" in ln:
            assert ln.lstrip().startswith("#"), ln
    uses = re.findall(r"uses:\s*(\S+)", t)
    assert uses and all(re.fullmatch(r"actions/[\w-]+@v\d+", u) for u in uses), uses
