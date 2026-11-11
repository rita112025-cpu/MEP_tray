"""Duplicate location, duplicate family names, and count ranges."""
from bim_inspector.checks import run_checks
from bim_inspector.model import validate_rules, validate_snapshot

from tests.bim_inspector_support import element, rules, snapshot


def _at(eid, x, y, z, family="Pump-Sample", type_name="Pump-Type"):
    return element(
        id=eid,
        family=family,
        type=type_name,
        location_mm={"x": x, "y": y, "z": z},
        parameters={"SCADA_TAG": "T", "SCADA_POINT": "P"},
    )


def test_same_family_and_type_within_tolerance_are_duplicates():
    pair = [_at("E-A", 0, 0, 0), _at("E-B", 1, 0, 0)]
    findings = run_checks(validate_snapshot(snapshot(pair)), validate_rules(rules()))
    assert sorted(item.element_id for item in findings) == ["E-A", "E-B"]
    assert {item.rule_id for item in findings} == {"DUP-LOCATION"}
    assert "同群元件：E-A, E-B" in findings[0].issue
    assert "容差 1 mm" in findings[0].issue


def test_points_outside_tolerance_or_different_type_are_kept():
    apart = [_at("E-A", 0, 0, 0), _at("E-B", 1.01, 0, 0)]
    other_type = [_at("E-C", 5, 0, 0, type_name="Other-Type"), _at("E-D", 5, 0, 0)]
    # E-D shares the location of E-C but the type differs, so it is not a pair.
    # E-D is alone in its type group.
    findings = run_checks(validate_snapshot(snapshot(apart + other_type)), validate_rules(rules()))
    assert findings == []


def test_vertical_distance_counts():
    stacked = [_at("E-A", 0, 0, 0), _at("E-B", 0, 0, 1.5)]
    findings = run_checks(validate_snapshot(snapshot(stacked)), validate_rules(rules()))
    assert findings == []


def test_family_suffixes_are_grouped_only_when_another_name_shares_the_base():
    names = [
        _at("E-P", 0, 0, 0, family="Pump"),
        _at("E-P2", 3, 0, 0, family="Pump (2)"),
        _at("E-P1", 6, 0, 0, family="Pump1"),
        _at("E-LONE", 9, 0, 0, family="Fan1"),
    ]
    findings = run_checks(validate_snapshot(snapshot(names)), validate_rules(rules()))
    family_findings = [item for item in findings if item.rule_id == "DUP-FAMILY-NAME"]
    assert sorted(item.family for item in family_findings) == ["Pump (2)", "Pump1"]
    assert all(item.element_id == "" for item in family_findings)
    assert all("基準名 Pump" in item.issue for item in family_findings)
    assert not any(item.family == "Fan1" for item in findings)


def test_count_below_and_above_the_stated_range():
    rule = rules()
    rule["checks"]["duplicates"]["count_ranges"][0]["min_count"] = 2
    rule["checks"]["duplicates"]["count_ranges"][0]["max_count"] = 3
    one = run_checks(validate_snapshot(snapshot([element()])), validate_rules(rule))
    assert len(one) == 1
    assert one[0].rule_id == "COUNT-AHU"
    assert one[0].element_id == ""
    assert one[0].issue.startswith("數量 1 超出允許範圍 [2, 3]")
    assert "族 AHU-Sample" in one[0].issue

    four = [element(id=f"E{i}", location_mm={"x": i * 10, "y": 0, "z": 5}) for i in range(4)]
    high = run_checks(validate_snapshot(snapshot(four)), validate_rules(rule))
    assert [item.issue for item in high] == ["數量 4 超出允許範圍 [2, 3]（內建品類 OST_MechanicalEquipment；族 AHU-Sample）"]


def test_count_on_the_boundary_is_clean_and_disabled_duplicates_emit_nothing():
    rule = rules()
    rule["checks"]["duplicates"]["count_ranges"][0]["min_count"] = 1
    rule["checks"]["duplicates"]["count_ranges"][0]["max_count"] = 1
    assert run_checks(validate_snapshot(snapshot([element()])), validate_rules(rule)) == []
    rule["checks"]["duplicates"]["enabled"] = False
    pair = [_at("E-A", 0, 0, 0), _at("E-B", 0, 0, 0)]
    assert run_checks(validate_snapshot(snapshot(pair)), validate_rules(rule)) == []
