from datetime import UTC, datetime

import pytest

from netsleuth.sandbox.engine import (
    AddUpstreamNoise,
    CutFiberRoute,
    Engine,
    EngineError,
    Fault,
    MaintenanceWindow,
    Restore,
    ScheduledEffect,
    TakeDown,
)
from netsleuth.sandbox.topology import Node, ServiceGroup, Topology, generate_topology

MIDNIGHT = datetime(2026, 9, 1, tzinfo=UTC)
TICKS_PER_HOUR = 12


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def engine_with(topo: Topology, *effects: ScheduledEffect) -> Engine:
    fault = Fault(
        fault_id="f",
        incident_id="i",
        category="fiber_cut",
        root_device_id="n/a",
        graded_level="node",
        correct_action={"action": "no_action", "target": None},
        effects=effects,
    )
    return Engine(topo, faults=[fault], seed=0, start=MIDNIGHT)


def at(tick: int, effect: object) -> ScheduledEffect:
    return ScheduledEffect.model_validate({"at_tick": tick, "effect": effect})


def subtree_and_self(topo: Topology, device_id: str) -> set[str]:
    return {device_id} | {d.device_id for d in topo.subtree(device_id)}


def test_restore_brings_a_device_and_its_plant_back(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    engine = engine_with(
        topo, at(0, TakeDown(device_id=node.device_id)), at(3, Restore(device_id=node.device_id))
    )

    assert node.device_id in engine.unreachable
    engine.run_until(3)
    assert engine.unreachable == frozenset()
    assert engine.state(node.device_id).status == "healthy"


def test_route_cut_takes_out_every_node_on_the_route(topo: Topology) -> None:
    route = topo.of_type(Node)[0].fiber_route
    on_route = [n for n in topo.of_type(Node) if n.fiber_route == route]
    engine = engine_with(topo, at(0, CutFiberRoute(route=route)))

    expected = set().union(*(subtree_and_self(topo, n.device_id) for n in on_route))
    assert engine.unreachable == expected
    assert all(engine.state(n.device_id).status == "healthy" for n in on_route)


def test_unknown_route_is_rejected(topo: Topology) -> None:
    with pytest.raises(EngineError, match="route-nowhere"):
        engine_with(topo, at(0, CutFiberRoute(route="route-nowhere")))


def test_noise_is_only_active_inside_its_daily_window(topo: Topology) -> None:
    sg = topo.of_type(ServiceGroup)[0]
    noise = AddUpstreamNoise(
        service_group_id=sg.device_id, snr_drop_db=10, start_hour=17, end_hour=23
    )
    engine = engine_with(topo, at(0, noise))

    engine.run_until(17 * TICKS_PER_HOUR - 1)
    assert engine.us_noise_db(sg.device_id) == 0.0
    engine.step()
    assert engine.us_noise_db(sg.device_id) == 10.0
    engine.run_until(23 * TICKS_PER_HOUR)
    assert engine.us_noise_db(sg.device_id) == 0.0


def test_noise_window_can_wrap_midnight(topo: Topology) -> None:
    sg = topo.of_type(ServiceGroup)[0]
    noise = AddUpstreamNoise(
        service_group_id=sg.device_id, snr_drop_db=6, start_hour=22, end_hour=2
    )
    engine = engine_with(topo, at(0, noise))

    assert engine.us_noise_db(sg.device_id) == 6.0  # 00:00
    engine.run_until(2 * TICKS_PER_HOUR)
    assert engine.us_noise_db(sg.device_id) == 0.0  # 02:00
    engine.run_until(22 * TICKS_PER_HOUR)
    assert engine.us_noise_db(sg.device_id) == 6.0  # 22:00


def test_noise_only_affects_its_own_service_group(topo: Topology) -> None:
    sg, other = topo.of_type(ServiceGroup)[:2]
    noise = AddUpstreamNoise(
        service_group_id=sg.device_id, snr_drop_db=5, start_hour=0, end_hour=24
    )
    engine = engine_with(topo, at(0, noise))

    assert engine.us_noise_db(other.device_id) == 0.0


def test_noise_must_target_a_service_group(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    noise = AddUpstreamNoise(
        service_group_id=node.device_id, snr_drop_db=5, start_hour=0, end_hour=24
    )

    with pytest.raises(EngineError, match="service group"):
        engine_with(topo, at(0, noise))


def test_maintenance_window_is_published_to_the_calendar(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    window = MaintenanceWindow(
        window_id="mw-1", scope_id=node.device_id, start_tick=20, end_tick=30
    )
    engine = engine_with(topo, at(4, window))

    assert [e.window_id for e in engine.calendar] == []  # nothing published yet
    engine.run_until(4)
    (entry,) = engine.calendar
    assert (entry.window_id, entry.scope_id, entry.start_tick, entry.end_tick) == (
        "mw-1",
        node.device_id,
        20,
        30,
    )
    assert entry.published_tick == 4


def test_maintenance_window_must_end_after_it_starts() -> None:
    with pytest.raises(ValueError):
        MaintenanceWindow(window_id="mw", scope_id="x", start_tick=10, end_tick=10)


def test_a_restore_scheduled_after_the_run_simply_never_fires(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    engine = engine_with(
        topo, at(0, TakeDown(device_id=node.device_id)), at(500, Restore(device_id=node.device_id))
    )

    engine.run_until(100)
    assert node.device_id in engine.unreachable
