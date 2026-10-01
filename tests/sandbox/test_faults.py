import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from netsleuth.sandbox.engine import (
    AddUpstreamNoise,
    CutFiberRoute,
    Engine,
    EngineError,
    MaintenanceWindow,
    Restore,
    TakeDown,
    fiber_cut,
    ingress_noise,
    planned_maintenance,
    write_ground_truth,
)
from netsleuth.sandbox.topology import Amplifier, Node, ServiceGroup, Topology, generate_topology


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def test_route_cut_is_graded_at_the_route(topo: Topology) -> None:
    route = topo.of_type(Node)[0].fiber_route

    fault = fiber_cut(topo, route=route, at_tick=5, incident_id="i")

    assert fault.category == "fiber_cut"
    assert fault.graded_level == "fiber_route"
    assert fault.root_device_id == route
    assert fault.variant == "route"
    assert fault.correct_action.action == "dispatch_tech"
    assert fault.correct_action.target == route
    assert fault.correct_action.params == {"work_type": "fiber_repair"}
    (scheduled,) = fault.effects
    assert scheduled.effect == CutFiberRoute(route=route)


def test_node_cut_is_graded_at_the_node(topo: Topology) -> None:
    node = topo.of_type(Node)[0]

    fault = fiber_cut(topo, node_id=node.device_id, at_tick=5, incident_id="i")

    assert fault.graded_level == "node"
    assert fault.root_device_id == node.device_id
    assert fault.variant == "node"
    assert fault.effects[0].effect == TakeDown(device_id=node.device_id)


def test_fiber_cut_needs_exactly_one_target(topo: Topology) -> None:
    node = topo.of_type(Node)[0]

    with pytest.raises(EngineError):
        fiber_cut(topo, at_tick=0, incident_id="i")
    with pytest.raises(EngineError):
        fiber_cut(topo, route=node.fiber_route, node_id=node.device_id, at_tick=0, incident_id="i")
    with pytest.raises(EngineError, match="route-nowhere"):
        fiber_cut(topo, route="route-nowhere", at_tick=0, incident_id="i")


def test_ingress_noise_lands_on_the_nodes_service_group(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    sg = topo.parent(node.device_id)
    assert isinstance(sg, ServiceGroup)

    fault = ingress_noise(topo, node.device_id, at_tick=0, incident_id="i", snr_drop_db=12)

    assert fault.category == "ingress_noise"
    assert fault.graded_level == "node"
    assert fault.root_device_id == node.device_id
    assert fault.correct_action.params == {"work_type": "ingress_sweep"}
    assert fault.effects[0].effect == AddUpstreamNoise(
        service_group_id=sg.device_id, snr_drop_db=12, start_hour=17, end_hour=23
    )


def test_ingress_noise_rejects_a_non_node(topo: Topology) -> None:
    amp = topo.of_type(Amplifier)[0]

    with pytest.raises(EngineError):
        ingress_noise(topo, amp.device_id, at_tick=0, incident_id="i")


def test_planned_maintenance_publishes_then_works_then_restores(topo: Topology) -> None:
    node = topo.of_type(Node)[0]

    fault = planned_maintenance(topo, node.device_id, start_tick=20, end_tick=32, incident_id="i")

    assert fault.category == "planned_maintenance"
    assert fault.graded_level == "node"
    assert fault.correct_action.action == "no_action"
    assert fault.start_tick == 20  # the work, not the publication, is when it starts
    ticks_and_kinds = [(e.at_tick, e.effect.kind) for e in fault.effects]
    assert ticks_and_kinds == [(0, "maintenance_window"), (20, "take_down"), (32, "restore")]
    assert isinstance(fault.effects[0].effect, MaintenanceWindow)
    assert fault.effects[2].effect == Restore(device_id=node.device_id)


def test_maintenance_cannot_be_published_after_it_starts(topo: Topology) -> None:
    node = topo.of_type(Node)[0]

    with pytest.raises(EngineError):
        planned_maintenance(
            topo, node.device_id, start_tick=5, end_tick=10, incident_id="i", publish_tick=6
        )


def test_ground_truth_uses_the_window_start_for_maintenance(topo: Topology, tmp_path: Path) -> None:
    node = topo.of_type(Node)[0]
    fault = planned_maintenance(topo, node.device_id, start_tick=24, end_tick=36, incident_id="i")
    engine = Engine(topo, [fault], seed=0, start=datetime(2026, 9, 1, tzinfo=UTC))

    record = json.loads(write_ground_truth(engine, "r", tmp_path).read_text(encoding="utf-8"))

    assert record["faults"][0]["start_tick"] == 24
    assert record["faults"][0]["start_time"] == "2026-09-01T02:00:00+00:00"
