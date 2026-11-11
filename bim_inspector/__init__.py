"""Read-only BIM model inspector (phase 1).

The checker reads a JSON snapshot and a JSON rule file. It does not import
Revit or AutoCAD and it has no path that edits, saves, syncs, or upgrades a
model. Extraction from an open Revit document lives in ``bim_inspector.dynamo``
and is run manually by the user.
"""
from __future__ import annotations

__version__ = "0.1.0"
SCHEMA_VERSION = 1

CHECK_ORDER = (
    "required_parameters",
    "open_connectors",
    "elevation",
    "duplicates",
)

CHECK_LABELS = {
    "required_parameters": "必要參數",
    "open_connectors": "未連接端點",
    "elevation": "高程",
    "duplicates": "重複",
}
