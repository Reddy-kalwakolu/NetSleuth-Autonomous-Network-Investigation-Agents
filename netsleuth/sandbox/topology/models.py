"""Device types in the simulated HFC network.

The tree runs backbone > hub > CMTS > service group > fiber node > amplifiers > taps > modems.
Peering links hang off the backbone. Power supplies sit outside the tree: they feed actives
(nodes and amplifiers) and belong to a geographic power area, like the homes do.

Coordinates (``x_km``, ``y_km``) are local to each hub.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

DeviceType = Literal[
    "backbone",
    "peering_link",
    "hub",
    "cmts",
    "service_group",
    "node",
    "amplifier",
    "tap",
    "modem",
    "power_supply",
]

# Which device types each type may hang off. None means it has no parent.
ALLOWED_PARENTS: dict[DeviceType, frozenset[DeviceType | None]] = {
    "backbone": frozenset({None}),
    "peering_link": frozenset({"backbone"}),
    "hub": frozenset({"backbone"}),
    "cmts": frozenset({"hub"}),
    "service_group": frozenset({"cmts"}),
    "node": frozenset({"service_group"}),
    "amplifier": frozenset({"node", "amplifier"}),
    "tap": frozenset({"node", "amplifier"}),
    "modem": frozenset({"tap"}),
    "power_supply": frozenset({None}),
}


class _Device(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str
    parent_id: str | None


class Backbone(_Device):
    device_type: Literal["backbone"] = "backbone"


class PeeringLink(_Device):
    device_type: Literal["peering_link"] = "peering_link"
    partner: str
    capacity_gbps: int


class Hub(_Device):
    device_type: Literal["hub"] = "hub"
    region: str
    city: str


class Cmts(_Device):
    device_type: Literal["cmts"] = "cmts"
    vendor: str
    model: str
    architecture: Literal["integrated", "virtual"]
    line_cards: int


class ServiceGroup(_Device):
    device_type: Literal["service_group"] = "service_group"
    line_card: int
    us_channels: int
    ds_channels: int
    split: Literal["sub", "mid", "high"]


class Node(_Device):
    device_type: Literal["node"] = "node"
    architecture: Literal["analog"]
    homes_passed: int
    fiber_route: str
    x_km: float
    y_km: float


class Amplifier(_Device):
    device_type: Literal["amplifier"] = "amplifier"
    cascade_position: int
    vendor: str
    reports_telemetry: bool
    x_km: float
    y_km: float


class Tap(_Device):
    device_type: Literal["tap"] = "tap"
    port_count: int
    x_km: float
    y_km: float


class Modem(_Device):
    device_type: Literal["modem"] = "modem"
    model: str
    firmware: str
    docsis_version: Literal["3.0", "3.1"]
    customer_id: str
    power_area: str
    x_km: float
    y_km: float


class PowerSupply(_Device):
    device_type: Literal["power_supply"] = "power_supply"
    battery_runtime_min: int
    feeds: tuple[str, ...]
    power_area: str
    x_km: float
    y_km: float


Device = (
    Backbone
    | PeeringLink
    | Hub
    | Cmts
    | ServiceGroup
    | Node
    | Amplifier
    | Tap
    | Modem
    | PowerSupply
)
