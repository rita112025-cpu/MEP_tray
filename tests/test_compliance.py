from mep_tray.compliance import check_compliance, max_hanger_spacing
from mep_tray.model import Inputs
from mep_tray.router import Route, place_hangers
from mep_tray.rules import FAIL, UNVERIFIED, merge_strictest

GOV = merge_strictest(["CNS", "IEC", "NEC", "TW_BUILDING", "MRT_APPX_C"])


def manual(*pts):
    wp = list(pts)
    return Route([wp], list(zip(wp, wp[1:])), wp[1:-1], 0.0)


def inp(**kw):
    return Inputs(start=(1, 1, 3), ends=[(10, 1, 3)], **kw)


def kinds(rep, k):
    return [f for f in rep.findings if f.kind == k]


def test_missing_hangers_disclosed_not_silently_passed():
    assert "note_hangers" in check_compliance(inp(), manual((1, 1, 3), (10, 1, 3)), GOV).design


def test_hanger_span_on_user_edited_hangers():
    r = manual((1, 1, 3), (10, 1, 3))
    r.hangers = [(1.3, 1, 3), (9.7, 1, 3)]                       # 手改：只剩兩個吊架
    f = kinds(check_compliance(inp(), r, GOV), "hanger_span")
    assert f and f[0].actual == 8.4 or abs(f[0].actual - 8.4) < 1e-6
    place_hangers(r, GOV["span_max_m"].value)
    assert not kinds(check_compliance(inp(), r, GOV), "hanger_span")


def test_segment_without_any_hanger_counts_as_its_length():
    r = manual((1, 1, 3), (5, 1, 3), (5, 8, 3))
    r.hangers = [(1.3, 1, 3), (3, 1, 3), (4.7, 1, 3)]            # 第二段(7m)完全沒有吊架
    worst, _ = max_hanger_spacing(r)
    assert worst == 7.0
    assert kinds(check_compliance(inp(), r, GOV), "hanger_span")


def test_fill_fail_recommends_width_and_status_is_unverified():
    i = inp(cables=[{"od_mm": 30, "count": 40, "kind": "power"}], tray_w_mm=300)
    f = kinds(check_compliance(i, manual((1, 1, 3), (10, 1, 3)), GOV), "fill")[0]
    assert "建議橋架寬" in f.suggestion and f.code == "CNS"
    assert f.indicative == FAIL and f.status == UNVERIFIED


def test_default_cables_disclosed():
    rep = check_compliance(inp(), manual((1, 1, 3), (10, 1, 3)), GOV)
    assert "cables_defaulted" in rep.design
    assert "cables_defaulted" not in check_compliance(
        inp(cables=[{"od_mm": 20, "count": 5, "kind": "power"}]), manual((1, 1, 3), (10, 1, 3)), GOV).design


def test_bend_leg_finding_and_radius_used():
    rep = check_compliance(inp(), manual((1, 1, 3), (1.2, 1, 3), (1.2, 5, 3)), GOV)
    assert kinds(rep, "bend") and rep.design["fitting_radius_mm_used"] >= 300


def test_bend_basis_follows_whichever_radius_governs():
    import dataclasses
    gov = dict(GOV, bend_radius_factor=dataclasses.replace(
        GOV["bend_radius_factor"], code="NEC", clause="NEC-bend-clause", verified=True))
    route = manual((1, 1, 3), (1.2, 1, 3), (1.2, 5, 3))
    big = check_compliance(inp(cables=[{"od_mm": 60, "count": 3, "kind": "power"}]), route, gov)
    f = kinds(big, "bend")[0]
    assert f.required == 720 and f.code == "NEC" and f.clause == "NEC-bend-clause"
    assert "電纜最小彎曲半徑" in f.subject and f.unit == "mm" and f.status == FAIL   # verified=True
    small = check_compliance(inp(cables=[{"od_mm": 20, "count": 3, "kind": "power"}]), route, gov)
    g = kinds(small, "bend")[0]
    assert g.required == 300 and g.clause == GOV["fitting_radius_mm"].clause
    assert "配件" in g.subject and g.status == UNVERIFIED
