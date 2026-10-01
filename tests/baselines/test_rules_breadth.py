from collections.abc import Sequence
from pathlib import Path

import pytest

from netsleuth.baselines import rules_baseline
from netsleuth.detector import detect_anomalies
from netsleuth.diagnosis import Diagnosis
from netsleuth.eval import (
    AmplifierFailureSpec,
    Case,
    FaultSpec,
    FiberCutSpec,
    IngressNoiseSpec,
    PlannedMaintenanceSpec,
    simulate_case,
)
from netsleuth.sandbox.topology import Amplifier, Modem, Node, Topology, generate_topology
from netsleuth.storage import DuckDBStorage


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def first_diagnosis(tmp_path: Path, faults: Sequence[FaultSpec], ticks: int) -> Diagnosis:
    case = Case(case_id="c", ticks=ticks, faults=list(faults))
    sim = simulate_case(case, tmp_path / "d", tmp_path / "g")
    storage = DuckDBStorage(tmp_path / "d")
    with storage.session("c", sim.engine.time_of(ticks - 1)) as s:
        anomaly = detect_anomalies(s).row(0, named=True)
    with storage.session("c", anomaly["ts"]) as s:
        return rules_baseline(s, anomaly)


def test_maintenance_window_is_recognised_as_planned_work(topo: Topology, tmp_path: Path) -> None:
    node = topo.of_type(Node)[0]
    spec = PlannedMaintenanceSpec(node_id=node.device_id, start_tick=12, end_tick=24)

    diagnosis = first_diagnosis(tmp_path, [spec], 30)

    assert diagnosis.root_cause_category == "planned_maintenance"
    assert diagnosis.root_cause_device_id == node.device_id


def test_without_a_calendar_entry_the_same_outage_is_a_node_cut(
    topo: Topology, tmp_path: Path
) -> None:
    node = topo.of_type(Node)[0]

    diagnosis = first_diagnosis(tmp_path, [FiberCutSpec(node_id=node.device_id, at_tick=12)], 30)

    assert diagnosis.root_cause_category == "fiber_cut"
    assert diagnosis.root_cause_device_id == node.device_id


def test_route_cut_is_called_at_the_route(topo: Topology, tmp_path: Path) -> None:
    route = topo.of_type(Node)[0].fiber_route

    diagnosis = first_diagnosis(tmp_path, [FiberCutSpec(route=route, at_tick=12)], 30)

    assert diagnosis.root_cause_category == "fiber_cut"
    assert diagnosis.root_cause_device_id == route


def test_ingress_is_called_at_the_noisy_node(topo: Topology, tmp_path: Path) -> None:
    node = topo.of_type(Node)[0]

    diagnosis = first_diagnosis(
        tmp_path, [IngressNoiseSpec(node_id=node.device_id, at_tick=0)], 288
    )

    assert diagnosis.root_cause_category == "ingress_noise"
    assert diagnosis.root_cause_device_id in {
        n.device_id
        for n in topo.children(topo.parent(node.device_id).device_id)  # type: ignore[union-attr]
    }


def test_partial_failure_is_pinned_to_the_amplifier(topo: Topology, tmp_path: Path) -> None:
    amp = max(
        topo.of_type(Amplifier),
        key=lambda a: sum(isinstance(d, Modem) for d in topo.subtree(a.device_id)),
    )
    spec = AmplifierFailureSpec(amp_id=amp.device_id, at_tick=20, partial=True)

    diagnosis = first_diagnosis(tmp_path, [spec], 40)

    assert diagnosis.root_cause_category == "amplifier_failure"
    assert diagnosis.root_cause_device_id == amp.device_id


def test_missing_event_and_calendar_tables_are_treated_as_empty(
    topo: Topology, tmp_path: Path
) -> None:
    amp = topo.of_type(Amplifier)[0]
    case = Case(
        case_id="c", ticks=30, faults=[AmplifierFailureSpec(amp_id=amp.device_id, at_tick=10)]
    )
    sim = simulate_case(case, tmp_path / "d", tmp_path / "g")
    storage = DuckDBStorage(tmp_path / "d")
    with storage.session("c", sim.engine.time_of(29)) as s:
        assert "maintenance" not in s.tables
        anomaly = detect_anomalies(s).row(0, named=True)
    with storage.session("c", anomaly["ts"]) as s:
        diagnosis = rules_baseline(s, anomaly)

    assert diagnosis.root_cause_category == "amplifier_failure"


def test_window_counts_as_active_at_its_exact_start(topo: Topology, tmp_path: Path) -> None:
    node = topo.of_type(Node)[0]
    spec = PlannedMaintenanceSpec(node_id=node.device_id, start_tick=12, end_tick=13)

    diagnosis = first_diagnosis(tmp_path, [spec], 20)

    assert diagnosis.root_cause_category == "planned_maintenance"
