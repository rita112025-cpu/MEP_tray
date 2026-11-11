"""Command line entry. Reads a snapshot and a rule file, writes xlsx and html."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checks import run_checks
from .model import load_rules, load_snapshot
from .report_html import render_html
from .report_xlsx import write_xlsx

_MODEL_SUFFIXES = (".rvt", ".rfa", ".rte", ".rft", ".dwg", ".dxf", ".dwt", ".ifc")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bim_inspector",
        description="Read a BIM JSON snapshot and a rule file, then write xlsx and html reports.",
    )
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--xlsx", required=True, type=Path)
    parser.add_argument("--html", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        _refuse_model_output(args.xlsx)
        _refuse_model_output(args.html)
        if args.xlsx.suffix.lower() != ".xlsx":
            raise ValueError("--xlsx path must end with .xlsx")
        if args.html.suffix.lower() != ".html":
            raise ValueError("--html path must end with .html")
        snapshot = load_snapshot(args.snapshot)
        rules = load_rules(args.rules)
        findings = run_checks(snapshot, rules)
        write_xlsx(args.xlsx, snapshot, rules, findings)
        args.html.write_text(render_html(snapshot, rules, findings), encoding="utf-8", newline="\n")
    except (OSError, ValueError) as exc:
        print(f"bim_inspector: {exc}", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.check] = counts.get(finding.check, 0) + 1
    detail = " ".join(f"{name}={counts.get(name, 0)}" for name in (
        "required_parameters", "open_connectors", "elevation", "duplicates"))
    print(f"findings={len(findings)} {detail}")
    return 0


def _refuse_model_output(path: Path) -> None:
    if path.suffix.lower() in _MODEL_SUFFIXES:
        raise ValueError(f"refusing to write a model file: {path.name}")
