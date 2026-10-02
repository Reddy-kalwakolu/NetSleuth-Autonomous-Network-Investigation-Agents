"""Constants and a scripted model shared by the tool, agent, baseline, harness and CLI tests."""

from collections.abc import Callable

from pydantic import BaseModel

from netsleuth.agents.schemas import (
    EvidenceRequest,
    Hypotheses,
    Hypothesis,
    ScoredHypothesis,
    Scores,
)
from netsleuth.diagnosis import DiagnosisCategory

AMP = "amp-hub1-node04-a1"  # 166 modems behind it in the dev network, topology seed 0
FAULT_TICK = 20
TICKS = 40

Responder = Callable[[type[BaseModel], str, str], BaseModel]


def oracle(
    category: DiagnosisCategory, device: str | None, confidence: float = 0.9, more: bool = False
) -> Responder:
    """A scripted model that always lands on one answer, asks for the first check on the menu,
    and cites every fact it was shown."""

    def respond(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        if schema is Hypotheses:
            return Hypotheses(
                ranked=[Hypothesis(category=category, suspect_device_id=device, why="facts")]
            )
        if schema is EvidenceRequest:
            menu = [
                line[2:]
                for line in user.split("Menu:\n", 1)[1].splitlines()
                if line.startswith("- ")
            ]
            return EvidenceRequest(checks=menu[:1], done=not more)
        refs = [line.split("] ", 1)[0][3:] for line in user.splitlines() if line.startswith("- [")]
        return Scores(
            scored=[
                ScoredHypothesis(
                    category=category, device_id=device, confidence=confidence, supporting=refs
                )
            ],
            summary=f"{category} at {device}.",
        )

    return respond
