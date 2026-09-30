from collections.abc import Iterator

import polars as pl
import pytest

from netsleuth.sandbox.engine import (
    DegradeLevels,
    Engine,
    Fault,
    ScheduledEffect,
    amplifier_failure,
)
from netsleuth.sandbox.telemetry import TelemetryGenerator, TickTelemetry
from netsleuth.sandbox.topology import (
    Amplifier,
    Modem,
    Node,
    ServiceGroup,
    Topology,
    generate_topology,
)

TICKS_PER_DAY = 288


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def first_amp_with_amp_below(topo: Topology) -> Amplifier:
    for amp in topo.of_type(Amplifier):
        if any(isinstance(d, Amplifier) for d in topo.children(amp.device_id)):
            return amp
    raise AssertionError("dev topology should have a cascade deeper than one")


def run(engine: Engine, ticks: int) -> Iterator[TickTelemetry]:
    gen = TelemetryGenerator(engine)
    yield gen.emit()
    for _ in range(ticks):
        engine.step()
        yield gen.emit()


def collect(engine: Engine, ticks: int) -> dict[str, pl.DataFrame]:
    frames = list(run(engine, ticks))
    return {
        name: pl.concat([getattr(f, name) for f in frames])
        for name in ("cm_status", "cm_rf", "sg_channels", "sg_status", "node_optical")
    }


@pytest.fixture(scope="module")
def healthy_day(topo: Topology) -> dict[str, pl.DataFrame]:
    return collect(Engine(topo, faults=[], seed=1), TICKS_PER_DAY - 1)


def modems_behind(topo: Topology, device_id: str) -> set[str]:
    return {d.device_id for d in topo.subtree(device_id) if isinstance(d, Modem)}


# ---------- healthy ranges ----------


def test_healthy_modem_rf_stays_in_range(healthy_day: dict[str, pl.DataFrame]) -> None:
    rf = healthy_day["cm_rf"]

    assert rf["ds_rx_power_dbmv"].is_between(-7, 7).all()
    assert rf["ds_mer_db"].is_between(36, 42).all()
    assert rf["us_tx_power_dbmv"].is_between(35, 49).all()


def test_healthy_cmts_view_stays_in_range(healthy_day: dict[str, pl.DataFrame]) -> None:
    status = healthy_day["cm_status"]

    assert status["online"].all()
    assert status["us_rx_power_dbmv"].is_between(-2, 2).all()
    assert (status["us_rx_mer_db"] >= 30).all()


def test_healthy_channels_and_nodes_stay_in_range(healthy_day: dict[str, pl.DataFrame]) -> None:
    assert (healthy_day["sg_channels"]["us_snr_db"] >= 30).all()
    assert healthy_day["node_optical"]["optical_rx_dbm"].is_between(-3, 2).all()
    sg = healthy_day["sg_status"]
    assert sg["us_util_pct"].is_between(0, 100).all()
    assert sg["ds_util_pct"].is_between(0, 100).all()
    assert (sg["modems_online"] == sg["modems_total"]).all()


def test_traffic_peaks_in_the_evening(healthy_day: dict[str, pl.DataFrame]) -> None:
    sg = healthy_day["sg_status"].with_columns(pl.col("ts").dt.hour().alias("hour"))
    evening = sg.filter(pl.col("hour") == 21)["ds_util_pct"].mean()
    night = sg.filter(pl.col("hour") == 4)["ds_util_pct"].mean()

    assert isinstance(evening, float) and isinstance(night, float)
    assert evening > night + 10


# ---------- shape of the output ----------


def test_every_modem_appears_in_cmts_view_every_tick(topo: Topology) -> None:
    frames = list(run(Engine(topo, faults=[], seed=1), 5))
    modems = len(topo.of_type(Modem))

    assert all(f.cm_status.height == modems for f in frames)


def test_rf_is_polled_every_third_tick(topo: Topology) -> None:
    frames = list(run(Engine(topo, faults=[], seed=1), 6))
    modems = len(topo.of_type(Modem))

    assert [f.cm_rf.height for f in frames] == [modems, 0, 0, modems, 0, 0, modems]


def test_rows_carry_the_tick_timestamp(topo: Topology) -> None:
    engine = Engine(topo, faults=[], seed=1)
    frames = list(run(engine, 3))

    assert frames[3].sg_status["ts"].unique().to_list() == [engine.time_of(3)]


def test_same_seed_gives_identical_telemetry(topo: Topology) -> None:
    a = collect(Engine(topo, faults=[], seed=5), 6)
    b = collect(Engine(topo, faults=[], seed=5), 6)

    assert all(a[name].equals(b[name]) for name in a)


# ---------- counters ----------


def test_codeword_counters_only_go_up(healthy_day: dict[str, pl.DataFrame]) -> None:
    rf = healthy_day["cm_rf"].sort("modem_id", "tick")
    steps = rf.with_columns(
        pl.col("corrected_cw_total").diff().over("modem_id").alias("d_corr"),
        pl.col("uncorrectable_cw_total").diff().over("modem_id").alias("d_unc"),
    ).drop_nulls("d_corr")

    assert steps.height > 0
    assert (steps["d_corr"] >= 0).all()
    assert (steps["d_unc"] >= 0).all()
    assert (steps["d_corr"] > 0).any()


# ---------- the in band rule ----------


def test_unreachable_modems_send_no_rf_rows(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = Engine(
        topo, faults=[amplifier_failure(topo, amp.device_id, at_tick=0, incident_id="i")], seed=1
    )
    frame = TelemetryGenerator(engine).emit()

    dark = modems_behind(topo, amp.device_id)
    assert dark
    assert set(frame.cm_rf["modem_id"]).isdisjoint(dark)
    assert frame.cm_rf.height == len(topo.of_type(Modem)) - len(dark)


def test_cmts_still_reports_unreachable_modems_as_offline(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = Engine(
        topo, faults=[amplifier_failure(topo, amp.device_id, at_tick=0, incident_id="i")], seed=1
    )
    status = TelemetryGenerator(engine).emit().cm_status

    dark = status.filter(pl.col("modem_id").is_in(list(modems_behind(topo, amp.device_id))))
    assert dark.height == len(modems_behind(topo, amp.device_id))
    assert not dark["online"].any()
    assert dark["us_rx_power_dbmv"].null_count() == dark.height


def test_service_group_counts_offline_modems(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = Engine(
        topo, faults=[amplifier_failure(topo, amp.device_id, at_tick=0, incident_id="i")], seed=1
    )
    sg = TelemetryGenerator(engine).emit().sg_status

    sg_id = next(a for a in topo.ancestors(amp.device_id) if isinstance(a, ServiceGroup)).device_id
    row = sg.filter(pl.col("sg_id") == sg_id)
    assert row["modems_total"][0] - row["modems_online"][0] == len(
        modems_behind(topo, amp.device_id)
    )


def test_a_down_node_sends_no_optical_row(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    fault = Fault(
        fault_id="f",
        incident_id="i",
        category="fiber_cut",
        root_device_id=node.device_id,
        graded_level="node",
        correct_action={"action": "no_action", "target": None},
        effects=[
            ScheduledEffect(at_tick=0, effect={"kind": "take_down", "device_id": node.device_id})
        ],
    )
    frame = TelemetryGenerator(Engine(topo, faults=[fault], seed=1)).emit()

    assert node.device_id not in set(frame.node_optical["node_id"])
    assert frame.node_optical.height == len(topo.of_type(Node)) - 1


# ---------- fault effects ----------


def test_partial_amp_failure_shifts_levels_by_exactly_the_offset(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=0, incident_id="i", partial=True)
    base = TelemetryGenerator(Engine(topo, faults=[], seed=9)).emit().cm_rf
    hit = TelemetryGenerator(Engine(topo, faults=[f1], seed=9)).emit().cm_rf

    joined = base.join(hit, on="modem_id", suffix="_hit").with_columns(
        (pl.col("ds_rx_power_dbmv_hit") - pl.col("ds_rx_power_dbmv")).alias("ds_shift"),
        (pl.col("ds_mer_db_hit") - pl.col("ds_mer_db")).alias("mer_shift"),
    )
    behind = joined.filter(pl.col("modem_id").is_in(list(modems_behind(topo, amp.device_id))))
    elsewhere = joined.filter(~pl.col("modem_id").is_in(list(modems_behind(topo, amp.device_id))))

    assert ((behind["ds_shift"] + 8.0).abs() < 1e-9).all()
    assert (behind["mer_shift"] < 0).all()  # weaker signal, noisier decode
    assert (elsewhere["ds_shift"].abs() < 1e-9).all()


def test_upstream_loss_beyond_max_transmit_weakens_what_the_cmts_hears(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    fault = Fault(
        fault_id="f",
        incident_id="i",
        category="amplifier_failure",
        root_device_id=node.device_id,
        graded_level="node",
        correct_action={"action": "no_action", "target": None},
        effects=[
            ScheduledEffect(
                at_tick=0,
                effect=DegradeLevels(scope_id=node.device_id, ds_db=0.0, us_db=25.0),
            )
        ],
    )
    frame = TelemetryGenerator(Engine(topo, faults=[fault], seed=1)).emit()

    behind = list(modems_behind(topo, node.device_id))
    rf = frame.cm_rf.filter(pl.col("modem_id").is_in(behind))
    status = frame.cm_status.filter(pl.col("modem_id").is_in(behind))
    assert (rf["us_tx_power_dbmv"] <= 57.0 + 1.0).all()
    assert (status["us_rx_power_dbmv"] < -2).all()


def test_a_failure_mid_run_shows_up_from_that_tick(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = Engine(
        topo, faults=[amplifier_failure(topo, amp.device_id, at_tick=3, incident_id="i")], seed=1
    )
    frames = list(run(engine, 6))
    dark = modems_behind(topo, amp.device_id)

    polled_before, polled_after = frames[0].cm_rf, frames[3].cm_rf
    assert dark <= set(polled_before["modem_id"])
    assert set(polled_after["modem_id"]).isdisjoint(dark)
    assert [f.cm_status["online"].sum() for f in frames[2:4]] == [
        len(topo.of_type(Modem)),
        len(topo.of_type(Modem)) - len(dark),
    ]
