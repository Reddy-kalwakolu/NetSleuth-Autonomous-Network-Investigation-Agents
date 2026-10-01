from pathlib import Path

import pytest

from netsleuth.eval import (
    Case,
    CustomFaultSpec,
    FiberCutSpec,
    IngressNoiseSpec,
    PlannedMaintenanceSpec,
    ScenarioError,
    build_fault,
    load_case,
)
from netsleuth.sandbox.topology import Node, Topology, generate_topology


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_every_fault_kind_loads_from_yaml(tmp_path: Path) -> None:
    case = load_case(
        write(
            tmp_path / "mixed.yaml",
            "ticks: 288\n"
            "faults:\n"
            "  - kind: fiber_cut\n    route: route-hub1-1\n    at_tick: 10\n"
            "  - kind: fiber_cut\n    node_id: node-hub1-02\n    at_tick: 40\n"
            "  - kind: ingress_noise\n    node_id: node-hub1-03\n    at_tick: 0\n"
            "  - kind: planned_maintenance\n    node_id: node-hub1-04\n"
            "    start_tick: 60\n    end_tick: 72\n",
        )
    )

    kinds = [type(f) for f in case.faults]
    assert kinds == [FiberCutSpec, FiberCutSpec, IngressNoiseSpec, PlannedMaintenanceSpec]


def test_custom_fault_is_written_from_primitives(tmp_path: Path, topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    case = load_case(
        write(
            tmp_path / "custom.yaml",
            "ticks: 48\n"
            "faults:\n"
            "  - kind: custom\n"
            "    category: fiber_cut\n"
            f"    root_device_id: {node.device_id}\n"
            "    graded_level: node\n"
            "    correct_action:\n      action: dispatch_tech\n"
            f"      target: {node.device_id}\n      params:\n        work_type: fiber_repair\n"
            "    effects:\n"
            "      - at_tick: 10\n        effect:\n          kind: take_down\n"
            f"          device_id: {node.device_id}\n",
        )
    )
    (spec,) = case.faults
    assert isinstance(spec, CustomFaultSpec)

    fault = build_fault(spec, topo, "inc-1")

    assert fault.category == "fiber_cut"
    assert fault.start_tick == 10
    assert fault.incident_id == "inc-1"


def test_fiber_cut_spec_needs_exactly_one_target() -> None:
    with pytest.raises(ValueError):
        FiberCutSpec(at_tick=0)
    with pytest.raises(ValueError):
        FiberCutSpec(at_tick=0, route="r", node_id="n")


def test_bad_device_in_any_kind_fails_at_load(tmp_path: Path) -> None:
    path = write(
        tmp_path / "bad.yaml",
        "faults:\n  - kind: ingress_noise\n    node_id: amp-hub1-node01-a1\n    at_tick: 0\n",
    )

    with pytest.raises(ScenarioError, match="amp-hub1-node01-a1"):
        load_case(path)


def test_maintenance_may_end_after_the_run(tmp_path: Path) -> None:
    case = load_case(
        write(
            tmp_path / "long.yaml",
            "ticks: 30\nfaults:\n  - kind: planned_maintenance\n    node_id: node-hub1-01\n"
            "    start_tick: 20\n    end_tick: 400\n",
        )
    )

    assert isinstance(case.faults[0], PlannedMaintenanceSpec)


def test_maintenance_starting_after_the_run_is_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path / "late.yaml",
        "ticks: 30\nfaults:\n  - kind: planned_maintenance\n    node_id: node-hub1-01\n"
        "    start_tick: 30\n    end_tick: 40\n",
    )

    with pytest.raises(ScenarioError, match="after the run ends"):
        load_case(path)


def test_build_fault_returns_engine_faults(topo: Topology) -> None:
    route = topo.of_type(Node)[0].fiber_route
    case = Case(case_id="c", faults=[FiberCutSpec(route=route, at_tick=3)])

    fault = build_fault(case.faults[0], topo, "inc-c-1")

    assert fault.root_device_id == route


def test_custom_fault_with_an_unknown_device_fails_at_load(tmp_path: Path) -> None:
    # Custom faults skip the builders, so only the engine's own check stands between a typo in
    # a sealed scenario and a run that silently does nothing.
    path = write(
        tmp_path / "typo.yaml",
        "faults:\n"
        "  - kind: custom\n"
        "    category: amplifier_failure\n"
        "    root_device_id: amp-hub1-node01-a1\n"
        "    graded_level: amplifier\n"
        "    correct_action:\n      action: no_action\n      target: null\n"
        "    effects:\n"
        "      - at_tick: 1\n        effect:\n          kind: take_down\n"
        "          device_id: amp-hub1-node01-a999\n",
    )

    with pytest.raises(ScenarioError, match="amp-hub1-node01-a999"):
        load_case(path)
