from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from netsleuth.diagnosis import Diagnosis, DiagnosisCategory
from netsleuth.eval import (
    AmplifierFailureSpec,
    Case,
    CustomFaultSpec,
    FiberCutSpec,
    IngressNoiseSpec,
    PlannedMaintenanceSpec,
    System,
    fault_scopes,
    run_case,
)
from netsleuth.sandbox.engine import CorrectAction, ScheduledEffect, TakeDown
from netsleuth.sandbox.topology import Node, ServiceGroup, Topology, generate_topology
from netsleuth.storage import StorageSession


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def answer(category: DiagnosisCategory, device: str | None) -> System:
    """A stand in system that always gives the same answer, to test scoring alone."""

    def system(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
        return Diagnosis(
            incident_id=str(anomaly["anomaly_id"]),
            root_cause_category=category,
            root_cause_device_id=device,
        )

    return system


def test_scopes_cover_every_node_in_the_service_groups_a_fault_touches(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    sg = topo.parent(node.device_id)
    assert isinstance(sg, ServiceGroup)
    siblings = {c.device_id for c in topo.children(sg.device_id) if isinstance(c, Node)}

    assert fault_scopes(topo, node.device_id) == siblings | {sg.device_id}
    assert {n.device_id for n in topo.of_type(Node) if n.fiber_route == node.fiber_route} <= (
        fault_scopes(topo, node.fiber_route)
    )


def test_sibling_node_anomalies_from_ingress_are_not_false_alarms(
    topo: Topology, tmp_path: Path
) -> None:
    node = topo.of_type(Node)[0]
    case = Case(
        case_id="ingress", ticks=288, faults=[IngressNoiseSpec(node_id=node.device_id, at_tick=0)]
    )

    result = run_case(case, answer("ingress_noise", node.device_id), tmp_path / "d", tmp_path / "g")

    assert result.faults[0].detected
    assert result.false_alarms == 0


def test_route_cut_claims_every_node_anomaly(topo: Topology, tmp_path: Path) -> None:
    route = topo.of_type(Node)[0].fiber_route
    case = Case(case_id="route", ticks=30, faults=[FiberCutSpec(route=route, at_tick=10)])

    result = run_case(case, answer("fiber_cut", route), tmp_path / "d", tmp_path / "g")

    (score,) = result.faults
    assert score.detected
    assert score.location_score == 1.0
    assert result.false_alarms == 0


def test_naming_one_node_on_the_route_earns_half(topo: Topology, tmp_path: Path) -> None:
    node = topo.of_type(Node)[0]
    case = Case(case_id="half", ticks=30, faults=[FiberCutSpec(route=node.fiber_route, at_tick=10)])

    result = run_case(case, answer("fiber_cut", node.device_id), tmp_path / "d", tmp_path / "g")

    assert result.faults[0].location_score == 0.5


def test_overlapping_faults_at_the_same_tick_do_not_crash(topo: Topology, tmp_path: Path) -> None:
    node = topo.of_type(Node)[0]
    amp_on_node = next(d for d in topo.subtree(node.device_id) if d.device_type == "amplifier")
    case = Case(
        case_id="both",
        ticks=30,
        faults=[
            FiberCutSpec(node_id=node.device_id, at_tick=10),
            AmplifierFailureSpec(amp_id=amp_on_node.device_id, at_tick=10),
        ],
    )

    result = run_case(case, answer("fiber_cut", node.device_id), tmp_path / "d", tmp_path / "g")

    assert [s.detected for s in result.faults] == [True, False]
    assert result.false_alarms == 0


def test_a_later_fault_on_the_same_node_gets_its_own_anomaly(
    topo: Topology, tmp_path: Path
) -> None:
    node = topo.of_type(Node)[0]
    amps = [d for d in topo.children(node.device_id) if d.device_type == "amplifier"]
    case = Case(
        case_id="later",
        ticks=48,
        faults=[
            AmplifierFailureSpec(amp_id=amps[0].device_id, at_tick=5),
            AmplifierFailureSpec(amp_id=amps[1].device_id, at_tick=35),
        ],
    )

    result = run_case(case, answer("amplifier_failure", None), tmp_path / "d", tmp_path / "g")

    assert [s.detected for s in result.faults] == [True, True]


def test_anomalies_outside_every_fault_are_false_alarms(topo: Topology, tmp_path: Path) -> None:
    # The answer key names one node while the effect darkens a node in another service group, so
    # the only anomaly belongs to no fault.
    claimed_node = topo.of_type(Node)[0]
    dark_node = next(
        n
        for n in topo.of_type(Node)
        if topo.parent(n.device_id) != topo.parent(claimed_node.device_id)
    )
    case = Case(
        case_id="stray",
        ticks=30,
        faults=[
            CustomFaultSpec(
                category="fiber_cut",
                root_device_id=claimed_node.device_id,
                graded_level="node",
                correct_action=CorrectAction(action="no_action", target=None),
                effects=(
                    ScheduledEffect(at_tick=10, effect=TakeDown(device_id=dark_node.device_id)),
                ),
            )
        ],
    )

    result = run_case(case, answer("fiber_cut", None), tmp_path / "d", tmp_path / "g")

    assert not result.faults[0].detected
    assert result.false_alarms == 1


def test_evening_ingress_is_not_swallowed_by_a_later_sibling_failure(
    topo: Topology, tmp_path: Path
) -> None:
    node01 = topo.of_type(Node)[0]
    sg = topo.parent(node01.device_id)
    assert isinstance(sg, ServiceGroup)
    sibling = next(c for c in topo.children(sg.device_id) if c.device_id != node01.device_id)
    sibling_amp = next(d for d in topo.subtree(sibling.device_id) if d.device_type == "amplifier")
    case = Case(
        case_id="evening",
        ticks=288,
        faults=[
            IngressNoiseSpec(node_id=node01.device_id, at_tick=0),
            AmplifierFailureSpec(amp_id=sibling_amp.device_id, at_tick=100),
        ],
    )

    result = run_case(
        case, answer("ingress_noise", node01.device_id), tmp_path / "d", tmp_path / "g"
    )

    assert [s.detected for s in result.faults] == [True, True]


def test_a_finished_maintenance_window_does_not_hide_later_false_alarms(
    topo: Topology, tmp_path: Path
) -> None:
    node03 = next(n for n in topo.of_type(Node) if n.device_id == "node-hub1-03")
    node04 = next(n for n in topo.of_type(Node) if n.device_id == "node-hub1-04")
    elsewhere = next(
        n for n in topo.of_type(Node) if topo.parent(n.device_id) != topo.parent(node03.device_id)
    )
    case = Case(
        case_id="after",
        ticks=48,
        faults=[
            PlannedMaintenanceSpec(node_id=node03.device_id, start_tick=10, end_tick=20),
            # The answer key names a node elsewhere, so node 04 going dark belongs to no fault.
            CustomFaultSpec(
                category="fiber_cut",
                root_device_id=elsewhere.device_id,
                graded_level="node",
                correct_action=CorrectAction(action="no_action", target=None),
                effects=(ScheduledEffect(at_tick=40, effect=TakeDown(device_id=node04.device_id)),),
            ),
        ],
    )

    result = run_case(
        case, answer("planned_maintenance", node03.device_id), tmp_path / "d", tmp_path / "g"
    )

    assert result.false_alarms == 1


def test_a_fault_above_node_level_claims_every_node_beneath_it(topo: Topology) -> None:
    from netsleuth.sandbox.topology import Cmts

    sg = topo.of_type(ServiceGroup)[0]
    sg_nodes = {c.device_id for c in topo.children(sg.device_id) if isinstance(c, Node)}
    cmts = topo.of_type(Cmts)[0]
    cmts_nodes = {d.device_id for d in topo.subtree(cmts.device_id) if isinstance(d, Node)}

    assert fault_scopes(topo, sg.device_id) == sg_nodes | {sg.device_id}
    assert cmts_nodes <= fault_scopes(topo, cmts.device_id)
    assert not {n.device_id for n in topo.of_type(Node)} - cmts_nodes & fault_scopes(
        topo, cmts.device_id
    )


def test_a_power_supply_claims_the_nodes_it_feeds(topo: Topology) -> None:
    from netsleuth.sandbox.topology.models import PowerSupply

    supply = topo.of_type(PowerSupply)[0]
    fed_nodes = {
        a.device_id
        if isinstance(a := topo[active], Node)
        else next(p.device_id for p in topo.ancestors(active) if isinstance(p, Node))
        for active in supply.feeds
    }

    assert fed_nodes <= fault_scopes(topo, supply.device_id)


def test_a_device_with_no_node_near_it_claims_the_whole_network(topo: Topology) -> None:
    from netsleuth.sandbox.topology.models import PeeringLink

    link = topo.of_type(PeeringLink)[0]

    assert {n.device_id for n in topo.of_type(Node)} <= fault_scopes(topo, link.device_id)


def test_location_outside_the_tree_scores_exact_or_nothing(topo: Topology) -> None:
    from netsleuth.eval.metrics import location_score
    from netsleuth.sandbox.topology.models import PowerSupply

    supply = topo.of_type(PowerSupply)[0].device_id
    node = topo.of_type(Node)[0].device_id

    assert location_score(topo, supply, supply) == 1.0
    assert location_score(topo, node, supply) == 0.0
    assert location_score(topo, supply, node) == 0.0


def test_a_service_group_level_fault_runs_end_to_end(topo: Topology, tmp_path: Path) -> None:
    sg = topo.of_type(ServiceGroup)[0]
    nodes = [c.device_id for c in topo.children(sg.device_id) if isinstance(c, Node)]
    case = Case(
        case_id="sg-level",
        ticks=30,
        faults=[
            CustomFaultSpec(
                category="config_change",
                root_device_id=sg.device_id,
                graded_level="service_group",
                correct_action=CorrectAction(action="no_action", target=None),
                effects=tuple(
                    ScheduledEffect(at_tick=10, effect=TakeDown(device_id=n)) for n in nodes
                ),
            )
        ],
    )

    result = run_case(case, answer("config_change", sg.device_id), tmp_path / "d", tmp_path / "g")

    (score,) = result.faults
    assert score.detected
    assert (score.category_score, score.location_score) == (1.0, 1.0)
