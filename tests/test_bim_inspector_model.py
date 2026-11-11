"""Rule and snapshot schema."""
import json

import pytest

from bim_inspector.jsonutil import parse_json
from bim_inspector.model import validate_rules, validate_snapshot

from tests.bim_inspector_support import FIXTURES, element, rules, snapshot


def test_sample_fixtures_validate_and_stay_deidentified():
    for name in ("sample_rules.json", "sample_snapshot.json", "sample_snapshot_clean.json"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "sample" in lowered
        for banned in (".rvt", ".dwg", "c:\\", "/users/", "/home/", "workshare"):
            assert banned not in lowered, (name, banned)
    validate_rules(json.loads((FIXTURES / "sample_rules.json").read_text(encoding="utf-8")))
    validate_snapshot(json.loads((FIXTURES / "sample_snapshot.json").read_text(encoding="utf-8")))
    validate_snapshot(json.loads((FIXTURES / "sample_snapshot_clean.json").read_text(encoding="utf-8")))


def test_duplicate_keys_and_non_finite_numbers_are_rejected():
    with pytest.raises(ValueError, match="duplicate key"):
        parse_json('{"a": 1, "a": 2}')
    with pytest.raises(ValueError, match="non-finite"):
        parse_json('{"a": NaN}')


def test_rules_reject_guessable_or_mistyped_thresholds():
    missing_margin = rules()
    del missing_margin["checks"]["elevation"]["level_consistency"]["margin_mm"]
    with pytest.raises(ValueError, match="margin_mm"):
        validate_rules(missing_margin)

    backwards = rules()
    backwards["checks"]["elevation"]["offset_rules"][0]["min_offset_mm"] = 30
    backwards["checks"]["elevation"]["offset_rules"][0]["max_offset_mm"] = 10
    with pytest.raises(ValueError, match="min_offset_mm"):
        validate_rules(backwards)

    bad_category = rules()
    bad_category["checks"]["open_connectors"]["builtin_categories"] = ["Cable Tray"]
    with pytest.raises(ValueError, match="OST_"):
        validate_rules(bad_category)

    wrong_schema = rules()
    wrong_schema["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        validate_rules(wrong_schema)


def test_snapshot_rejects_duplicate_ids_and_non_mm_units():
    data = snapshot([element(), element(id="E1")])
    with pytest.raises(ValueError, match="duplicate element id"):
        validate_snapshot(data)
    other = snapshot([element()])
    other["source"]["units"] = "ft"
    with pytest.raises(ValueError, match="mm"):
        validate_snapshot(other)


def test_checker_has_no_hardcoded_project_dimensions():
    text = (FIXTURES.parents[0] / "checks.py").read_text(encoding="utf-8")
    for literal in ("2400", "4500", "304.8", "margin_mm =", "min_offset_mm ="):
        assert literal not in text
