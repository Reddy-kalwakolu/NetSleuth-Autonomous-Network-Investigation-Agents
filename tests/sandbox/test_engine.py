import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from netsleuth.sandbox.engine import (
    DegradeLevels,
    Engine,
    EngineError,
    Fault,
    ScheduledEffect,
    TakeDown,
    amplifier_failure,
    write_ground_truth,
)
from netsleuth.sandbox.topology import Amplifier, Modem, Node, Topology, generate_topology


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def first_amp_with_amp_below(topo: Topology) -> Amplifier:
    """An amplifier early in a cascade, so its subtree holds more amplifiers."""
    for amp in topo.of_type(Amplifier):
        if any(isinstance(d, Amplifier) for d in topo.children(amp.device_id)):
            return amp
    raise AssertionError("dev topology should have a cascade deeper than one")


def fault(*effects: ScheduledEffect, fault_id: str = "f-test") -> Fault:
    return Fault(
        fault_id=fault_id,
        incident_id="inc-test",
        category="amplifier_failure",
        root_device_id="n/a",
        graded_level="amplifier",
        correct_action={"action": "no_action", "target": None, "params": {}},
        effects=list(effects),
    )


def engine_with(topo: Topology, *effects: ScheduledEffect, seed: int = 0) -> Engine:
    return Engine(topo, faults=[fault(*effects)], seed=seed)


def down(device_id: str, at: int = 0) -> ScheduledEffect:
    return ScheduledEffect(at_tick=at, effect=TakeDown(device_id=device_id))


def degrade(scope_id: str, ds_db: float, us_db: float) -> ScheduledEffect:
    return ScheduledEffect(
        at_tick=0, effect=DegradeLevels(scope_id=scope_id, ds_db=ds_db, us_db=us_db)
    )


def device_and_subtree(topo: Topology, device_id: str) -> set[str]:
    return {device_id} | {d.device_id for d in topo.subtree(device_id)}


# ---------- reachability ----------


def test_healthy_network_has_nothing_unreachable(topo: Topology) -> None:
    engine = Engine(topo, faults=[], seed=0)

    assert engine.unreachable == frozenset()


def test_taking_down_an_amplifier_makes_exactly_its_subtree_unreachable(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = engine_with(topo, down(amp.device_id))

    expected = device_and_subtree(topo, amp.device_id)
    assert engine.unreachable == expected
    assert any(isinstance(topo[d], Modem) for d in expected)


def test_taking_down_a_node_cuts_its_whole_plant(topo: Topology) -> None:
    node = topo.of_type(Node)[0]
    engine = engine_with(topo, down(node.device_id))

    assert engine.unreachable == device_and_subtree(topo, node.device_id)


def test_down_device_reports_down_status(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = engine_with(topo, down(amp.device_id))

    assert engine.state(amp.device_id).status == "down"
    child = topo.children(amp.device_id)[0]
    assert engine.state(child.device_id).status == "healthy"  # unreachable, not broken


# ---------- timing ----------


def test_effect_applies_at_its_tick_and_not_before(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = engine_with(topo, down(amp.device_id, at=3))

    engine.run_until(2)
    assert engine.tick == 2
    assert engine.unreachable == frozenset()

    engine.step()
    assert engine.tick == 3
    assert amp.device_id in engine.unreachable


def test_run_until_cannot_go_back(topo: Topology) -> None:
    engine = Engine(topo, faults=[], seed=0)
    engine.run_until(5)

    with pytest.raises(EngineError):
        engine.run_until(4)


def test_time_of_tick_uses_five_minute_steps(topo: Topology) -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    engine = Engine(topo, faults=[], seed=0, start=start)

    assert engine.time_of(0) == start
    assert engine.time_of(12) == start + timedelta(hours=1)


# ---------- levels ----------


def test_degrade_levels_shifts_everything_behind_the_scope(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    engine = engine_with(topo, degrade(amp.device_id, ds_db=-8.0, us_db=6.0))

    behind = topo.subtree(amp.device_id)
    assert all(engine.level_offsets(d.device_id) == (-8.0, 6.0) for d in behind)
    node = next(a for a in topo.ancestors(amp.device_id) if isinstance(a, Node))
    assert engine.level_offsets(node.device_id) == (0.0, 0.0)
    assert engine.unreachable == frozenset()


def test_level_offsets_from_nested_scopes_add_up(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    node = next(a for a in topo.ancestors(amp.device_id) if isinstance(a, Node))
    engine = engine_with(
        topo,
        degrade(node.device_id, ds_db=-2.0, us_db=1.0),
        degrade(amp.device_id, ds_db=-3.0, us_db=2.0),
    )

    modem = next(d for d in topo.subtree(amp.device_id) if isinstance(d, Modem))
    assert engine.level_offsets(modem.device_id) == (-5.0, 3.0)


# ---------- validation ----------


def test_unknown_target_is_rejected(topo: Topology) -> None:
    with pytest.raises(EngineError):
        engine_with(topo, down("amp-nowhere"))


def test_negative_tick_is_rejected() -> None:
    with pytest.raises(ValueError):
        ScheduledEffect(at_tick=-1, effect=TakeDown(device_id="x"))


# ---------- F1 amplifier failure ----------


def test_full_amplifier_failure(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=2, incident_id="inc-1")
    engine = Engine(topo, faults=[f1], seed=0)
    engine.run_until(2)

    assert f1.category == "amplifier_failure"
    assert f1.root_device_id == amp.device_id
    assert f1.graded_level == "amplifier"
    assert f1.correct_action.action == "dispatch_tech"
    assert f1.correct_action.target == amp.device_id
    assert f1.correct_action.params == {"work_type": "amp_repair"}
    assert engine.unreachable == device_and_subtree(topo, amp.device_id)


def test_partial_amplifier_failure_degrades_without_outage(topo: Topology) -> None:
    amp = first_amp_with_amp_below(topo)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=0, incident_id="inc-1", partial=True)
    engine = Engine(topo, faults=[f1], seed=0)

    assert f1.variant == "partial"
    assert engine.unreachable == frozenset()
    assert engine.state(amp.device_id).status == "degraded"
    ds, us = engine.level_offsets(topo.subtree(amp.device_id)[0].device_id)
    assert ds < 0 < us  # downstream power drops, upstream transmit power rises


def test_amplifier_failure_rejects_a_non_amplifier(topo: Topology) -> None:
    node = topo.of_type(Node)[0]

    with pytest.raises(EngineError):
        amplifier_failure(topo, node.device_id, at_tick=0, incident_id="inc-1")


# ---------- randomness ----------


def test_same_seed_gives_same_random_stream(topo: Topology) -> None:
    a = Engine(topo, faults=[], seed=7).rng.normal(size=5)
    b = Engine(topo, faults=[], seed=7).rng.normal(size=5)
    c = Engine(topo, faults=[], seed=8).rng.normal(size=5)

    assert a.tolist() == b.tolist()
    assert a.tolist() != c.tolist()


# ---------- ground truth ----------


def test_ground_truth_file_records_every_fault(topo: Topology, tmp_path: Path) -> None:
    amp = first_amp_with_amp_below(topo)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    f1 = amplifier_failure(topo, amp.device_id, at_tick=6, incident_id="inc-7", partial=True)
    engine = Engine(topo, faults=[f1], seed=3, start=start)

    path = write_ground_truth(engine, run_id="run-abc", directory=tmp_path)

    assert path == tmp_path / "run-abc.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-abc"
    assert data["seed"] == 3
    (record,) = data["faults"]
    assert record["incident_id"] == "inc-7"
    assert record["category"] == "amplifier_failure"
    assert record["root_device_id"] == amp.device_id
    assert record["graded_level"] == "amplifier"
    assert record["variant"] == "partial"
    assert record["correct_action"] == {
        "action": "dispatch_tech",
        "target": amp.device_id,
        "params": {"work_type": "amp_repair"},
    }
    assert record["start_tick"] == 6
    assert record["start_time"] == "2026-10-01T00:30:00+00:00"
