"""Baselines the agents are compared against: a rules engine and a single prompt."""

from netsleuth.baselines.rules import rules_baseline
from netsleuth.baselines.single_prompt import SinglePromptBaseline

__all__ = ["SinglePromptBaseline", "rules_baseline"]
