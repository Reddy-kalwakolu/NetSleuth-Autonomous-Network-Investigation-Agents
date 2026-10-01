import pytest

from netsleuth.eval import category_score, location_score, route_location_score, tree_distance
from netsleuth.sandbox.topology import Amplifier, Modem, Node, Topology, generate_topology


@pytest.fixture(scope="module")
def topo() -> Topology:
    return generate_topology("dev", seed=0)


def amp_chain(topo: Topology) -> tuple[Node, Amplifier, Amplifier]:
    """A node, an amplifier hanging off it, and an amplifier behind that one."""
    for amp in topo.of_type(Amplifier):
        parent = topo.parent(amp.device_id)
        below = [d for d in topo.children(amp.device_id) if isinstance(d, Amplifier)]
        if isinstance(parent, Node) and below:
            return parent, amp, below[0]
    raise AssertionError("dev topology should have a node with a cascade")


def test_distance_to_self_is_zero(topo: Topology) -> None:
    _, amp, _ = amp_chain(topo)

    assert tree_distance(topo, amp.device_id, amp.device_id) == 0


def test_distance_counts_hops_through_the_common_ancestor(topo: Topology) -> None:
    node, amp, deeper = amp_chain(topo)

    assert tree_distance(topo, amp.device_id, deeper.device_id) == 1
    assert tree_distance(topo, node.device_id, deeper.device_id) == 2
    assert tree_distance(topo, deeper.device_id, node.device_id) == 2


def test_location_credit_falls_with_distance(topo: Topology) -> None:
    node, amp, deeper = amp_chain(topo)
    modem = next(d for d in topo.subtree(deeper.device_id) if isinstance(d, Modem))

    assert location_score(topo, predicted=amp.device_id, actual=amp.device_id) == 1.0
    assert location_score(topo, predicted=deeper.device_id, actual=amp.device_id) == 0.75
    assert location_score(topo, predicted=node.device_id, actual=deeper.device_id) == 0.5
    assert location_score(topo, predicted=modem.device_id, actual=node.device_id) == 0.0
    assert location_score(topo, predicted=None, actual=amp.device_id) == 0.0


def test_category_is_all_or_nothing() -> None:
    assert category_score("amplifier_failure", "amplifier_failure") == 1.0
    assert category_score("fiber_cut", "amplifier_failure") == 0.0
    assert category_score(None, "amplifier_failure") == 0.0


def test_route_faults_credit_the_route_fully_and_its_nodes_half(topo: Topology) -> None:
    nodes = topo.of_type(Node)
    route = nodes[0].fiber_route
    on_route = next(n for n in nodes if n.fiber_route == route)
    off_route = next(n for n in nodes if n.fiber_route != route)

    assert route_location_score(topo, route, route) == 1.0
    assert route_location_score(topo, on_route.device_id, route) == 0.5
    assert route_location_score(topo, off_route.device_id, route) == 0.0
    assert route_location_score(topo, None, route) == 0.0
