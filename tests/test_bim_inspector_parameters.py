"""Required-parameter check."""
from bim_inspector.checks import run_checks
from bim_inspector.model import validate_rules, validate_snapshot

from tests.bim_inspector_support import element, rules, snapshot


def test_compliant_element_has_no_findings():
    findings = run_checks(validate_snapshot(snapshot([element()])), validate_rules(rules()))
    assert findings == []


def test_missing_parameter_is_a_finding():
    params = {"SCADA_POINT": "P-1"}
    findings = run_checks(validate_snapshot(snapshot([element(parameters=params)])), validate_rules(rules()))
    assert [(item.element_id, item.issue) for item in findings] == [("E1", "缺少必要參數 SCADA_TAG")]


def test_empty_and_whitespace_values_are_findings():
    rule = rules()
    empty = element(id="E-EMPTY", parameters={"SCADA_TAG": "T-1", "SCADA_POINT": ""})
    blank = element(id="E-BLANK", parameters={"SCADA_TAG": "T-1", "SCADA_POINT": " \t "},
                    location_mm={"x": 10, "y": 0, "z": 5})
    findings = run_checks(validate_snapshot(snapshot([empty, blank])), validate_rules(rule))
    assert [item.element_id for item in findings] == ["E-BLANK", "E-EMPTY"]
    assert all(item.issue == "參數 SCADA_POINT 值為空" for item in findings)


def test_empty_value_is_kept_when_the_rule_says_so():
    rule = rules()
    rule["checks"]["required_parameters"]["rules"][0]["empty_is_missing"] = False
    present = element(parameters={"SCADA_TAG": "T-1", "SCADA_POINT": "  "})
    missing = element(id="E-MISS", parameters={"SCADA_POINT": "P-1"}, location_mm={"x": 8, "y": 0, "z": 5})
    findings = run_checks(validate_snapshot(snapshot([present, missing])), validate_rules(rule))
    assert [item.element_id for item in findings] == ["E-MISS"]
    assert findings[0].issue == "缺少必要參數 SCADA_TAG"


def test_missing_prefix_and_empty_prefixed_parameter():
    rule = rules()
    none = element(id="E-NONE", parameters={})
    extra = element(
        id="E-EXTRA",
        parameters={"SCADA_TAG": "T-1", "SCADA_POINT": "P-1", "SCADA_STATUS": "  "},
        location_mm={"x": 4, "y": 0, "z": 5},
    )
    findings = run_checks(validate_snapshot(snapshot([none, extra])), validate_rules(rule))
    issues = {(item.element_id, item.issue) for item in findings}
    assert ("E-NONE", "缺少必要參數 SCADA_TAG") in issues
    assert ("E-NONE", "缺少必要參數 SCADA_POINT") in issues
    assert ("E-NONE", "找不到前綴 SCADA_ 的參數") in issues
    assert ("E-EXTRA", "參數 SCADA_STATUS 值為空") in issues
    assert all(item.rule_id == "EQ-SCADA" and item.check == "required_parameters" for item in findings)


def test_category_and_family_filters_skip_other_elements():
    rule = rules()
    rule["checks"]["required_parameters"]["rules"][0]["families"] = ["AHU-Sample"]
    other_category = element(
        id="E-PANEL",
        category="Electrical Equipment",
        builtin_category="OST_ElectricalEquipment",
        family="Panel-Sample",
        parameters={},
        location_mm={"x": 3, "y": 0, "z": 5},
    )
    other_family = element(id="E-PUMP", family="Pump-Sample", parameters={}, location_mm={"x": 6, "y": 0, "z": 5})
    findings = run_checks(validate_snapshot(snapshot([other_category, other_family, element()])), validate_rules(rule))
    assert findings == []


def test_disabled_parameter_check_emits_nothing():
    rule = rules()
    rule["checks"]["required_parameters"]["enabled"] = False
    findings = run_checks(validate_snapshot(snapshot([element(parameters={})])), validate_rules(rule))
    assert findings == []
