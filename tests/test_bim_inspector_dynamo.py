"""Offline checks for the Manual Dynamo extractor. The script is not executed in Revit."""
import json
from types import SimpleNamespace

import pytest

import bim_inspector
import bim_inspector.dynamo.extract_snapshot as extract
from bim_inspector.dynamo_graph import build_graph
from bim_inspector.dynamo_safety import scan_source
from bim_inspector.model import load_snapshot, validate_snapshot

from tests.bim_inspector_support import ROOT

_DYN = ROOT / "bim_inspector" / "dynamo" / "bim_inspector_extract.dyn"
_SCRIPT = ROOT / "bim_inspector" / "dynamo" / "extract_snapshot.py"
_CI = ROOT / ".github" / "workflows" / "ci.yml"


class _Id:
    def __init__(self, value, legacy=None):
        self.Value = value
        if legacy is not None:
            self.IntegerValue = legacy


class _XYZ:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class _NetBool:
    def __init__(self, value):
        self._value = value

    def __str__(self):
        return "True" if self._value else "False"


class _Param:
    def __init__(self, name, storage, value, has_value=True):
        self.Definition = SimpleNamespace(Name=name)
        self.StorageType = storage
        self.HasValue = has_value
        self._value = value

    def AsString(self):
        return self._value

    def AsValueString(self):
        return None

    def AsInteger(self):
        return self._value

    def AsDouble(self):
        return self._value

    def AsElementId(self):
        return self._value


class _Collector:
    def __init__(self, doc, level_cls):
        self._doc = doc
        self._level_cls = level_cls
        self._mode = None
        self._key = None

    def OfCategory(self, bic):
        self._mode = "category"
        self._key = bic
        return self

    def WhereElementIsNotElementType(self):
        return self

    def OfClass(self, cls):
        self._mode = "class"
        self._key = cls
        return self

    def __iter__(self):
        if self._mode == "class" and self._key is self._level_cls:
            return iter(self._doc.levels)
        if self._mode == "category":
            return iter(self._doc.by_category.get(self._key, []))
        return iter(())


def _db(doc):
    string_kind = object()
    element_kind = object()

    class _Categories:
        OST_MechanicalEquipment = "OST_MechanicalEquipment"

    class _DB:
        BuiltInCategory = _Categories
        BuiltInParameter = SimpleNamespace(RBS_START_LEVEL_PARAM="RBS_START_LEVEL_PARAM")
        StorageType = SimpleNamespace(String=string_kind, Double=object(), Integer=object(), ElementId=element_kind)
        Level = object()

        def FilteredElementCollector(self, document):
            return _Collector(document, self.Level)

    database = _DB()
    database.string_kind = string_kind
    database.element_kind = element_kind
    return database


def _model(database):
    level_param = _Param("Level", database.element_kind, _Id(100))

    class _Element:
        Id = _Id(2001)
        Category = SimpleNamespace(Name="Mechanical Equipment", BuiltInCategory="BuiltInCategory.OST_MechanicalEquipment")
        Parameters = [
            _Param("SCADA_TAG", database.string_kind, "T-1"),
            _Param("SCADA_POINT", database.string_kind, None, has_value=False),
        ]
        Location = SimpleNamespace(Point=_XYZ(1, 2, 10), Curve=None)
        Symbol = SimpleNamespace(
            Name="AHU-Type-A",
            Family=SimpleNamespace(Name="AHU-Sample"),
            Parameters=[
                _Param("SCADA_TAG", database.string_kind, "from-type"),
                _Param("SCADA_EXTRA", database.string_kind, "extra"),
            ],
        )
        ConnectorManager = SimpleNamespace(Connectors=[SimpleNamespace(
            Origin=_XYZ(0, 0, 1), Domain="DomainHvac", IsConnected=_NetBool(False),
        )])

        def get_Parameter(self, bip):
            if bip == "RBS_START_LEVEL_PARAM":
                return level_param
            return None

        def get_BoundingBox(self, view):
            return None

    class _Curve:
        def Evaluate(self, position, normalized):
            assert position == 0.5 and normalized is True
            return _XYZ(0, 0, 3)

    class _CurveElement:
        Id = _Id(2005)
        Category = _Element.Category
        Parameters = []
        Location = SimpleNamespace(Point=None, Curve=_Curve())
        Symbol = None
        ConnectorManager = None

        def get_Parameter(self, bip):
            return None

        def get_BoundingBox(self, view):
            return None

    class _BoxElement:
        Id = _Id(2003)
        Category = _Element.Category
        Parameters = []
        Location = SimpleNamespace(Point=None, Curve=None)
        Symbol = None
        ConnectorManager = None
        MEPModel = SimpleNamespace(ConnectorManager=SimpleNamespace(Connectors=[SimpleNamespace(
            Origin=_XYZ(0, 0, 0), Domain="DomainPiping", IsConnected=True,
        )]))

        def get_Parameter(self, bip):
            return None

        def get_BoundingBox(self, view):
            return SimpleNamespace(Min=_XYZ(0, 0, 0), Max=_XYZ(2, 0, 0))

    class _Bad:
        Id = _Id(2002)
        Category = _Element.Category
        Parameters = []
        Symbol = None
        ConnectorManager = None

        @property
        def Location(self):
            raise RuntimeError(r"C:\secret\project.rvt")

        def get_Parameter(self, bip):
            return None

        def get_BoundingBox(self, view):
            return None

    class _Doc:
        Application = SimpleNamespace(VersionNumber="2025.4")
        levels = [SimpleNamespace(Id=_Id(100), Name="Level-Sample-1", Elevation=0)]
        by_category = {"OST_MechanicalEquipment": [_Element(), _CurveElement(), _BoxElement(), _Bad()]}

        def GetElement(self, eid):
            raise AssertionError("GetElement")

    return _Doc()


def test_import_does_not_run_and_versions_match():
    assert extract.OUT is None
    assert extract.EXTRACTOR_VERSION == bim_inspector.__version__
    assert extract.SCHEMA_VERSION == bim_inspector.SCHEMA_VERSION


def test_pure_helpers():
    assert extract.to_mm(1) == 304.8
    assert extract.to_mm(10) == 3048
    with pytest.raises(TypeError):
        extract.to_mm(True)
    assert extract.element_id_text(_Id(5)) == "5"
    assert extract.element_id_text(_Id(None, legacy=8)) == "8"
    assert extract.element_id_text(_Id(-1)) == ""
    assert extract.builtin_category_name("BuiltInCategory.OST_DuctCurves") == "OST_DuctCurves"
    assert extract.builtin_category_name("-2008000") == ""
    assert extract.assert_json_path("  out.JSON  ") == "out.JSON"
    with pytest.raises(RuntimeError):
        extract.assert_json_path(r"C:\models\building.rvt")
    with pytest.raises(RuntimeError):
        extract.assert_json_path("notes.txt")
    assert extract.clean_label(r"C:\secret\central") == "UNNAMED"
    assert extract.clean_label("  SAMPLE-DEIDENTIFIED  ") == "SAMPLE-DEIDENTIFIED"
    assert extract._as_bool(_NetBool(False)) is False
    assert extract._as_bool(None) is None


def test_length_uses_unit_utils_and_falls_back():
    class _Utils:
        def __init__(self):
            self.calls = 0

        def ConvertFromInternalUnits(self, value, unit):
            self.calls += 1
            assert unit == "mm"
            return float(value) * 1000

    utils = _Utils()
    database = SimpleNamespace(UnitUtils=utils, UnitTypeId=SimpleNamespace(Millimeters="mm"))
    assert extract.length_to_mm(database, 2) == 2000
    assert utils.calls == 1

    class _Boom:
        def ConvertFromInternalUnits(self, value, unit):
            raise RuntimeError("unavailable")

    fallback = SimpleNamespace(UnitUtils=_Boom(), UnitTypeId=SimpleNamespace(Millimeters="mm"))
    assert extract.length_to_mm(fallback, 1) == 304.8


def test_build_snapshot_reads_without_model_writes(tmp_path):
    database = _db(None)
    doc = _model(database)
    payload = extract.build_snapshot(database, doc, "SAMPLE-DEIDENTIFIED")
    validate_snapshot(payload)
    encoded = json.dumps(payload)
    assert "secret" not in encoded
    assert "PathName" not in payload["source"]
    assert payload["source"]["document_identity_omitted"] is True
    assert payload["source"]["host_version"] == "2025.4"
    assert "OST_DuctCurves" in payload["source"]["skipped_categories"]
    assert "OST_MechanicalEquipment" not in payload["source"]["skipped_categories"]
    assert payload["source"]["element_errors"] == [{"id": "2002", "error": "RuntimeError"}]
    by_id = {item["id"]: item for item in payload["elements"]}
    point = by_id["2001"]
    assert point["family"] == "AHU-Sample"
    assert point["type"] == "AHU-Type-A"
    assert point["builtin_category"] == "OST_MechanicalEquipment"
    assert point["level_id"] == "100"
    assert point["level_name"] == "Level-Sample-1"
    assert point["location_mm"] == {"x": 304.8, "y": 609.6, "z": 3048.0}
    assert point["parameters"]["SCADA_TAG"] == "T-1"
    assert point["parameters"]["SCADA_POINT"] == ""
    assert point["parameters"]["SCADA_EXTRA"] == "extra"
    assert point["connectors"] == [{
        "index": 0,
        "domain": "DomainHvac",
        "is_connected": False,
        "origin_mm": {"x": 0.0, "y": 0.0, "z": 304.8},
    }]
    assert by_id["2005"]["location_mm"]["z"] == 914.4
    assert by_id["2003"]["location_mm"]["x"] == 304.8
    assert by_id["2003"]["connectors"][0]["is_connected"] is True
    assert by_id["2003"]["connectors"][0]["domain"] == "DomainPiping"
    target = tmp_path / "snap.json"
    assert extract.write_snapshot(str(target), payload) == str(target)
    assert load_snapshot(target)["elements"][0]["id"] == "2001"
    with pytest.raises(RuntimeError):
        extract.write_snapshot(str(tmp_path / "model.dwg"), payload)


def test_script_and_graph_are_manual_and_read_only():
    source = _SCRIPT.read_text(encoding="utf-8")
    assert scan_source(source) == []
    assert any("Save" in item for item in scan_source("def f(doc):\n    doc.Save()\n"))
    assert any("Set" in item for item in scan_source("def f(param):\n    param.Set(1)\n"))
    dyn = json.loads(_DYN.read_text(encoding="utf-8"))
    assert dyn == build_graph(source)
    assert dyn["View"]["Dynamo"]["RunType"] == "Manual"
    assert dyn["View"]["Dynamo"]["HasRunWithoutCrash"] is False
    assert dyn["Nodes"][0]["Engine"] == "CPython3"
    assert dyn["Nodes"][0]["NodeType"] == "PythonScriptNode"
    assert len(dyn["Nodes"]) == 1
    assert dyn["Connectors"] == []
    hidden = json.loads(json.dumps(dyn))
    hidden["Nodes"][0]["Code"] = ""
    assert "Automatic" not in json.dumps(hidden)


def test_checker_package_does_not_call_revit_and_ci_compiles_it():
    banned = ("Autodesk", "RevitServices", "Transaction", "SynchronizeWithCentral")
    root = ROOT / "bim_inspector"
    for path in root.rglob("*.py"):
        if "dynamo" in path.parts or path.name == "dynamo_safety.py":
            continue
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, (path.name, word)
    workflow = _CI.read_text(encoding="utf-8")
    assert "python -m compileall -q mep_tray tests" in workflow
    assert "python -m compileall -q bim_inspector" in workflow
