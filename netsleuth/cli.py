"""The ``netsleuth`` command.

``netsleuth run scenarios/dev/f1_*.yaml`` runs each scenario end to end: simulate the network, write
its data, detect anomalies, diagnose each one, and print scores against the ground truth.
``--system`` picks who diagnoses: the rules baseline (the default), the single prompt baseline, or
the investigation agent. The two LLM systems need a model in the settings and its API key in the
environment, and every run is priced and stopped once it has spent ``max_cost_usd_per_run``.
"""

import argparse
import glob
import sys
from collections.abc import Sequence
from pathlib import Path

from netsleuth.agents import InvestigationAgent
from netsleuth.baselines import SinglePromptBaseline, rules_baseline
from netsleuth.config import Settings, load_settings
from netsleuth.eval import (
    BudgetExceeded,
    Pricing,
    ScenarioError,
    System,
    format_scores,
    load_case,
    run_cases,
    tracing_from_env,
)
from netsleuth.models import LangChainLLM, ModelConfigError, StructuredLLM, chat_model

WILDCARDS = "*?["
SYSTEMS = ("rules", "single-prompt", "agent")


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
    run.add_argument(
        "--system", choices=SYSTEMS, default="rules", help="which system diagnoses (default: rules)"
    )
    args = parser.parse_args(argv)
    return _run(args)


def build_llm(settings: Settings) -> StructuredLLM:
    return LangChainLLM(chat_model(settings))


def build_system(name: str, settings: Settings) -> System:
    if name == "rules":
        return rules_baseline
    llm = build_llm(settings)
    if name == "single-prompt":
        return SinglePromptBaseline(llm, confidence_threshold=settings.confidence_threshold)
    return InvestigationAgent(
        llm,
        max_tool_calls=settings.max_tool_calls,
        confidence_threshold=settings.confidence_threshold,
    )


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
    # The system is built before anything runs, so a missing model or key costs nothing.
    try:
        system = build_system(args.system, settings)
    except ModelConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    pricing = None
    if settings.llm_input_usd_per_mtok is not None and settings.llm_output_usd_per_mtok is not None:
        pricing = Pricing(settings.llm_input_usd_per_mtok, settings.llm_output_usd_per_mtok)
    try:
        results = run_cases(
            cases,
            system,
            data_dir,
            ground_truth_dir,
            system_name=args.system,
            pricing=pricing,
            tracing=tracing_from_env(settings),
            max_cost_usd=settings.max_cost_usd_per_run,
            on_case=lambda c: print(f"running {c.case_id}", file=sys.stderr, flush=True),
        )
    except BudgetExceeded as stopped:
        print(format_scores(stopped.results))
        print(f"error: {stopped}", file=sys.stderr)
        return 3
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
