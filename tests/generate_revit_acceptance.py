"""Generate existing exporter fixtures for manual Revit acceptance; no GUI PASS claims.

python -m tests.generate_revit_acceptance --type-name "<exact CableTrayType name>"
"""
import argparse

from mep_tray.export_revit import write_model
from mep_tray.paths import out_path
from tests.revit_live import _routed_model, scenarios


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type-name", required=True)
    parser.add_argument("--run-id", default="revit2025_gui")
    args = parser.parse_args()
    if not args.type_name.strip() or args.type_name == "__FIRST__":
        parser.error("Use the exact type name from the active Revit document")
    existing = scenarios()
    cases = dict(straight=_routed_model([(10, 1, 3)], "gui_straight", type_name=args.type_name),
                 elbow=existing["b_elbow"], tee=existing["a_tee"], cross=existing["g_cross"])
    expected = dict(straight=(1, []), elbow=(2, ["elbow"]), tee=(3, ["tee"]), cross=(4, ["cross"]))
    # Preflight all paths, preserving any existing acceptance output.
    for name in cases:
        target = out_path(args.run_id, f"tray_{name}.json")
        if target.exists():
            raise FileExistsError(target)
    for name, model in cases.items():
        model["run_id"] = f"gui_{name}"
        model["tray"]["type_name"] = args.type_name
        model["coordinate_system"]["basis"] = "INTERNAL_ORIGIN"
        count, kinds = expected[name]
        assert len(model["segments"]) == count
        assert [joint["kind"] for joint in model["joints"]] == kinds
        path = write_model(model, args.run_id, f"tray_{name}.json")
        print(f"{path}: {count} segments; joints={kinds}; Revit result UNVERIFIED")


if __name__ == "__main__":
    main()
