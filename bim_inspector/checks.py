"""Four read-only checks. Every threshold comes from the rule file."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import CHECK_ORDER

_DISTANCE_EPSILON_MM = 1e-6


@dataclass(frozen=True)
class Finding:
    check: str
    rule_id: str
    element_id: str
    category: str
    builtin_category: str
    family: str
    type_name: str
    level: str
    issue: str

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "rule_id": self.rule_id,
            "element_id": self.element_id,
            "category": self.category,
            "builtin_category": self.builtin_category,
            "family": self.family,
            "type": self.type_name,
            "level": self.level,
            "issue": self.issue,
        }


def run_checks(snapshot: dict, rules: dict) -> list[Finding]:
    """Return findings in a stable order. ``snapshot`` and ``rules`` must already be validated."""
    findings: list[Finding] = []
    checks = rules["checks"]
    if checks["required_parameters"]["enabled"]:
        findings.extend(_parameters(snapshot, checks["required_parameters"]))
    if checks["open_connectors"]["enabled"]:
        findings.extend(_connectors(snapshot, checks["open_connectors"]))
    if checks["elevation"]["enabled"]:
        findings.extend(_elevation(snapshot, checks["elevation"]))
    if checks["duplicates"]["enabled"]:
        findings.extend(_duplicates(snapshot, checks["duplicates"]))
    order = {name: index for index, name in enumerate(CHECK_ORDER)}
    findings.sort(key=lambda item: (order[item.check], item.rule_id, item.element_id, item.issue))
    return findings


def _parameters(snapshot: dict, block: dict) -> list[Finding]:
    found: list[Finding] = []
    for rule in block["rules"]:
        prefix = rule["parameter_prefix"]
        explicit = set(rule["parameters"])
        for element in snapshot["elements"]:
            if not _matches(element, rule):
                continue
            params = element["parameters"]
            for name in rule["parameters"]:
                if name not in params:
                    found.append(_finding(element, "required_parameters", rule["id"], f"缺少必要參數 {name}"))
                elif rule["empty_is_missing"] and _is_empty(params[name]):
                    found.append(_finding(element, "required_parameters", rule["id"], f"參數 {name} 值為空"))
            if prefix is None:
                continue
            prefixed = [name for name in params if name.startswith(prefix)]
            if not prefixed:
                found.append(_finding(element, "required_parameters", rule["id"], f"找不到前綴 {prefix} 的參數"))
                continue
            if not rule["empty_is_missing"]:
                continue
            for name in sorted(prefixed):
                if name in explicit:
                    continue
                if _is_empty(params[name]):
                    found.append(_finding(element, "required_parameters", rule["id"], f"參數 {name} 值為空"))
    return found


def _connectors(snapshot: dict, block: dict) -> list[Finding]:
    found: list[Finding] = []
    scope = {"builtin_categories": block["builtin_categories"], "categories": block["categories"], "families": [], "types": []}
    for element in snapshot["elements"]:
        if not _matches(element, scope):
            continue
        connectors = element["connectors"]
        if not connectors and block["flag_when_no_connectors"]:
            found.append(_finding(element, "open_connectors", block["id"], "沒有端點資料"))
            continue
        for connector in connectors:
            if connector["is_connected"] is False:
                domain = connector["domain"] or "未提供"
                issue = f"未連接端點 index={connector['index']} domain={domain}"
                found.append(_finding(element, "open_connectors", block["id"], issue))
    return found


def _elevation(snapshot: dict, block: dict) -> list[Finding]:
    levels_by_id = {level["id"]: level for level in snapshot["levels"]}
    found: list[Finding] = []
    for rule in block["offset_rules"]:
        for element in snapshot["elements"]:
            if not _matches(element, rule):
                continue
            if not _level_selected(element, rule):
                continue
            offset = _offset_mm(element, levels_by_id)
            if offset is None:
                found.append(_finding(element, "elevation", rule["id"], "資料不足，無法判斷高程：缺少元件高程或樓層高程"))
                continue
            lo, hi = float(rule["min_offset_mm"]), float(rule["max_offset_mm"])
            if offset < lo or offset > hi:
                level = element["level_name"] or element["level_id"] or "未提供"
                issue = f"偏移 {_fmt(offset)} mm 超出允許範圍 [{_fmt(lo)}, {_fmt(hi)}] mm（樓層 {level}）"
                found.append(_finding(element, "elevation", rule["id"], issue))
    consistency = block["level_consistency"]
    if consistency["enabled"]:
        found.extend(_level_consistency(snapshot, consistency, levels_by_id))
    return found


def _level_consistency(snapshot: dict, rule: dict, levels_by_id: dict) -> list[Finding]:
    levels = snapshot["levels"]
    found: list[Finding] = []
    margin = float(rule["margin_mm"])
    for element in snapshot["elements"]:
        if not _matches(element, rule):
            continue
        point = element["location_mm"]
        assigned = levels_by_id.get(element["level_id"])
        if point is None or assigned is None:
            found.append(_finding(element, "elevation", rule["id"], "資料不足，無法判斷高程：缺少元件高程或參照樓層"))
            continue
        if len(levels) < 2:
            continue
        z = float(point["z"])
        assigned_distance = abs(z - float(assigned["elevation_mm"]))
        others = [level for level in levels if level["id"] != assigned["id"]]
        nearest = min(others, key=lambda level: abs(z - float(level["elevation_mm"])))
        nearest_distance = abs(z - float(nearest["elevation_mm"]))
        if nearest_distance + margin < assigned_distance:
            issue = (
                f"參照樓層 {assigned['name']}（高程 {_fmt(assigned['elevation_mm'])} mm）"
                f"與元件高程 {_fmt(z)} mm 不一致；較近樓層為 {nearest['name']}"
                f"（高程 {_fmt(nearest['elevation_mm'])} mm），差距超過設定邊界 {_fmt(margin)} mm"
            )
            found.append(_finding(element, "elevation", rule["id"], issue))
    return found


def _duplicates(snapshot: dict, block: dict) -> list[Finding]:
    found: list[Finding] = []
    if block["same_location"]["enabled"]:
        found.extend(_same_location(snapshot, block["same_location"]))
    if block["family_names"]["enabled"]:
        found.extend(_family_names(snapshot, block["family_names"]))
    found.extend(_counts(snapshot, block["count_ranges"]))
    return found


def _same_location(snapshot: dict, rule: dict) -> list[Finding]:
    groups: dict[tuple, list[dict]] = {}
    for element in snapshot["elements"]:
        if not _matches(element, rule):
            continue
        point = element["location_mm"]
        if point is None:
            continue
        key = (element["builtin_category"], element["category"], element["family"], element["type"])
        groups.setdefault(key, []).append(element)
    tolerance = float(rule["tolerance_mm"])
    found: list[Finding] = []
    for elements in groups.values():
        clusters = _clusters(elements, tolerance)
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            ids = ", ".join(sorted(element["id"] for element in cluster))
            issue = f"族與類型在相同位置重複（容差 {_fmt(tolerance)} mm），同群元件：{ids}"
            for element in cluster:
                found.append(_finding(element, "duplicates", rule["id"], issue))
    return found


def _clusters(elements: list[dict], tolerance: float) -> list[list[dict]]:
    parent = list(range(len(elements)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(len(elements)):
        for right in range(left + 1, len(elements)):
            if _distance(elements[left]["location_mm"], elements[right]["location_mm"]) <= tolerance + _DISTANCE_EPSILON_MM:
                union(left, right)
    buckets: dict[int, list[dict]] = {}
    for index, element in enumerate(elements):
        buckets.setdefault(find(index), []).append(element)
    return list(buckets.values())


def _family_names(snapshot: dict, rule: dict) -> list[Finding]:
    patterns = [re.compile(pattern) for pattern in rule["suffix_patterns"]]
    names = sorted({element["family"] for element in snapshot["elements"] if element["family"]})
    groups: dict[str, set[str]] = {}
    for name in names:
        groups.setdefault(_strip_suffixes(name, patterns), set()).add(name)
    found: list[Finding] = []
    for base, group in groups.items():
        if len(group) < 2:
            continue
        primary = base if base in group else None
        for name in sorted(group):
            if name == primary:
                continue
            others = ", ".join(sorted(item for item in group if item != name))
            issue = f"族名稱疑似重複：{name} 與 {others} 共用基準名 {base}"
            found.append(Finding("duplicates", rule["id"], "", "", "", name, "", "", issue))
    return found


def _counts(snapshot: dict, rules: list[dict]) -> list[Finding]:
    found: list[Finding] = []
    for rule in rules:
        matched = [element for element in snapshot["elements"] if _count_match(element, rule)]
        count = len(matched)
        lo, hi = rule["min_count"], rule["max_count"]
        if lo <= count <= hi:
            continue
        issue = f"數量 {count} 超出允許範圍 [{lo}, {hi}]（{_scope(rule)}）"
        category = rule["categories"][0] if len(rule["categories"]) == 1 else ""
        builtin = rule["builtin_categories"][0] if len(rule["builtin_categories"]) == 1 else ""
        family = rule["family"] or ""
        type_name = rule["type"] or ""
        found.append(Finding("duplicates", rule["id"], "", category, builtin, family, type_name, "", issue))
    return found


def _count_match(element: dict, rule: dict) -> bool:
    if rule["builtin_categories"] and element["builtin_category"] not in rule["builtin_categories"]:
        return False
    if rule["categories"] and element["category"] not in rule["categories"]:
        return False
    if rule["family"] is not None and element["family"] != rule["family"]:
        return False
    if rule["type"] is not None and element["type"] != rule["type"]:
        return False
    return True


def _scope(rule: dict) -> str:
    parts: list[str] = []
    if rule["builtin_categories"]:
        parts.append("內建品類 " + ", ".join(rule["builtin_categories"]))
    if rule["categories"]:
        parts.append("品類 " + ", ".join(rule["categories"]))
    if rule["family"] is not None:
        parts.append("族 " + rule["family"])
    if rule["type"] is not None:
        parts.append("類型 " + rule["type"])
    return "；".join(parts) if parts else "全部元件"


def _strip_suffixes(name: str, patterns: list[re.Pattern[str]]) -> str:
    current = name
    for _ in range(3):
        nxt = _strip_once(current, patterns)
        if nxt == current:
            break
        current = nxt
    return current


def _strip_once(name: str, patterns: list[re.Pattern[str]]) -> str:
    for pattern in patterns:
        match = None
        for candidate in pattern.finditer(name):
            if candidate.end() == len(name):
                match = candidate
        if match is None or match.start() <= 0:
            continue
        stripped = name[:match.start()].rstrip()
        if stripped:
            return stripped
    return name


def _matches(element: dict, rule: dict) -> bool:
    if rule["builtin_categories"] and element["builtin_category"] not in rule["builtin_categories"]:
        return False
    if rule["categories"] and element["category"] not in rule["categories"]:
        return False
    if rule.get("families") and element["family"] not in rule["families"]:
        return False
    if rule.get("types") and element["type"] not in rule["types"]:
        return False
    return True


def _level_selected(element: dict, rule: dict) -> bool:
    if rule["level_ids"] and element["level_id"] not in rule["level_ids"]:
        return False
    if rule["level_names"] and element["level_name"] not in rule["level_names"]:
        return False
    return True


def _offset_mm(element: dict, levels_by_id: dict) -> float | None:
    point = element["location_mm"]
    level = levels_by_id.get(element["level_id"])
    if point is None or level is None:
        return None
    return float(point["z"]) - float(level["elevation_mm"])


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _distance(left: dict, right: dict) -> float:
    return math.dist((left["x"], left["y"], left["z"]), (right["x"], right["y"], right["z"]))


def _finding(element: dict, check: str, rule_id: str, issue: str) -> Finding:
    return Finding(
        check,
        rule_id,
        element["id"],
        element["category"],
        element["builtin_category"],
        element["family"],
        element["type"],
        element["level_name"],
        issue,
    )


def _fmt(value: float) -> str:
    rounded = round(float(value), 3) + 0.0
    return f"{rounded:g}"
