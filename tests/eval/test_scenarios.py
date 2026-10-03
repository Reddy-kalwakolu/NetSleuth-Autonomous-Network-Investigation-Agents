from pathlib import Path

import pytest

from netsleuth.eval import AmplifierFailureSpec, ScenarioError, load_case

REPO = Path(__file__).resolve().parents[2]
DEV_SCENARIOS = sorted((REPO / "scenarios" / "dev").glob("*.yaml"))


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_scenario_file_loads_into_a_case(tmp_path: Path) -> None:
    path = write(
        tmp_path / "f1_example.yaml",
        "case_id: f1-example\n"
        "topology_seed: 0\n"
        "seed: 3\n"
        "ticks: 24\n"
        "faults:\n"
        "  - kind: amplifier_failure\n"
        "    amp_id: amp-hub1-node01-a5\n"
        "    at_tick: 10\n",
    )

    case = load_case(path)

    assert case.case_id == "f1-example"
    assert case.seed == 3
    assert case.ticks == 24
    (fault,) = case.faults
    assert isinstance(fault, AmplifierFailureSpec)
    assert fault.amp_id == "amp-hub1-node01-a5"
    assert fault.at_tick == 10
    assert not fault.partial


def test_case_id_defaults_to_the_file_name(tmp_path: Path) -> None:
    case = load_case(write(tmp_path / "healthy_day.yaml", "ticks: 12\n"))

    assert case.case_id == "healthy_day"


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError):
        load_case(write(tmp_path / "typo.yaml", "tickz: 12\n"))


def test_a_device_that_is_not_an_amplifier_is_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path / "bad.yaml",
        "faults:\n  - kind: amplifier_failure\n    amp_id: node-hub1-01\n    at_tick: 1\n",
    )

    with pytest.raises(ScenarioError, match="node-hub1-01"):
        load_case(path)


def test_a_fault_after_the_run_ends_is_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path / "late.yaml",
        "ticks: 10\nfaults:\n  - kind: amplifier_failure\n    amp_id: amp-hub1-node01-a5\n"
        "    at_tick: 10\n",
    )

    with pytest.raises(ScenarioError, match="after the run ends"):
        load_case(path)


def test_a_missing_file_is_a_scenario_error(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError):
        load_case(tmp_path / "nope.yaml")


def test_the_dev_set_covers_every_built_fault_kind() -> None:
    kinds = {fault.kind for path in DEV_SCENARIOS for fault in load_case(path).faults}

    assert 20 <= len(DEV_SCENARIOS) <= 40
    assert kinds == {
        "amplifier_failure",
        "fiber_cut",
        "ingress_noise",
        "planned_maintenance",
        "utility_outage",
        "config_push",
        "peering_congestion",
    }
    assert any(len(load_case(path).faults) > 1 for path in DEV_SCENARIOS)


@pytest.mark.parametrize("path", DEV_SCENARIOS, ids=lambda p: p.name)
def test_every_repo_scenario_is_valid(path: Path) -> None:
    case = load_case(path)

    assert case.faults
    assert case.case_id == path.stem.replace("_", "-")
