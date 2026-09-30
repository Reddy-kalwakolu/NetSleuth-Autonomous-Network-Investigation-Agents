from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

from netsleuth.sandbox.engine import Engine
from netsleuth.sandbox.telemetry import TelemetryGenerator, TickTelemetry
from netsleuth.sandbox.topology import generate_topology, to_frames
from netsleuth.storage import DuckDBStorage, RunWriter, StorageError

TIMED_TABLES = ("cm_status", "cm_rf", "sg_channels", "sg_status", "node_optical")
# 23:30 start, so 12 ticks of 5 minutes cross midnight into a second day partition.
START = datetime(2026, 9, 1, 23, 30, tzinfo=UTC)
RUN = "run-test"


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Engine, list[TickTelemetry]]:
    data_dir = tmp_path_factory.mktemp("data")
    topo = generate_topology("dev", seed=0)
    engine = Engine(topo, faults=[], seed=2, start=START)
    gen = TelemetryGenerator(engine)
    ticks = []
    with RunWriter(data_dir, RUN) as writer:
        devices, edges = to_frames(topo)
        writer.write_static("topology_devices", devices)
        writer.write_static("topology_edges", edges)
        for i in range(12):
            if i:
                engine.step()
            tick = gen.emit()
            ticks.append(tick)
            writer.add_tick(tick)
            if i == 2:
                writer.flush()  # a mid run flush leaves two part files in one partition
    return data_dir, engine, ticks


def rows_up_to(ticks: list[TickTelemetry], table: str, last_tick: int) -> int:
    return sum(getattr(t, table).height for t in ticks if t.tick <= last_tick)


# ---------- layout ----------


def test_timed_tables_are_partitioned_by_day(
    written: tuple[Path, Engine, list[TickTelemetry]],
) -> None:
    data_dir, _, _ = written

    days = sorted(p.name for p in (data_dir / RUN / "cm_status").iterdir())
    assert days == ["dt=2026-09-01", "dt=2026-09-02"]
    assert len(list((data_dir / RUN / "cm_status" / "dt=2026-09-01").glob("part-*.parquet"))) == 2


def test_static_tables_are_not_partitioned(
    written: tuple[Path, Engine, list[TickTelemetry]],
) -> None:
    data_dir, _, _ = written

    assert [p.name for p in (data_dir / RUN / "topology_devices").iterdir()] == [
        "part-00000.parquet"
    ]


# ---------- the time cutoff ----------


@pytest.mark.parametrize("cutoff_tick", [0, 5, 6, 11])
def test_no_query_returns_rows_after_as_of(
    written: tuple[Path, Engine, list[TickTelemetry]], cutoff_tick: int
) -> None:
    data_dir, engine, ticks = written
    as_of = engine.time_of(cutoff_tick)

    with DuckDBStorage(data_dir).session(RUN, as_of) as session:
        for table in TIMED_TABLES:
            result = session.query(f"SELECT count(*) AS n, max(ts) AS latest FROM {table}")
            assert result["n"][0] == rows_up_to(ticks, table, cutoff_tick), table
            latest = result["latest"][0]
            if latest is not None:
                assert latest <= as_of, table
            everything = session.query(f"SELECT * FROM {table}")
            assert (everything["tick"] <= cutoff_tick).all(), table


def test_rows_exactly_at_as_of_are_included(
    written: tuple[Path, Engine, list[TickTelemetry]],
) -> None:
    data_dir, engine, _ = written
    as_of = engine.time_of(6)

    with DuckDBStorage(data_dir).session(RUN, as_of) as session:
        latest = session.query("SELECT max(ts) AS latest FROM sg_status")["latest"][0]

    assert latest == as_of


def test_as_of_before_any_data_returns_nothing(
    written: tuple[Path, Engine, list[TickTelemetry]],
) -> None:
    data_dir, _, _ = written

    with DuckDBStorage(data_dir).session(RUN, datetime(2026, 8, 1, tzinfo=UTC)) as session:
        assert session.query("SELECT count(*) AS n FROM cm_status")["n"][0] == 0


def test_static_tables_ignore_the_cutoff(
    written: tuple[Path, Engine, list[TickTelemetry]],
) -> None:
    data_dir, _, _ = written

    with DuckDBStorage(data_dir).session(RUN, datetime(2026, 8, 1, tzinfo=UTC)) as session:
        assert session.query("SELECT count(*) AS n FROM topology_devices")["n"][0] > 0
        assert "dt" not in session.query("SELECT * FROM cm_status LIMIT 1").columns


def test_as_of_cannot_be_changed(written: tuple[Path, Engine, list[TickTelemetry]]) -> None:
    data_dir, engine, _ = written
    session = DuckDBStorage(data_dir).session(RUN, engine.time_of(3))

    with pytest.raises(AttributeError):
        session.as_of = engine.time_of(11)  # type: ignore[misc]
    session.close()


def test_naive_as_of_is_rejected(written: tuple[Path, Engine, list[TickTelemetry]]) -> None:
    data_dir, _, _ = written

    with pytest.raises(StorageError):
        DuckDBStorage(data_dir).session(RUN, datetime(2026, 9, 1, 23, 45))


def test_unknown_run_is_rejected(written: tuple[Path, Engine, list[TickTelemetry]]) -> None:
    data_dir, engine, _ = written

    with pytest.raises(StorageError):
        DuckDBStorage(data_dir).session("no-such-run", engine.time_of(3))


# ---------- query guard and lockdown ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('data/run-test/cm_status/**/*.parquet')",
        "SELECT * FROM 'data/run-test/cm_status/dt=2026-09-02/part-00000.parquet'",
        "SELECT * FROM read_csv('ground_truth/run-test.json')",
        "DROP VIEW cm_status",
        "SET enable_external_access = true",
        "ATTACH 'other.db'",
        "SELECT 1; SELECT 2",
        "COPY cm_status TO 'out.parquet'",
    ],
)
def test_queries_that_could_bypass_the_cutoff_are_rejected(
    written: tuple[Path, Engine, list[TickTelemetry]], sql: str
) -> None:
    data_dir, engine, _ = written

    with (
        DuckDBStorage(data_dir).session(RUN, engine.time_of(3)) as session,
        pytest.raises(StorageError),
    ):
        session.query(sql)


def test_session_cannot_read_outside_its_run_even_without_the_guard(
    written: tuple[Path, Engine, list[TickTelemetry]], tmp_path: Path
) -> None:
    data_dir, engine, _ = written
    secret = data_dir / "ground_truth.csv"
    secret.write_text("answer\n42\n", encoding="utf-8")

    with DuckDBStorage(data_dir).session(RUN, engine.time_of(3)) as session:
        raw = session._connection  # the lockdown is the second line of defence
        with pytest.raises(duckdb.PermissionException):
            raw.execute(f"SELECT * FROM read_csv('{secret.as_posix()}')").fetchall()
        with pytest.raises(duckdb.Error):
            raw.execute("SET enable_external_access = true")


def test_parameters_are_supported(written: tuple[Path, Engine, list[TickTelemetry]]) -> None:
    data_dir, engine, ticks = written
    modem = ticks[0].cm_status["modem_id"][0]

    with DuckDBStorage(data_dir).session(RUN, engine.time_of(3)) as session:
        result = session.query("SELECT count(*) AS n FROM cm_status WHERE modem_id = ?", [modem])

    assert result["n"][0] == 4


def test_results_come_back_as_polars_in_utc(
    written: tuple[Path, Engine, list[TickTelemetry]],
) -> None:
    data_dir, engine, _ = written

    with DuckDBStorage(data_dir).session(RUN, engine.time_of(3)) as session:
        result = session.query("SELECT ts FROM sg_status LIMIT 1")

    assert isinstance(result, pl.DataFrame)
    assert result.schema["ts"] == pl.Datetime("us", "UTC")
