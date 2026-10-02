"""One small interface for every LLM call, so tests and CI never touch a paid API.

``LangChainLLM`` wraps a LangChain chat model and asks for typed, structured answers.
``ScriptedLLM`` answers from a function, for tests and offline runs. Both count tokens, so the
harness can price every case.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """An LLM call that failed or came back unusable."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def copy(self) -> "Usage":
        return replace(self)

    def minus(self, earlier: "Usage") -> "Usage":
        return Usage(
            self.calls - earlier.calls,
            self.input_tokens - earlier.input_tokens,
            self.output_tokens - earlier.output_tokens,
        )

    def cost_usd(self, input_usd_per_mtok: float, output_usd_per_mtok: float) -> float:
        return (
            self.input_tokens * input_usd_per_mtok + self.output_tokens * output_usd_per_mtok
        ) / 1e6


class StructuredLLM(Protocol):
    usage: Usage

    def ask(self, schema: type[T], system: str, user: str, *, run_name: str) -> T: ...


class _SupportsStructuredOutput(Protocol):
    def with_structured_output(self, schema: Any, *, include_raw: bool = ...) -> Any: ...


class LangChainLLM:
    def __init__(self, model: _SupportsStructuredOutput) -> None:
        self.model = model
        self.usage = Usage()

    def ask(self, schema: type[T], system: str, user: str, *, run_name: str) -> T:
        runnable = self.model.with_structured_output(schema, include_raw=True)
        try:
            out = runnable.invoke(
                [SystemMessage(content=system), HumanMessage(content=user)],
                config={"run_name": run_name},
            )
        except Exception as error:  # the provider boundary: every failure becomes an LLMError
            raise LLMError(f"{run_name}: {error}") from error
        meta = getattr(out.get("raw"), "usage_metadata", None) or {}
        self.usage.add(int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0)))
        parsed = out.get("parsed")
        if out.get("parsing_error") is not None or not isinstance(parsed, schema):
            raise LLMError(f"{run_name}: the answer did not match {schema.__name__}")
        return parsed


Responder = Callable[[type[BaseModel], str, str], BaseModel]


class ScriptedLLM:
    """Answers from a function. Token counts are a rough four characters per token."""

    def __init__(self, responder: Responder, output_tokens: int = 40) -> None:
        self.responder = responder
        self.output_tokens = output_tokens
        self.usage = Usage()

    def ask(self, schema: type[T], system: str, user: str, *, run_name: str) -> T:
        self.usage.add((len(system) + len(user)) // 4, self.output_tokens)
        answer = self.responder(schema, system, user)
        if not isinstance(answer, schema):
            raise LLMError(f"{run_name}: scripted answer is not a {schema.__name__}")
        return answer
