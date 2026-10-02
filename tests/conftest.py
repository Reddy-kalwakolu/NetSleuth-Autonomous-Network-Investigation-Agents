import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from langsmith import tracing_context

from netsleuth.detector import detect_anomalies
from netsleuth.eval import AmplifierFailureSpec, Case, PlannedMaintenanceSpec, simulate_case
from netsleuth.sandbox.engine import DEFAULT_START
from netsleuth.storage import DuckDBSession, DuckDBStorage, RunWriter
from tests.support import AMP, FAULT_TICK, TICKS

# Anything that could reach a paid API, send a trace, or pick up a developer's local settings.
_ISOLATED_PREFIXES = ("OPENAI_", "ANTHROPIC_", "AWS_", "LANGSMITH_", "LANGCHAIN_", "NETSLEUTH_")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No test sees real credentials, a local netsleuth.yaml or .env, or live tracing."""
    for name in list(os.environ):
        if name.startswith(_ISOLATED_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    with tracing_context(enabled=False):
        yield


@pytest.fixture(scope="session")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One simulated run shared by the tool and agent tests: a full amplifier failure on node 04
    at tick 20, and planned maintenance on node 07 from tick 30."""
    base = tmp_path_factory.mktemp("tools")
    case = Case(
        case_id="tools",
        ticks=TICKS,
        faults=[
            AmplifierFailureSpec(amp_id=AMP, at_tick=FAULT_TICK),
            PlannedMaintenanceSpec(node_id="node-hub1-07", start_tick=30, end_tick=60),
        ],
    )
    sim = simulate_case(case, base / "data", base / "gt")
    with DuckDBStorage(base / "data").session("tools", sim.engine.time_of(TICKS - 1)) as s:
        anomalies = detect_anomalies(s)
    with RunWriter(base / "data", "tools") as writer:
        writer.append("anomaly_events", anomalies)
    return base / "data"


def _at(tick: int) -> timedelta:
    return timedelta(minutes=5 * tick)


@pytest.fixture
def after(run_dir: Path) -> Iterator[DuckDBSession]:
    """A session at the end of the run, after the amplifier failed."""
    with DuckDBStorage(run_dir).session("tools", DEFAULT_START + _at(TICKS - 1)) as s:
        yield s


@pytest.fixture
def before(run_dir: Path) -> Iterator[DuckDBSession]:
    """A session just before the amplifier failed."""
    with DuckDBStorage(run_dir).session("tools", DEFAULT_START + _at(FAULT_TICK - 1)) as s:
        yield s
