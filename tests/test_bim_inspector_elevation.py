"""Elevation offset and reference-level consistency."""
from bim_inspector.checks import run_checks
from bim_inspector.model import validate_rules, validate_snapshot

from tests.bim_inspector_support import element, levels, rules, snapshot


def _duct(z, eid="E-DUCT", level_id="L1", level_name="L-A", location=True):
    return element(
        id=eid,
        category="Ducts",
        builtin_category="OST_DuctCurves",
        family="Duct-Sample",
        type="Duct-Type",
        level_id=level_id,
        level_name=level_name,
        location_mm={"x": 0, "y": 0, "z": z} if location else None,
        parameters={},
    )


def test_offset_inside_inclusive_bounds_is_clean():
    ducts = [_duct(10, "E-LO"), _duct(15, "E-MID"), _duct(20, "E-HI")]
    findings = run_checks(validate_snapshot(snapshot(ducts)), validate_rules(rules()))
    assert findings == []


def test_offset_outside_bounds_uses_only_the_rule_numbers():
    findings = run_checks(
        validate_snapshot(snapshot([_duct(9, "E-LOW"), _duct(21, "E-HIGH")])),
        validate_rules(rules()),
    )
    assert [item.element_id for item in findings] == ["E-HIGH", "E-LOW"]
    assert findings[0].rule_id == "ELEV-DUCT"
    assert "超出允許範圍 [10, 20] mm" in findings[0].issue
    assert "樓層 L-A" in findings[1].issue


def test_offset_rule_ignores_other_levels_and_categories():
    other_level = _duct(0, "E-OTHER", level_id="L2", level_name="L-B")
    equipment = element(location_mm={"x": 0, "y": 0, "z": 0})
    findings = run_checks(validate_snapshot(snapshot([other_level, equipment])), validate_rules(rules()))
    assert findings == []


def test_missing_geometry_is_insufficient_data_not_a_guessed_offset():
    findings = run_checks(
        validate_snapshot(snapshot([_duct(0, location=False)])),
        validate_rules(rules()),
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "ELEV-DUCT"
    assert findings[0].issue.startswith("資料不足")


def test_reference_level_farther_than_the_configured_margin_is_a_finding():
    # Assigned level is 0. Another level is at 100. Margin is 5.
    flagged = element(id="E-FAR", location_mm={"x": 0, "y": 0, "z": 100})
    boundary = element(id="E-EDGE", location_mm={"x": 5, "y": 0, "z": 10})
    findings = run_checks(validate_snapshot(snapshot([flagged, boundary, element()])), validate_rules(rules()))
    assert [item.element_id for item in findings] == ["E-FAR"]
    assert "較近樓層為 L-B" in findings[0].issue
    assert "設定邊界 5 mm" in findings[0].issue


def test_level_consistency_needs_two_levels_and_explicit_margin():
    rule = rules()
    far = element(id="E-FAR", location_mm={"x": 0, "y": 0, "z": 100})
    only_one = run_checks(
        validate_snapshot(snapshot([far], level_rows=levels()[:1])),
        validate_rules(rule),
    )
    assert only_one == []
    rule["checks"]["elevation"]["level_consistency"]["enabled"] = False
    disabled = run_checks(validate_snapshot(snapshot([far])), validate_rules(rule))
    assert disabled == []


def test_level_consistency_reports_missing_level_instead_of_picking_one():
    orphan = element(id="E-ORPHAN", level_id="L-MISSING", level_name="")
    findings = run_checks(validate_snapshot(snapshot([orphan])), validate_rules(rules()))
    assert len(findings) == 1
    assert findings[0].rule_id == "ELEV-LEVEL"
    assert findings[0].issue.startswith("資料不足")


def test_disabled_elevation_check_emits_nothing():
    rule = rules()
    rule["checks"]["elevation"]["enabled"] = False
    far = element(location_mm={"x": 0, "y": 0, "z": 100})
    low = _duct(0)
    assert run_checks(validate_snapshot(snapshot([far, low])), validate_rules(rule)) == []
