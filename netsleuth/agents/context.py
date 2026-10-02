"""The context every LLM based system starts from: prechecks and the blast radius.

Both run in code, so the agent and the single prompt baseline see exactly the same facts.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from netsleuth.storage import StorageSession
from netsleuth.tools import ToolResult, call_tool, inventory, offline_now


@dataclass(frozen=True)
class Scope:
    anomaly_scope: str
    node: str
    service_group: str
    route: str


@dataclass
class Context:
    scope: Scope
    findings: list[ToolResult] = field(default_factory=list)
    blast: dict[str, Any] = field(default_factory=dict)


def scope_for(session: StorageSession, anomaly: Mapping[str, Any]) -> Scope:
    inv = inventory(session)
    scope = str(anomaly["scope_device_id"])
    if inv.device_type[scope] == "node":
        node = scope
    else:
        node = sorted(c for c in inv.children[scope] if inv.device_type[c] == "node")[0]
    sg = inv.parent[node]
    assert sg is not None
    return Scope(
        anomaly_scope=scope, node=node, service_group=sg, route=inv.fiber_route[node] or ""
    )


def gather_context(session: StorageSession, anomaly: Mapping[str, Any]) -> Context:
    scope = scope_for(session, anomaly)
    context = Context(scope=scope)
    for name, args in (
        ("get_maintenance_windows", {"scope_id": scope.node}),
        ("get_service_group_health", {"device_id": scope.node}),
        ("get_cm_events", {"scope_id": scope.service_group}),
        ("summarize_modem_health", {"scope_id": scope.anomaly_scope}),
    ):
        context.findings.append(call_tool(session, name, args))
    context.blast = _blast_radius(session, scope)
    return context


def _blast_radius(session: StorageSession, scope: Scope) -> dict[str, Any]:
    inv = inventory(session)
    offline = offline_now(session)
    modems = set(inv.modems_under(scope.node))
    dark = offline & modems
    dark_root = inv.highest_fully_affected(dark, scope.node, modems) if dark else None
    peers = [n for n in inv.nodes_on_route(scope.route) if n != scope.node]
    peers_dark = [n for n in peers if inv.modems_under(n) and inv.modems_under(n) <= offline]
    return {
        "node_modems": len(modems),
        "dark_modems": len(dark),
        "dark_root": dark_root,
        "dark_root_type": inv.device_type[dark_root] if dark_root else None,
        "route": scope.route,
        "route_peers_dark": peers_dark,
    }


def render_findings(findings: list[ToolResult]) -> str:
    return "\n".join(f"- [{f.query_ref}] {f.summary}" for f in findings)


def render_blast(blast: Mapping[str, Any]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in blast.items())


def named_in(device_id: str, text: str) -> bool:
    """Whether ``device_id`` appears in ``text`` as a whole ID, not as part of a longer one."""
    if not device_id:
        return False
    return re.search(rf"(?<![\w-]){re.escape(device_id)}(?![\w-])", text) is not None
