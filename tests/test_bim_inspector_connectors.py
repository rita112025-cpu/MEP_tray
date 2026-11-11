"""Open-connector check."""
from bim_inspector.checks import run_checks
from bim_inspector.model import validate_rules, validate_snapshot

from tests.bim_inspector_support import element, rules, snapshot


def _tray(**overrides):
    base = dict(
        id="E-TRAY",
        category="Cable Trays",
        builtin_category="OST_CableTray",
        family="Tray-Sample",
        type="Tray-Type",
        location_mm={"x": 0, "y": 0, "z": 15},
        parameters={},
        connectors=[],
    )
    base.update(overrides)
    return element(**base)


def _connector(index, connected, domain="DomainCableTray"):
    return {"index": index, "domain": domain, "is_connected": connected, "origin_mm": {"x": 0, "y": 0, "z": 0}}


def test_open_connector_is_a_finding_and_connected_is_not():
    tray = _tray(connectors=[_connector(0, True), _connector(1, False)])
    findings = run_checks(validate_snapshot(snapshot([tray])), validate_rules(rules()))
    assert len(findings) == 1
    assert findings[0].check == "open_connectors"
    assert findings[0].rule_id == "OPEN-END"
    assert findings[0].element_id == "E-TRAY"
    assert findings[0].issue == "未連接端點 index=1 domain=DomainCableTray"
    assert findings[0].category == "Cable Trays"
    assert findings[0].family == "Tray-Sample"


def test_unknown_connection_and_other_category_are_not_findings():
    tray = _tray(connectors=[_connector(0, None)])
    equipment = element(connectors=[_connector(0, False)])
    findings = run_checks(validate_snapshot(snapshot([tray, equipment])), validate_rules(rules()))
    assert findings == []


def test_each_open_end_is_reported():
    tray = _tray(connectors=[_connector(0, False, ""), _connector(2, False, "DomainHvac")])
    findings = run_checks(validate_snapshot(snapshot([tray])), validate_rules(rules()))
    assert [item.issue for item in findings] == [
        "未連接端點 index=0 domain=未提供",
        "未連接端點 index=2 domain=DomainHvac",
    ]


def test_missing_connector_list_follows_the_explicit_flag():
    rule = rules()
    tray = _tray()
    quiet = run_checks(validate_snapshot(snapshot([tray])), validate_rules(rule))
    assert quiet == []
    rule["checks"]["open_connectors"]["flag_when_no_connectors"] = True
    flagged = run_checks(validate_snapshot(snapshot([tray])), validate_rules(rule))
    assert [item.issue for item in flagged] == ["沒有端點資料"]


def test_disabled_connector_check_emits_nothing():
    rule = rules()
    rule["checks"]["open_connectors"]["enabled"] = False
    tray = _tray(connectors=[_connector(0, False)])
    assert run_checks(validate_snapshot(snapshot([tray])), validate_rules(rule)) == []
