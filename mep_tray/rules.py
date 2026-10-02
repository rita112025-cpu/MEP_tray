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
    direction: str                 # 必填：min=值為下限(越大越嚴) | max=值為上限(越小越嚴)
    all_values: dict = field(default_factory=dict)  # {code: value}


UNIT_SUFFIX = {"_mm": "mm", "_m": "m"}
UNITS = {"ratio", "m", "mm", "xOD"}


def validate_rules(rules: dict) -> dict:
    """結構驗證：缺欄位、方向缺漏、單位不符、未知參數鍵、非數值，一律拋 ValueError。"""
    errs: list[str] = []
    params, codes = rules.get("params"), rules.get("codes")
    if not isinstance(params, dict) or not isinstance(codes, dict) or not codes:
        raise ValueError("rules 需含 params 與 codes")
    for k, m in params.items():
        if m.get("direction") not in ("min", "max"):
            errs.append(f"param {k}: direction 必須為 min|max")
        if m.get("unit") not in UNITS:
            errs.append(f"param {k}: unit 未知 {m.get('unit')!r}")
        for suf, u in UNIT_SUFFIX.items():
            if k.endswith(suf) and m.get("unit") != u:
                errs.append(f"param {k}: 單位應為 {u}（鍵名後綴）")
        if "label" not in m:
            errs.append(f"param {k}: 缺 label")
    for c, info in codes.items():
        for f in ("name", "values", "clause", "verified"):
            if f not in info:
                errs.append(f"code {c}: 缺欄位 {f}")
        if not isinstance(info.get("verified"), bool):
            errs.append(f"code {c}: verified 必須為布林")
        for k, v in info.get("values", {}).items():
            if k not in params:
                errs.append(f"code {c}: 未知參數 {k}")
            elif v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                errs.append(f"code {c}.{k}: 需為非負數或 null")
    if errs:
        raise ValueError("rules 驗證失敗:\n  " + "\n  ".join(errs))
    return rules


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return validate_rules(json.load(f))


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
                             info["clause"], info["verified"], meta["direction"], vals)
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


# 穩定的 ASCII 狀態代碼（供報告/DXF/Revit 等下游使用）；中文僅為顯示標籤，改文案不影響下游
PASS, FAIL, UNVERIFIED, CLASH = "PASS", "FAIL", "UNVERIFIED", "CLASH"
LABELS = {PASS: "符合", FAIL: "不符合", UNVERIFIED: "規範值未驗證", CLASH: "衝突"}


@dataclass
class Check:
    key: str
    label: str
    unit: str
    actual: float
    required: float
    status: str            # PASS | FAIL | UNVERIFIED
    indicative: str        # 與規範值直接比較的結果（PASS/FAIL），僅供參考
    code: str              # 決定該要求的規範
    clause: str
    verified: bool


def evaluate(gov: Governing, actual: float) -> Check:
    """三態判定：規範值 verified=false 時一律回「規範值未驗證」，不得當作通過。"""
    ok = actual >= gov.value - 1e-9 if gov.direction == "min" else actual <= gov.value + 1e-9
    ind = PASS if ok else FAIL
    status = ind if gov.verified else UNVERIFIED
    return Check(gov.key, gov.label, gov.unit, actual, gov.value, status, ind,
                 gov.code, gov.clause, gov.verified)
