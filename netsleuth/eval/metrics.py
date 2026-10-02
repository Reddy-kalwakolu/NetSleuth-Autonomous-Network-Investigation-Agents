"""Scores checked by code against the ground truth. No LLM involved."""

from netsleuth.sandbox.topology import Node, Topology

# Location credit drops by a quarter per hop in the tree, so a truck sent one amplifier over still
# earns something, and four or more hops away earns nothing.
CREDIT_LOST_PER_HOP = 0.25


def tree_distance(topology: Topology, a: str, b: str) -> int:
    """Hops between two devices through their lowest common ancestor."""
    chain_a = [a] + [d.device_id for d in topology.ancestors(a)]
    chain_b = [b] + [d.device_id for d in topology.ancestors(b)]
    position_b = {device_id: i for i, device_id in enumerate(chain_b)}
    for i, device_id in enumerate(chain_a):
        if device_id in position_b:
            return i + position_b[device_id]
    raise ValueError(f"{a} and {b} share no ancestor")


def location_score(topology: Topology, predicted: str | None, actual: str) -> float:
    if predicted is None or predicted not in topology:
        return 0.0
    if predicted == actual:
        return 1.0
    try:
        hops = tree_distance(topology, predicted, actual)
    except ValueError:  # one of them is outside the tree, such as a power supply
        return 0.0
    return max(0.0, 1.0 - CREDIT_LOST_PER_HOP * hops)


def category_score(predicted: str | None, actual: str) -> float:
    return 1.0 if predicted == actual else 0.0


# Sending a crew to one node on a cut route finds the cut eventually, but the route is the answer
# that gets the right crew and the right span from the start.
ROUTE_NODE_CREDIT = 0.5


def route_location_score(topology: Topology, predicted: str | None, route: str) -> float:
    if predicted == route:
        return 1.0
    if predicted is not None and predicted in topology:
        device = topology[predicted]
        if isinstance(device, Node) and device.fiber_route == route:
            return ROUTE_NODE_CREDIT
    return 0.0
