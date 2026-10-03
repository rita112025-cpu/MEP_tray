"""MRT_APPX_C（業主附錄C）：出處 metadata、heat_bare、強弱電 500、verified 保持 false。"""
import copy

import pytest

from mep_tray import versioning as V
from mep_tray import webui as W
from mep_tray.clash import check_route
from mep_tray.geometry import Box, segment_box
from mep_tray.model import OBSTACLE_KINDS, Inputs
from mep_tray.report import _sources_text
from mep_tray.router import Obstacle, clearance_m, route_tray, rule_key
from mep_tray.rules import SOURCE_STRENGTHS, load_rules, merge_strictest, validate_rules
from tests.test_pipeline import isolate  # noqa: F401  (autouse fixture)

MRT = "MRT_APPX_C"
RULES = load_rules()
M = RULES["codes"][MRT]
OTHERS = [c for c in RULES["codes"] if c != MRT]


# ───────────── M1：sources ─────────────
def test_every_non_null_mrt_value_has_complete_sources():
    nonnull = {k for k, v in M["values"].items() if v is not None}
    assert nonnull and set(M["sources"]) == nonnull
    for k, lst in M["sources"].items():
        assert lst, k
        for s in lst:
            assert s["rule_id"].startswith("C-") and len(s["rule_id"]) == 5
            assert isinstance(s["pdf_page"], int) and s["pdf_page"] > 0
            assert s["appendix_page"].strip() and s["strength"] in SOURCE_STRENGTHS and isinstance(s["note"], str)


def test_specific_source_facts_from_the_owner_requirements():
    src = M["sources"]
    assert (src["fitting_radius_mm"][0]["rule_id"], src["fitting_radius_mm"][0]["strength"]) == ("C-050", "SHOULD")
    assert [s["rule_id"] for s in src["sep_power_signal_mm"]] == ["C-048", "C-049"]
    assert (src["clear_heat_bare_mm"][0]["rule_id"], src["clear_heat_bare_mm"][0]["pdf_page"]) == ("C-041", 8)
    assert (src["clear_duct_mm"][0]["pdf_page"], src["clear_duct_mm"][0]["appendix_page"]) == (9, "C-8")
    assert (src["side_clear_mm"][0]["rule_id"], src["side_clear_mm"][0]["pdf_page"]) == ("C-010", 6)
    assert "Q-07" in src["structure_clear_mm"][0]["note"] and M["values"]["structure_clear_mm"] == 100
    assert M["values"]["clear_water_mm"] == 400 and M["values"]["headroom_mm"] == 300


def _mutated(fn):
    r = copy.deepcopy(RULES)
    fn(r["codes"][MRT])
    return r


@pytest.mark.parametrize("name,fn", [
    ("缺 rule_id", lambda c: c["sources"]["headroom_mm"][0].pop("rule_id")),
    ("rule_id 格式", lambda c: c["sources"]["headroom_mm"][0].update(rule_id="X-1")),
    ("pdf_page 為 0", lambda c: c["sources"]["headroom_mm"][0].update(pdf_page=0)),
    ("pdf_page 為布林", lambda c: c["sources"]["headroom_mm"][0].update(pdf_page=True)),
    ("appendix_page 空白", lambda c: c["sources"]["headroom_mm"][0].update(appendix_page=" ")),
    ("strength 未知", lambda c: c["sources"]["headroom_mm"][0].update(strength="MAY")),
    ("note 非字串", lambda c: c["sources"]["headroom_mm"][0].update(note=5)),
    ("未知參數", lambda c: c["sources"].update(nope=[dict(c["sources"]["headroom_mm"][0])])),
    ("空清單", lambda c: c["sources"].update(headroom_mm=[])),
    ("無值參數附出處", lambda c: c["sources"].update(fill_max=[dict(c["sources"]["headroom_mm"][0])])),
    ("sources 非物件", lambda c: c.update(sources=[])),
])
def test_validator_rejects_bad_sources(name, fn):
    with pytest.raises(ValueError):
        validate_rules(_mutated(fn))


def test_rules_without_sources_remain_valid_and_other_codes_have_none():
    r = copy.deepcopy(RULES)
    del r["codes"][MRT]["sources"]
    validate_rules(r)
    assert merge_strictest([MRT], r)["clear_heat_bare_mm"].sources == []
    assert all("sources" not in RULES["codes"][c] for c in OTHERS)


# ───────────── verified / 未填入的值 ─────────────
def test_mrt_is_never_verified_and_unconfirmed_values_stay_unset():
    assert M["verified"] is False
    assert all(not g.verified for g in merge_strictest([MRT]).values())
    for k in ("fill_max", "span_max_m", "bend_radius_factor"):
        assert M["values"][k] is None, k
    assert "gas" not in OBSTACLE_KINDS and "equipment" not in OBSTACLE_KINDS


# ───────────── M3：強弱電 500 ─────────────
def test_power_signal_separation_is_500_and_strictest_wins_over_cns():
    g = merge_strictest(["CNS", MRT])["sep_power_signal_mm"]
    assert g.value == 500 and g.code == MRT and g.all_values == {"CNS": 300, MRT: 500}
    assert [s["rule_id"] for s in g.sources] == ["C-048", "C-049"]
    assert merge_strictest([MRT])["sep_power_signal_mm"].value == 500
    assert merge_strictest(["CNS"])["sep_power_signal_mm"].value == 300


# ───────────── M2：heat_bare ─────────────
def test_heat_bare_is_an_accepted_kind_in_the_web_validation_and_unknown_kinds_still_fail():
    assert "heat_bare" in OBSTACLE_KINDS and "heat" in OBSTACLE_KINDS
    body = {"room": {"x": 12, "y": 6, "z": 4}, "tray": {"width_mm": 300, "height_mm": 100, "kind": "power"},
            "start": {"x": 1, "y": 1, "z": 3}, "ends": [{"x": 10, "y": 1, "z": 3}], "codes": [MRT],
            "obstacles": [{"name": "h", "kind": "heat_bare", "lo": [5, 0, 0], "hi": [5.5, 4, 2]}]}
    inp, _ = W.validate_request(body)
    assert inp.obstacles[0]["kind"] == "heat_bare"
    body["obstacles"][0]["kind"] = "gas"
    with pytest.raises(W.ApiError) as ei:
        W.validate_request(body)
    assert ei.value.code == "invalid_choice"


def test_heat_bare_clearance_is_1000_with_mrt_and_falls_back_to_clear_heat_without():
    gm = merge_strictest([MRT])
    assert rule_key("heat_bare", "power", gm) == "clear_heat_bare_mm" and clearance_m("heat_bare", "power", gm) == 1.0
    assert clearance_m("heat", "power", gm) == 0.5                                   # 既有 heat 語意不變
    for codes in (["CNS"], ["IEC", "NEC"], ["TW_BUILDING"], OTHERS):
        g = merge_strictest(codes)
        assert "clear_heat_bare_mm" not in g
        assert rule_key("heat_bare", "power", g) == "clear_heat_mm"
        assert clearance_m("heat_bare", "power", g) == clearance_m("heat", "power", g)


def _inp(kind, codes):
    return Inputs(room=((0, 0, 0), (12, 6, 4)), start=(1, 1, 3), ends=[(10, 1, 3)], cell_m=0.25, codes=list(codes),
                  obstacles=[{"name": "pipe", "kind": kind, "lo": [5, 0, 0], "hi": [5.5, 4, 2.0]}])


def _scene(kind, codes):
    gov = merge_strictest(codes)
    obs = [Obstacle("pipe", kind, Box((5, 0, 0), (5.5, 4, 2.0)))]
    route = route_tray(Box((0, 0, 0), (12, 6, 4)), (1, 1, 3), [(10, 1, 3)], obs, 0.3, 0.1, "power", gov, cell=0.25)
    gap = min(segment_box(a, b, 0.3, 0.1).gap(obs[0].box) for a, b in route.segments)
    return route, check_route(_inp(kind, codes), route, gov), gap


def _bad(rep):
    return [f for f in rep.findings if f.kind in ("clash", "clearance") and f.indicative in ("FAIL", "CLASH")]


def test_route_keeps_1000mm_from_bare_heat_but_only_500_from_insulated_heat():
    _, rep_bare, gap_bare = _scene("heat_bare", [MRT])
    assert gap_bare >= 1.0 - 1e-9, gap_bare
    assert not _bad(rep_bare)
    _, rep_heat, gap_heat = _scene("heat", [MRT])
    assert 0.5 - 1e-9 <= gap_heat < 1.0                    # 同場景一般熱源只需 500；證明 heat_bare 確實更嚴
    assert not _bad(rep_heat)


def test_a_route_planned_for_heat_is_flagged_against_heat_bare_1000():
    route, _, _ = _scene("heat", [MRT])
    rep = check_route(_inp("heat_bare", [MRT]), route, merge_strictest([MRT]))
    fs = [f for f in rep.findings if f.kind == "clearance"]
    assert fs and fs[0].required == 1000 and fs[0].indicative == "FAIL"


# ───────────── 報告與快照帶出處 ─────────────
def test_report_text_keeps_every_source_and_snapshot_keeps_provenance():
    g = merge_strictest([MRT])
    t = _sources_text(g["sep_power_signal_mm"])
    assert "C-048" in t and "C-049" in t and "PDF p.8" in t
    assert "C-041 PDF p.8" in _sources_text(g["clear_heat_bare_mm"])
    assert _sources_text(merge_strictest(["CNS"])["headroom_mm"]) == "—"
    snap = V.gov_snapshot(g)
    assert snap["clear_heat_bare_mm"]["sources"] == [
        {"rule_id": "C-041", "pdf_page": 8, "appendix_page": "C-7", "strength": "MUST"}]
    assert [s["rule_id"] for s in snap["sep_power_signal_mm"]["sources"]] == ["C-048", "C-049"]
    assert snap["fitting_radius_mm"]["sources"][0]["strength"] == "SHOULD"


def test_pipeline_report_and_manifest_snapshot_carry_the_provenance(isolate):
    from mep_tray import pipeline as P
    inp = _inp("heat_bare", [MRT])
    r = P.run(inp, [MRT], "prov1", make_dwg=False)
    assert r.ok, r.error
    snap = r.manifest["rules"]["snapshot"]
    assert snap["clear_heat_bare_mm"]["value"] == 1000
    assert snap["clear_heat_bare_mm"]["sources"][0]["rule_id"] == "C-041"
    html = open(r.files["report"], encoding="utf-8").read()
    assert "C-041 PDF p.8" in html and "C-048" in html and "C-049" in html
