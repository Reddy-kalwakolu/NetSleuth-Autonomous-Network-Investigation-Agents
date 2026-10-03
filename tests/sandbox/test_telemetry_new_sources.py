"""Power supply status, utility power events, the change log, peering status and tickets."""

import math
from statistics import fmean

import polars as pl
import pytest

from netsleuth.sandbox.engine import (
    ConfigChange,
    CorrectAction,
    Engine,
    Fault,
    PeeringLoad,
    ScheduledEffect,
    TakeDown,
    UtilityOutage,
)
from netsleuth.sandbox.engine.primitives import AnyEffect
from netsleuth.sandbox.telemetry import TelemetryGenerator
from netsleuth.sandbox.topology import Topology, generate_topology
from netsleuth.sandbox.topology.models import Modem, PeeringLink, PowerSupply

AREA = "pa-hub1-3-3"
AMP = "amp-hub1-node04-a1"


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def run(
    topo: Topology, effects: list[tuple[int, AnyEffect]], ticks: int
) -> dict[str, pl.DataFrame]:
    fault = Fault(
        fault_id="f",
        incident_id="inc",
        category="config_change",
        root_device_id="x",
        graded_level="cmts",
        correct_action=CorrectAction(action="no_action", target=None),
        effects=tuple(ScheduledEffect(at_tick=t, effect=e) for t, e in effects),
    )
    engine = Engine(topo, [fault] if effects else [], seed=3)
    generator = TelemetryGenerator(engine)
    names = ("ps_status", "power_events", "change_log", "peering_status", "tickets", "sg_channels")
    frames: dict[str, list[pl.DataFrame]] = {n: [] for n in names}
    for tick in range(ticks):
        if tick:
            engine.step()
        out = generator.emit()
        for n in names:
            frames[n].append(getattr(out, n))
    return {n: pl.concat(parts, how="vertical_relaxed") for n, parts in frames.items()}


def weakest(topo: Topology) -> PowerSupply:
    return min(
        (p for p in topo.of_type(PowerSupply) if p.power_area == AREA),
        key=lambda p: p.battery_runtime_min,
    )


def test_a_supply_reports_its_battery_until_it_drains_then_goes_silent(topo: Topology) -> None:
    supply = weakest(topo)
    drains_at = 5 + math.ceil(supply.battery_runtime_min / 5)
    data = run(topo, [(5, UtilityOutage(power_area=AREA, duration_ticks=200))], drains_at + 5)
    rows = data["ps_status"].filter(pl.col("ps_id") == supply.device_id).sort("tick")

    before = rows.filter(pl.col("tick") < 5)
    during = rows.filter((pl.col("tick") > 5) & (pl.col("tick") < drains_at))
    assert before["ac_ok"].all() and not before["on_battery"].any()
    assert during["on_battery"].all()
    assert during["battery_min_left"].is_sorted(descending=True)
    assert max(rows["tick"].to_list()) < drains_at  # silent once its own node loses power


def test_every_supply_reports_every_tick_on_a_quiet_day(topo: Topology) -> None:
    data = run(topo, [], 4)

    assert data["ps_status"].height == 4 * len(topo.of_type(PowerSupply))
    assert data["power_events"].is_empty() and data["change_log"].is_empty()


def test_power_events_mark_the_outage_and_the_restore(topo: Topology) -> None:
    data = run(topo, [(5, UtilityOutage(power_area=AREA, duration_ticks=10))], 20)

    assert data["power_events"].select("tick", "power_area", "event").rows() == [
        (5, AREA, "outage_start"),
        (15, AREA, "restored"),
    ]


def test_a_config_push_is_logged_and_drops_snr_on_every_channel(topo: Topology) -> None:
    sg = "sg-hub1-c1-1"
    push = ConfigChange(change_id="chg-7", target_id=sg, snr_drop_db=8.0)
    data = run(topo, [(6, push)], 12)

    assert data["change_log"].select("tick", "change_id", "target_id").rows() == [(6, "chg-7", sg)]
    snr = data["sg_channels"].filter(pl.col("sg_id") == sg)
    per_channel = snr.group_by("channel").agg(
        (
            pl.col("us_snr_db").filter(pl.col("tick") < 6).mean()
            - pl.col("us_snr_db").filter(pl.col("tick") >= 6).mean()
        ).alias("drop")
    )
    assert min(per_channel["drop"].to_list()) > 7.0  # high channels too, unlike ingress


def test_peering_load_shows_as_utilization_latency_and_drops(topo: Topology) -> None:
    link = topo.of_type(PeeringLink)[0].device_id
    load = PeeringLoad(link_id=link, peak_util_pct=99, start_hour=0, end_hour=2)
    data = run(topo, [(0, load)], 36)  # midnight to 03:00
    rows = data["peering_status"].filter(pl.col("link_id") == link)

    busy = rows.filter(pl.col("tick") < 24)
    calm = rows.filter(pl.col("tick") >= 24)
    assert min(busy["util_pct"].to_list()) > 95
    assert max(calm["util_pct"].to_list()) < 80
    assert fmean(busy["latency_ms"].to_list()) > 2 * fmean(calm["latency_ms"].to_list())
    assert fmean(busy["drop_pct"].to_list()) > 0 and max(calm["drop_pct"].to_list()) == 0


def test_customers_behind_a_dead_amplifier_call_once_each(topo: Topology) -> None:
    behind = {d.device_id for d in topo.subtree(AMP) if isinstance(d, Modem)}
    data = run(topo, [(2, TakeDown(device_id=AMP))], 40)
    tickets = data["tickets"].filter(pl.col("kind") == "no_service")

    from_outage = tickets.filter(pl.col("modem_id").is_in(list(behind)))
    assert from_outage.height >= 0.2 * len(behind)
    assert from_outage["modem_id"].n_unique() == from_outage.height
    assert min(from_outage["tick"].to_list()) >= 4  # nobody calls the moment it drops


def test_homes_without_power_rarely_call(topo: Topology) -> None:
    homes = {m.device_id for m in topo.of_type(Modem) if m.power_area == AREA}
    behind = {d.device_id for d in topo.subtree(AMP) if isinstance(d, Modem)}
    unpowered = run(topo, [(2, UtilityOutage(power_area=AREA, duration_ticks=30))], 30)
    plant = run(topo, [(2, TakeDown(device_id=AMP))], 30)

    def share(data: dict[str, pl.DataFrame], modems: set[str]) -> float:
        t = data["tickets"].filter(pl.col("modem_id").is_in(list(modems)))
        return t.height / len(modems)

    assert share(unpowered, homes) < share(plant, behind) / 4


def test_peering_congestion_brings_slow_tickets_from_all_over(topo: Topology) -> None:
    link = topo.of_type(PeeringLink)[0].device_id
    load = PeeringLoad(link_id=link, peak_util_pct=99, start_hour=0, end_hour=4)
    data = run(topo, [(0, load)], 48)
    slow = data["tickets"].filter(pl.col("kind") == "slow_service")

    node_of = {
        m.device_id: next(
            a.device_id for a in topo.ancestors(m.device_id) if a.device_type == "node"
        )
        for m in topo.of_type(Modem)
    }
    assert slow.height >= 10
    assert len({node_of[m] for m in slow["modem_id"]}) >= 4


def test_a_quiet_network_still_gets_a_few_unrelated_tickets(topo: Topology) -> None:
    data = run(topo, [], 48)

    assert 0 < data["tickets"].height < 20
