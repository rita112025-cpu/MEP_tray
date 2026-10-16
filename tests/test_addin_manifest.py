"""Revit .addin 清單、命令類別與「吊架族群盤點」唯讀性的原始碼檢查（不需 Revit／.NET）。"""
import re
import uuid
import xml.etree.ElementTree as ET

from tests.dotnet_util import ROOT

ADDIN = ROOT / "revit" / "MepTrayImport" / "MepTray.addin"
SRC = ROOT / "revit" / "MepTrayImport"
INV = SRC / "HangerInventoryCommand.cs"


def _code_no_comments(path):
    t = path.read_text(encoding="utf-8")
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    return re.sub(r"//[^\n]*", "", t)


def test_every_command_class_exists_and_ids_unique():
    addins = ET.parse(ADDIN).getroot().findall("AddIn")
    names = {a.findtext("FullClassName") for a in addins}
    assert {"MepTray.Revit.ImportCommand", "MepTray.Revit.HangerInventoryCommand"} <= names
    ids = [str(uuid.UUID(a.findtext("AddInId"))) for a in addins]
    assert len(set(ids)) == len(ids)
    code = "\n".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.cs"))
    for a in addins:
        assert a.get("Type") == "Command"
        assert a.findtext("Assembly") == "MepTrayImport.dll"      # install 腳本以字串取代改寫為絕對路徑
        cls = a.findtext("FullClassName").rsplit(".", 1)[1]
        assert re.search(rf"public\s+class\s+{cls}\s*:\s*IExternalCommand", code), cls
    inv = next(a for a in addins if a.findtext("FullClassName").endswith("HangerInventoryCommand"))
    assert inv.findtext("Text") == "吊架族群盤點"


def test_inventory_is_read_only_by_construction():
    code = _code_no_comments(INV)
    assert "[Transaction(TransactionMode.ReadOnly)]" in code
    for forbidden in ("new Transaction", "SubTransaction", "TransactionGroup", "NewFamilyInstance", ".Delete(", "LoadFamily",
                      "ElementTransformUtils", ".Save(", ".SaveAs(", "Close(true)", ".Set(", "HttpClient", "WebClient",
                      "Task.Run", "new Thread", "ThreadPool", "EditFamily"):
        assert forbidden not in code, forbidden
    assert "fdoc.Close(false)" in code and "openedHere" in code     # 只關閉自己開啟的族文件，且不儲存


def test_inventory_default_folder_and_no_hardcoded_rules():
    code = INV.read_text(encoding="utf-8")
    assert r"04_台灣專用元件族\13_固定工具" in code
    core = (ROOT / "revit" / "MepTray.Core" / "HangerInventory.cs").read_text(encoding="utf-8")
    for src in (code, core):
        assert not re.search(r"\b(1000|255)\b", src)                  # 不把支撐間距/接頭距離寫死

def test_hanger_commands_registered_with_manual_transaction():
    import re as _re
    xml = (SRC / "MepTray.addin").read_text(encoding="utf-8")
    assert "<FullClassName>MepTray.Revit.AddHangersCommand</FullClassName>" in xml
    run_cs = (SRC / "HangerRun.cs").read_text(encoding="utf-8")
    assert _re.search(r"\[Transaction\(TransactionMode\.Manual\)\]\s*public class AddHangersCommand", run_cs)
    imp = (SRC / "ImportCommand.cs").read_text(encoding="utf-8")
    assert "TransactionGroup" in imp and "Assimilate" in imp and "RollBack" in imp
    placer = (SRC / "HangerPlacer.cs").read_text(encoding="utf-8")
    assert "SubTransaction" in placer and "Unblock" not in placer
    ps = (SRC.parent.parent / "scripts" / "install_revit_addin.ps1").read_text(encoding="utf-8")
    assert "foreach ($a in $addin.RevitAddIns.AddIn)" in ps          # 逐一檢查 manifest 內每個命令類別
    for cls in ("TargetFamilyCommand", "CalibrateHangerCommand"):
        assert f"<FullClassName>MepTray.Revit.{cls}</FullClassName>" in xml
    tgt = (SRC / "TargetFamilyCommand.cs").read_text(encoding="utf-8")
    assert _re.search(r"\[Transaction\(TransactionMode\.ReadOnly\)\]\s*public class TargetFamilyCommand", tgt)
    assert "new Transaction" not in tgt and ".Save(" not in tgt and ".SaveAs(" not in tgt
    cal = (SRC / "CalibrateHangerCommand.cs").read_text(encoding="utf-8")
    assert _re.search(r"\[Transaction\(TransactionMode\.Manual\)\]\s*public class CalibrateHangerCommand", cal)
    assert "group.RollBack()" in cal and "trial?.RollBack()" in cal
    assert "SaveAs" not in cal and "EditFamily" not in cal          # 不開啟/儲存原族檔
    inv = (SRC / "HangerInventoryCommand.cs").read_text(encoding="utf-8")
    assert "[Transaction(TransactionMode.ReadOnly)]" in inv