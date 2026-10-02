"""Constants and a scripted model shared by the tool, agent, baseline, harness and CLI tests."""

from collections.abc import Callable

from pydantic import BaseModel

AMP = "amp-hub1-node04-a1"  # 166 modems behind it in the dev network, topology seed 0
FAULT_TICK = 20
TICKS = 40

Responder = Callable[[type[BaseModel], str, str], BaseModel]
