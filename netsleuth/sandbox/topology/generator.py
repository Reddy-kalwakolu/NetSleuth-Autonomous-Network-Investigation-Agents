"""Seeded topology generator.

The same size and seed always give the same topology. Every random choice goes through one
``random.Random`` instance, in a fixed order.

Two things deliberately cut across the tree, because real faults do:

* Fiber routes. Several nodes share a route, and the nodes on a route come from different
  service groups, so a route cut hits nodes with no common parent below the hub.
* Power areas. These are cells of a geographic grid. A node's homes spread over several cells,
  so a utility outage darkens part of one node and part of another.
"""

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from netsleuth.config import TopologySize
from netsleuth.sandbox.topology.models import (
    Amplifier,
    Backbone,
    Cmts,
    Device,
    Hub,
    Modem,
    Node,
    PeeringLink,
    PowerSupply,
    ServiceGroup,
    Tap,
)
from netsleuth.sandbox.topology.topology import Topology


@dataclass(frozen=True)
class SizeSpec:
    hubs: int
    cmts_per_hub: int
    service_groups_per_cmts: int
    nodes_per_service_group: int


SIZES: dict[TopologySize, SizeSpec] = {
    "dev": SizeSpec(hubs=1, cmts_per_hub=2, service_groups_per_cmts=2, nodes_per_service_group=2),
    "eval": SizeSpec(hubs=2, cmts_per_hub=2, service_groups_per_cmts=5, nodes_per_service_group=1),
    "scale": SizeSpec(hubs=4, cmts_per_hub=5, service_groups_per_cmts=5, nodes_per_service_group=2),
}

PEERING_LINKS = (("transit-a", 100), ("transit-b", 100), ("cdn-c", 40))
CITIES = ("Riverton", "Lakewood", "Fairview", "Cedar Falls")
CMTS_MODELS = (("vendor-a", "cmts-a100"), ("vendor-b", "cmts-b200"))
AMP_VENDORS = ("vendor-a", "vendor-c")

# split: (weight, upstream channels)
SPLITS: dict[Literal["sub", "mid", "high"], tuple[float, int]] = {
    "sub": (0.5, 4),
    "mid": (0.3, 6),
    "high": (0.2, 8),
}
DS_CHANNELS = 32
SERVICE_GROUPS_PER_LINE_CARD = 2

# model: (weight, DOCSIS version, firmware versions)
MODEM_MODELS: dict[str, tuple[float, Literal["3.0", "3.1"], tuple[str, ...]]] = {
    "dx-3100": (0.25, "3.0", ("1.0.4", "1.1.2")),
    "dx-3200": (0.45, "3.1", ("2.0.1", "2.1.0", "2.1.3")),
    "gw-5100": (0.30, "3.1", ("5.4.2", "5.5.0")),
}

HUB_AREA_KM = 8.0
NODE_MARGIN_KM = 2.5  # keeps a node's plant inside the hub area
POWER_AREA_KM = 1.0

HOMES_PASSED_TARGET = (250, 492)  # the last tap can add up to 7 more, so homes stay under 500
TAP_PORTS = (2, 4, 8)
TAP_SPREAD_KM = 0.12
HOME_SPREAD_KM = 0.04
TAKE_RATE = 0.67  # share of homes passed that have a modem

LEGS_PER_NODE = (2, 4)
MAX_CASCADE = (2, 5)  # N+2 to N+5
AMP_SPACING_KM = (0.25, 0.4)
AMP_REPORTS_SHARE = 0.15

ACTIVES_PER_POWER_SUPPLY = 6
BATTERY_RUNTIME_MIN = (120, 240)
POWER_SUPPLY_OFFSET_KM = 0.05

BACKBONE_ID = "bb"


def generate_topology(size: TopologySize, seed: int) -> Topology:
    return Topology(_Builder(random.Random(seed)).build(SIZES[size]))


class _Builder:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.devices: list[Device] = []
        self._used_macs: set[str] = set()
        self._used_customers: set[str] = set()

    def build(self, spec: SizeSpec) -> list[Device]:
        self.devices.append(Backbone(device_id=BACKBONE_ID, parent_id=None))
        for i, (partner, capacity) in enumerate(PEERING_LINKS, start=1):
            self.devices.append(
                PeeringLink(
                    device_id=f"peer-{i}",
                    parent_id=BACKBONE_ID,
                    partner=partner,
                    capacity_gbps=capacity,
                )
            )
        for h in range(1, spec.hubs + 1):
            self._build_hub(h, spec)
        return self.devices

    def _build_hub(self, h: int, spec: SizeSpec) -> None:
        hub_id = f"hub{h}"
        self.devices.append(
            Hub(
                device_id=hub_id,
                parent_id=BACKBONE_ID,
                region=f"region-{h}",
                city=CITIES[(h - 1) % len(CITIES)],
            )
        )
        nodes_in_hub = spec.cmts_per_hub * spec.service_groups_per_cmts
        nodes_in_hub *= spec.nodes_per_service_group
        routes = max(2, nodes_in_hub // 3)
        line_cards = math.ceil(spec.service_groups_per_cmts / SERVICE_GROUPS_PER_LINE_CARD)

        node_index = 0
        for c in range(1, spec.cmts_per_hub + 1):
            cmts_id = f"cmts-{hub_id}-{c}"
            vendor, model = self.rng.choice(CMTS_MODELS)
            self.devices.append(
                Cmts(
                    device_id=cmts_id,
                    parent_id=hub_id,
                    vendor=vendor,
                    model=model,
                    architecture="integrated",
                    line_cards=line_cards,
                )
            )
            for s in range(1, spec.service_groups_per_cmts + 1):
                sg_id = f"sg-{hub_id}-c{c}-{s}"
                split = self.rng.choices(list(SPLITS), weights=[w for w, _ in SPLITS.values()])[0]
                self.devices.append(
                    ServiceGroup(
                        device_id=sg_id,
                        parent_id=cmts_id,
                        line_card=(s - 1) // SERVICE_GROUPS_PER_LINE_CARD + 1,
                        us_channels=SPLITS[split][1],
                        ds_channels=DS_CHANNELS,
                        split=split,
                    )
                )
                for _ in range(spec.nodes_per_service_group):
                    # Neighbouring nodes in the tree land on different routes, so every
                    # route spans more than one service group.
                    route = f"route-{hub_id}-{node_index % routes + 1}"
                    node_index += 1
                    self._build_node(hub_id, sg_id, node_index, route)

    def _build_node(self, hub_id: str, sg_id: str, index: int, route: str) -> None:
        rng = self.rng
        node_id = f"node-{hub_id}-{index:02d}"
        tag = f"{hub_id}-node{index:02d}"
        x = rng.uniform(NODE_MARGIN_KM, HUB_AREA_KM - NODE_MARGIN_KM)
        y = rng.uniform(NODE_MARGIN_KM, HUB_AREA_KM - NODE_MARGIN_KM)

        # Amplifier legs. The first leg sets the node's cascade depth.
        legs = rng.randint(*LEGS_PER_NODE)
        max_depth = rng.randint(*MAX_CASCADE)
        lengths = [max_depth] + [rng.randint(1, max_depth) for _ in range(legs - 1)]
        amps: list[Amplifier] = []
        for leg, length in enumerate(lengths):
            angle = 2 * math.pi * leg / legs + rng.uniform(-0.3, 0.3)
            ax, ay, parent = x, y, node_id
            for position in range(1, length + 1):
                step = rng.uniform(*AMP_SPACING_KM)
                ax += step * math.cos(angle)
                ay += step * math.sin(angle)
                amp = Amplifier(
                    device_id=f"amp-{tag}-a{len(amps) + 1}",
                    parent_id=parent,
                    cascade_position=position,
                    vendor=rng.choice(AMP_VENDORS),
                    reports_telemetry=rng.random() < AMP_REPORTS_SHARE,
                    x_km=ax,
                    y_km=ay,
                )
                amps.append(amp)
                parent = amp.device_id

        actives = [(node_id, x, y)] + [(a.device_id, a.x_km, a.y_km) for a in amps]

        # Taps go on the node and the amplifiers until the node passes enough homes.
        target = rng.randint(*HOMES_PASSED_TARGET)
        taps: list[Tap] = []
        homes_passed = 0
        while homes_passed < target:
            parent_id, px, py = rng.choice(actives)
            ports = rng.choice(TAP_PORTS)
            taps.append(
                Tap(
                    device_id=f"tap-{tag}-t{len(taps) + 1}",
                    parent_id=parent_id,
                    port_count=ports,
                    x_km=px + rng.uniform(-TAP_SPREAD_KM, TAP_SPREAD_KM),
                    y_km=py + rng.uniform(-TAP_SPREAD_KM, TAP_SPREAD_KM),
                )
            )
            homes_passed += ports

        self.devices.append(
            Node(
                device_id=node_id,
                parent_id=sg_id,
                architecture="analog",
                homes_passed=homes_passed,
                fiber_route=route,
                x_km=x,
                y_km=y,
            )
        )
        self.devices.extend(amps)
        self.devices.extend(taps)
        for tap in taps:
            for _ in range(tap.port_count):
                if rng.random() < TAKE_RATE:
                    self.devices.append(self._modem(hub_id, tap))

        for j in range(0, len(actives), ACTIVES_PER_POWER_SUPPLY):
            chunk = actives[j : j + ACTIVES_PER_POWER_SUPPLY]
            _, fx, fy = chunk[0]
            px, py = fx + POWER_SUPPLY_OFFSET_KM, fy + POWER_SUPPLY_OFFSET_KM
            self.devices.append(
                PowerSupply(
                    device_id=f"ps-{tag}-{j // ACTIVES_PER_POWER_SUPPLY + 1}",
                    parent_id=None,
                    battery_runtime_min=rng.randint(*BATTERY_RUNTIME_MIN),
                    feeds=tuple(active_id for active_id, _, _ in chunk),
                    power_area=_power_area(hub_id, px, py),
                    x_km=px,
                    y_km=py,
                )
            )

    def _modem(self, hub_id: str, tap: Tap) -> Modem:
        rng = self.rng
        model = rng.choices(list(MODEM_MODELS), weights=[w for w, _, _ in MODEM_MODELS.values()])[0]
        _, docsis, firmwares = MODEM_MODELS[model]
        x = tap.x_km + rng.uniform(-HOME_SPREAD_KM, HOME_SPREAD_KM)
        y = tap.y_km + rng.uniform(-HOME_SPREAD_KM, HOME_SPREAD_KM)
        return Modem(
            device_id=self._unique(self._used_macs, lambda: f"cm-{rng.getrandbits(48):012x}"),
            parent_id=tap.device_id,
            model=model,
            firmware=rng.choice(firmwares),
            docsis_version=docsis,
            customer_id=self._unique(
                self._used_customers, lambda: f"cust-{rng.randint(10**7, 10**8 - 1)}"
            ),
            power_area=_power_area(hub_id, x, y),
            x_km=x,
            y_km=y,
        )

    @staticmethod
    def _unique(used: set[str], make: Callable[[], str]) -> str:
        # Modem and customer ids are random so they don't give away where a modem sits.
        value = make()
        while value in used:
            value = make()
        used.add(value)
        return value


def _power_area(hub_id: str, x_km: float, y_km: float) -> str:
    return f"pa-{hub_id}-{math.floor(x_km / POWER_AREA_KM)}-{math.floor(y_km / POWER_AREA_KM)}"
