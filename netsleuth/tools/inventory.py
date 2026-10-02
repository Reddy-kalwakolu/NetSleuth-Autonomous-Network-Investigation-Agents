"""The stored inventory, shared by the tools, the rules baseline and the agents.

It is loaded through the storage session, so it is the inventory as stored (drift and all), never
the simulator's true topology.
"""

import weakref
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from netsleuth.storage import StorageSession


@dataclass
class Inventory:
    parent: dict[str, str | None]
    device_type: dict[str, str]
    fiber_route: dict[str, str | None]
    children: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _modems: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, session: StorageSession) -> "Inventory":
        rows = session.query(
            "SELECT device_id, device_type, parent_id, fiber_route FROM topology_devices"
        )
        inventory = cls(
            parent=dict(zip(rows["device_id"], rows["parent_id"], strict=True)),
            device_type=dict(zip(rows["device_id"], rows["device_type"], strict=True)),
            fiber_route=dict(zip(rows["device_id"], rows["fiber_route"], strict=True)),
        )
        for device_id, parent_id in inventory.parent.items():
            if parent_id is not None:
                inventory.children[parent_id].append(device_id)
        return inventory

    def modems_under(self, device_id: str) -> frozenset[str]:
        if device_id not in self._modems:
            if self.device_type[device_id] == "modem":
                found = frozenset({device_id})
            else:
                found = frozenset().union(*(self.modems_under(c) for c in self.children[device_id]))
            self._modems[device_id] = found
        return self._modems[device_id]

    def highest_fully_affected(self, affected: set[str], ceiling: str, universe: set[str]) -> str:
        """Walk up from the common ancestor of ``affected`` while every modem (among
        ``universe``) under the next device up is affected too, stopping at ``ceiling``."""
        root = lowest_common_ancestor(affected, self.parent)
        while root != ceiling:
            up = self.parent[root]
            if up is None or not (self.modems_under(up) & universe) <= affected:
                break
            root = up
        return root

    @property
    def routes(self) -> set[str]:
        return {r for d, r in self.fiber_route.items() if r and self.device_type[d] == "node"}

    def nodes_on_route(self, route: str) -> list[str]:
        return sorted(
            d for d, r in self.fiber_route.items() if r == route and self.device_type[d] == "node"
        )


_CACHE: "weakref.WeakKeyDictionary[Any, Inventory]" = weakref.WeakKeyDictionary()


def inventory(session: StorageSession) -> Inventory:
    """The stored inventory, loaded once per session."""
    if session not in _CACHE:
        _CACHE[session] = Inventory.load(session)
    return _CACHE[session]


def offline_now(session: StorageSession) -> set[str]:
    """Modems the CMTS sees as offline at the session's latest tick."""
    rows = session.query(
        "SELECT modem_id FROM cm_status WHERE ts = (SELECT max(ts) FROM cm_status) AND NOT online"
    )
    return set(rows["modem_id"])


def lowest_common_ancestor(devices: set[str], parent: Mapping[str, str | None]) -> str:
    def chain(device_id: str) -> list[str]:
        path = [device_id]
        while (up := parent[path[-1]]) is not None:
            path.append(up)
        return path

    first, *rest = devices
    shared = set(chain(first))
    for device_id in rest:
        shared &= set(chain(device_id))
    return next(d for d in chain(first) if d in shared)
