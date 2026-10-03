"""The single prompt baseline: the same facts the agent can reach, one LLM call, no graph.

It shows how much the graph itself adds over handing a model everything at once.
"""

from collections.abc import Mapping
from typing import Any

from netsleuth.agents import (
    PLAYBOOKS,
    PROMPT_VERSION,
    InvestigationReport,
    PromptVersion,
    gather_context,
    load_prompt,
)
from netsleuth.agents.context import named_in, render_blast, render_findings
from netsleuth.agents.schemas import SinglePromptAnswer
from netsleuth.models import LLMError, StructuredLLM, Usage
from netsleuth.storage import StorageSession
from netsleuth.tools import ToolError, call_tool, inventory


class SinglePromptBaseline:
    name = "single-prompt"

    def __init__(
        self,
        llm: StructuredLLM,
        *,
        confidence_threshold: float = 0.5,
        prompt_version: PromptVersion = PROMPT_VERSION,
    ) -> None:
        self.llm = llm
        self.prompt_version = prompt_version
        self.confidence_threshold = confidence_threshold
        self.tool_calls = 0

    @property
    def usage(self) -> Usage:
        return self.llm.usage

    def __call__(self, session: StorageSession, anomaly: Mapping[str, Any]) -> InvestigationReport:
        incident_id = str(anomaly["anomaly_id"])
        try:
            context = gather_context(session, anomaly)
            seen = {f.query_ref for f in context.findings}
            for checks in PLAYBOOKS.values() if context.scope.node else ():
                for check in checks:
                    result = call_tool(session, check.tool, check.args(context.scope))
                    self.tool_calls += 1
                    if result.query_ref not in seen:
                        context.findings.append(result)
                        seen.add(result.query_ref)
            user = load_prompt("single_prompt", self.prompt_version).format(
                anomaly="\n".join(
                    f"- {k}: {anomaly.get(k)}"
                    for k in ("signal", "scope_device_id", "tick", "value", "baseline")
                ),
                findings=render_findings(context.findings),
                blast=render_blast(context.blast),
            )
            system = load_prompt("system", self.prompt_version)
            answer = self.llm.ask(SinglePromptAnswer, system, user, run_name="single prompt")
        except (LLMError, ToolError) as error:
            return InvestigationReport(
                incident_id=incident_id,
                root_cause_category="insufficient_evidence",
                root_cause_device_id=None,
                confidence=0.0,
                summary=f"The single prompt could not finish: {error}",
                prompt_version=self.prompt_version,
            )
        # The same rule as the agent: a real device, named in the prompt it was given.
        inv = inventory(session)
        device = answer.device_id
        if device is None or not (
            (device in inv.device_type or device in inv.routes) and named_in(device, user)
        ):
            device = None
        unsure = answer.confidence < self.confidence_threshold
        return InvestigationReport(
            incident_id=incident_id,
            root_cause_category="insufficient_evidence" if unsure else answer.category,
            root_cause_device_id=None if unsure else device,
            confidence=answer.confidence,
            summary=answer.summary,
            prompt_version=self.prompt_version,
        )
