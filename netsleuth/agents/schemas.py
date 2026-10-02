"""What each LLM step must answer with. Structured output keeps every answer typed."""

from pydantic import BaseModel, Field

from netsleuth.diagnosis import DiagnosisCategory


class Hypothesis(BaseModel):
    category: DiagnosisCategory
    suspect_device_id: str | None = None
    why: str


class Hypotheses(BaseModel):
    ranked: list[Hypothesis] = Field(min_length=1, max_length=4)


class EvidenceRequest(BaseModel):
    checks: list[str] = Field(default_factory=list, max_length=3)
    done: bool = False


class ScoredHypothesis(BaseModel):
    category: DiagnosisCategory
    device_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    supporting: list[str] = Field(default_factory=list)
    against: str = ""


class Scores(BaseModel):
    scored: list[ScoredHypothesis] = Field(min_length=1, max_length=4)
    summary: str


class SinglePromptAnswer(BaseModel):
    category: DiagnosisCategory
    device_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
