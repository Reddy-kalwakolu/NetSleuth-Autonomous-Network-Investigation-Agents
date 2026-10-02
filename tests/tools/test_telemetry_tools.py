from pathlib import Path

import pytest

from netsleuth.storage import DuckDBSession
from netsleuth.tools import ToolError, call_tool
from tests.support import AMP, FAULT_TICK


def test_modem_health_sees_the_outage_after_and_not_before(
    after: DuckDBSession, before: DuckDBSession
) -> None:
    # Two hours back from tick 39 is tick 15, before the failure at tick 20.
    now = call_tool(after, "summarize_modem_health", {"scope_id": "node-hub1-04", "hours": 2}).data
    then = call_tool(before, "summarize_modem_health", {"scope_id": "node-hub1-04"}).data

    assert now["offline_now"] == 166
    assert now["offline_before"] == 0
    assert then["offline_now"] == 0  # the session before the failure can't see it


def test_modem_health_works_on_a_fiber_route(after: DuckDBSession) -> None:
    data = call_tool(after, "summarize_modem_health", {"scope_id": "route-hub1-2"}).data

    assert data["offline_now"] >= 166
    assert data["modems"] > data["offline_now"]


def test_metric_series_shows_silence_after_a_device_goes_dark(after: DuckDBSession) -> None:
    dark_modem = after.query("SELECT modem_id FROM cm_status WHERE NOT online LIMIT 1")["modem_id"][
        0
    ]
    series = call_tool(
        after, "get_metric_series", {"device_id": dark_modem, "metric": "ds_rx_power_dbmv"}
    ).data

    assert series["silent_ticks"] > 0
    assert series["last_tick"] < series["as_of_tick"]


def test_metric_series_rejects_a_metric_the_device_does_not_have(after: DuckDBSession) -> None:
    with pytest.raises(ToolError, match="optical_rx_dbm"):
        call_tool(after, "get_metric_series", {"device_id": AMP, "metric": "optical_rx_dbm"})


def test_service_group_health_reports_each_channel(after: DuckDBSession) -> None:
    data = call_tool(after, "get_service_group_health", {"device_id": "node-hub1-04"}).data

    assert data["service_group"] == "sg-hub1-c1-2"
    assert [c["channel"] for c in data["channels"]][:2] == ["us1", "us2"]
    assert data["modems_online"] < data["modems_total"]


def test_maintenance_windows_shows_published_work(after: DuckDBSession) -> None:
    windows = call_tool(after, "get_maintenance_windows", {"scope_id": "node-hub1-07"}).data[
        "windows"
    ]

    assert [w["active"] for w in windows] == [True]


def test_maintenance_windows_for_a_node_without_work_is_empty(after: DuckDBSession) -> None:
    assert (
        call_tool(after, "get_maintenance_windows", {"scope_id": "node-hub1-04"}).data["windows"]
        == []
    )


def test_cm_events_counts_t4_only_after_modems_come_back(after: DuckDBSession) -> None:
    data = call_tool(after, "get_cm_events", {"scope_id": "node-hub1-04"}).data

    assert data["t4_events"] == 0  # the amplifier never came back


def test_get_anomaly_reads_the_detectors_output(after: DuckDBSession) -> None:
    anomaly_id = after.query("SELECT anomaly_id FROM anomaly_events LIMIT 1")["anomaly_id"][0]

    result = call_tool(after, "get_anomaly", {"anomaly_id": anomaly_id})

    assert result.data["scope_device_id"] in {"node-hub1-04", "node-hub1-07"}


def test_every_tool_summary_is_short(after: DuckDBSession) -> None:
    for name, args in [
        ("summarize_modem_health", {"scope_id": "node-hub1-04"}),
        ("get_service_group_health", {"device_id": "node-hub1-04"}),
        ("get_cm_events", {"scope_id": "sg-hub1-c1-2"}),
    ]:
        assert len(call_tool(after, name, args).summary) <= 600, name


def test_right_after_the_failure_dark_modems_have_not_answered_the_poll(run_dir: Path) -> None:
    # The harness diagnoses right after onset; this is when the facts must be consistent. Tick 21
    # is the first RF poll after the amplifier failed at tick 20.
    from datetime import timedelta

    from netsleuth.sandbox.engine import DEFAULT_START
    from netsleuth.storage import DuckDBStorage

    as_of = DEFAULT_START + timedelta(minutes=5 * (FAULT_TICK + 1))
    with DuckDBStorage(run_dir).session("tools", as_of) as session:
        data = call_tool(session, "summarize_modem_health", {"scope_id": AMP}).data

    assert data["offline_now"] == 166
    assert data["polled_now"] == 0


def test_modem_health_says_how_old_the_rf_poll_is(run_dir: Path) -> None:
    from datetime import timedelta

    from netsleuth.sandbox.engine import DEFAULT_START
    from netsleuth.storage import DuckDBStorage

    # RF is polled every third tick, so at the failure tick the latest poll is from tick 18, when
    # the modems behind the amplifier still answered.
    at_failure = DEFAULT_START + timedelta(minutes=5 * FAULT_TICK)
    with DuckDBStorage(run_dir).session("tools", at_failure) as session:
        result = call_tool(session, "summarize_modem_health", {"scope_id": AMP})

    assert result.data["offline_now"] == 166
    assert result.data["rf_poll_minutes_ago"] == 10
    assert "latest RF poll, taken 10 min ago" in result.summary


def test_modem_health_names_the_device_above_every_weakened_modem(tmp_path: Path) -> None:
    from netsleuth.eval import AmplifierFailureSpec, Case, simulate_case
    from netsleuth.storage import DuckDBStorage

    # A partial failure: nothing goes offline, so only the 4 dB drops can point at the amplifier.
    case = Case(
        case_id="sag",
        ticks=30,
        faults=[AmplifierFailureSpec(amp_id=AMP, at_tick=20, partial=True)],
    )
    sim = simulate_case(case, tmp_path / "d", tmp_path / "g")
    # Tick 21 is the first RF poll after the sag, as when the detector raises it. The hour back
    # is still mostly before the fault, so the drop shows against it.
    with DuckDBStorage(tmp_path / "d").session("sag", sim.engine.time_of(21)) as session:
        result = call_tool(session, "summarize_modem_health", {"scope_id": "node-hub1-04"})

    assert result.data["offline_now"] == 0
    assert result.data["dropped_root"] == AMP
    assert f"all under {AMP}" in result.summary


def test_modem_health_names_no_root_when_nothing_dropped(after: DuckDBSession) -> None:
    result = call_tool(after, "summarize_modem_health", {"scope_id": "node-hub1-04"})

    assert result.data["dropped_root"] is None
    assert "all under" not in result.summary
