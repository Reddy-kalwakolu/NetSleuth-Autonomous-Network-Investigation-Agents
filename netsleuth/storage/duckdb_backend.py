"""DuckDB storage: the default backend for development and evaluation.

Each session gets its own in-memory DuckDB connection with one view per table. Timed tables are
filtered to ``ts <= as_of`` inside the view, so a query can only ever name data from before the
cutoff. Two more layers sit behind that:

1. The connection is locked to the run's own folder. It can't read the ground truth, another run,
   or anything else on disk, and the lock can't be undone.
2. ``query`` accepts a single SELECT and rejects anything that reads files directly, which is the
   only way left to reach rows past the cutoff inside the run's folder.

Agents never write SQL. The guard is there to catch mistakes in my own tool code.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import duckdb
import polars as pl

from netsleuth.storage.base import StorageError

_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_FILE_FUNCTION = re.compile(
    r"\b(read_\w+|parquet_\w+|glob|sniff_csv|query_table|getenv)\s*\(", re.I
)
_PATH_LITERAL = re.compile(r"'[^']*(?:[/\\]|\.(?:parquet|csv|json|db|duckdb))[^']*'", re.I)
_READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.I)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_read_only(sql: str) -> None:
    """Refuse anything but a single SELECT that stays inside the session's views."""
    statement = sql.strip().rstrip(";")
    if ";" in statement:
        raise StorageError("one statement per query")
    if not _READ_ONLY_START.match(statement):
        raise StorageError("only SELECT and WITH queries are allowed")
    if _FILE_FUNCTION.search(statement) or _PATH_LITERAL.search(statement):
        raise StorageError("queries can't read files directly, only the session's tables")


class DuckDBSession:
    def __init__(self, run_dir: Path, run_id: str, as_of: datetime) -> None:
        self._run_id = run_id
        self._as_of = as_of
        run_dir = run_dir.resolve()

        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET allowed_directories = [?]", [run_dir.as_posix() + "/"])
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")

        tables = []
        for table_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            name = table_dir.name
            if not _TABLE_NAME.match(name):
                continue
            partitioned = any(child.name.startswith("dt=") for child in table_dir.iterdir())
            if partitioned:
                files = _literal((table_dir / "**" / "*.parquet").as_posix())
                con.execute(
                    f"CREATE VIEW {name} AS "
                    f"SELECT * EXCLUDE (dt) FROM read_parquet({files}, hive_partitioning = true) "
                    f"WHERE dt <= DATE {_literal(as_of.date().isoformat())} "
                    f"AND ts <= TIMESTAMPTZ {_literal(as_of.isoformat())}"
                )
            else:
                files = _literal((table_dir / "*.parquet").as_posix())
                con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({files})")
            tables.append(name)

        self._connection = con
        self._tables = tuple(tables)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def as_of(self) -> datetime:
        return self._as_of

    @property
    def tables(self) -> tuple[str, ...]:
        return self._tables

    def query(self, sql: str, params: Sequence[object] | None = None) -> pl.DataFrame:
        check_read_only(sql)
        return self._connection.execute(sql, list(params or [])).pl()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class DuckDBStorage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def session(self, run_id: str, as_of: datetime) -> DuckDBSession:
        if as_of.tzinfo is None:
            raise StorageError("as_of needs a time zone")
        run_dir = self.data_dir / run_id
        if not run_dir.is_dir():
            raise StorageError(f"no run {run_id} under {self.data_dir}")
        return DuckDBSession(run_dir, run_id, as_of)
