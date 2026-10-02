from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from netsleuth.agents import PLAYBOOKS, PROMPT_VERSION, gather_context, load_prompt, scope_for
from netsleuth.eval import Case, FiberCutSpec, simulate_case
from netsleuth.storage import DuckDBSession, DuckDBStorage
from netsleuth.tools import rerun
from tests.support import AMP


@pytest.fixture
def amp_anomaly(after: DuckDBSession) -> Mapping[str, Any]:
    row = after.query(
        "SELECT * FROM anomaly_events WHERE scope_device_id = 'node-hub1-04' ORDER BY tick LIMIT 1"
    )
    return row.row(0, named=True)


def test_scope_resolves_node_service_group_and_route(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    scope = scope_for(after, amp_anomaly)

    assert (scope.node, scope.service_group, scope.route) == (
        "node-hub1-04",
        "sg-hub1-c1-2",
        "route-hub1-2",
    )


def test_context_runs_prechecks_and_the_blast_radius(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    context = gather_context(after, amp_anomaly)

    tools = {f.tool for f in context.findings}
    assert {
        "get_maintenance_windows",
        "get_service_group_health",
        "get_cm_events",
        "summarize_modem_health",
    } <= tools
    assert context.blast["dark_root"] == AMP
    assert context.blast["route_peers_dark"] == []


def test_every_finding_reruns_to_the_same_result(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    for finding in gather_context(after, amp_anomaly).findings:
        assert rerun(after, finding.query_ref) == finding


def test_every_playbook_check_runs_for_a_real_scope(
    after: DuckDBSession, amp_anomaly: Mapping[str, Any]
) -> None:
    from netsleuth.tools import call_tool

    scope = scope_for(after, amp_anomaly)
    for category, checks in PLAYBOOKS.items():
        for check in checks:
            assert call_tool(after, check.tool, check.args(scope)).summary, (
                f"{category} {check.name}"
            )


def test_prompts_are_versioned_and_load() -> None:
    assert PROMPT_VERSION == "investigation-v1"
    for name in ("system", "hypothesize", "gather", "score", "single_prompt"):
        assert len(load_prompt(name)) > 100, name


def test_route_cut_shows_dark_route_peers(tmp_path: Path) -> None:
    case = Case(case_id="route", ticks=30, faults=[FiberCutSpec(route="route-hub1-1", at_tick=10)])
    sim = simulate_case(case, tmp_path / "d", tmp_path / "g")
    as_of = sim.engine.time_of(29)
    with DuckDBStorage(tmp_path / "d").session("route", as_of) as session:
        anomaly = {"anomaly_id": "a", "scope_device_id": "node-hub1-01", "ts": as_of}

        blast = gather_context(session, anomaly).blast

    assert blast["dark_root"] == "node-hub1-01"
    assert blast["route_peers_dark"] == ["node-hub1-03", "node-hub1-05", "node-hub1-07"]
