import math

import pytest

from netsleuth.sandbox.engine import (
    Engine,
    EngineError,
    Fault,
    ScheduledEffect,
    UtilityOutage,
)
from netsleuth.sandbox.engine.faults import CorrectAction
from netsleuth.sandbox.topology import Topology, generate_topology
from netsleuth.sandbox.topology.models import Modem, PowerSupply

SUPPLY = "ps-hub1-node01-1"  # 206 minutes of battery, in pa-hub1-3-3 (dev network, seed 0)


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def outage(area: str, at_tick: int, duration_ticks: int) -> Fault:
    return Fault(
        fault_id="power",
        incident_id="inc-power",
        category="commercial_power_outage",
        root_device_id=SUPPLY,
        graded_level="power_supply",
        correct_action=CorrectAction(action="monitor", target=None),
        effects=(
            ScheduledEffect(
                at_tick=at_tick,
                effect=UtilityOutage(power_area=area, duration_ticks=duration_ticks),
            ),
        ),
    )


def area_of(topo: Topology, supply_id: str) -> str:
    supply = topo[supply_id]
    assert isinstance(supply, PowerSupply)
    return supply.power_area


def first_to_drain(topo: Topology, area: str) -> PowerSupply:
    return min(
        (p for p in topo.of_type(PowerSupply) if p.power_area == area),
        key=lambda p: p.battery_runtime_min,
    )


def test_homes_in_an_out_area_lose_their_modems_and_nothing_else(topo: Topology) -> None:
    area = area_of(topo, SUPPLY)
    engine = Engine(topo, [outage(area, 5, 10)], seed=0)
    in_area = {m.device_id for m in topo.of_type(Modem) if m.power_area == area}

    engine.run_until(5)

    assert in_area <= engine.unreachable
    assert not any(isinstance(topo[d], Modem) and d not in in_area for d in engine.unreachable)
    assert engine.area_out(area)


def test_supplies_ride_through_a_short_outage_on_battery(topo: Topology) -> None:
    area = area_of(topo, SUPPLY)
    weakest = first_to_drain(topo, area)
    short = math.floor(weakest.battery_runtime_min / engine_tick()) - 2
    engine = Engine(topo, [outage(area, 5, short)], seed=0)

    engine.run_until(5 + short - 1)
    state = engine.power_supply_state(weakest.device_id)

    assert state.on_battery and not state.ac_ok
    assert 0 < state.battery_min_left < weakest.battery_runtime_min
    assert not set(weakest.feeds) & engine.unreachable


def test_a_long_outage_drains_the_battery_and_takes_the_actives_down(topo: Topology) -> None:
    area = area_of(topo, SUPPLY)
    weakest = first_to_drain(topo, area)
    drains_at = 5 + math.ceil(weakest.battery_runtime_min / engine_tick())
    engine = Engine(topo, [outage(area, 5, 120)], seed=0)

    engine.run_until(drains_at - 1)
    assert not set(weakest.feeds) & engine.unreachable

    engine.run_until(drains_at)
    state = engine.power_supply_state(weakest.device_id)
    assert state.battery_min_left == 0 and not state.on_battery
    assert set(weakest.feeds) <= engine.unreachable
    behind = {d.device_id for a in weakest.feeds for d in topo.subtree(a)}
    assert behind <= engine.unreachable


def test_power_returns_actives_come_back_and_the_battery_recharges(topo: Topology) -> None:
    area = area_of(topo, SUPPLY)
    weakest = first_to_drain(topo, area)
    engine = Engine(topo, [outage(area, 5, 60)], seed=0)

    engine.run_until(65)
    state = engine.power_supply_state(weakest.device_id)
    assert state.ac_ok and not state.on_battery
    assert not engine.area_out(area)
    assert not set(weakest.feeds) & engine.unreachable
    assert not engine.unreachable

    engine.run_until(65 + 12)
    assert engine.power_supply_state(weakest.device_id).battery_min_left > state.battery_min_left


def test_power_events_record_the_outage_and_the_restore(topo: Topology) -> None:
    area = area_of(topo, SUPPLY)
    engine = Engine(topo, [outage(area, 5, 10)], seed=0)

    engine.run_until(20)

    assert engine.power_events == ((5, area, "outage_start"), (15, area, "restored"))


def test_an_unknown_power_area_is_rejected(topo: Topology) -> None:
    with pytest.raises(EngineError, match="pa-nowhere"):
        Engine(topo, [outage("pa-nowhere", 5, 10)], seed=0)


def engine_tick() -> int:
    return 5
