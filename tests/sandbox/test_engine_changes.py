import pytest

from netsleuth.sandbox.engine import (
    ConfigChange,
    CorrectAction,
    Engine,
    EngineError,
    Fault,
    PeeringLoad,
    ScheduledEffect,
)
from netsleuth.sandbox.engine.primitives import AnyEffect
from netsleuth.sandbox.topology import Cmts, ServiceGroup, Topology, generate_topology
from netsleuth.sandbox.topology.models import PeeringLink


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def fault(at_tick: int, effect: AnyEffect) -> Fault:
    return Fault(
        fault_id="f",
        incident_id="inc",
        category="config_change",
        root_device_id="x",
        graded_level="cmts",
        correct_action=CorrectAction(action="no_action", target=None),
        effects=(ScheduledEffect(at_tick=at_tick, effect=effect),),
    )


def groups_under(topo: Topology, device_id: str) -> set[str]:
    return {d.device_id for d in topo.subtree(device_id) if isinstance(d, ServiceGroup)}


def test_a_push_to_a_cmts_hurts_every_service_group_under_it(topo: Topology) -> None:
    cmts = topo.of_type(Cmts)[0].device_id
    push = ConfigChange(change_id="chg-1", target_id=cmts, snr_drop_db=7.0)
    engine = Engine(topo, [fault(10, push)], seed=0)

    engine.run_until(9)
    assert all(engine.config_snr_db(sg) == 0.0 for sg in groups_under(topo, cmts))
    engine.run_until(10)

    hit = groups_under(topo, cmts)
    others = {sg.device_id for sg in topo.of_type(ServiceGroup)} - hit
    assert {engine.config_snr_db(sg) for sg in hit} == {7.0}
    assert {engine.config_snr_db(sg) for sg in others} == {0.0}
    (entry,) = engine.change_log
    assert (entry.change_id, entry.target_id, entry.tick) == ("chg-1", cmts, 10)


def test_a_push_to_one_service_group_hurts_only_it(topo: Topology) -> None:
    sg = topo.of_type(ServiceGroup)[1].device_id
    engine = Engine(
        topo, [fault(3, ConfigChange(change_id="c", target_id=sg, snr_drop_db=5.0))], seed=0
    )

    engine.run_until(3)

    assert [
        s.device_id for s in topo.of_type(ServiceGroup) if engine.config_snr_db(s.device_id)
    ] == [sg]


def test_a_push_must_target_a_cmts_or_service_group(topo: Topology) -> None:
    with pytest.raises(EngineError, match="CMTS or service group"):
        Engine(
            topo,
            [fault(3, ConfigChange(change_id="c", target_id="node-hub1-01", snr_drop_db=5.0))],
            seed=0,
        )


def test_peering_load_is_active_only_inside_its_hours(topo: Topology) -> None:
    link = topo.of_type(PeeringLink)[0].device_id
    load = PeeringLoad(link_id=link, peak_util_pct=98, start_hour=19, end_hour=23)
    engine = Engine(topo, [fault(0, load)], seed=0)  # the run starts at midnight

    assert engine.peering_load_pct(link) is None
    engine.run_until(20 * 12)  # 20:00
    assert engine.peering_load_pct(link) == 98
    engine.run_until(23 * 12)  # 23:00, the window has closed
    assert engine.peering_load_pct(link) is None


def test_peering_load_needs_a_peering_link(topo: Topology) -> None:
    load = PeeringLoad(link_id="node-hub1-01", peak_util_pct=98, start_hour=19, end_hour=23)

    with pytest.raises(EngineError, match="peering link"):
        Engine(topo, [fault(0, load)], seed=0)
