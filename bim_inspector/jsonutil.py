"""Strict JSON loading. Duplicate keys and NaN/Infinity are rejected."""
from __future__ import annotations

import json
from pathlib import Path


def _reject_constant(name: str):
    raise ValueError(f"non-finite number: {name}")


def _pairs(pairs: list[tuple]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key: {key}")
        obj[key] = value
    return obj


def parse_json(text: str) -> dict:
    try:
        data = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def load_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read JSON: {exc.strerror or exc.__class__.__name__}") from exc
    return parse_json(text)
