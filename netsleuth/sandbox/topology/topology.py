"""An in-memory topology with tree and power lookups."""

from collections.abc import Iterable, Iterator
from typing import TypeVar

from netsleuth.sandbox.topology.models import (
    ALLOWED_PARENTS,
    Amplifier,
    Device,
    Node,
    PowerSupply,
)

T = TypeVar("T", bound=Device)


class TopologyError(ValueError):
    """The devices don't form a valid topology."""


class Topology:
    """The true network. Devices keep the order they were added in, which keeps output stable."""

    def __init__(self, devices: Iterable[Device]) -> None:
        self._devices: dict[str, Device] = {}
        self._children: dict[str, list[str]] = {}
        self._power_supply_of: dict[str, str] = {}

        for device in devices:
            if device.device_id in self._devices:
                raise TopologyError(f"duplicate device id {device.device_id}")
            self._devices[device.device_id] = device
            self._children[device.device_id] = []

        for device in self._devices.values():
            self._check_parent(device)
            if device.parent_id is not None:
                self._children[device.parent_id].append(device.device_id)
            if isinstance(device, PowerSupply):
                self._register_feeds(device)

    def _check_parent(self, device: Device) -> None:
        allowed = ALLOWED_PARENTS[device.device_type]
        if device.parent_id is None:
            if None not in allowed:
                raise TopologyError(f"{device.device_id} needs a parent")
            return
        parent = self._devices.get(device.parent_id)
        if parent is None:
            raise TopologyError(f"{device.device_id} has unknown parent {device.parent_id}")
        if parent.device_type not in allowed:
            raise TopologyError(
                f"{device.device_id} ({device.device_type}) can't hang off "
                f"{parent.device_id} ({parent.device_type})"
            )

    def _register_feeds(self, ps: PowerSupply) -> None:
        for active_id in ps.feeds:
            active = self._devices.get(active_id)
            if not isinstance(active, (Node, Amplifier)):
                raise TopologyError(f"{ps.device_id} feeds {active_id}, which is not an active")
            if active_id in self._power_supply_of:
                raise TopologyError(f"{active_id} is fed by more than one power supply")
            self._power_supply_of[active_id] = ps.device_id

    def __len__(self) -> int:
        return len(self._devices)

    def __iter__(self) -> Iterator[Device]:
        return iter(self._devices.values())

    def __getitem__(self, device_id: str) -> Device:
        return self._devices[device_id]

    def __contains__(self, device_id: object) -> bool:
        return device_id in self._devices

    def of_type(self, device_class: type[T]) -> list[T]:
        return [d for d in self._devices.values() if isinstance(d, device_class)]

    def parent(self, device_id: str) -> Device | None:
        parent_id = self._devices[device_id].parent_id
        return None if parent_id is None else self._devices[parent_id]

    def children(self, device_id: str) -> list[Device]:
        return [self._devices[c] for c in self._children[device_id]]

    def ancestors(self, device_id: str) -> list[Device]:
        """Parents up to the root, nearest first."""
        chain: list[Device] = []
        parent = self.parent(device_id)
        while parent is not None:
            chain.append(parent)
            parent = self.parent(parent.device_id)
        return chain

    def subtree(self, device_id: str) -> list[Device]:
        """Everything below a device, not including the device itself."""
        found: list[Device] = []
        stack = list(reversed(self._children[device_id]))
        while stack:
            current = self._devices[stack.pop()]
            found.append(current)
            stack.extend(reversed(self._children[current.device_id]))
        return found

    def power_supply_for(self, active_id: str) -> PowerSupply | None:
        ps_id = self._power_supply_of.get(active_id)
        if ps_id is None:
            return None
        ps = self._devices[ps_id]
        assert isinstance(ps, PowerSupply)
        return ps
