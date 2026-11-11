# -*- coding: utf-8 -*-
"""Read-only Revit snapshot for the BIM inspector.

Paste this file into a Dynamo 3.3 CPython3 Python node, or open
``bim_inspector_extract.dyn`` and run it by hand (Manual).

How to run in Dynamo:
1. Open the graph. Confirm the run mode is Manual. Do not switch it to run by itself.
2. Fill DEFAULT_OUTPUT_JSON below with a .json path, or wire a string into IN[0].
3. Optionally fill DEFAULT_PROJECT_LABEL, or wire a fake label into IN[1].
   Do not put a real project name, path, or central-model name in the label.
4. Open the model you want to read, then click Run once.

The script reads the open document and writes JSON. It does not start a
transaction, and it does not save, sync, or upgrade the model.

UNVERIFIED against a live Revit 2025 session. Connector properties, category
enums, and UnitUtils are used defensively and may differ on a real document.
"""
from __future__ import annotations

import json
import math
import os
import re

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "0.1.0"
DEFAULT_OUTPUT_JSON = ""
DEFAULT_PROJECT_LABEL = "UNNAMED"
LENGTH_NOTE = (
    "Millimetres. Revit internal feet are converted with UnitUtils to millimetres when that API is present; "
    "otherwise feet are multiplied by 304.8 and rounded to 0.001 mm."
)

# Built-in category enum names. Display names are localized, so the checker matches these.
EXTRACT_CATEGORIES = (
    "OST_MechanicalEquipment",
    "OST_ElectricalEquipment",
    "OST_PlumbingFixtures",
    "OST_SpecialityEquipment",
    "OST_Sprinklers",
    "OST_DataDevices",
    "OST_LightingDevices",
    "OST_LightingFixtures",
    "OST_ElectricalFixtures",
    "OST_FireAlarmDevices",
    "OST_SecurityDevices",
    "OST_CommunicationDevices",
    "OST_NurseCallDevices",
    "OST_TelephoneDevices",
    "OST_CableTray",
    "OST_CableTrayFitting",
    "OST_Conduit",
    "OST_ConduitFitting",
    "OST_PipeCurves",
    "OST_PipeFitting",
    "OST_FlexPipeCurves",
    "OST_DuctCurves",
    "OST_DuctFitting",
    "OST_FlexDuctCurves",
    "OST_PlaceHolderPipes",
    "OST_PlaceHolderDucts",
)

_FAMILY_PARAMS = ("ALL_MODEL_FAMILY_NAME", "SYMBOL_FAMILY_NAME_PARAM", "ELEM_FAMILY_PARAM")
_TYPE_PARAMS = ("ALL_MODEL_TYPE_NAME", "SYMBOL_NAME_PARAM", "ELEM_TYPE_PARAM")
_LEVEL_PARAMS = (
    "RBS_START_LEVEL_PARAM",
    "FAMILY_LEVEL_PARAM",
    "SCHEDULE_LEVEL_PARAM",
    "INSTANCE_REFERENCE_LEVEL_PARAM",
    "LEVEL_PARAM",
)
_MODEL_SUFFIXES = (".rvt", ".rfa", ".rte", ".rft", ".dwg", ".dxf", ".dwt", ".ifc")
_PATHY = re.compile(r"([A-Za-z]:\\|\\\\|/home/|/Users/)")


def to_mm(feet: object) -> float:
    """Convert a Revit internal foot value with the fixed 304.8 mm factor."""
    if isinstance(feet, bool) or not isinstance(feet, (int, float)):
        raise TypeError("length must be a number")
    value = float(feet) * 304.8
    if not math.isfinite(value):
        raise ValueError("non-finite length")
    return round(value, 3)


def length_to_mm(DB, feet: object) -> float:
    converted = _via_unit_utils(DB, feet)
    if converted is None:
        return to_mm(feet)
    value = float(converted)
    if not math.isfinite(value):
        raise ValueError("non-finite length")
    return round(value, 3)


def element_id_text(element_id: object) -> str:
    """Revit 2024+ uses ElementId.Value; older builds expose IntegerValue."""
    if element_id is None:
        return ""
    value = getattr(element_id, "Value", None)
    if value is None:
        value = getattr(element_id, "IntegerValue", None)
    if isinstance(value, bool) or value is None:
        return ""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number < 0:
        return ""
    return str(number)


def builtin_category_name(raw: object) -> str:
    if raw is None:
        return ""
    text = str(raw)
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if not text.startswith("OST_"):
        return ""
    return text


def assert_json_path(path: object) -> str:
    if not isinstance(path, str):
        raise RuntimeError("輸出路徑必須是字串")
    if "\x00" in path:
        raise RuntimeError("輸出路徑無效")
    cleaned = path.strip().strip('"')
    if not cleaned:
        raise RuntimeError("需要輸出 .json 路徑（IN[0] 或 DEFAULT_OUTPUT_JSON）")
    lower = cleaned.lower()
    for suffix in _MODEL_SUFFIXES:
        if lower.endswith(suffix):
            raise RuntimeError("拒絕寫入模型檔，輸出必須是 .json")
    if not lower.endswith(".json"):
        raise RuntimeError("輸出路徑必須以 .json 結尾")
    return cleaned


def clean_label(label: object) -> str:
    text = " ".join(str(label).split())
    if not text or any(sep in text for sep in ("\\", "/", ":")):
        return "UNNAMED"
    return text[:80]


def build_snapshot(DB, doc, project_label: object) -> dict:
    """Read categories, levels, parameters, connectors, and locations. No writes."""
    level_rows, level_errors = _read_levels(DB, doc)
    levels_by_id = {row["id"]: row["name"] for row in level_rows}
    elements: list[dict] = []
    errors = list(level_errors)
    skipped: list[str] = []
    for name in EXTRACT_CATEGORIES:
        bic = getattr(DB.BuiltInCategory, name, None)
        if bic is None:
            skipped.append(name)
            continue
        try:
            collected = list(DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType())
        except Exception as exc:
            skipped.append(name)
            errors.append({"id": "", "error": _short_error(exc)})
            continue
        for element in collected:
            try:
                elements.append(_read_element(DB, doc, element, levels_by_id))
            except Exception as exc:
                errors.append({"id": _safe_id(element), "error": _short_error(exc)})
    elements.sort(key=lambda item: item["id"])
    errors.sort(key=lambda item: (item["id"], item["error"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "extractor": "bim_inspector.dynamo.extract_snapshot",
            "extractor_version": EXTRACTOR_VERSION,
            "host": "Revit",
            "host_version": _host_version(doc),
            "units": "mm",
            "project_label": clean_label(project_label),
            "document_identity_omitted": True,
            "length_unit_note": LENGTH_NOTE,
            "skipped_categories": sorted(set(skipped)),
            "element_errors": errors,
        },
        "levels": level_rows,
        "elements": elements,
    }


def write_snapshot(path: object, payload: dict) -> str:
    target = assert_json_path(path)
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        raise RuntimeError("輸出資料夾不存在")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")
    return target


def dynamo_main() -> str:
    output = _injected_input(0) or DEFAULT_OUTPUT_JSON
    label = _injected_input(1) or DEFAULT_PROJECT_LABEL
    DB, document = _open_readonly_document()
    payload = build_snapshot(DB, document, label)
    return write_snapshot(output, payload)


def _via_unit_utils(DB, feet: object):
    utils = getattr(DB, "UnitUtils", None)
    unit_ids = getattr(DB, "UnitTypeId", None)
    if utils is None or unit_ids is None:
        return None
    millimeters = getattr(unit_ids, "Millimeters", None)
    convert = getattr(utils, "ConvertFromInternalUnits", None)
    if millimeters is None or convert is None:
        return None
    try:
        return convert(float(feet), millimeters)
    except Exception:
        return None


def _read_levels(DB, doc) -> tuple[list[dict], list[dict]]:
    try:
        raw = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
    except Exception as exc:
        return [], [{"id": "", "error": _short_error(exc)}]
    rows: list[dict] = []
    errors: list[dict] = []
    for level in raw:
        try:
            ident = element_id_text(getattr(level, "Id", None))
            if not ident:
                errors.append({"id": "", "error": "LevelMissingId"})
                continue
            rows.append({
                "id": ident,
                "name": _clip(str(getattr(level, "Name", "") or ""), 200),
                "elevation_mm": length_to_mm(DB, level.Elevation),
            })
        except Exception as exc:
            errors.append({"id": _safe_id(level), "error": _short_error(exc)})
    rows.sort(key=lambda item: item["id"])
    return rows, errors


def _read_element(DB, doc, element, levels_by_id: dict) -> dict:
    ident = element_id_text(getattr(element, "Id", None))
    if not ident:
        raise RuntimeError("ElementMissingId")
    category_name, builtin = _category(element)
    family, type_name = _family_and_type(DB, doc, element)
    level_id = _level_id(DB, element)
    return {
        "id": ident,
        "category": category_name,
        "builtin_category": builtin,
        "family": family,
        "type": type_name,
        "level_id": level_id,
        "level_name": levels_by_id.get(level_id, ""),
        "location_mm": _location_mm(DB, element),
        "parameters": _parameters(DB, doc, element),
        "connectors": _connectors(DB, element),
    }


def _category(element) -> tuple[str, str]:
    category = getattr(element, "Category", None)
    if category is None:
        return "", ""
    name = _clip(str(getattr(category, "Name", "") or ""), 200)
    builtin = builtin_category_name(getattr(category, "BuiltInCategory", None))
    return name, builtin


def _family_and_type(DB, doc, element) -> tuple[str, str]:
    symbol = _type_element(DB, doc, element)
    family = ""
    type_name = ""
    if symbol is not None:
        type_name = _clip(str(getattr(symbol, "Name", "") or ""), 200)
        fam = getattr(symbol, "Family", None)
        if fam is not None:
            family = _clip(str(getattr(fam, "Name", "") or ""), 200)
        if not family:
            family = _clip(str(getattr(symbol, "FamilyName", "") or ""), 200)
    if not family:
        family = _clip(_named_param_text(DB, element, _FAMILY_PARAMS), 200)
    if not type_name:
        type_name = _clip(_named_param_text(DB, element, _TYPE_PARAMS), 200)
    return family, type_name


def _type_element(DB, doc, element):
    symbol = getattr(element, "Symbol", None)
    if symbol is not None:
        return symbol
    getter = getattr(element, "GetTypeId", None)
    if getter is None:
        return None
    try:
        type_id = getter()
    except Exception:
        return None
    if not element_id_text(type_id):
        return None
    try:
        return doc.GetElement(type_id)
    except Exception:
        return None


def _parameters(DB, doc, element) -> dict:
    found: dict[str, str] = {}
    _absorb(DB, found, element)
    symbol = _type_element(DB, doc, element)
    if symbol is not None and symbol is not element:
        _absorb(DB, found, symbol)
    return dict(sorted(found.items()))


def _absorb(DB, found: dict, element) -> None:
    try:
        raw = element.Parameters
    except Exception:
        return
    if raw is None:
        return
    try:
        params = list(raw)
    except Exception:
        return
    for param in params:
        name = _param_name(param)
        if not name or name in found:
            continue
        found[name] = _param_text(DB, param)


def _param_name(param) -> str:
    definition = getattr(param, "Definition", None)
    if definition is None:
        return ""
    return str(getattr(definition, "Name", "") or "").strip()


def _param_text(DB, param) -> str:
    if param is None or not getattr(param, "HasValue", False):
        return ""
    storage = param.StorageType
    kinds = DB.StorageType
    if storage == kinds.String:
        return _clip(_as_text(_call(param, "AsString")), 500)
    if storage == kinds.Integer:
        shown = _as_text(_call(param, "AsValueString"))
        if shown.strip():
            return _clip(shown, 500)
        number = _call(param, "AsInteger")
        return "" if number is None else _clip(str(int(number)), 500)
    if storage == kinds.Double:
        shown = _as_text(_call(param, "AsValueString"))
        if shown.strip():
            return _clip(shown, 500)
        number = _call(param, "AsDouble")
        return "" if number is None else _clip(_number_text(number), 500)
    if storage == kinds.ElementId:
        return element_id_text(_call(param, "AsElementId"))
    return _clip(_as_text(_call(param, "AsValueString")), 500)


def _named_param_text(DB, element, names: tuple[str, ...]) -> str:
    bips = getattr(DB, "BuiltInParameter", None)
    getter = getattr(element, "get_Parameter", None)
    if bips is None or getter is None:
        return ""
    for name in names:
        bip = getattr(bips, name, None)
        if bip is None:
            continue
        try:
            param = getter(bip)
        except Exception:
            continue
        text = _param_text(DB, param) if param is not None else ""
        if text.strip():
            return text.strip()
    return ""


def _level_id(DB, element) -> str:
    bips = getattr(DB, "BuiltInParameter", None)
    getter = getattr(element, "get_Parameter", None)
    if bips is None or getter is None:
        return ""
    for name in _LEVEL_PARAMS:
        bip = getattr(bips, name, None)
        if bip is None:
            continue
        try:
            param = getter(bip)
        except Exception:
            continue
        if param is None or not getattr(param, "HasValue", False):
            continue
        if param.StorageType != DB.StorageType.ElementId:
            continue
        text = element_id_text(_call(param, "AsElementId"))
        if text:
            return text
    return ""


def _location_mm(DB, element):
    location = getattr(element, "Location", None)
    if location is not None:
        point = getattr(location, "Point", None)
        if point is not None:
            return _xyz_mm(DB, point)
        curve = getattr(location, "Curve", None)
        if curve is not None:
            try:
                return _xyz_mm(DB, curve.Evaluate(0.5, True))
            except Exception:
                pass
    getter = getattr(element, "get_BoundingBox", None)
    if getter is None:
        return None
    try:
        box = getter(None)
    except Exception:
        return None
    if box is None:
        return None
    low, high = box.Min, box.Max
    return {
        "x": length_to_mm(DB, (low.X + high.X) / 2.0),
        "y": length_to_mm(DB, (low.Y + high.Y) / 2.0),
        "z": length_to_mm(DB, (low.Z + high.Z) / 2.0),
    }


def _xyz_mm(DB, point) -> dict:
    return {"x": length_to_mm(DB, point.X), "y": length_to_mm(DB, point.Y), "z": length_to_mm(DB, point.Z)}


def _connectors(DB, element) -> list[dict]:
    manager = getattr(element, "ConnectorManager", None)
    if manager is None:
        mep = getattr(element, "MEPModel", None)
        if mep is not None:
            manager = getattr(mep, "ConnectorManager", None)
    if manager is None:
        return []
    try:
        raw = manager.Connectors
    except Exception:
        return []
    if raw is None:
        return []
    try:
        items = list(raw)
    except Exception:
        return []
    found = []
    for index, connector in enumerate(items):
        origin = None
        try:
            origin = _xyz_mm(DB, connector.Origin)
        except Exception:
            origin = None
        try:
            domain = _clip(str(connector.Domain), 80)
        except Exception:
            domain = ""
        found.append({
            "index": index,
            "domain": domain,
            "is_connected": _as_bool(getattr(connector, "IsConnected", None)),
            "origin_mm": origin,
        })
    return found


def _as_bool(value):
    if value is None:
        return None
    if value is True or value is False or type(value) is bool:
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "false"):
        return text == "true"
    return None


def _open_readonly_document():
    # Imported only when Dynamo actually runs this script.
    import clr
    clr.AddReference("RevitAPI")
    clr.AddReference("RevitServices")
    import Autodesk.Revit.DB as DB
    from RevitServices.Persistence import DocumentManager
    document = DocumentManager.Instance.CurrentDBDocument
    if document is None:
        raise RuntimeError("沒有開啟中的 Revit 文件")
    return DB, document


def _injected_input(index: int):
    values = globals().get("IN")
    if values is None:
        return ""
    try:
        value = values[index]
    except Exception:
        return ""
    return _unwrap(value)


def _unwrap(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for attr in ("FileName", "FullName"):
        got = getattr(value, attr, None)
        if isinstance(got, str) and got:
            return got
    if isinstance(value, (list, tuple)) and value:
        return _unwrap(value[0])
    return str(value)


def _host_version(doc) -> str:
    try:
        text = str(doc.Application.VersionNumber)
    except Exception:
        return ""
    return "".join(ch for ch in text if ch.isdigit() or ch == ".")[:20]


def _safe_id(element) -> str:
    try:
        return element_id_text(getattr(element, "Id", None))
    except Exception:
        return ""


def _short_error(exc: BaseException) -> str:
    text = " ".join(f"{exc.__class__.__name__}: {exc}".split())
    if _PATHY.search(text):
        return exc.__class__.__name__
    return text[:200]


def _call(obj, name: str):
    method = getattr(obj, name, None)
    if method is None:
        return None
    try:
        return method()
    except Exception:
        return None


def _as_text(value) -> str:
    return "" if value is None else str(value)


def _number_text(value) -> str:
    number = float(value)
    if not math.isfinite(number):
        return ""
    rounded = round(number, 6)
    text = f"{rounded:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _run_if_dynamo():
    if "IN" not in globals():
        return None
    return dynamo_main()


OUT = _run_if_dynamo()
