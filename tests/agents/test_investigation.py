from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from netsleuth.agents import InvestigationAgent
from netsleuth.agents.schemas import (
    EvidenceRequest,
    Hypotheses,
    Hypothesis,
    ScoredHypothesis,
    Scores,
)
from netsleuth.eval import AmplifierFailureSpec, Case, run_case
from netsleuth.models import LLMError, ScriptedLLM
from netsleuth.storage import DuckDBSession
from netsleuth.tools import rerun
from tests.support import oracle


@pytest.fixture
def amp_anomaly(after: DuckDBSession) -> Mapping[str, Any]:
    return after.query(
        "SELECT * FROM anomaly_events WHERE scope_device_id = 'node-hub1-04' ORDER BY tick LIMIT 1"
    ).row(0, named=True)


def test_agent_reports_the_scripted_answer_with_rerunnable_evidence(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    agent = InvestigationAgent(ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1")))

    report = agent(after, amp_anomaly)

    assert report.root_cause_category == "amplifier_failure"
    assert report.root_cause_device_id == "amp-hub1-node04-a1"
    assert report.confidence == 0.9
    assert report.evidence
    for item in report.evidence:
        assert rerun(after, item.query_ref).summary.startswith(item.claim[:20])
    assert report.prompt_version == "investigation-v1"


def test_an_invented_device_is_dropped(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    agent = InvestigationAgent(ScriptedLLM(oracle("amplifier_failure", "amp-made-up")))

    report = agent(after, amp_anomaly)

    assert report.root_cause_category == "amplifier_failure"
    assert report.root_cause_device_id is None


def test_a_route_id_is_accepted_as_a_location(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    report = InvestigationAgent(ScriptedLLM(oracle("fiber_cut", "route-hub1-2")))(
        after, amp_anomaly
    )

    assert report.root_cause_device_id == "route-hub1-2"


def test_low_confidence_becomes_insufficient_evidence(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    agent = InvestigationAgent(ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1", 0.3)))

    report = agent(after, amp_anomaly)

    assert report.root_cause_category == "insufficient_evidence"
    assert report.root_cause_device_id is None


def test_tool_calls_never_exceed_the_cap(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    def greedy(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        if schema is EvidenceRequest:
            menu = [
                line[2:]
                for line in user.split("Menu:\n", 1)[1].splitlines()
                if line.startswith("- ")
            ]
            return EvidenceRequest(checks=["made.up", *menu, *menu][:3], done=False)
        if schema is Hypotheses:
            return Hypotheses(
                ranked=[
                    Hypothesis(category="fiber_cut", why="x"),
                    Hypothesis(category="ingress_noise", why="y"),
                ]
            )
        return Scores(scored=[ScoredHypothesis(category="fiber_cut", confidence=0.6)], summary="s")

    agent = InvestigationAgent(ScriptedLLM(greedy), max_tool_calls=1)

    agent(after, amp_anomaly)

    assert agent.tool_calls == 1  # it asked for two real checks at once; the cap held


def test_a_failed_llm_call_returns_insufficient_evidence_and_says_why(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    def down(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        raise LLMError("rate limited")

    report = InvestigationAgent(ScriptedLLM(down))(after, amp_anomaly)

    assert report.root_cause_category == "insufficient_evidence"
    assert "rate limited" in report.summary


def test_agent_runs_end_to_end_through_the_harness(tmp_path: Path) -> None:
    case = Case(
        case_id="agent",
        ticks=40,
        faults=[AmplifierFailureSpec(amp_id="amp-hub1-node04-a1", at_tick=20)],
    )
    agent = InvestigationAgent(ScriptedLLM(oracle("amplifier_failure", "amp-hub1-node04-a1")))

    (score,) = run_case(case, agent, tmp_path / "d", tmp_path / "g", system_name="agent").faults

    assert (score.category_score, score.location_score) == (1.0, 1.0)


def test_citations_of_facts_that_were_never_shown_are_dropped(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    honest = oracle("amplifier_failure", "amp-hub1-node04-a1")

    def embellish(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        answer = honest(schema, system, user)
        if isinstance(answer, Scores):
            top = answer.scored[0]
            padded = top.model_copy(
                update={"supporting": ['get_device:{"device_id":"invented"}', *top.supporting]}
            )
            return answer.model_copy(update={"scored": [padded]})
        return answer

    report = InvestigationAgent(ScriptedLLM(embellish))(after, amp_anomaly)

    assert report.evidence
    assert all("invented" not in item.query_ref for item in report.evidence)


def test_model_access_errors_escape_the_agent(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    from netsleuth.models import ModelAccessError

    def locked_out(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        raise ModelAccessError("401 invalid api key")

    with pytest.raises(ModelAccessError):
        InvestigationAgent(ScriptedLLM(locked_out))(after, amp_anomaly)
