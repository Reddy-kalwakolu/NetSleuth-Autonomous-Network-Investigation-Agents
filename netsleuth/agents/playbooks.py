"""Checks per suspected cause. The model picks from these by name; code runs them."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from netsleuth.agents.context import Scope


@dataclass(frozen=True)
class Check:
    name: str
    tool: str
    args: Callable[[Scope], dict[str, Any]]


PLAYBOOKS: dict[str, tuple[Check, ...]] = {
    "amplifier_failure": (
        Check("amplifier.subtree", "get_subtree", lambda s: {"device_id": s.node, "depth": 2}),
        Check(
            "amplifier.health_2h",
            "summarize_modem_health",
            lambda s: {"scope_id": s.node, "hours": 2},
        ),
    ),
    "fiber_cut": (
        Check(
            "fiber.route_nodes", "find_devices", lambda s: {"attr": "fiber_route", "value": s.route}
        ),
        Check("fiber.route_health", "summarize_modem_health", lambda s: {"scope_id": s.route}),
        Check(
            "fiber.optical",
            "get_metric_series",
            lambda s: {"device_id": s.node, "metric": "optical_rx_dbm"},
        ),
    ),
    "ingress_noise": (
        Check("ingress.events_by_node", "get_cm_events", lambda s: {"scope_id": s.service_group}),
        Check(
            "ingress.utilization",
            "get_metric_series",
            lambda s: {"device_id": s.service_group, "metric": "us_util_pct", "hours": 3},
        ),
    ),
    "planned_maintenance": (
        Check(
            "maintenance.service_group",
            "get_maintenance_windows",
            lambda s: {"scope_id": s.service_group},
        ),
    ),
}


# Offered when no suspected cause has a playbook of its own, such as power, which has no data yet.
# They tell the outage shapes apart: the whole route, the node's amplifier legs, and the node over
# a longer window.
GENERAL_CHECKS: tuple[Check, ...] = (
    PLAYBOOKS["fiber_cut"][1],
    PLAYBOOKS["amplifier_failure"][0],
    PLAYBOOKS["amplifier_failure"][1],
)


def checks_for(categories: Iterable[str]) -> list[Check]:
    seen: dict[str, Check] = {}
    for category in categories:
        for check in PLAYBOOKS.get(category, ()):
            seen.setdefault(check.name, check)
    return list(seen.values()) or list(GENERAL_CHECKS)
