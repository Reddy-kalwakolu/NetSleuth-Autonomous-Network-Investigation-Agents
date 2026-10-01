from pathlib import Path

import polars as pl
import pytest

from netsleuth.baselines import rules_baseline
from netsleuth.detector import detect_anomalies
from netsleuth.diagnosis import Diagnosis
from netsleuth.eval import AmplifierFailureSpec, Case, simulate_case
from netsleuth.sandbox.topology import Amplifier, Modem, Node, Tap, Topology, generate_topology
from netsleuth.storage import DuckDBStorage

TICKS = 30
FAULT_TICK = 12
# A topology seed whose dev network has an amplifier with no taps of its own feeding one other.
SEED_WITH_TAPLESS_AMP = 7


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def diagnose_single_failure(amp_id: str, tmp_path: Path, topology_seed: int = 0) -> Diagnosis:
    case = Case(
        case_id="c",
        topology_seed=topology_seed,
        ticks=TICKS,
        faults=[AmplifierFailureSpec(amp_id=amp_id, at_tick=FAULT_TICK)],
    )
    sim = simulate_case(case, tmp_path / "data", tmp_path / "gt")
    storage = DuckDBStorage(tmp_path / "data")
    with storage.session(sim.run_id, sim.engine.time_of(TICKS - 1)) as session:
        anomalies = detect_anomalies(session)
    assert anomalies.height == 1
    anomaly = anomalies.row(0, named=True)
    with storage.session(sim.run_id, anomaly["ts"]) as session:
        return rules_baseline(session, anomaly)


def has_its_own_tap(topo: Topology, amp: Amplifier) -> bool:
    return any(isinstance(c, Tap) for c in topo.children(amp.device_id))


def test_finds_an_amplifier_with_its_own_taps_exactly(topo: Topology, tmp_path: Path) -> None:
    amp = next(
        a
        for a in topo.of_type(Amplifier)
        if has_its_own_tap(topo, a)
        and any(isinstance(c, Amplifier) for c in topo.children(a.device_id))
    )

    diagnosis = diagnose_single_failure(amp.device_id, tmp_path)

    assert diagnosis.root_cause_category == "amplifier_failure"
    assert diagnosis.root_cause_device_id == amp.device_id
    assert len(diagnosis.affected_device_ids) == sum(
        isinstance(d, Modem) for d in topo.subtree(amp.device_id)
    )


def test_finds_the_last_amplifier_in_a_cascade(topo: Topology, tmp_path: Path) -> None:
    amp = next(
        a
        for a in topo.of_type(Amplifier)
        if not any(isinstance(c, Amplifier) for c in topo.children(a.device_id))
        and not isinstance(topo.parent(a.device_id), Node)
    )

    diagnosis = diagnose_single_failure(amp.device_id, tmp_path)

    assert diagnosis.root_cause_device_id == amp.device_id


def test_blames_the_upstream_amp_when_it_has_no_taps_of_its_own(tmp_path: Path) -> None:
    # Known blind spot: if amp A feeds only amp B, a failure of B looks exactly like a failure of A.
    topo = generate_topology("dev", seed=SEED_WITH_TAPLESS_AMP)
    upstream = next(
        a
        for a in topo.of_type(Amplifier)
        if not has_its_own_tap(topo, a) and len(topo.children(a.device_id)) == 1
    )
    (failed,) = topo.children(upstream.device_id)

    diagnosis = diagnose_single_failure(failed.device_id, tmp_path, SEED_WITH_TAPLESS_AMP)

    assert diagnosis.root_cause_device_id == upstream.device_id  # one hop off, 0.75 credit


def test_nothing_offline_means_insufficient_evidence(topo: Topology, tmp_path: Path) -> None:
    case = Case(case_id="c", ticks=5, faults=[])
    sim = simulate_case(case, tmp_path / "data", tmp_path / "gt")
    node = topo.of_type(Node)[0]
    fake_anomaly = {
        "anomaly_id": "an-x",
        "scope_device_id": node.device_id,
        "ts": sim.engine.time_of(4),
    }

    with DuckDBStorage(tmp_path / "data").session(sim.run_id, sim.engine.time_of(4)) as session:
        diagnosis = rules_baseline(session, fake_anomaly)

    assert diagnosis.root_cause_category == "insufficient_evidence"
    assert diagnosis.root_cause_device_id is None


def test_a_whole_dark_node_is_called_a_fiber_cut(topo: Topology, tmp_path: Path) -> None:
    # Every modem on the node offline and nothing below the node explains it on its own.
    case = Case(case_id="c", ticks=5, faults=[])
    sim = simulate_case(case, tmp_path / "data", tmp_path / "gt")
    node = topo.of_type(Node)[0]
    data = tmp_path / "data" / sim.run_id / "cm_status"
    node_modems = [d.device_id for d in topo.subtree(node.device_id) if isinstance(d, Modem)]
    for part in data.rglob("*.parquet"):
        frame = pl.read_parquet(part)
        frame.with_columns(
            pl.when(pl.col("modem_id").is_in(node_modems))
            .then(False)
            .otherwise(pl.col("online"))
            .alias("online")
        ).write_parquet(part)
    anomaly = {"anomaly_id": "an-x", "scope_device_id": node.device_id, "ts": sim.engine.time_of(4)}

    with DuckDBStorage(tmp_path / "data").session(sim.run_id, sim.engine.time_of(4)) as session:
        diagnosis = rules_baseline(session, anomaly)

    assert diagnosis.root_cause_category == "fiber_cut"
    assert diagnosis.root_cause_device_id == node.device_id
