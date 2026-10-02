import pytest

from netsleuth.storage import DuckDBSession
from netsleuth.tools import ToolError, call_tool, inventory
from tests.support import AMP


def test_get_device_reports_type_parent_and_live_state(after: DuckDBSession) -> None:
    result = call_tool(after, "get_device", {"device_id": "node-hub1-04"})

    assert result.data["device_type"] == "node"
    assert result.data["parent_id"] == "sg-hub1-c1-2"
    assert result.data["fiber_route"] == "route-hub1-2"
    assert result.data["modems_offline"] == 166
    assert "node-hub1-04" in result.summary


def test_get_device_rejects_an_unknown_id(after: DuckDBSession) -> None:
    with pytest.raises(ToolError, match="no device"):
        call_tool(after, "get_device", {"device_id": "amp-nowhere"})


def test_get_ancestors_walks_to_the_root(after: DuckDBSession) -> None:
    chain = call_tool(after, "get_ancestors", {"device_id": AMP}).data["ancestors"]

    assert chain[:2] == ["node-hub1-04", "sg-hub1-c1-2"]
    assert chain[-1] == "bb"


def test_get_subtree_counts_what_hangs_below(after: DuckDBSession) -> None:
    result = call_tool(after, "get_subtree", {"device_id": AMP, "depth": 1})

    assert result.data["modems_below"] == 166
    assert result.data["children"]
    assert set(result.data["counts"]) <= {"amplifier", "tap"}


def test_find_devices_by_fiber_route(after: DuckDBSession) -> None:
    result = call_tool(after, "find_devices", {"attr": "fiber_route", "value": "route-hub1-1"})

    assert set(result.data["device_ids"]) == {
        "node-hub1-01",
        "node-hub1-03",
        "node-hub1-05",
        "node-hub1-07",
    }


def test_find_devices_only_searches_known_attributes(after: DuckDBSession) -> None:
    with pytest.raises(ToolError, match="can't search"):
        call_tool(after, "find_devices", {"attr": "device_id; DROP TABLE x", "value": "y"})


def test_inventory_is_cached_per_session(after: DuckDBSession) -> None:
    assert inventory(after) is inventory(after)
