from collections import Counter, defaultdict

import pytest

from netsleuth.sandbox.topology import (
    ALLOWED_PARENTS,
    Amplifier,
    Cmts,
    Hub,
    Modem,
    Node,
    PeeringLink,
    PowerSupply,
    ServiceGroup,
    Tap,
    Topology,
    TopologyError,
    generate_topology,
    to_frames,
)
from netsleuth.sandbox.topology.models import Backbone


@pytest.fixture(scope="module")
def dev() -> Topology:
    return generate_topology("dev", seed=0)


# ---------- determinism ----------


def test_same_seed_gives_identical_tables() -> None:
    a_devices, a_edges = to_frames(generate_topology("dev", seed=11))
    b_devices, b_edges = to_frames(generate_topology("dev", seed=11))

    assert a_devices.equals(b_devices)
    assert a_edges.equals(b_edges)


def test_different_seed_gives_different_topology() -> None:
    a_devices, _ = to_frames(generate_topology("dev", seed=1))
    b_devices, _ = to_frames(generate_topology("dev", seed=2))

    assert not a_devices.equals(b_devices)


# ---------- counts ----------


def test_dev_size_counts(dev: Topology) -> None:
    assert len(dev.of_type(Backbone)) == 1
    assert len(dev.of_type(PeeringLink)) == 3
    assert len(dev.of_type(Hub)) == 1
    assert len(dev.of_type(Cmts)) == 2
    assert len(dev.of_type(ServiceGroup)) == 4
    assert len(dev.of_type(Node)) == 8
    assert 1500 <= len(dev.of_type(Modem)) <= 2500


def test_eval_size_counts() -> None:
    topo = generate_topology("eval", seed=0)

    assert len(topo.of_type(Hub)) == 2
    assert len(topo.of_type(Node)) == 20
    assert 4000 <= len(topo.of_type(Modem)) <= 6000


# ---------- tree shape ----------


def test_every_device_has_an_allowed_parent_type(dev: Topology) -> None:
    for device in dev:
        parent = dev.parent(device.device_id)
        parent_type = None if parent is None else parent.device_type
        assert parent_type in ALLOWED_PARENTS[device.device_type], device.device_id


def test_topology_rejects_a_wrong_parent_type() -> None:
    backbone = Backbone(device_id="bb", parent_id=None)
    bad_hub = Hub(device_id="hub1", parent_id="bb", region="r", city="c")
    tap_under_hub = Tap(device_id="t1", parent_id="hub1", port_count=4, x_km=0.0, y_km=0.0)

    with pytest.raises(TopologyError):
        Topology([backbone, bad_hub, tap_under_hub])


def test_topology_rejects_a_missing_parent() -> None:
    orphan = Hub(device_id="hub1", parent_id="nowhere", region="r", city="c")

    with pytest.raises(TopologyError):
        Topology([orphan])


def test_device_ids_are_unique(dev: Topology) -> None:
    devices, _ = to_frames(dev)

    assert devices["device_id"].n_unique() == devices.height == len(dev)


def test_amplifier_cascades_are_n_plus_2_to_n_plus_5(dev: Topology) -> None:
    deepest: dict[str, int] = defaultdict(int)
    for amp in dev.of_type(Amplifier):
        amps_above = [a for a in dev.ancestors(amp.device_id) if isinstance(a, Amplifier)]
        assert amp.cascade_position == len(amps_above) + 1
        node = next(a for a in dev.ancestors(amp.device_id) if isinstance(a, Node))
        deepest[node.device_id] = max(deepest[node.device_id], amp.cascade_position)

    assert set(deepest) == {n.device_id for n in dev.of_type(Node)}
    assert all(2 <= depth <= 5 for depth in deepest.values())


def test_homes_passed_matches_tap_ports(dev: Topology) -> None:
    for node in dev.of_type(Node):
        taps = [d for d in dev.subtree(node.device_id) if isinstance(d, Tap)]
        assert node.homes_passed == sum(t.port_count for t in taps)
        assert 250 <= node.homes_passed <= 500


def test_modems_never_exceed_tap_ports(dev: Topology) -> None:
    for tap in dev.of_type(Tap):
        assert tap.port_count in (2, 4, 8)
        assert len(dev.children(tap.device_id)) <= tap.port_count


def test_about_15_percent_of_amplifiers_report(dev: Topology) -> None:
    amps = dev.of_type(Amplifier)
    share = sum(a.reports_telemetry for a in amps) / len(amps)

    assert 0.05 <= share <= 0.30


# ---------- fiber routes ----------


def test_several_nodes_share_each_fiber_route(dev: Topology) -> None:
    per_route = Counter(n.fiber_route for n in dev.of_type(Node))

    assert len(per_route) >= 2
    assert all(count >= 2 for count in per_route.values())


def test_fiber_routes_cut_across_service_groups_and_cmtses(dev: Topology) -> None:
    # A route cut has to look different from a service group or CMTS problem (F3 vs F5).
    groups_per_route: dict[str, set[str]] = defaultdict(set)
    cmtses_per_route: dict[str, set[str]] = defaultdict(set)
    for node in dev.of_type(Node):
        sg, cmts = dev.ancestors(node.device_id)[:2]
        groups_per_route[node.fiber_route].add(sg.device_id)
        cmtses_per_route[node.fiber_route].add(cmts.device_id)

    assert all(len(groups) >= 2 for groups in groups_per_route.values())
    assert all(len(cmtses) >= 2 for cmtses in cmtses_per_route.values())


# ---------- power ----------


def test_every_active_is_fed_by_exactly_one_power_supply(dev: Topology) -> None:
    fed = Counter(active for ps in dev.of_type(PowerSupply) for active in ps.feeds)
    actives = {d.device_id for d in dev if isinstance(d, (Node, Amplifier))}

    assert set(fed) == actives
    assert all(count == 1 for count in fed.values())


def test_power_supply_lookup_matches_feeds(dev: Topology) -> None:
    for ps in dev.of_type(PowerSupply):
        assert 120 <= ps.battery_runtime_min <= 240
        for active in ps.feeds:
            assert dev.power_supply_for(active) == ps


def test_power_areas_do_not_follow_the_tree(dev: Topology) -> None:
    areas_per_node = [
        {d.power_area for d in dev.subtree(node.device_id) if isinstance(d, Modem)}
        for node in dev.of_type(Node)
    ]

    assert any(len(areas) >= 2 for areas in areas_per_node)


# ---------- tables ----------


def test_edges_table_has_tree_and_power_edges(dev: Topology) -> None:
    devices, edges = to_frames(dev)
    counts = dict(edges["edge_type"].value_counts().iter_rows())

    assert counts["contains"] == devices["parent_id"].is_not_null().sum()
    assert counts["powers"] == len(dev.of_type(Node)) + len(dev.of_type(Amplifier))


def test_devices_table_keeps_type_specific_columns(dev: Topology) -> None:
    devices, _ = to_frames(dev)
    nodes = devices.filter(devices["device_type"] == "node")

    assert nodes["fiber_route"].null_count() == 0
    assert nodes["homes_passed"].null_count() == 0
    assert devices.filter(devices["device_type"] == "modem")["power_area"].null_count() == 0
