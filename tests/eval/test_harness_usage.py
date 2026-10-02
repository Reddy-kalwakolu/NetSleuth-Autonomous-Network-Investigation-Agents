from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from langsmith.run_helpers import get_tracing_context

from netsleuth.agents import InvestigationAgent
from netsleuth.baselines import rules_baseline
from netsleuth.diagnosis import Diagnosis
from netsleuth.eval import (
    AmplifierFailureSpec,
    BudgetExceeded,
    Case,
    Pricing,
    Tracing,
    format_scores,
    run_case,
    run_cases,
)
from netsleuth.models import ScriptedLLM
from netsleuth.storage import StorageSession
from tests.support import oracle

AMP_CASE = Case(
    case_id="cost", ticks=30, faults=[AmplifierFailureSpec(amp_id="amp-hub1-node04-a1", at_tick=10)]
)


def agent() -> InvestigationAgent:
    return InvestigationAgent(ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1")))


def test_usage_and_cost_are_recorded_per_case(tmp_path: Path) -> None:
    result = run_case(
        AMP_CASE,
        agent(),
        tmp_path / "d",
        tmp_path / "g",
        system_name="agent",
        pricing=Pricing(2.0, 10.0),
    )

    assert result.usage is not None
    assert result.usage.calls >= 3  # hypothesize, gather, score
    assert result.usage.cost_usd == pytest.approx(
        (result.usage.input_tokens * 2.0 + result.usage.output_tokens * 10.0) / 1e6
    )
    assert "usage:" in format_scores([result])


def test_rules_have_no_usage(tmp_path: Path) -> None:
    assert run_case(AMP_CASE, rules_baseline, tmp_path / "d", tmp_path / "g").usage is None


def test_usage_is_per_case_not_cumulative(tmp_path: Path) -> None:
    system = agent()
    first = run_case(AMP_CASE, system, tmp_path / "d", tmp_path / "g", system_name="agent")
    second = run_case(
        AMP_CASE.model_copy(update={"case_id": "cost2"}),
        system,
        tmp_path / "d",
        tmp_path / "g",
        system_name="agent",
    )

    assert first.usage is not None and second.usage is not None
    assert second.usage.calls == first.usage.calls


def test_budget_stops_the_run_after_the_case_that_spent_it(tmp_path: Path) -> None:
    cases = [AMP_CASE.model_copy(update={"case_id": f"c{i}"}) for i in range(3)]

    with pytest.raises(BudgetExceeded) as stopped:
        run_cases(
            cases,
            agent(),
            tmp_path / "d",
            tmp_path / "g",
            system_name="agent",
            pricing=Pricing(2_000.0, 10_000.0),
            max_cost_usd=0.01,
        )

    assert [r.case_id for r in stopped.value.results] == ["c0"]


def test_each_diagnosis_runs_inside_a_tagged_tracing_context(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def spy(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
        seen.append(dict(get_tracing_context()))
        return rules_baseline(session, anomaly)

    run_case(
        AMP_CASE,
        spy,
        tmp_path / "d",
        tmp_path / "g",
        system_name="spy",
        tracing=Tracing(project="netsleuth-test"),
    )

    (ctx,) = seen
    assert ctx["project_name"] == "netsleuth-test"
    assert {"spy", "cost"} <= set(ctx["tags"])
    assert ctx["metadata"]["case_id"] == "cost"


def test_without_tracing_settings_tracing_is_off(tmp_path: Path) -> None:
    seen: list[Any] = []

    def spy(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
        seen.append(get_tracing_context()["enabled"])
        return rules_baseline(session, anomaly)

    run_case(AMP_CASE, spy, tmp_path / "d", tmp_path / "g", system_name="spy")

    assert seen == [False]


def test_an_unexpected_failure_keeps_the_scores_already_paid_for(tmp_path: Path) -> None:
    from netsleuth.eval import RunAborted

    calls = {"n": 0}

    def fragile(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return rules_baseline(session, anomaly)

    cases = [AMP_CASE.model_copy(update={"case_id": f"c{i}"}) for i in range(3)]

    with pytest.raises(RunAborted) as aborted:
        run_cases(cases, fragile, tmp_path / "d", tmp_path / "g", system_name="fragile")

    assert [r.case_id for r in aborted.value.results] == ["c0"]
    assert "disk full" in str(aborted.value)
