"""Faults: named bundles of primitives plus the answer an agent should reach."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from netsleuth.sandbox.engine.primitives import (
    DegradeLevels,
    EngineError,
    ScheduledEffect,
    TakeDown,
)
from netsleuth.sandbox.topology import Amplifier, Topology

RootCauseCategory = Literal[
    "amplifier_failure",
    "ingress_noise",
    "fiber_cut",
    "power_supply_failure",
    "config_change",
    "planned_maintenance",
    "peering_congestion",
    "commercial_power_outage",
    "capacity_congestion",
]

# The level a fault is graded at. Localization gets partial credit by distance from it.
GradedLevel = Literal[
    "amplifier",
    "node",
    "service_group",
    "cmts",
    "fiber_route",
    "power_supply",
    "power_area",
    "peering_link",
]

ActionName = Literal[
    "dispatch_tech",
    "dispatch_generator",
    "rollback_change",
    "change_modulation_profile",
    "reset_modems",
    "route_to_team",
    "open_capacity_ticket",
    "monitor",
    "no_action",
]

# Partial amplifier failure: downstream power drops behind it while modems push harder upstream.
PARTIAL_AMP_DS_DB = -8.0
PARTIAL_AMP_US_DB = 6.0


class CorrectAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ActionName
    target: str | None
    params: dict[str, str] = Field(default_factory=dict)


class Fault(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fault_id: str
    incident_id: str
    category: RootCauseCategory
    root_device_id: str
    graded_level: GradedLevel
    correct_action: CorrectAction
    effects: tuple[ScheduledEffect, ...]
    variant: str | None = None

    @property
    def start_tick(self) -> int:
        return min(e.at_tick for e in self.effects)


def amplifier_failure(
    topology: Topology,
    amp_id: str,
    *,
    at_tick: int,
    incident_id: str,
    partial: bool = False,
    fault_id: str | None = None,
) -> Fault:
    """F1. A full failure takes the amplifier down. A partial one degrades levels behind it."""
    if amp_id not in topology or not isinstance(topology[amp_id], Amplifier):
        raise EngineError(f"{amp_id} is not an amplifier")

    if partial:
        effect: TakeDown | DegradeLevels = DegradeLevels(
            scope_id=amp_id, ds_db=PARTIAL_AMP_DS_DB, us_db=PARTIAL_AMP_US_DB
        )
    else:
        effect = TakeDown(device_id=amp_id)

    return Fault(
        fault_id=fault_id or f"f1-{amp_id}-t{at_tick}",
        incident_id=incident_id,
        category="amplifier_failure",
        root_device_id=amp_id,
        graded_level="amplifier",
        correct_action=CorrectAction(
            action="dispatch_tech", target=amp_id, params={"work_type": "amp_repair"}
        ),
        effects=(ScheduledEffect(at_tick=at_tick, effect=effect),),
        variant="partial" if partial else "full",
    )
