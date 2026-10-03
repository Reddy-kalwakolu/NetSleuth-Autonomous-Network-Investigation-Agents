"""The rules baseline on the milestone 3a events, scored end to end through the harness."""

import math
from pathlib import Path

import pytest

from netsleuth.baselines import rules_baseline
from netsleuth.eval import (
    Case,
    ConfigPushSpec,
    PeeringCongestionSpec,
    UtilityOutageSpec,
    run_case,
)
from netsleuth.eval.cases import FaultSpec
from netsleuth.sandbox.topology import generate_topology
from netsleuth.sandbox.topology.models import PeeringLink, PowerSupply

AREA = "pa-hub1-3-3"
TOPO = generate_topology("dev", seed=0)
WEAKEST = min(
    (p for p in TOPO.of_type(PowerSupply) if p.power_area == AREA),
    key=lambda p: (p.battery_runtime_min, p.device_id),
)
DRAINS = math.ceil(WEAKEST.battery_runtime_min / 5)


def score(tmp_path: Path, ticks: int, spec: FaultSpec) -> tuple[float, float, int]:
    result = run_case(
        Case(case_id="r", ticks=ticks, faults=[spec]),
        rules_baseline,
        tmp_path / "d",
        tmp_path / "g",
    )
    (fault,) = result.faults
    return fault.category_score, fault.location_score, result.false_alarms


def test_a_short_utility_outage_is_called_at_its_power_area(tmp_path: Path) -> None:
    spec = UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=DRAINS - 4)

    assert score(tmp_path, 10 + DRAINS, spec) == (1.0, 1.0, 0)


def test_a_drained_battery_is_called_at_the_supply_when_its_actives_drop(
    tmp_path: Path,
) -> None:
    spec = UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=DRAINS + 20)

    category, location, false_alarms = score(tmp_path, 10 + DRAINS + 6, spec)

    assert (category, location) == (1.0, 1.0)
    assert false_alarms == 0  # the homes going dark earlier belong to the same fault


def test_a_config_push_on_a_cmts_is_called_at_the_cmts(tmp_path: Path) -> None:
    assert score(tmp_path, 40, ConfigPushSpec(target_id="cmts-hub1-1", at_tick=30)) == (
        1.0,
        1.0,
        0,
    )


def test_a_config_push_on_one_service_group_is_called_at_it(tmp_path: Path) -> None:
    spec = ConfigPushSpec(target_id="sg-hub1-c2-2", at_tick=30)

    assert score(tmp_path, 40, spec) == (1.0, 1.0, 0)


@pytest.mark.parametrize("hour", [19, 20])
def test_peering_congestion_is_called_at_the_link(tmp_path: Path, hour: int) -> None:
    link = TOPO.of_type(PeeringLink)[0].device_id
    spec = PeeringCongestionSpec(link_id=link, at_tick=0, start_hour=hour, end_hour=23)

    assert score(tmp_path, (hour + 1) * 12, spec) == (1.0, 1.0, 0)


def test_a_supply_silenced_by_a_failed_amplifier_is_not_called_drained(tmp_path: Path) -> None:
    # The supply's transponder talks through the amplifier, so it goes quiet when the amplifier
    # dies, during a utility outage its battery easily rides through.
    from netsleuth.eval import AmplifierFailureSpec

    result = run_case(
        Case(
            case_id="mix",
            ticks=40,
            faults=[
                UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=22),
                AmplifierFailureSpec(amp_id="amp-hub1-node01-a6", at_tick=26),
            ],
        ),
        rules_baseline,
        tmp_path / "d",
        tmp_path / "g",
    )

    amp = next(f for f in result.faults if f.true_category == "amplifier_failure")
    assert amp.predicted_category != "power_supply_failure"
