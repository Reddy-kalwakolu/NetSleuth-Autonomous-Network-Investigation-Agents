from collections.abc import Sequence
from datetime import UTC, datetime

import polars as pl
import pytest

from netsleuth.sandbox.engine import Engine, Fault, ingress_noise, planned_maintenance
from netsleuth.sandbox.telemetry import TelemetryGenerator, TickTelemetry
from netsleuth.sandbox.topology import Modem, Node, ServiceGroup, Topology, generate_topology

MIDNIGHT = datetime(2026, 9, 1, tzinfo=UTC)
EVENING = 18 * 12  # 18:00 in 5 minute ticks


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def frames(
    topo: Topology, faults: Sequence[Fault], ticks: int, seed: int = 1
) -> list[TickTelemetry]:
    engine = Engine(topo, faults, seed=seed, start=MIDNIGHT)
    gen = TelemetryGenerator(engine)
    out = [gen.emit()]
    for _ in range(ticks - 1):
        engine.step()
        out.append(gen.emit())
    return out


def concat(fs: list[TickTelemetry], table: str) -> pl.DataFrame:
    tables: list[pl.DataFrame] = [getattr(f, table) for f in fs]
    return pl.concat(tables)


def modems_on(topo: Topology, device_id: str) -> set[str]:
    return {d.device_id for d in topo.subtree(device_id) if isinstance(d, Modem)}


@pytest.fixture(scope="module")
def noisy_day(topo: Topology) -> tuple[Node, list[TickTelemetry], list[TickTelemetry]]:
    node = topo.of_type(Node)[0]
    fault = ingress_noise(topo, node.device_id, at_tick=0, incident_id="i", snr_drop_db=10)
    return node, frames(topo, [], 288), frames(topo, [fault], 288)


def test_healthy_network_has_few_t3_and_no_t4(
    noisy_day: tuple[Node, list[TickTelemetry], list[TickTelemetry]], topo: Topology
) -> None:
    _, healthy, _ = noisy_day
    events = concat(healthy, "cm_events")

    assert (events["event"] == "T4").sum() == 0
    assert (events["event"] == "T3").sum() / (288 * len(topo.of_type(Modem))) < 0.001


def test_ingress_drops_low_channel_snr_only_inside_its_window(
    noisy_day: tuple[Node, list[TickTelemetry], list[TickTelemetry]], topo: Topology
) -> None:
    node, healthy, noisy = noisy_day
    sg = topo.parent(node.device_id)
    assert isinstance(sg, ServiceGroup)

    def snr(fs: list[TickTelemetry], tick: int, channel: str) -> float:
        rows = fs[tick].sg_channels.filter(
            (pl.col("sg_id") == sg.device_id) & (pl.col("channel") == channel)
        )
        return float(rows["us_snr_db"][0])

    assert snr(noisy, EVENING, "us1") - snr(healthy, EVENING, "us1") == pytest.approx(-10.0)
    assert snr(noisy, EVENING, "us3") - snr(healthy, EVENING, "us3") == pytest.approx(-2.5)
    assert snr(noisy, 12, "us1") == pytest.approx(snr(healthy, 12, "us1"))


def test_ingress_raises_t3_across_the_service_group(
    noisy_day: tuple[Node, list[TickTelemetry], list[TickTelemetry]], topo: Topology
) -> None:
    node, _, noisy = noisy_day
    sg = topo.parent(node.device_id)
    assert sg is not None
    in_sg = modems_on(topo, sg.device_id)
    evening = concat(noisy[EVENING : EVENING + 12], "cm_events").filter(pl.col("event") == "T3")

    inside = evening.filter(pl.col("modem_id").is_in(list(in_sg))).height
    outside = evening.height - inside
    assert inside > 20 * max(outside, 1)


def test_ingress_lowers_upstream_mer_at_the_cmts(
    noisy_day: tuple[Node, list[TickTelemetry], list[TickTelemetry]], topo: Topology
) -> None:
    node, healthy, noisy = noisy_day
    sg = topo.parent(node.device_id)
    assert sg is not None
    ids = list(modems_on(topo, sg.device_id))
    before = healthy[EVENING].cm_status.filter(pl.col("modem_id").is_in(ids))["us_rx_mer_db"]
    after = noisy[EVENING].cm_status.filter(pl.col("modem_id").is_in(ids))["us_rx_mer_db"]

    assert (after - before).mean() == pytest.approx(-8.0)


def test_upstream_codeword_counters_only_rise_and_rise_faster_under_noise(
    noisy_day: tuple[Node, list[TickTelemetry], list[TickTelemetry]], topo: Topology
) -> None:
    node, healthy, noisy = noisy_day
    sg = topo.parent(node.device_id)
    assert sg is not None

    def total(fs: list[TickTelemetry]) -> pl.DataFrame:
        return (
            concat(fs, "sg_channels")
            .filter(pl.col("sg_id") == sg.device_id)
            .sort("channel", "tick")
        )

    rises = (
        total(noisy)
        .with_columns(pl.col("us_uncorrectable_cw_total").diff().over("channel").alias("d"))
        .drop_nulls("d")
    )
    assert (rises["d"] >= 0).all()
    noisy_total = int(total(noisy)["us_uncorrectable_cw_total"].max())  # type: ignore[arg-type]
    healthy_total = int(total(healthy)["us_uncorrectable_cw_total"].max())  # type: ignore[arg-type]
    assert noisy_total > 10 * max(healthy_total, 1)


def test_maintenance_window_appears_once_when_published(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    fault = planned_maintenance(
        topo, node.device_id, start_tick=10, end_tick=20, incident_id="i", publish_tick=2
    )
    fs = frames(topo, [fault], 25)
    rows = concat(fs, "maintenance")

    assert rows.height == 1
    row = rows.row(0, named=True)
    assert row["tick"] == 2
    assert row["scope_device_id"] == node.device_id
    assert row["starts_at"] == datetime(2026, 9, 1, 0, 50, tzinfo=UTC)
    assert row["ends_at"] == datetime(2026, 9, 1, 1, 40, tzinfo=UTC)


def test_modems_reboot_after_an_outage_with_t4_and_fresh_counters(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    fault = planned_maintenance(topo, node.device_id, start_tick=3, end_tick=9, incident_id="i")
    fs = frames(topo, [fault], 13)
    behind = modems_on(topo, node.device_id)

    t4 = concat(fs, "cm_events").filter(pl.col("event") == "T4")
    assert set(t4["modem_id"]) == behind
    assert set(t4["tick"]) == {9}
    # Tick 9 is an RF poll. After a reboot the total holds only that poll's errors, so for
    # many modems it is below the total from tick 0. Without the reset it never could be.
    ids = list(behind)
    before = fs[0].cm_rf.filter(pl.col("modem_id").is_in(ids)).sort("modem_id")
    after = fs[9].cm_rf.filter(pl.col("modem_id").is_in(ids)).sort("modem_id")
    assert (after["corrected_cw_total"] < before["corrected_cw_total"]).sum() > len(ids) // 4


def test_unreachable_modems_log_nothing(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    dark = planned_maintenance(topo, node.device_id, start_tick=0, end_tick=500, incident_id="i")
    noise = ingress_noise(
        topo, node.device_id, at_tick=0, incident_id="j", start_hour=0, end_hour=24
    )
    events = concat(frames(topo, [dark, noise], 24), "cm_events")

    assert set(events["modem_id"]).isdisjoint(modems_on(topo, node.device_id))
