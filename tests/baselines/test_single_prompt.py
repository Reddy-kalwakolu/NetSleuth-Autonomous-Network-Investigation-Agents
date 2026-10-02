from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from netsleuth.agents.schemas import SinglePromptAnswer
from netsleuth.baselines import SinglePromptBaseline
from netsleuth.diagnosis import DiagnosisCategory
from netsleuth.models import LLMError, ScriptedLLM
from netsleuth.storage import DuckDBSession
from tests.support import AMP, Responder


@pytest.fixture
def amp_anomaly(after: DuckDBSession) -> Mapping[str, Any]:
    return after.query(
        "SELECT * FROM anomaly_events WHERE scope_device_id = 'node-hub1-04' ORDER BY tick LIMIT 1"
    ).row(0, named=True)


def answer(category: DiagnosisCategory, device: str | None, confidence: float = 0.8) -> Responder:
    def respond(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        assert schema is SinglePromptAnswer
        assert "fiber.route_nodes" not in user  # facts, not menu names
        return SinglePromptAnswer(
            category=category, device_id=device, confidence=confidence, summary="s"
        )

    return respond


def test_one_call_with_every_playbook_fact(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    llm = ScriptedLLM(answer("amplifier_failure", AMP))

    report = SinglePromptBaseline(llm)(after, amp_anomaly)

    assert llm.usage.calls == 1
    assert report.root_cause_category == "amplifier_failure"
    assert report.root_cause_device_id == AMP


def test_invented_device_and_low_confidence_are_handled(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    invented = SinglePromptBaseline(ScriptedLLM(answer("fiber_cut", "route-nowhere")))(
        after, amp_anomaly
    )
    unsure = SinglePromptBaseline(ScriptedLLM(answer("fiber_cut", "route-hub1-2", 0.2)))(
        after, amp_anomaly
    )

    assert invented.root_cause_device_id is None
    assert unsure.root_cause_category == "insufficient_evidence"


def test_failed_call_is_insufficient_evidence(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    def down(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        raise LLMError("timeout")

    report = SinglePromptBaseline(ScriptedLLM(down))(after, amp_anomaly)

    assert report.root_cause_category == "insufficient_evidence"
    assert "timeout" in report.summary


def test_the_prompt_carries_every_playbook_fact(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    prompts: list[str] = []

    def capture(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        prompts.append(user)
        return SinglePromptAnswer(category="unknown", confidence=0.9, summary="s")

    SinglePromptBaseline(ScriptedLLM(capture))(after, amp_anomaly)

    (prompt,) = prompts
    assert "find_devices:" in prompt  # the fiber route check
    assert "get_subtree:" in prompt  # the amplifier check


def test_the_single_prompt_records_its_prompt_version(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    default = SinglePromptBaseline(ScriptedLLM(answer("amplifier_failure", AMP)))(
        after, amp_anomaly
    )
    v1 = SinglePromptBaseline(
        ScriptedLLM(answer("amplifier_failure", AMP)), prompt_version="investigation-v1"
    )(after, amp_anomaly)

    assert default.prompt_version == "investigation-v2"
    assert v1.prompt_version == "investigation-v1"


def test_a_real_device_missing_from_the_prompt_is_dropped(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    from netsleuth.tools import inventory

    unshown = "amp-hub1-node07-a1"
    report = SinglePromptBaseline(ScriptedLLM(answer("amplifier_failure", unshown)))(
        after, amp_anomaly
    )

    assert unshown in inventory(after).device_type
    assert report.root_cause_category == "amplifier_failure"
    assert report.root_cause_device_id is None
