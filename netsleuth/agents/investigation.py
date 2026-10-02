"""Investigation agent v1: a LangGraph graph where code does the lookups and the model judges.

    context (code: intake, prechecks, blast radius)
    hypothesize (LLM)
    gather (LLM picks checks from the playbooks, code runs them, loops under a hard cap)
    score (LLM)
    decide (code: threshold, only real devices)
    report (code)

Confidence is the model's own score in v1. Calibration against dev results replaces it in
milestone 5.
"""

from collections.abc import Mapping
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from netsleuth.agents.context import Context, gather_context, render_blast, render_findings
from netsleuth.agents.playbooks import Check, checks_for
from netsleuth.agents.prompts import PROMPT_VERSION, load_prompt
from netsleuth.agents.report import Evidence, InvestigationReport, RuledOut
from netsleuth.agents.schemas import EvidenceRequest, Hypotheses, Scores
from netsleuth.models import LLMError, StructuredLLM, Usage
from netsleuth.storage import StorageSession
from netsleuth.tools import ToolError, call_tool, inventory

RECURSION_LIMIT = 50
CHECKS_PER_ROUND = 3


class State(TypedDict, total=False):
    anomaly: dict[str, Any]
    context: Context
    hypotheses: Hypotheses
    offered: list[Check]
    tool_calls: int
    done: bool
    scores: Scores
    report: InvestigationReport


class InvestigationAgent:
    name = "agent"

    def __init__(
        self, llm: StructuredLLM, *, max_tool_calls: int = 8, confidence_threshold: float = 0.5
    ) -> None:
        self.llm = llm
        self.max_tool_calls = max_tool_calls
        self.confidence_threshold = confidence_threshold
        self.tool_calls = 0

    @property
    def usage(self) -> Usage:
        return self.llm.usage

    def __call__(self, session: StorageSession, anomaly: Mapping[str, Any]) -> InvestigationReport:
        incident_id = str(anomaly["anomaly_id"])
        try:
            final = self._graph(session).invoke(
                {"anomaly": dict(anomaly), "tool_calls": 0},
                config={
                    "recursion_limit": RECURSION_LIMIT,
                    "run_name": f"investigate {incident_id}",
                },
            )
        except (LLMError, ToolError) as error:
            return InvestigationReport(
                incident_id=incident_id,
                root_cause_category="insufficient_evidence",
                root_cause_device_id=None,
                confidence=0.0,
                summary=f"The investigation could not finish: {error}",
                prompt_version=PROMPT_VERSION,
            )
        report: InvestigationReport = final["report"]
        return report

    def _graph(self, session: StorageSession) -> Any:
        llm, cap, threshold, system = (
            self.llm,
            self.max_tool_calls,
            self.confidence_threshold,
            load_prompt("system"),
        )

        def context(state: State) -> State:
            return {"context": gather_context(session, state["anomaly"])}

        def hypothesize(state: State) -> State:
            ctx = state["context"]
            user = load_prompt("hypothesize").format(
                anomaly=_render_anomaly(state["anomaly"]),
                findings=render_findings(ctx.findings),
                blast=render_blast(ctx.blast),
            )
            hypotheses = llm.ask(Hypotheses, system, user, run_name="hypothesize")
            offered = checks_for(h.category for h in hypotheses.ranked[:2])
            return {"hypotheses": hypotheses, "offered": offered, "done": not offered}

        def gather(state: State) -> State:
            ctx, offered, used = state["context"], list(state["offered"]), state["tool_calls"]
            user = load_prompt("gather").format(
                budget=cap - used,
                hypotheses=_render_hypotheses(state["hypotheses"]),
                findings=render_findings(ctx.findings),
                menu="\n".join(f"- {c.name}" for c in offered),
            )
            request = llm.ask(EvidenceRequest, system, user, run_name="gather")
            by_name = {c.name: c for c in offered}
            ran = 0
            for name in dict.fromkeys(request.checks):  # ignore repeats, keep order
                check = by_name.get(name)
                if check is None or used + ran >= cap or ran >= CHECKS_PER_ROUND:
                    continue
                ctx.findings.append(call_tool(session, check.tool, check.args(ctx.scope)))
                offered.remove(check)
                ran += 1
                self.tool_calls += 1
            done = request.done or ran == 0 or not offered or used + ran >= cap
            return {"offered": offered, "tool_calls": used + ran, "done": done}

        def score(state: State) -> State:
            ctx = state["context"]
            user = load_prompt("score").format(
                hypotheses=_render_hypotheses(state["hypotheses"]),
                findings=render_findings(ctx.findings),
                blast=render_blast(ctx.blast),
            )
            return {"scores": llm.ask(Scores, system, user, run_name="score")}

        def decide_and_report(state: State) -> State:
            ctx, scores = state["context"], state["scores"]
            ranked = sorted(scores.scored, key=lambda s: s.confidence, reverse=True)
            top = ranked[0]
            inv = inventory(session)
            device = (
                top.device_id
                if top.device_id in inv.device_type or top.device_id in inv.routes
                else None
            )
            category = top.category if top.confidence >= threshold else "insufficient_evidence"
            if category == "insufficient_evidence":
                device = None
            known = {f.query_ref: f for f in ctx.findings}
            evidence = tuple(
                Evidence(claim=known[ref].summary, source_tool=known[ref].tool, query_ref=ref)
                for ref in dict.fromkeys(top.supporting)
                if ref in known
            )
            report = InvestigationReport(
                incident_id=str(state["anomaly"]["anomaly_id"]),
                root_cause_category=category,
                root_cause_device_id=device,
                confidence=top.confidence,
                evidence=evidence,
                alternatives_ruled_out=tuple(
                    RuledOut(category=s.category, reason=s.against or "scored lower")
                    for s in ranked[1:]
                ),
                summary=scores.summary,
                prompt_version=PROMPT_VERSION,
            )
            return {"report": report}

        graph = StateGraph(State)
        graph.add_node("context", context)
        graph.add_node("hypothesize", hypothesize)
        graph.add_node("gather", gather)
        graph.add_node("score", score)
        graph.add_node("report", decide_and_report)
        graph.add_edge(START, "context")
        graph.add_edge("context", "hypothesize")
        graph.add_conditional_edges(
            "hypothesize", lambda s: "score" if s["done"] else "gather", ["gather", "score"]
        )
        graph.add_conditional_edges(
            "gather", lambda s: "score" if s["done"] else "gather", ["gather", "score"]
        )
        graph.add_edge("score", "report")
        graph.add_edge("report", END)
        return graph.compile()


def _render_anomaly(anomaly: Mapping[str, Any]) -> str:
    keys = ("anomaly_id", "signal", "scope_device_id", "tick", "value", "baseline", "score")
    return "\n".join(f"- {k}: {anomaly.get(k)}" for k in keys)


def _render_hypotheses(hypotheses: Hypotheses) -> str:
    return "\n".join(
        f"{i}. {h.category} at {h.suspect_device_id or 'unknown device'}: {h.why}"
        for i, h in enumerate(hypotheses.ranked, start=1)
    )
