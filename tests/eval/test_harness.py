import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from netsleuth.baselines import rules_baseline
from netsleuth.diagnosis import Diagnosis
from netsleuth.eval import AmplifierFailureSpec, Case, CustomFaultSpec, format_scores, run_case
from netsleuth.sandbox.engine import CorrectAction, DegradeLevels, ScheduledEffect
from netsleuth.sandbox.topology import Amplifier, Tap, Topology, generate_topology
from netsleuth.storage import StorageSession


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def folders(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "data", tmp_path / "ground_truth"


def clear_amp(topo: Topology) -> Amplifier:
    """An amplifier with its own taps, so the rules can pin it down exactly."""
    return next(
        a
        for a in topo.of_type(Amplifier)
        if any(isinstance(c, Tap) for c in topo.children(a.device_id))
    )


def test_single_failure_is_detected_and_scored(topo: Topology, tmp_path: Path) -> None:
    amp = clear_amp(topo)
    case = Case(
        case_id="f1-one", ticks=30, faults=[AmplifierFailureSpec(amp_id=amp.device_id, at_tick=10)]
    )

    result = run_case(case, rules_baseline, *folders(tmp_path))

    assert result.case_id == "f1-one"
    assert result.system == "rules"
    (score,) = result.faults
    assert score.detected
    assert score.true_category == "amplifier_failure"
    assert score.true_device_id == amp.device_id
    assert score.predicted_category == "amplifier_failure"
    assert score.predicted_device_id == amp.device_id
    assert score.category_score == 1.0
    assert score.location_score == 1.0
    assert result.false_alarms == 0


def test_a_missed_fault_scores_zero(topo: Topology, tmp_path: Path) -> None:
    amp = clear_amp(topo)
    # A 2 dB sag is below every detector's threshold.
    case = Case(
        case_id="too-small",
        ticks=30,
        faults=[
            CustomFaultSpec(
                category="amplifier_failure",
                root_device_id=amp.device_id,
                graded_level="amplifier",
                correct_action=CorrectAction(
                    action="dispatch_tech", target=amp.device_id, params={"work_type": "amp_repair"}
                ),
                effects=(
                    ScheduledEffect(
                        at_tick=10,
                        effect=DegradeLevels(scope_id=amp.device_id, ds_db=-2.0, us_db=1.0),
                    ),
                ),
            )
        ],
    )

    (score,) = run_case(case, rules_baseline, *folders(tmp_path)).faults

    assert not score.detected
    assert score.predicted_category is None
    assert score.category_score == 0.0
    assert score.location_score == 0.0


def test_healthy_case_has_nothing_to_score(tmp_path: Path) -> None:
    result = run_case(
        Case(case_id="healthy", ticks=20, faults=[]), rules_baseline, *folders(tmp_path)
    )

    assert result.faults == ()
    assert result.false_alarms == 0


def test_ground_truth_is_written_outside_the_data_folder(topo: Topology, tmp_path: Path) -> None:
    amp = clear_amp(topo)
    case = Case(
        case_id="gt", ticks=20, faults=[AmplifierFailureSpec(amp_id=amp.device_id, at_tick=5)]
    )

    run_case(case, rules_baseline, *folders(tmp_path))

    (gt_file,) = (tmp_path / "ground_truth").glob("*.json")
    assert (
        json.loads(gt_file.read_text(encoding="utf-8"))["faults"][0]["root_device_id"]
        == amp.device_id
    )
    assert not list((tmp_path / "data").rglob("*.json"))


def test_scores_print_for_each_case(topo: Topology, tmp_path: Path) -> None:
    amp = clear_amp(topo)
    results = [
        run_case(
            Case(
                case_id="first",
                ticks=20,
                faults=[AmplifierFailureSpec(amp_id=amp.device_id, at_tick=5)],
            ),
            rules_baseline,
            *folders(tmp_path),
        ),
        run_case(Case(case_id="second", ticks=20, faults=[]), rules_baseline, *folders(tmp_path)),
    ]

    text = format_scores(results)

    assert "first" in text and "second" in text
    assert amp.device_id in text
    assert "category 1.00" in text
    assert "location 1.00" in text


def test_systems_only_see_data_up_to_the_detection_time(topo: Topology, tmp_path: Path) -> None:
    amp = clear_amp(topo)
    case = Case(
        case_id="clock", ticks=30, faults=[AmplifierFailureSpec(amp_id=amp.device_id, at_tick=10)]
    )
    seen: list[tuple[object, object, object]] = []

    def spy(session: StorageSession, anomaly: Mapping[str, Any]) -> Diagnosis:
        latest = session.query("SELECT max(ts) AS latest FROM cm_status")["latest"][0]
        seen.append((session.as_of, anomaly["ts"], latest))
        return rules_baseline(session, anomaly)

    run_case(case, spy, *folders(tmp_path), system_name="spy")

    ((as_of, detected_at, latest),) = seen
    assert as_of == detected_at == latest


def test_a_one_hop_miss_earns_partial_location_credit(tmp_path: Path) -> None:
    # Seed 7 has an amplifier with no taps of its own feeding one other. When the downstream one
    # fails, the rules blame the upstream one: right category, one hop off.
    topo = generate_topology("dev", seed=7)
    upstream = next(
        a
        for a in topo.of_type(Amplifier)
        if not any(isinstance(c, Tap) for c in topo.children(a.device_id))
        and len(topo.children(a.device_id)) == 1
    )
    (failed,) = topo.children(upstream.device_id)
    case = Case(
        case_id="one-hop",
        topology_seed=7,
        ticks=30,
        faults=[AmplifierFailureSpec(amp_id=failed.device_id, at_tick=10)],
    )

    (score,) = run_case(case, rules_baseline, *folders(tmp_path)).faults

    assert score.predicted_device_id == upstream.device_id
    assert score.category_score == 1.0
    assert score.location_score == 0.75
