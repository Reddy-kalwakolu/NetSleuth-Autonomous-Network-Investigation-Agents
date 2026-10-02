"""What each LLM step must answer with. Structured output keeps every answer typed.

A model that returns more items than a step uses is trimmed to the first ones, not rejected:
losing a whole diagnosis over a fifth hypothesis would be the wrong trade.
"""

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field

from netsleuth.diagnosis import DiagnosisCategory


def _first(n: int) -> AfterValidator:
    def keep(items: list[Any]) -> list[Any]:
        return items[:n]

    return AfterValidator(keep)


class Hypothesis(BaseModel):
    category: DiagnosisCategory
    suspect_device_id: str | None = None
    why: str


class Hypotheses(BaseModel):
    ranked: Annotated[list[Hypothesis], Field(min_length=1), _first(4)]


class EvidenceRequest(BaseModel):
    checks: Annotated[list[str], _first(3)] = Field(default_factory=list)
    done: bool = False


class ScoredHypothesis(BaseModel):
    category: DiagnosisCategory
    device_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    supporting: list[str] = Field(default_factory=list)
    against: str = ""


class Scores(BaseModel):
    scored: Annotated[list[ScoredHypothesis], Field(min_length=1), _first(4)]
    summary: str


class SinglePromptAnswer(BaseModel):
    category: DiagnosisCategory
    device_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
