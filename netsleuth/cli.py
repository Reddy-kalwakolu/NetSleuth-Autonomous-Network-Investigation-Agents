"""The ``netsleuth`` command.

``netsleuth run scenarios/dev/f1_*.yaml`` runs each scenario end to end: simulate the network, write
its data, detect anomalies, diagnose each one with the rules baseline, and print scores against the
ground truth.
"""

import argparse
import glob
import sys
from collections.abc import Sequence
from pathlib import Path

from netsleuth.baselines import rules_baseline
from netsleuth.config import load_settings
from netsleuth.eval import ScenarioError, format_scores, load_case, run_case

WILDCARDS = "*?["


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="netsleuth",
        description="Network investigation agents, tested against a simulated cable network.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run scenarios end to end and print scores")
    run.add_argument("scenarios", nargs="+", help="scenario files, wildcards allowed")
    run.add_argument("--config", type=Path, help="settings file (default: netsleuth.yaml)")
    run.add_argument("--data-dir", type=Path, help="where run data is written")
    run.add_argument("--ground-truth-dir", type=Path, help="where answer keys are written")
    args = parser.parse_args(argv)
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    data_dir: Path = args.data_dir or settings.data_dir
    ground_truth_dir: Path = args.ground_truth_dir or settings.ground_truth_dir

    paths = _expand(args.scenarios)
    if not paths:
        print(f"error: no scenario files match {' '.join(args.scenarios)}", file=sys.stderr)
        return 2
    try:
        cases = [load_case(path) for path in paths]
    except ScenarioError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    results = []
    for case in cases:
        print(f"running {case.case_id}", file=sys.stderr, flush=True)
        results.append(run_case(case, rules_baseline, data_dir, ground_truth_dir))
    print(format_scores(results))
    return 0


def _expand(patterns: Sequence[str]) -> list[Path]:
    """Expand wildcards here, because PowerShell and cmd pass them through unexpanded."""
    paths: list[Path] = []
    for pattern in patterns:
        if any(char in pattern for char in WILDCARDS):
            paths.extend(sorted(Path(match) for match in glob.glob(pattern)))
        else:
            paths.append(Path(pattern))
    return paths


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
