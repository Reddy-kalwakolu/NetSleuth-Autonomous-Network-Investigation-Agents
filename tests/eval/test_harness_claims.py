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
