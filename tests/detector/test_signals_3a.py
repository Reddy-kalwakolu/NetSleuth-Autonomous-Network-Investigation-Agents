"""Detection of the milestone 3a events: utility outages, drained batteries, config pushes and
peering congestion."""

import math
from pathlib import Path

import polars as pl
import pytest

from netsleuth.detector import detect_anomalies
from netsleuth.eval import (
    Case,
    ConfigPushSpec,
    PeeringCongestionSpec,
    UtilityOutageSpec,
    simulate_case,
)
from netsleuth.eval.cases import FaultSpec
from netsleuth.sandbox.topology import ServiceGroup, Topology, generate_topology
from netsleuth.sandbox.topology.models import PeeringLink, PowerSupply
from netsleuth.storage import DuckDBStorage

AREA = "pa-hub1-3-3"


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def anomalies(tmp_path: Path, ticks: int, *faults: FaultSpec) -> pl.DataFrame:
    case = Case(case_id="c", ticks=ticks, faults=list(faults))
    sim = simulate_case(case, tmp_path / "d", tmp_path / "g")
    with DuckDBStorage(tmp_path / "d").session("c", sim.engine.time_of(ticks - 1)) as session:
        return detect_anomalies(session)


def test_a_congested_peering_link_raises_one_anomaly_at_evening_peak(
    tmp_path: Path, topo: Topology
) -> None:
    link = topo.of_type(PeeringLink)[0].device_id
    found = anomalies(tmp_path, 240, PeeringCongestionSpec(link_id=link, at_tick=0))

    peering = found.filter(pl.col("signal") == "peering_util")
    assert peering.select("scope_device_id", "tick").rows() == [(link, 19 * 12)]


def test_a_quiet_day_raises_no_peering_anomaly(tmp_path: Path) -> None:
    found = anomalies(tmp_path, 288)

    assert found.filter(pl.col("signal") == "peering_util").is_empty()


def test_a_utility_outage_shows_as_modems_dropping_where_the_homes_are(
    tmp_path: Path,
) -> None:
    found = anomalies(
        tmp_path, 30, UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=8)
    )

    offline = found.filter(pl.col("signal") == "share_offline")
    assert offline.height >= 1
    assert set(offline["tick"].to_list()) == {10}


def test_a_drained_battery_raises_a_fresh_anomaly_when_the_actives_drop(
    tmp_path: Path, topo: Topology
) -> None:
    supply = min(
        (p for p in topo.of_type(PowerSupply) if p.power_area == AREA),
        key=lambda p: (p.battery_runtime_min, p.device_id),
    )
    deadline = 10 + math.ceil(supply.battery_runtime_min / 5)
    first = supply.feeds[0]  # feeds start with the node, or with the amplifier nearest it
    fed_node = (
        first
        if first.startswith("node-")
        else next(a.device_id for a in topo.ancestors(first) if a.device_type == "node")
    )
    found = anomalies(
        tmp_path,
        deadline + 6,
        UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=deadline - 10 + 10),
    )

    at_deadline = found.filter(
        (pl.col("signal") == "share_offline")
        & (pl.col("scope_device_id") == fed_node)
        & (pl.col("tick") == deadline)
    )
    assert at_deadline.height == 1


def test_a_config_push_drops_snr_on_every_service_group_under_the_cmts(
    tmp_path: Path, topo: Topology
) -> None:
    cmts = "cmts-hub1-1"
    groups = {d.device_id for d in topo.subtree(cmts) if isinstance(d, ServiceGroup)}

    found = anomalies(tmp_path, 40, ConfigPushSpec(target_id=cmts, at_tick=30))

    snr = found.filter((pl.col("signal") == "sg_snr") & (pl.col("tick") == 30))
    assert set(snr["scope_device_id"].to_list()) == groups
