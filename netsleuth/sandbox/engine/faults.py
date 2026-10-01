"""Faults: named bundles of primitives plus the answer an agent should reach."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from netsleuth.sandbox.engine.primitives import (
    AddUpstreamNoise,
    CutFiberRoute,
    DegradeLevels,
    EngineError,
    MaintenanceWindow,
    Restore,
    ScheduledEffect,
    TakeDown,
)
from netsleuth.sandbox.topology import Amplifier, Node, ServiceGroup, Topology

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
    onset_tick: NonNegativeInt | None = None

    @property
    def start_tick(self) -> int:
        """When the fault starts to show. Defaults to its first effect."""
        if self.onset_tick is not None:
            return self.onset_tick
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
    _require(topology, amp_id, Amplifier, "an amplifier")

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


def fiber_cut(
    topology: Topology,
    *,
    at_tick: int,
    incident_id: str,
    route: str | None = None,
    node_id: str | None = None,
    fault_id: str | None = None,
) -> Fault:
    """F3. A cut on a whole fiber route, or on the fiber to one node."""
    if (route is None) == (node_id is None):
        raise EngineError("a fiber cut needs either a route or a node, not both")
    effect: CutFiberRoute | TakeDown
    if route is not None:
        if not any(n.fiber_route == route for n in topology.of_type(Node)):
            raise EngineError(f"no nodes on fiber route {route}")
        effect = CutFiberRoute(route=route)
        root: str = route
        level: GradedLevel = "fiber_route"
        variant = "route"
    else:
        assert node_id is not None
        _require(topology, node_id, Node, "a node")
        effect = TakeDown(device_id=node_id)
        root, level, variant = node_id, "node", "node"
    return Fault(
        fault_id=fault_id or f"f3-{root}-t{at_tick}",
        incident_id=incident_id,
        category="fiber_cut",
        root_device_id=root,
        graded_level=level,
        correct_action=CorrectAction(
            action="dispatch_tech", target=root, params={"work_type": "fiber_repair"}
        ),
        effects=(ScheduledEffect(at_tick=at_tick, effect=effect),),
        variant=variant,
    )


def ingress_noise(
    topology: Topology,
    node_id: str,
    *,
    at_tick: int,
    incident_id: str,
    snr_drop_db: float = 10.0,
    start_hour: int = 17,
    end_hour: int = 23,
    fault_id: str | None = None,
) -> Fault:
    """F2. Noise leaks in at one node and hurts the whole service group's upstream, mostly in
    the evening. The fix is a sweep of the node's plant."""
    _require(topology, node_id, Node, "a node")
    sg = topology.parent(node_id)
    assert isinstance(sg, ServiceGroup)
    return Fault(
        fault_id=fault_id or f"f2-{node_id}-t{at_tick}",
        incident_id=incident_id,
        category="ingress_noise",
        root_device_id=node_id,
        graded_level="node",
        correct_action=CorrectAction(
            action="dispatch_tech", target=node_id, params={"work_type": "ingress_sweep"}
        ),
        effects=(
            ScheduledEffect(
                at_tick=at_tick,
                effect=AddUpstreamNoise(
                    service_group_id=sg.device_id,
                    snr_drop_db=snr_drop_db,
                    start_hour=start_hour,
                    end_hour=end_hour,
                ),
            ),
        ),
    )


def planned_maintenance(
    topology: Topology,
    node_id: str,
    *,
    start_tick: int,
    end_tick: int,
    incident_id: str,
    publish_tick: int = 0,
    fault_id: str | None = None,
) -> Fault:
    """D1. Planned work on a node, published to the calendar ahead of time. It looks exactly
    like an outage, and the right response is to do nothing."""
    _require(topology, node_id, Node, "a node")
    if publish_tick > start_tick:
        raise EngineError("a maintenance window has to be published before it starts")
    window_id = f"mw-{node_id}-t{start_tick}"
    return Fault(
        fault_id=fault_id or f"d1-{node_id}-t{start_tick}",
        incident_id=incident_id,
        category="planned_maintenance",
        root_device_id=node_id,
        graded_level="node",
        correct_action=CorrectAction(action="no_action", target=None),
        effects=(
            ScheduledEffect(
                at_tick=publish_tick,
                effect=MaintenanceWindow(
                    window_id=window_id,
                    scope_id=node_id,
                    start_tick=start_tick,
                    end_tick=end_tick,
                ),
            ),
            ScheduledEffect(at_tick=start_tick, effect=TakeDown(device_id=node_id)),
            ScheduledEffect(at_tick=end_tick, effect=Restore(device_id=node_id)),
        ),
        onset_tick=start_tick,
    )


def _require(topology: Topology, device_id: str, kind: type, label: str) -> None:
    if device_id not in topology or not isinstance(topology[device_id], kind):
        raise EngineError(f"{device_id} is not {label}")
