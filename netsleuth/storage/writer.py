"""Writes a run's tables as Parquet.

Layout::

    <data_dir>/<run_id>/<table>/dt=YYYY-MM-DD/part-00000.parquet   tables with a ts column
    <data_dir>/<run_id>/<table>/part-00000.parquet                  static tables, like topology

Timed rows are buffered in memory and written on ``flush``, one file per table and day, so a run
doesn't produce a file for every tick.
"""

import shutil
from collections import defaultdict
from pathlib import Path
from types import TracebackType
from typing import Self

import polars as pl

from netsleuth.sandbox.telemetry import TickTelemetry

TELEMETRY_TABLES = ("cm_status", "cm_rf", "sg_channels", "sg_status", "node_optical")


class RunWriter:
    def __init__(self, data_dir: Path, run_id: str) -> None:
        self.run_dir = data_dir / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._buffers: dict[str, list[pl.DataFrame]] = defaultdict(list)

    def write_static(self, table: str, frame: pl.DataFrame) -> None:
        table_dir = self.run_dir / table
        if table_dir.exists():
            shutil.rmtree(table_dir)
        table_dir.mkdir(parents=True)
        frame.write_parquet(table_dir / "part-00000.parquet")

    def append(self, table: str, frame: pl.DataFrame) -> None:
        if "ts" not in frame.columns:
            raise ValueError(f"{table} rows need a ts column to be partitioned by day")
        self._buffers[table].append(frame)

    def add_tick(self, telemetry: TickTelemetry) -> None:
        for table in TELEMETRY_TABLES:
            self.append(table, getattr(telemetry, table))

    def flush(self) -> None:
        for table, frames in self._buffers.items():
            rows = pl.concat(frames)
            if rows.is_empty():
                continue
            rows = rows.with_columns(pl.col("ts").dt.date().alias("_day"))
            for (day,), part in rows.partition_by("_day", as_dict=True).items():
                day_dir = self.run_dir / table / f"dt={day}"
                day_dir.mkdir(parents=True, exist_ok=True)
                index = len(list(day_dir.glob("part-*.parquet")))
                part.drop("_day").write_parquet(day_dir / f"part-{index:05d}.parquet")
        self._buffers.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.flush()
