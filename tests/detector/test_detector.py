from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from netsleuth.detector import detect_anomalies
from netsleuth.sandbox.engine import Engine, Fault, amplifier_failure
from netsleuth.sandbox.telemetry import TelemetryGenerator
from netsleuth.sandbox.topology import (
    Amplifier,
    Modem,
    Node,
    Topology,
    generate_topology,
    to_frames,
)
from netsleuth.storage import DuckDBStorage, RunWriter

START = datetime(2026, 9, 1, tzinfo=UTC)
TICKS = 48
FAULT_TICK = 20


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def simulate(topo: Topology, faults: Sequence[Fault], data_dir: Path, run_id: str) -> Engine:
    engine = Engine(topo, faults=faults, seed=4, start=START)
    gen = TelemetryGenerator(engine)
    with RunWriter(data_dir, run_id) as writer:
        devices, edges = to_frames(topo)
        writer.write_static("topology_devices", devices)
        writer.write_static("topology_edges", edges)
        writer.add_tick(gen.emit())
        for _ in range(TICKS - 1):
            engine.step()
            writer.add_tick(gen.emit())
    return engine


def detect(data_dir: Path, run_id: str, as_of: datetime) -> pl.DataFrame:
    with DuckDBStorage(data_dir).session(run_id, as_of) as session:
        return detect_anomalies(session)


def modems_behind(topo: Topology, device_id: str) -> int:
    return sum(isinstance(d, Modem) for d in topo.subtree(device_id))


def node_of(topo: Topology, device_id: str) -> str:
    return next(a.device_id for a in topo.ancestors(device_id) if isinstance(a, Node))


def smallest_and_largest_amps(topo: Topology) -> tuple[Amplifier, Amplifier]:
    amps = sorted(topo.of_type(Amplifier), key=lambda a: modems_behind(topo, a.device_id))
    return amps[0], amps[-1]


# ---------- the done when ----------


@pytest.mark.parametrize("which", ["smallest", "largest"])
def test_amplifier_failure_raises_one_anomaly_on_the_right_node(
    topo: Topology, tmp_path: Path, which: str
) -> None:
    smallest, largest = smallest_and_largest_amps(topo)
    amp = smallest if which == "smallest" else largest
    f1 = amplifier_failure(topo, amp.device_id, at_tick=FAULT_TICK, incident_id="inc-1")
    engine = simulate(topo, [f1], tmp_path, "run")

    anomalies = detect(tmp_path, "run", engine.time_of(TICKS - 1))

    assert anomalies.height == 1
    row = anomalies.row(0, named=True)
    assert row["scope_device_id"] == node_of(topo, amp.device_id)
    assert row["signal"] == "share_offline"
    assert row["ts"] == engine.time_of(FAULT_TICK)
    assert row["offline_modems"] == modems_behind(topo, amp.device_id)
    assert row["score"] > 0


# ---------- no false alarms, and the cutoff is respected ----------


def test_healthy_network_raises_nothing(topo: Topology, tmp_path: Path) -> None:
    engine = simulate(topo, [], tmp_path, "run")

    assert detect(tmp_path, "run", engine.time_of(TICKS - 1)).height == 0


def test_detector_only_sees_data_up_to_as_of(topo: Topology, tmp_path: Path) -> None:
    _, amp = smallest_and_largest_amps(topo)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=FAULT_TICK, incident_id="inc-1")
    engine = simulate(topo, [f1], tmp_path, "run")

    assert detect(tmp_path, "run", engine.time_of(FAULT_TICK - 1)).height == 0
    assert detect(tmp_path, "run", engine.time_of(FAULT_TICK)).height == 1


def test_two_failures_on_two_nodes_give_two_anomalies(topo: Topology, tmp_path: Path) -> None:
    smallest, largest = smallest_and_largest_amps(topo)
    assert node_of(topo, smallest.device_id) != node_of(topo, largest.device_id)
    faults = [
        amplifier_failure(topo, smallest.device_id, at_tick=10, incident_id="inc-1"),
        amplifier_failure(topo, largest.device_id, at_tick=30, incident_id="inc-2"),
    ]
    engine = simulate(topo, faults, tmp_path, "run")

    anomalies = detect(tmp_path, "run", engine.time_of(TICKS - 1)).sort("ts")

    assert anomalies["scope_device_id"].to_list() == [
        node_of(topo, smallest.device_id),
        node_of(topo, largest.device_id),
    ]
    assert anomalies["ts"].to_list() == [engine.time_of(10), engine.time_of(30)]


def test_partial_failure_is_not_an_outage(topo: Topology, tmp_path: Path) -> None:
    # Nothing goes offline in a partial failure, so share offline stays quiet. The RF signal
    # catches it instead.
    _, amp = smallest_and_largest_amps(topo)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=FAULT_TICK, incident_id="i", partial=True)
    engine = simulate(topo, [f1], tmp_path, "run")

    found = detect(tmp_path, "run", engine.time_of(TICKS - 1))
    assert set(found["signal"]) == {"rf_level_drop"}


# ---------- output shape ----------


def test_anomalies_can_be_stored_and_read_back_with_the_cutoff(
    topo: Topology, tmp_path: Path
) -> None:
    _, amp = smallest_and_largest_amps(topo)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=FAULT_TICK, incident_id="inc-1")
    engine = simulate(topo, [f1], tmp_path, "run")
    anomalies = detect(tmp_path, "run", engine.time_of(TICKS - 1))

    with RunWriter(tmp_path, "run") as writer:
        writer.append("anomaly_events", anomalies)

    with DuckDBStorage(tmp_path).session("run", engine.time_of(FAULT_TICK)) as session:
        stored = session.query("SELECT anomaly_id, scope_device_id FROM anomaly_events")
    with DuckDBStorage(tmp_path).session("run", engine.time_of(FAULT_TICK - 1)) as session:
        before = session.query("SELECT count(*) AS n FROM anomaly_events")

    assert stored["anomaly_id"].to_list() == anomalies["anomaly_id"].to_list()
    assert before["n"][0] == 0


def two_unrelated_amps_on_one_node(topo: Topology) -> tuple[Amplifier, Amplifier]:
    """Two amplifiers on the same node, neither behind the other."""
    for node in topo.of_type(Node):
        amps = [d for d in topo.subtree(node.device_id) if isinstance(d, Amplifier)]
        for a in amps:
            below_a = {d.device_id for d in topo.subtree(a.device_id)}
            for b in amps:
                below_b = {d.device_id for d in topo.subtree(b.device_id)}
                if a is not b and b.device_id not in below_a and a.device_id not in below_b:
                    return a, b
    raise AssertionError("dev topology should have a node with two separate legs")


def test_second_failure_on_an_already_dark_node_is_still_caught(
    topo: Topology, tmp_path: Path
) -> None:
    # Once the node's recent median catches up with the first outage, a fresh jump fires again.
    first, second = two_unrelated_amps_on_one_node(topo)
    faults = [
        amplifier_failure(topo, first.device_id, at_tick=5, incident_id="inc-1"),
        amplifier_failure(topo, second.device_id, at_tick=35, incident_id="inc-2"),
    ]
    engine = simulate(topo, faults, tmp_path, "run")

    anomalies = detect(tmp_path, "run", engine.time_of(TICKS - 1)).sort("ts")

    assert anomalies["ts"].to_list() == [engine.time_of(5), engine.time_of(35)]
    assert set(anomalies["scope_device_id"]) == {node_of(topo, first.device_id)}
    assert anomalies["offline_modems"][1] == modems_behind(topo, first.device_id) + modems_behind(
        topo, second.device_id
    )
