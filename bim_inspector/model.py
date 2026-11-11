"""Snapshot and rule-file validation.

Thresholds are accepted only when the rule file states them. This module does
not fill in project dimensions.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from . import SCHEMA_VERSION
from .jsonutil import load_json

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OST = re.compile(r"^OST_[A-Za-z0-9_]+$")
_CHECK_KEYS = ("required_parameters", "open_connectors", "elevation", "duplicates")

_SOURCE_KEYS = (
    "extractor",
    "extractor_version",
    "host",
    "host_version",
    "units",
    "project_label",
    "document_identity_omitted",
    "length_unit_note",
    "skipped_categories",
    "element_errors",
)
_ELEMENT_REQUIRED = (
    "id",
    "category",
    "builtin_category",
    "family",
    "type",
    "level_id",
    "level_name",
    "location_mm",
    "parameters",
    "connectors",
)
_ELEMENT_OPTIONAL = ("unique_id",)
_XYZ = ("x", "y", "z")


def load_snapshot(path: Path) -> dict:
    return validate_snapshot(load_json(path))


def load_rules(path: Path) -> dict:
    return validate_rules(load_json(path))


def validate_snapshot(data: dict) -> dict:
    _object(data, "snapshot")
    _exact_keys(data, ("schema_version", "source", "levels", "elements"), ("notes",), "snapshot")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"snapshot schema_version must be {SCHEMA_VERSION}")
    source = _object(data["source"], "source")
    _exact_keys(source, _SOURCE_KEYS, (), "source")
    for key in ("extractor", "extractor_version", "host", "host_version", "project_label", "length_unit_note"):
        _string(source[key], f"source.{key}")
    if source["units"] != "mm":
        raise ValueError("source.units must be mm")
    if source["document_identity_omitted"] is not True:
        raise ValueError("source.document_identity_omitted must be true")
    _string_list(source["skipped_categories"], "source.skipped_categories")
    errors = source["element_errors"]
    if not isinstance(errors, list):
        raise ValueError("source.element_errors must be a list")
    for index, item in enumerate(errors):
        item = _object(item, f"source.element_errors[{index}]")
        _exact_keys(item, ("id", "error"), (), f"source.element_errors[{index}]")
        _string(item["id"], f"source.element_errors[{index}].id")
        _string(item["error"], f"source.element_errors[{index}].error")

    levels = data["levels"]
    if not isinstance(levels, list):
        raise ValueError("levels must be a list")
    seen_levels: set[str] = set()
    for index, level in enumerate(levels):
        level = _object(level, f"levels[{index}]")
        _exact_keys(level, ("id", "name", "elevation_mm"), (), f"levels[{index}]")
        _string(level["id"], f"levels[{index}].id", empty=False)
        _string(level["name"], f"levels[{index}].name")
        _finite(level["elevation_mm"], f"levels[{index}].elevation_mm")
        if level["id"] in seen_levels:
            raise ValueError(f"duplicate level id: {level['id']}")
        seen_levels.add(level["id"])

    elements = data["elements"]
    if not isinstance(elements, list):
        raise ValueError("elements must be a list")
    seen_ids: set[str] = set()
    for index, element in enumerate(elements):
        _validate_element(element, index)
        if element["id"] in seen_ids:
            raise ValueError(f"duplicate element id: {element['id']}")
        seen_ids.add(element["id"])
    return data


def validate_rules(data: dict) -> dict:
    _object(data, "rules")
    _exact_keys(data, ("schema_version", "checks"), ("notes",), "rules")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"rules schema_version must be {SCHEMA_VERSION}")
    if "notes" in data:
        _string(data["notes"], "notes")
    checks = _object(data["checks"], "checks")
    _exact_keys(checks, _CHECK_KEYS, (), "checks")
    _validate_parameter_rules(checks["required_parameters"])
    _validate_open_connectors(checks["open_connectors"])
    _validate_elevation(checks["elevation"])
    _validate_duplicates(checks["duplicates"])
    return data


def _validate_element(element: object, index: int) -> None:
    where = f"elements[{index}]"
    element = _object(element, where)
    _exact_keys(element, _ELEMENT_REQUIRED, _ELEMENT_OPTIONAL, where)
    _string(element["id"], f"{where}.id", empty=False)
    for key in ("category", "builtin_category", "family", "type", "level_id", "level_name"):
        _string(element[key], f"{where}.{key}")
    if element["builtin_category"] and not _OST.match(element["builtin_category"]):
        raise ValueError(f"{where}.builtin_category must be an OST_ name or empty")
    if "unique_id" in element:
        _string(element["unique_id"], f"{where}.unique_id")
    _location(element["location_mm"], f"{where}.location_mm")
    params = element["parameters"]
    if not isinstance(params, dict):
        raise ValueError(f"{where}.parameters must be an object")
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{where}.parameters has an empty name")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{where}.parameters.{key} must be a scalar")
        if isinstance(value, float):
            _finite(value, f"{where}.parameters.{key}")
    connectors = element["connectors"]
    if not isinstance(connectors, list):
        raise ValueError(f"{where}.connectors must be a list")
    for c_index, connector in enumerate(connectors):
        c_where = f"{where}.connectors[{c_index}]"
        connector = _object(connector, c_where)
        _exact_keys(connector, ("index", "domain", "is_connected", "origin_mm"), (), c_where)
        if not isinstance(connector["index"], int) or isinstance(connector["index"], bool) or connector["index"] < 0:
            raise ValueError(f"{c_where}.index must be a non-negative integer")
        _string(connector["domain"], f"{c_where}.domain")
        if connector["is_connected"] is not None and not isinstance(connector["is_connected"], bool):
            raise ValueError(f"{c_where}.is_connected must be true, false, or null")
        _location(connector["origin_mm"], f"{c_where}.origin_mm")


def _validate_parameter_rules(block: object) -> None:
    block = _object(block, "required_parameters")
    _exact_keys(block, ("enabled", "rules"), (), "required_parameters")
    _bool(block["enabled"], "required_parameters.enabled")
    rules = _list(block["rules"], "required_parameters.rules")
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        where = f"required_parameters.rules[{index}]"
        rule = _object(rule, where)
        _exact_keys(
            rule,
            ("id", "builtin_categories", "categories", "families", "types", "parameters", "parameter_prefix", "empty_is_missing"),
            (),
            where,
        )
        _rule_id(rule["id"], f"{where}.id", seen)
        _category_filters(rule, where)
        _string_list(rule["families"], f"{where}.families")
        _string_list(rule["types"], f"{where}.types")
        _string_list(rule["parameters"], f"{where}.parameters", empty_item=False)
        prefix = rule["parameter_prefix"]
        if prefix is not None:
            _string(prefix, f"{where}.parameter_prefix", empty=False)
        _bool(rule["empty_is_missing"], f"{where}.empty_is_missing")
        if not rule["parameters"] and prefix is None:
            raise ValueError(f"{where} needs parameters or parameter_prefix")


def _validate_open_connectors(block: object) -> None:
    block = _object(block, "open_connectors")
    _exact_keys(block, ("enabled", "id", "builtin_categories", "categories", "flag_when_no_connectors"), (), "open_connectors")
    _bool(block["enabled"], "open_connectors.enabled")
    _rule_id(block["id"], "open_connectors.id", set())
    _string_list(block["builtin_categories"], "open_connectors.builtin_categories", ost=True)
    _string_list(block["categories"], "open_connectors.categories")
    _bool(block["flag_when_no_connectors"], "open_connectors.flag_when_no_connectors")


def _validate_elevation(block: object) -> None:
    block = _object(block, "elevation")
    _exact_keys(block, ("enabled", "offset_rules", "level_consistency"), (), "elevation")
    _bool(block["enabled"], "elevation.enabled")
    rules = _list(block["offset_rules"], "elevation.offset_rules")
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        where = f"elevation.offset_rules[{index}]"
        rule = _object(rule, where)
        _exact_keys(
            rule,
            ("id", "builtin_categories", "categories", "families", "types", "level_ids", "level_names", "min_offset_mm", "max_offset_mm"),
            (),
            where,
        )
        _rule_id(rule["id"], f"{where}.id", seen)
        _category_filters(rule, where)
        _string_list(rule["families"], f"{where}.families")
        _string_list(rule["types"], f"{where}.types")
        _string_list(rule["level_ids"], f"{where}.level_ids")
        _string_list(rule["level_names"], f"{where}.level_names")
        lo = _finite(rule["min_offset_mm"], f"{where}.min_offset_mm")
        hi = _finite(rule["max_offset_mm"], f"{where}.max_offset_mm")
        if lo > hi:
            raise ValueError(f"{where} min_offset_mm is greater than max_offset_mm")
    consistency = _object(block["level_consistency"], "elevation.level_consistency")
    _exact_keys(
        consistency,
        ("enabled", "id", "builtin_categories", "categories", "families", "types", "margin_mm"),
        (),
        "elevation.level_consistency",
    )
    _bool(consistency["enabled"], "elevation.level_consistency.enabled")
    _rule_id(consistency["id"], "elevation.level_consistency.id", seen)
    _category_filters(consistency, "elevation.level_consistency")
    _string_list(consistency["families"], "elevation.level_consistency.families")
    _string_list(consistency["types"], "elevation.level_consistency.types")
    margin = _finite(consistency["margin_mm"], "elevation.level_consistency.margin_mm")
    if margin < 0:
        raise ValueError("elevation.level_consistency.margin_mm must be >= 0")


def _validate_duplicates(block: object) -> None:
    block = _object(block, "duplicates")
    _exact_keys(block, ("enabled", "same_location", "family_names", "count_ranges"), (), "duplicates")
    _bool(block["enabled"], "duplicates.enabled")
    same = _object(block["same_location"], "duplicates.same_location")
    _exact_keys(
        same,
        ("enabled", "id", "tolerance_mm", "builtin_categories", "categories", "families", "types"),
        (),
        "duplicates.same_location",
    )
    _bool(same["enabled"], "duplicates.same_location.enabled")
    seen: set[str] = set()
    _rule_id(same["id"], "duplicates.same_location.id", seen)
    tolerance = _finite(same["tolerance_mm"], "duplicates.same_location.tolerance_mm")
    if tolerance < 0:
        raise ValueError("duplicates.same_location.tolerance_mm must be >= 0")
    _category_filters(same, "duplicates.same_location")
    _string_list(same["families"], "duplicates.same_location.families")
    _string_list(same["types"], "duplicates.same_location.types")

    names = _object(block["family_names"], "duplicates.family_names")
    _exact_keys(names, ("enabled", "id", "suffix_patterns"), (), "duplicates.family_names")
    _bool(names["enabled"], "duplicates.family_names.enabled")
    _rule_id(names["id"], "duplicates.family_names.id", seen)
    patterns = _string_list(names["suffix_patterns"], "duplicates.family_names.suffix_patterns", empty_item=False)
    if not patterns:
        raise ValueError("duplicates.family_names.suffix_patterns must not be empty")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid suffix pattern: {pattern}") from exc

    ranges = _list(block["count_ranges"], "duplicates.count_ranges")
    for index, rule in enumerate(ranges):
        where = f"duplicates.count_ranges[{index}]"
        rule = _object(rule, where)
        _exact_keys(rule, ("id", "builtin_categories", "categories", "family", "type", "min_count", "max_count"), (), where)
        _rule_id(rule["id"], f"{where}.id", seen)
        _string_list(rule["builtin_categories"], f"{where}.builtin_categories", ost=True)
        _string_list(rule["categories"], f"{where}.categories")
        if rule["family"] is not None:
            _string(rule["family"], f"{where}.family", empty=False)
        if rule["type"] is not None:
            _string(rule["type"], f"{where}.type", empty=False)
        lo = _count(rule["min_count"], f"{where}.min_count")
        hi = _count(rule["max_count"], f"{where}.max_count")
        if lo > hi:
            raise ValueError(f"{where} min_count is greater than max_count")


def _category_filters(rule: dict, where: str) -> None:
    _string_list(rule["builtin_categories"], f"{where}.builtin_categories", ost=True)
    _string_list(rule["categories"], f"{where}.categories")


def _exact_keys(obj: dict, required: tuple[str, ...], optional: tuple[str, ...], where: str) -> None:
    allowed = set(required) | set(optional)
    missing = [key for key in required if key not in obj]
    unknown = [key for key in obj if key not in allowed]
    if missing or unknown:
        raise ValueError(f"{where}: missing={missing} unknown={unknown}")


def _object(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _list(value: object, where: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    return value


def _string(value: object, where: str, empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string")
    if not empty and not value:
        raise ValueError(f"{where} must not be empty")
    return value


def _string_list(value: object, where: str, ost: bool = False, empty_item: bool = True) -> list[str]:
    items = _list(value, where)
    for index, item in enumerate(items):
        _string(item, f"{where}[{index}]", empty=empty_item)
        if ost and item and not _OST.match(item):
            raise ValueError(f"{where}[{index}] must be an OST_ name")
    return items


def _bool(value: object, where: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be a boolean")


def _finite(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{where} must be a finite number")
    return number


def _count(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an integer")
    if value < 0:
        raise ValueError(f"{where} must be >= 0")
    return value


def _rule_id(value: object, where: str, seen: set[str]) -> None:
    _string(value, where, empty=False)
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{where} must be a rule id")
    if value in seen:
        raise ValueError(f"duplicate rule id: {value}")
    seen.add(value)


def _location(value: object, where: str) -> None:
    if value is None:
        return
    point = _object(value, where)
    _exact_keys(point, _XYZ, (), where)
    for key in _XYZ:
        _finite(point[key], f"{where}.{key}")
