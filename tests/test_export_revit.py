import json
from pathlib import Path

import pytest

from mep_tray import export_revit as R
from mep_tray.clash import check_route
from mep_tray.compliance import check_compliance
from mep_tray.model import Inputs
from mep_tray.router import Route, place_hangers, route_tray
from mep_tray.rules import merge_strictest

GOV = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def out_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEP_OUTPUT_ROOT", str(tmp_path))


def routed(ends, obstacles=()):
    i = Inputs(start=(1, 1, 3), ends=ends, obstacles=list(obstacles))
    r = route_tray(i.room_box(), i.start, i.ends, i.obstacle_objs(), 0.3, 0.1, "power", GOV, cell=0.25)
    place_hangers(r, GOV["span_max_m"].value)
    return i, r


def model(ends, **kw):
    i, r = routed(ends)
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    return R.build_model(i, r, reps, GOV, "rv1", **kw), i, r


def test_tee_trunk_is_split_so_junction_is_shared_endpoint():
    m, i, r = model([(10, 1, 3), (6, 5, 3)], type_name="T")
    segs = m["segments"]
    assert len(r.segments) == 2 and len(segs) == 3                       # 主幹被切成兩段 + 支線
    tee = [j for j in m["joints"] if j["kind"] == "tee"]
    assert len(tee) == 1 and tee[0]["point"] == [6000.0, 1000.0, 3000.0]
    ends = {tuple(s["start"]) for s in segs} | {tuple(s["end"]) for s in segs}
    assert (6000.0, 1000.0, 3000.0) in ends
    inc = [s["id"] for s in segs if tuple(s["start"]) == (6000.0, 1000.0, 3000.0)
           or tuple(s["end"]) == (6000.0, 1000.0, 3000.0)]
    assert sorted(inc) == sorted(tee[0]["segments"]) and len(inc) == 3
    assert tee[0]["branch"] == tee[0]["segments"][2]                     # 支線排在最後（NewTeeFitting 參數順序）
    branch = next(s for s in segs if s["id"] == tee[0]["branch"])
    assert {branch["start"][0], branch["end"][0]} == {6000.0}            # 支線沿 Y，離開主幹


def test_elbow_joints_for_bends_and_no_joint_for_straight_run():
    m, *_ = model([(10, 5, 3)])
    assert [j["kind"] for j in m["joints"]] == ["elbow"] * len(m["joints"]) and m["joints"]
    s, *_ = model([(10, 1, 3)])
    assert s["joints"] == [] and len(s["segments"]) == 1


def test_cross_and_unsupported_classification():
    a = (5, 5, 3)
    cross = [((0, 5, 3), a), (a, (10, 5, 3)), ((5, 0, 3), a), (a, (5, 10, 3))]
    j = R.build_joints(cross)
    assert len(j) == 1 and j[0]["kind"] == "cross" and len(j[0]["segments"]) == 4
    # 三向且無共線（三軸各一）→ 無法支援，明確標示
    bad = R.build_joints([((0, 5, 3), a), (a, (5, 10, 3)), (a, (5, 5, 0))])
    assert bad[0]["kind"] == "unsupported"
    col = R.build_joints([((0, 5, 3), a), (a, (10, 5, 3))])
    assert col[0]["kind"] == "union"


def test_coordinates_are_exact_mm_and_origin_not_translated():
    i = Inputs(start=(1.1, 1, 3), ends=[(6.1, 1, 3)])
    r = Route([[(1.1, 1.0, 3.0), (6.1, 1.0, 3.0)]], [((1.1, 1.0, 3.0), (6.1, 1.0, 3.0))], [], 5.0)
    m = R.build_model(i, r, [], GOV, "x")
    assert m["segments"][0]["start"] == [1100.0, 1000.0, 3000.0]
    cs = m["coordinate_system"]
    assert cs["origin"] == [0.0, 0.0, 0.0] and cs["unit"] == "mm"
    assert cs["axis_x"] == [1.0, 0.0, 0.0] and cs["axis_z"] == [0.0, 0.0, 1.0] and cs["rotation_deg"] == 0.0


def test_cross_requires_opposite_legs_on_both_axes():
    center = (5, 5, 3)
    # 兩條 X 腿同向；每軸兩段並不足以構成十字接頭。
    segs = [(center, (6, 5, 3)), (center, (7, 5, 3)),
            (center, (5, 4, 3)), (center, (5, 6, 3))]
    joints = R.build_joints(segs)
    assert len(joints) == 1 and joints[0]["kind"] == "unsupported"


def test_basis_defaults_to_unspecified_and_is_validated():
    m, *_ = model([(10, 1, 3)])
    assert m["coordinate_system"]["basis"] == "UNSPECIFIED"
    for b in R.BASES[1:]:
        assert model([(10, 1, 3)], basis=b)[0]["coordinate_system"]["basis"] == b
    with pytest.raises(ValueError):
        model([(10, 1, 3)], basis="WORLD")


def test_findings_carry_segment_id_status_code_and_fix():
    i, r = routed([(10, 1, 3)])
    i.obstacles.append({"name": "W", "kind": "water", "lo": [4, 1.35, 2], "hi": [6, 1.6, 3.6]})
    reps = [check_route(i, r, GOV), check_compliance(i, r, GOV)]
    m = R.build_model(i, r, reps, GOV, "f1")
    f = [x for x in m["findings"] if x["kind"] == "clearance"][0]
    assert f["segment_id"] == "S001" and f["status"] == "UNVERIFIED" and f["fix"]["axis"] in "XYZ"


def test_disclosures_and_no_rfa_claim():
    m, *_ = model([(10, 1, 3)])
    text = "\n".join(m["disclosures"])
    assert "規範值" in text and "未於 Revit 驗證" in text and "未提供 .rfa" in text
    assert R.REVIT_STATUS in m["disclosures"] and "UNVERIFIED" in R.REVIT_STATUS
    assert "參數化族已交付" not in json.dumps(m, ensure_ascii=False)


def test_write_model_json_valid_no_nan_and_no_overwrite():
    m, *_ = model([(10, 1, 3), (6, 5, 3)])
    p = R.write_model(m, "rv1")
    back = json.loads(p.read_text(encoding="utf-8"))
    assert back == m and back["schema_version"] == 1
    with pytest.raises(FileExistsError):
        R.write_model(m, "rv1")
    R.write_model(m, "rv1", overwrite=True)


# ---- Comments 區段 ----
def test_comments_preserve_original_and_are_idempotent():
    old = "使用者原本的備註"
    once = R.merge_comments(old, "RUN=a; SEG=S001")
    assert once.startswith(old) and R.TAG_OPEN in once
    assert R.merge_comments(once, "RUN=a; SEG=S001") == once               # 冪等
    assert once.count(R.TAG_OPEN) == 1


def test_comments_update_own_section_in_place_without_duplicating():
    first = R.merge_comments("前文", "RUN=a")
    wrapped = "上\n" + first + "\n下"
    upd = R.merge_comments(wrapped, "RUN=b; STATUS=FAIL")
    assert upd.count(R.TAG_OPEN) == 1 and "RUN=a" not in upd and "RUN=b; STATUS=FAIL" in upd
    assert upd.startswith("上\n") and upd.endswith("\n下")


def test_comments_malformed_open_tag_is_not_treated_as_section():
    old = "x [MEP-TRAY] 沒有結尾"
    new = R.merge_comments(old, "RUN=a")
    assert new.startswith(old) and new.count(R.TAG_CLOSE) == 1


def test_comment_body_lists_findings_for_segment():
    i, r = routed([(10, 1, 3)])
    i.obstacles.append({"name": "W", "kind": "water", "lo": [4, 1.35, 2], "hi": [6, 1.6, 3.6]})
    m = R.build_model(i, r, [check_route(i, r, GOV)], GOV, "c1")
    body = R.comment_body(m, "S001")
    assert "RUN=c1" in body and "FINDINGS=UNVERIFIED clearance" in body
    assert "FINDINGS" not in R.comment_body(m, "S999")


# ---- 與 C# 的契約 ----
def test_checked_in_csharp_constants_match_generator():
    cs = (ROOT / "revit" / "MepTray.Core" / "Fields.g.cs").read_text(encoding="utf-8")
    assert cs == R.render_cs_constants()
    assert 'MmPerFoot = 304.8' in cs and '"[MEP-TRAY]"' in cs


def test_field_names_unique_per_scope_and_model_uses_them():
    m, *_ = model([(10, 1, 3), (6, 5, 3)])
    for k in (R.FIELDS["Segments"], R.FIELDS["Joints"], R.FIELDS["Tray"], R.FIELDS["CoordinateSystem"]):
        assert k in m
    assert set(m["segments"][0]) == {R.FIELDS["Id"], R.FIELDS["Start"], R.FIELDS["End"]}


def test_coordinate_system_origin_and_z_rotation_derive_consistent_axes():
    import math
    cs = R.coordinate_system("INTERNAL_ORIGIN", (2000, -1000, 500), 30)
    th = math.radians(30)
    assert cs["origin"] == [2000.0, -1000.0, 500.0] and cs["rotation_deg"] == 30.0
    assert cs["axis_x"] == [math.cos(th), math.sin(th), 0.0] and cs["axis_y"] == [-math.sin(th), math.cos(th), 0.0]
    assert cs["axis_z"] == [0.0, 0.0, 1.0]
    d = R.coordinate_system("INTERNAL_ORIGIN")
    assert d["origin"] == [0.0, 0.0, 0.0] and d["axis_x"] == [1.0, 0.0, 0.0] and d["axis_y"] == [0.0, 1.0, 0.0]
    assert "-0.0" not in repr(d["axis_x"] + d["axis_y"])
    for bad in ((1, 2), (1, 2, float("nan")), (1, 2, True), (1, 2, "3")):
        with pytest.raises(ValueError, match="origin_mm"):
            R.coordinate_system("INTERNAL_ORIGIN", bad)
    for bad in (float("inf"), float("nan"), True, "90"):
        with pytest.raises(ValueError, match="rotation_deg"):
            R.coordinate_system("INTERNAL_ORIGIN", (0, 0, 0), bad)


def test_build_model_passes_origin_and_rotation_through_and_keeps_schema_v1():
    m, _, _ = model([(10, 1, 3)], basis="SHARED_COORDINATES", origin_mm=(1, 2, 3), rotation_deg=45)
    assert m["schema_version"] == 1
    assert m["coordinate_system"]["origin"] == [1.0, 2.0, 3.0] and m["coordinate_system"]["rotation_deg"] == 45.0
