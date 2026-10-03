"""The milestone 3a tools: power events, power supply status, recent changes, peering status and
tickets."""

import math
from collections.abc import Iterator
from pathlib import Path

import pytest

from netsleuth.eval import (
    Case,
    ConfigPushSpec,
    PeeringCongestionSpec,
    UtilityOutageSpec,
    simulate_case,
)
from netsleuth.sandbox.topology import generate_topology
from netsleuth.sandbox.topology.models import Modem, PeeringLink, PowerSupply
from netsleuth.storage import DuckDBSession, DuckDBStorage
from netsleuth.tools import ToolError, call_tool, rerun

AREA = "pa-hub1-3-3"
SG = "sg-hub1-c2-2"
TICKS = 48  # the weakest two batteries are dead by the end, the third is still going

TOPO = generate_topology("dev", seed=0)
SUPPLIES = sorted(
    (p for p in TOPO.of_type(PowerSupply) if p.power_area == AREA),
    key=lambda p: (p.battery_runtime_min, p.device_id),
)
WEAKEST = SUPPLIES[0]
DEADLINE = 10 + math.ceil(WEAKEST.battery_runtime_min / 5)
LINK = TOPO.of_type(PeeringLink)[0].device_id


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DuckDBSession]:
    base = tmp_path_factory.mktemp("events")
    case = Case(
        case_id="events",
        ticks=TICKS,
        faults=[
            UtilityOutageSpec(power_area=AREA, at_tick=10, duration_ticks=200),
            ConfigPushSpec(target_id=SG, at_tick=5, change_id="chg-0099"),
            PeeringCongestionSpec(link_id=LINK, at_tick=0, start_hour=0, end_hour=4),
        ],
    )
    sim = simulate_case(case, base / "d", base / "g")
    with DuckDBStorage(base / "d").session("events", sim.engine.time_of(TICKS - 1)) as s:
        yield s


def node_of(device_id: str) -> str:
    return next(a.device_id for a in TOPO.ancestors(device_id) if a.device_type == "node")


def test_power_events_show_an_area_that_is_still_out(session: DuckDBSession) -> None:
    result = call_tool(session, "get_power_events", {"hours": 6})

    assert result.data["out_now"] == [AREA]
    assert AREA in result.summary and "still out" in result.summary


def test_power_events_can_be_asked_about_a_node(session: DuckDBSession) -> None:
    home = next(m for m in TOPO.of_type(Modem) if m.power_area == AREA)
    other = next(m for m in TOPO.of_type(Modem) if m.power_area == "pa-hub1-5-3")

    near = call_tool(session, "get_power_events", {"scope_id": node_of(home.device_id)})
    far = call_tool(session, "get_power_events", {"scope_id": "pa-hub1-5-3"})

    assert AREA in near.data["out_now"]
    assert far.data["out_now"] == [] and other.power_area == "pa-hub1-5-3"


def test_a_drained_supply_reads_as_silent_and_the_others_as_on_battery(
    session: DuckDBSession,
) -> None:
    result = call_tool(session, "get_power_supply_status", {"scope_id": AREA})
    by_id = {s["ps_id"]: s for s in result.data["supplies"]}

    drained = by_id[WEAKEST.device_id]
    assert drained["silent_min"] > 0
    assert drained["last_battery_min_left"] < 10
    still_up = [s for s in by_id.values() if s["silent_min"] == 0]
    assert still_up and all(s["on_battery"] for s in still_up)
    assert len(by_id) == len(SUPPLIES)


def test_supply_status_for_a_device_finds_the_supply_that_feeds_it(
    session: DuckDBSession,
) -> None:
    active = WEAKEST.feeds[0]

    result = call_tool(session, "get_power_supply_status", {"scope_id": active})

    assert [s["ps_id"] for s in result.data["supplies"]] == [WEAKEST.device_id]


def test_recent_changes_reach_a_node_through_its_service_group(session: DuckDBSession) -> None:
    node = next(c.device_id for c in TOPO.children(SG))

    under = call_tool(session, "get_recent_changes", {"scope_id": node})
    elsewhere = call_tool(session, "get_recent_changes", {"scope_id": "node-hub1-01"})

    assert [c["change_id"] for c in under.data["changes"]] == ["chg-0099"]
    assert "chg-0099" in under.summary
    assert elsewhere.data["changes"] == []


def test_peering_status_shows_the_peak_in_the_window(session: DuckDBSession) -> None:
    result = call_tool(session, "get_peering_status", {"hours": 2})
    link = next(r for r in result.data["links"] if r["link_id"] == LINK)

    assert link["peak_util_pct"] >= 95
    assert LINK in result.summary


def test_tickets_count_by_kind_and_node(session: DuckDBSession) -> None:
    behind = node_of(WEAKEST.feeds[0]) if WEAKEST.feeds[0].startswith("amp") else WEAKEST.feeds[0]

    near = call_tool(session, "get_tickets", {"scope_id": behind, "hours": 5})
    everywhere = call_tool(session, "get_tickets", {"hours": 5})

    assert near.data["by_kind"].get("no_service", 0) > 0
    assert everywhere.data["by_kind"].get("slow_service", 0) > 0
    assert len(everywhere.data["by_node"]) >= 3


def test_every_new_tool_reruns_from_its_query_ref(session: DuckDBSession) -> None:
    for name, args in (
        ("get_power_events", {"scope_id": AREA}),
        ("get_power_supply_status", {"scope_id": AREA}),
        ("get_recent_changes", {"scope_id": SG}),
        ("get_peering_status", {}),
        ("get_tickets", {"scope_id": SG}),
    ):
        first = call_tool(session, name, args)
        assert rerun(session, first.query_ref).summary == first.summary, name


def test_the_tools_work_on_a_run_without_these_events(after: DuckDBSession) -> None:
    assert call_tool(after, "get_power_events", {}).data["out_now"] == []
    assert call_tool(after, "get_recent_changes", {}).data["changes"] == []


def test_an_unknown_scope_is_refused(session: DuckDBSession) -> None:
    with pytest.raises(ToolError, match="nowhere"):
        call_tool(session, "get_tickets", {"scope_id": "nowhere"})


def test_this_file_has_its_own_run(tmp_path: Path) -> None:
    assert DEADLINE < TICKS  # the weakest battery runs out inside the run
