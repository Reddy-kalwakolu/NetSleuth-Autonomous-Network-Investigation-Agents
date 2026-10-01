"""The answer every system returns for an incident, whether rules, a single prompt or the agent.

It is a subset of the full investigation report: enough for the harness to score category and
location. Evidence, confidence and ruled out alternatives join it with the agent.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from netsleuth.sandbox.engine import RootCauseCategory

DiagnosisCategory = RootCauseCategory | Literal["unknown", "insufficient_evidence"]


class Diagnosis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    root_cause_category: DiagnosisCategory
    root_cause_device_id: str | None
    affected_device_ids: tuple[str, ...] = ()
    summary: str = ""
