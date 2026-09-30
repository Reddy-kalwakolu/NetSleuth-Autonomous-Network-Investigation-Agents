"""The storage interface every backend implements.

A session is bound to one run and one ``as_of`` time when it is created, and every query it runs
sees only rows at or before ``as_of``. Only the evaluation harness (or the agent service, from an
incident's detection time) creates sessions. Tools receive one and can't change its clock.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

import polars as pl


class StorageError(ValueError):
    """A session or query the storage layer refuses."""


class StorageSession(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def as_of(self) -> datetime: ...

    @property
    def tables(self) -> tuple[str, ...]: ...

    def query(self, sql: str, params: Sequence[object] | None = None) -> pl.DataFrame: ...

    def close(self) -> None: ...


class Storage(Protocol):
    def session(self, run_id: str, as_of: datetime) -> StorageSession: ...
