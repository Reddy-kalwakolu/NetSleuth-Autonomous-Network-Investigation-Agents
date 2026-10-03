"""Anomalies with no node behind them, like a peering link, must not break the LLM systems."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from netsleuth.agents import InvestigationAgent
from netsleuth.agents.schemas import SinglePromptAnswer
from netsleuth.baselines import SinglePromptBaseline
from netsleuth.eval import Case, PeeringCongestionSpec, run_case
from netsleuth.eval.harness import System
from netsleuth.models import ScriptedLLM
from netsleuth.sandbox.topology import generate_topology
from netsleuth.sandbox.topology.models import PeeringLink
from tests.support import oracle

LINK = generate_topology("dev", seed=0).of_type(PeeringLink)[0].device_id


def single_answer(schema: type[BaseModel], system: str, user: str) -> BaseModel:
    return SinglePromptAnswer(
        category="peering_congestion", device_id=LINK, confidence=0.9, summary="s"
    )


def agent() -> System:
    return InvestigationAgent(ScriptedLLM(oracle("peering_congestion", LINK)))


def single() -> System:
    return SinglePromptBaseline(ScriptedLLM(single_answer))


@pytest.mark.parametrize("make", [agent, single], ids=["agent", "single-prompt"])
def test_a_peering_anomaly_gets_a_real_diagnosis(tmp_path: Path, make: object) -> None:
    assert callable(make)
    case = Case(
        case_id="d2",
        ticks=20 * 12,
        faults=[PeeringCongestionSpec(link_id=LINK, at_tick=0)],
    )

    (score,) = run_case(case, make(), tmp_path / "d", tmp_path / "g").faults

    assert (score.category_score, score.location_score) == (1.0, 1.0)
