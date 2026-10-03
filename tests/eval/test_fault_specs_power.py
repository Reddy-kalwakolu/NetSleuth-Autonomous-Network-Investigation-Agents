"""Scenario kinds added in milestone 3a: utility outages (D3 or F4), config pushes (F5) and peering
congestion (D2)."""

import json
import math
from pathlib import Path

import pytest

from netsleuth.eval import (
    ConfigPushSpec,
    PeeringCongestionSpec,
    ScenarioError,
    UtilityOutageSpec,
    build_fault,
    load_case,
    simulate_case,
)
from netsleuth.eval.cases import Case
from netsleuth.sandbox.engine import EngineError
from netsleuth.sandbox.topology import Cmts, ServiceGroup, Topology, generate_topology
from netsleuth.sandbox.topology.models import PeeringLink, PowerSupply

AREA = "pa-hub1-3-3"


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def weakest(topo: Topology) -> PowerSupply:
    return min(
        (p for p in topo.of_type(PowerSupply) if p.power_area == AREA),
        key=lambda p: (p.battery_runtime_min, p.device_id),
    )


def test_an_outage_the_batteries_outlast_is_a_harmless_utility_outage(topo: Topology) -> None:
    short = math.ceil(weakest(topo).battery_runtime_min / 5) - 1
    spec = UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=short)

    fault = build_fault(spec, topo, "inc-1")

    assert fault.category == "commercial_power_outage"
    assert (fault.root_device_id, fault.graded_level) == (AREA, "power_area")
    assert fault.correct_action.action == "monitor"
    assert fault.deadline_tick is None
    assert (fault.start_tick, fault.end_tick) == (10, 10 + short)


def test_an_outage_that_outlasts_a_battery_is_a_power_supply_failure(topo: Topology) -> None:
    supply = weakest(topo)
    drains = math.ceil(supply.battery_runtime_min / 5)
    spec = UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=drains + 1)

    fault = build_fault(spec, topo, "inc-1")

    assert fault.category == "power_supply_failure"
    assert (fault.root_device_id, fault.graded_level) == (supply.device_id, "power_supply")
    assert fault.correct_action.action == "dispatch_generator"
    assert fault.correct_action.target == supply.device_id
    assert fault.deadline_tick == 10 + drains


def test_the_answer_key_records_the_battery_deadline(tmp_path: Path, topo: Topology) -> None:
    drains = math.ceil(weakest(topo).battery_runtime_min / 5)
    case = Case(
        case_id="f4",
        ticks=10 + drains + 4,
        faults=[UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=drains + 2)],
    )

    simulate_case(case, tmp_path / "d", tmp_path / "g")

    (fault,) = json.loads((tmp_path / "g" / "f4.json").read_text(encoding="utf-8"))["faults"]
    assert fault["deadline_tick"] == 10 + drains


def test_a_config_push_is_graded_at_its_target_with_its_change_id(topo: Topology) -> None:
    cmts = topo.of_type(Cmts)[0].device_id
    sg = topo.of_type(ServiceGroup)[0].device_id

    on_cmts = build_fault(ConfigPushSpec(target_id=cmts, at_tick=5), topo, "inc-1")
    on_sg = build_fault(
        ConfigPushSpec(target_id=sg, at_tick=5, change_id="chg-0042"), topo, "inc-2"
    )

    assert (on_cmts.category, on_cmts.graded_level, on_cmts.root_device_id) == (
        "config_change",
        "cmts",
        cmts,
    )
    assert on_cmts.correct_action.action == "rollback_change"
    assert on_cmts.correct_action.params["change_id"] == f"chg-{cmts}-t5"
    assert on_sg.graded_level == "service_group"
    assert on_sg.correct_action.params["change_id"] == "chg-0042"


def test_peering_congestion_starts_when_the_evening_load_first_shows(topo: Topology) -> None:
    link = topo.of_type(PeeringLink)[0].device_id

    fault = build_fault(PeeringCongestionSpec(link_id=link, at_tick=0), topo, "inc-1")

    assert (fault.category, fault.graded_level, fault.root_device_id) == (
        "peering_congestion",
        "peering_link",
        link,
    )
    assert fault.correct_action.action == "route_to_team"
    assert fault.start_tick == 19 * 12  # 19:00 on the first day


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_new_kinds_load_from_yaml(tmp_path: Path, topo: Topology) -> None:
    link = topo.of_type(PeeringLink)[0].device_id
    case = load_case(
        write(
            tmp_path / "new.yaml",
            "ticks: 288\nfaults:\n"
            f"  - kind: utility_outage\n    power_area: {AREA}\n    at_tick: 10\n"
            "    duration_ticks: 12\n"
            "  - kind: config_push\n    target_id: cmts-hub1-1\n    at_tick: 30\n"
            f"  - kind: peering_congestion\n    link_id: {link}\n    at_tick: 0\n",
        )
    )

    assert [f.kind for f in case.faults] == ["utility_outage", "config_push", "peering_congestion"]


def test_an_unknown_power_area_fails_at_load(tmp_path: Path) -> None:
    path = write(
        tmp_path / "bad.yaml",
        "faults:\n  - kind: utility_outage\n    power_area: pa-nowhere\n    at_tick: 1\n"
        "    duration_ticks: 5\n",
    )

    with pytest.raises(ScenarioError, match="pa-nowhere"):
        load_case(path)


def test_a_custom_fault_may_be_graded_at_a_power_area(tmp_path: Path) -> None:
    text = (
        "ticks: 24\nfaults:\n  - kind: custom\n    category: commercial_power_outage\n"
        "    root_device_id: {area}\n    graded_level: power_area\n"
        "    correct_action:\n      action: monitor\n      target: null\n"
        "    effects:\n      - at_tick: 4\n        effect:\n          kind: utility_outage\n"
        "          power_area: pa-hub1-3-3\n          duration_ticks: 6\n"
    )

    assert load_case(write(tmp_path / "ok.yaml", text.format(area=AREA))).faults
    with pytest.raises(ScenarioError, match="pa-hub1-9-9"):
        load_case(write(tmp_path / "bad.yaml", text.format(area="pa-hub1-9-9")))


def test_two_batteries_dying_on_the_same_tick_is_refused(topo: Topology) -> None:
    # pa-hub1-4-3 has supplies with 142 and 144 minutes: both run out on the 29th tick.
    spec = UtilityOutageSpec(power_area="pa-hub1-4-3", at_tick=10, duration_ticks=48)

    with pytest.raises(EngineError, match="same tick"):
        build_fault(spec, topo, "inc-1")


def test_a_power_area_level_answer_may_still_name_a_device(tmp_path: Path) -> None:
    # Before power areas could be answers, area level faults were keyed to a device. Sealed
    # scenarios written then must keep loading.
    text = (
        "ticks: 24\nfaults:\n  - kind: custom\n    category: commercial_power_outage\n"
        "    root_device_id: node-hub1-04\n    graded_level: power_area\n"
        "    correct_action:\n      action: monitor\n      target: null\n"
        "    effects:\n      - at_tick: 4\n        effect:\n          kind: take_down\n"
        "          device_id: node-hub1-04\n"
    )

    assert load_case(write(tmp_path / "old.yaml", text)).faults


def outage_yaml(area: str, at_tick: int, duration: int) -> str:
    return (
        f"  - kind: utility_outage\n    power_area: {area}\n    at_tick: {at_tick}\n"
        f"    duration_ticks: {duration}\n"
    )


def test_outages_in_one_area_closer_than_a_full_recharge_are_refused(tmp_path: Path) -> None:
    # The answer key assumes full batteries, which only holds once they have recharged.
    path = write(
        tmp_path / "twice.yaml",
        "ticks: 60\nfaults:\n" + outage_yaml(AREA, 0, 20) + outage_yaml(AREA, 30, 20),
    )

    with pytest.raises(ScenarioError, match="recharge"):
        load_case(path)


def test_outages_in_different_areas_may_overlap(tmp_path: Path) -> None:
    path = write(
        tmp_path / "two_areas.yaml",
        "ticks: 60\nfaults:\n" + outage_yaml(AREA, 0, 20) + outage_yaml("pa-hub1-5-3", 5, 20),
    )

    assert len(load_case(path).faults) == 2
