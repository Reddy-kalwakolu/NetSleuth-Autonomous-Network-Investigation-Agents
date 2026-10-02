"""The investigation report: a diagnosis plus the evidence behind it."""

from pydantic import BaseModel, ConfigDict, Field

from netsleuth.diagnosis import Diagnosis, DiagnosisCategory


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str
    source_tool: str
    query_ref: str


class RuledOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: DiagnosisCategory
    reason: str


class InvestigationReport(Diagnosis):
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[Evidence, ...] = ()
    alternatives_ruled_out: tuple[RuledOut, ...] = ()
    prompt_version: str = ""
