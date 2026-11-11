"""Static scan for model-writing names in the Dynamo extractor."""
from __future__ import annotations

import ast

# Names that would modify, save, sync, or upgrade a model, or run arbitrary code.
FORBIDDEN_NAMES = frozenset({
    "Transaction",
    "TransactionGroup",
    "SubTransaction",
    "TransactionManager",
    "EnsureInTransaction",
    "ForceCloseTransaction",
    "Save",
    "SaveAs",
    "SaveCloudModel",
    "SynchronizeWithCentral",
    "Relinquish",
    "Upgrade",
    "Set",
    "SetValueString",
    "Delete",
    "NewFamilyInstance",
    "MoveElement",
    "RotateElement",
    "MirrorElements",
    "eval",
    "exec",
})


def scan_source(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                problems.extend(_dotted(alias.name))
        elif isinstance(node, ast.ImportFrom):
            problems.extend(_dotted(node.module or ""))
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    problems.append(f"import {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            problems.append(f"attribute {node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"name {node.id}")
    return problems


def _dotted(module: str) -> list[str]:
    return [f"import {part}" for part in module.split(".") if part in FORBIDDEN_NAMES]
