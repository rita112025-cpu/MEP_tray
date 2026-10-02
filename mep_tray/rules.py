"""規範參數載入與「最嚴格條件」合併。"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

RULES_PATH = Path(__file__).with_name("rules.json")
STANDARD_WIDTHS_MM = [100, 150, 200, 300, 400, 500, 600, 750, 900]


@dataclass
class Governing:
    key: str
    label: str
    unit: str
    value: float
    code: str                      # 決定該值的規範代號
    clause: str
    verified: bool
    all_values: dict = field(default_factory=dict)  # {code: value}


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge_strictest(selected: list[str], rules: dict | None = None) -> dict[str, Governing]:
    """依勾選的規範，逐參數取最嚴格值（min 類取最大，max 類取最小）。"""
    rules = rules or load_rules()
    unknown = [c for c in selected if c not in rules["codes"]]
    if unknown:
        raise ValueError(f"未知規範代號: {unknown}")
    if not selected:
        raise ValueError("至少需勾選一套規範")
    out: dict[str, Governing] = {}
    for key, meta in rules["params"].items():
        vals = {c: rules["codes"][c]["values"].get(key) for c in selected}
        vals = {c: v for c, v in vals.items() if v is not None}
        if not vals:
            continue
        pick = (min if meta["direction"] == "max" else max)(vals, key=vals.get)
        info = rules["codes"][pick]
        out[key] = Governing(key, meta["label"], meta["unit"], vals[pick], pick,
                             info["clause"], info["verified"], vals)
    return out


def fill_ratio(cables: list[dict], width_mm: float, height_mm: float) -> float:
    """cables: [{od_mm, count, ...}]；以截面積比計算填充率。"""
    area = sum(math.pi / 4 * c["od_mm"] ** 2 * c.get("count", 1) for c in cables)
    return area / (width_mm * height_mm)


def recommend_width(cables: list[dict], height_mm: float, fill_max: float) -> int | None:
    for w in STANDARD_WIDTHS_MM:
        if fill_ratio(cables, w, height_mm) <= fill_max:
            return w
    return None
