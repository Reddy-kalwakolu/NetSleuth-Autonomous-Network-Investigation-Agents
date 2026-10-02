from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from netsleuth.models import LangChainLLM, LLMError, ModelAccessError, ScriptedLLM, Usage


class Answer(BaseModel):
    label: str


class StubRunnable:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.seen: list[Any] = []

    def invoke(self, messages: Any, config: Any = None) -> dict[str, Any]:
        self.seen.append((messages, config))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubModel:
    """Stands in for a LangChain chat model; only with_structured_output is used."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.runnable = StubRunnable(result)

    def with_structured_output(self, schema: type, include_raw: bool = False) -> StubRunnable:
        assert include_raw
        return self.runnable


def test_usage_adds_and_prices() -> None:
    usage = Usage()
    usage.add(1_000, 200)
    usage.add(500, 100)

    assert (usage.calls, usage.input_tokens, usage.output_tokens) == (2, 1_500, 300)
    assert usage.cost_usd(2.0, 10.0) == pytest.approx(1_500 * 2 / 1e6 + 300 * 10 / 1e6)
    assert usage.minus(Usage(1, 500, 100)) == Usage(1, 1_000, 200)


def test_langchain_llm_returns_the_parsed_answer_and_counts_tokens() -> None:
    raw = AIMessage(
        content="", usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128}
    )
    llm = LangChainLLM(StubModel({"raw": raw, "parsed": Answer(label="x"), "parsing_error": None}))

    answer = llm.ask(Answer, "system text", "user text", run_name="step")

    assert answer == Answer(label="x")
    assert (llm.usage.calls, llm.usage.input_tokens, llm.usage.output_tokens) == (1, 120, 8)


def test_langchain_llm_turns_provider_failures_into_llm_errors() -> None:
    llm = LangChainLLM(StubModel(TimeoutError("network down")))

    with pytest.raises(LLMError, match="network down"):
        llm.ask(Answer, "s", "u", run_name="step")


def test_langchain_llm_rejects_unparseable_output() -> None:
    raw = AIMessage(
        content="not json",
        usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
    )
    llm = LangChainLLM(StubModel({"raw": raw, "parsed": None, "parsing_error": ValueError("bad")}))

    with pytest.raises(LLMError):
        llm.ask(Answer, "s", "u", run_name="step")
    assert llm.usage.calls == 1  # a failed parse still cost tokens


def test_scripted_llm_answers_from_its_responder_and_counts_tokens() -> None:
    llm = ScriptedLLM(lambda schema, system, user: schema(label=user.upper()))

    assert llm.ask(Answer, "abcd" * 10, "hi", run_name="step") == Answer(label="HI")
    assert llm.usage.calls == 1
    assert llm.usage.input_tokens == (40 + 2) // 4
    assert llm.usage.output_tokens == 40


def test_scripted_llm_can_fail_like_a_real_model() -> None:
    def broken(schema: type, system: str, user: str) -> BaseModel:
        raise LLMError("rate limited")

    with pytest.raises(LLMError):
        ScriptedLLM(broken).ask(Answer, "s", "u", run_name="step")


class Unauthorized(Exception):
    status_code = 401


def test_a_rejected_key_or_unknown_model_is_not_a_one_off_failure() -> None:
    llm = LangChainLLM(StubModel(Unauthorized("invalid api key")))

    with pytest.raises(ModelAccessError):
        llm.ask(Answer, "s", "u", run_name="step")


def test_long_lists_from_the_model_are_trimmed_not_rejected() -> None:
    from netsleuth.agents.schemas import (
        EvidenceRequest,
        Hypotheses,
        Hypothesis,
        ScoredHypothesis,
        Scores,
    )

    many = [Hypothesis(category="unknown", why=str(i)) for i in range(6)]
    scored = [ScoredHypothesis(category="unknown", confidence=0.1) for _ in range(6)]

    assert len(Hypotheses(ranked=many).ranked) == 4
    assert len(EvidenceRequest(checks=["a", "b", "c", "d", "e"]).checks) == 3
    assert len(Scores(scored=scored, summary="s").scored) == 4
