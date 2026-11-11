"""Shared builders for BIM inspector tests. Not collected by pytest."""
from __future__ import annotations

import copy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bim_inspector" / "fixtures"


def minimal_rules() -> dict:
    return {
        "schema_version": 1,
        "notes": "unit-test rules; thresholds are explicit test values",
        "checks": {
            "required_parameters": {
                "enabled": True,
                "rules": [
                    {
                        "id": "EQ-SCADA",
                        "builtin_categories": ["OST_MechanicalEquipment"],
                        "categories": [],
                        "families": [],
                        "types": [],
                        "parameters": ["SCADA_TAG", "SCADA_POINT"],
                        "parameter_prefix": "SCADA_",
                        "empty_is_missing": True,
                    }
                ],
            },
            "open_connectors": {
                "enabled": True,
                "id": "OPEN-END",
                "builtin_categories": ["OST_CableTray", "OST_DuctCurves"],
                "categories": [],
                "flag_when_no_connectors": False,
            },
            "elevation": {
                "enabled": True,
                "offset_rules": [
                    {
                        "id": "ELEV-DUCT",
                        "builtin_categories": ["OST_DuctCurves"],
                        "categories": [],
                        "families": [],
                        "types": [],
                        "level_ids": [],
                        "level_names": ["L-A"],
                        "min_offset_mm": 10,
                        "max_offset_mm": 20,
                    }
                ],
                "level_consistency": {
                    "enabled": True,
                    "id": "ELEV-LEVEL",
                    "builtin_categories": ["OST_MechanicalEquipment"],
                    "categories": [],
                    "families": [],
                    "types": [],
                    "margin_mm": 5,
                },
            },
            "duplicates": {
                "enabled": True,
                "same_location": {
                    "enabled": True,
                    "id": "DUP-LOCATION",
                    "tolerance_mm": 1,
                    "builtin_categories": [],
                    "categories": [],
                    "families": [],
                    "types": [],
                },
                "family_names": {
                    "enabled": True,
                    "id": "DUP-FAMILY-NAME",
                    "suffix_patterns": ["\\s*\\(\\d+\\)\\s*", "(?<=\\D)\\d+"],
                },
                "count_ranges": [
                    {
                        "id": "COUNT-AHU",
                        "builtin_categories": ["OST_MechanicalEquipment"],
                        "categories": [],
                        "family": "AHU-Sample",
                        "type": None,
                        "min_count": 0,
                        "max_count": 5,
                    }
                ],
            },
        },
    }


def rules() -> dict:
    return copy.deepcopy(minimal_rules())


def levels() -> list[dict]:
    return [
        {"id": "L1", "name": "L-A", "elevation_mm": 0},
        {"id": "L2", "name": "L-B", "elevation_mm": 100},
    ]


def element(**overrides) -> dict:
    base = {
        "id": "E1",
        "category": "Mechanical Equipment",
        "builtin_category": "OST_MechanicalEquipment",
        "family": "AHU-Sample",
        "type": "AHU-Type-A",
        "level_id": "L1",
        "level_name": "L-A",
        "location_mm": {"x": 0, "y": 0, "z": 5},
        "parameters": {"SCADA_TAG": "T-1", "SCADA_POINT": "P-1", "SCADA_STATUS": "OK"},
        "connectors": [],
    }
    base.update(overrides)
    return base


def snapshot(elements, level_rows=None) -> dict:
    return {
        "schema_version": 1,
        "source": {
            "extractor": "unit-test",
            "extractor_version": "0.1.0",
            "host": "fixture",
            "host_version": "",
            "units": "mm",
            "project_label": "SAMPLE-DEIDENTIFIED",
            "document_identity_omitted": True,
            "length_unit_note": "millimetres",
            "skipped_categories": [],
            "element_errors": [],
        },
        "levels": levels() if level_rows is None else level_rows,
        "elements": elements,
    }


def ids(findings, check=None, rule_id=None) -> list[str]:
    rows = []
    for finding in findings:
        if check is not None and finding.check != check:
            continue
        if rule_id is not None and finding.rule_id != rule_id:
            continue
        rows.append(finding.element_id)
    return rows
