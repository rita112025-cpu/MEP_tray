"""Endpoint topology contract, invariant under ordering and segment reversal."""
from itertools import permutations, product

import pytest

from mep_tray.export_revit import build_joints

C = (5, 5, 3)
X0, X1, Y0, Y1 = (4, 5, 3), (6, 5, 3), (5, 4, 3), (5, 6, 3)

CASES = [
    ("straight", [(X0, X1)], []),
    ("elbow", [(X0, C), (C, Y1)], [(C, "elbow")]),
    ("tee", [(X0, C), (C, X1), (C, Y1)], [(C, "tee")]),
    ("cross", [(X0, C), (C, X1), (Y0, C), (C, Y1)], [(C, "cross")]),
    ("touching_endpoint", [(X0, C), (C, X1)], [(C, "union")]),
    ("parallel_nearby", [(X0, X1), ((4, 5.001, 3), (6, 5.001, 3))], []),
    ("collinear_overlapping", [(C, X1), (C, (7, 5, 3))], [(C, "unsupported")]),
    ("nearly_collinear", [(X0, C), (C, (6, 5.0001, 3))], [(C, "unsupported")]),
    ("duplicate_segment", [(C, X1), (C, X1)], [(C, "unsupported"), (X1, "unsupported")]),
    ("reversed_segment", [(C, X1), (X1, C)], [(C, "unsupported"), (X1, "unsupported")]),
]


@pytest.mark.parametrize("name,segs,expected", CASES, ids=[c[0] for c in CASES])
def test_topology_matrix_order_and_direction(name, segs, expected):
    want = sorted((tuple(v * 1000 for v in point), kind) for point, kind in expected)
    for order in permutations(segs):
        for flips in product((False, True), repeat=len(segs)):
            variant = [(b, a) if flip else (a, b) for (a, b), flip in zip(order, flips)]
            joints = build_joints(variant)
            got = sorted((tuple(j["point"]), j["kind"]) for j in joints)
            assert got == want, (name, variant, got)
            for joint in joints:
                if joint["kind"] == "tee":
                    assert joint["branch"] == joint["segments"][-1]
                    branch = variant[int(joint["branch"][1:]) - 1]
                    assert branch in [(C, Y1), (Y1, C)]
