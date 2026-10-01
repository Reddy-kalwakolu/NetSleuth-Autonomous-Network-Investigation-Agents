"""Evaluation cases and the simulation that produces their data.

A case names a topology, seeds, a length and the faults to inject. Scenario files load into cases.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from netsleuth.config import TopologySize
from netsleuth.sandbox.engine import Engine, amplifier_failure, write_ground_truth
from netsleuth.sandbox.telemetry import TelemetryGenerator
from netsleuth.sandbox.topology import Topology, generate_topology, to_frames
from netsleuth.storage import RunWriter


class AmplifierFailureSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["amplifier_failure"] = "amplifier_failure"
    amp_id: str
    at_tick: NonNegativeInt
    partial: bool = False


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    topology_size: TopologySize = "dev"
    topology_seed: int = 0
    seed: int = 0
    ticks: PositiveInt = 48
    faults: list[AmplifierFailureSpec] = Field(default_factory=list)


@dataclass(frozen=True)
class SimulatedRun:
    run_id: str
    topology: Topology
    engine: Engine


def simulate_case(case: Case, data_dir: Path, ground_truth_dir: Path) -> SimulatedRun:
    """Run the case's simulation, write its data, and write its ground truth separately."""
    run_id = case.case_id
    topology = generate_topology(case.topology_size, seed=case.topology_seed)
    faults = [
        amplifier_failure(
            topology,
            spec.amp_id,
            at_tick=spec.at_tick,
            incident_id=f"inc-{case.case_id}-{i}",
            partial=spec.partial,
        )
        for i, spec in enumerate(case.faults, start=1)
    ]
    engine = Engine(topology, faults, seed=case.seed)
    telemetry = TelemetryGenerator(engine)

    run_dir = data_dir / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    with RunWriter(data_dir, run_id) as writer:
        devices, edges = to_frames(topology)
        writer.write_static("topology_devices", devices)
        writer.write_static("topology_edges", edges)
        writer.add_tick(telemetry.emit())
        for _ in range(case.ticks - 1):
            engine.step()
            writer.add_tick(telemetry.emit())

    write_ground_truth(engine, run_id, ground_truth_dir)
    return SimulatedRun(run_id=run_id, topology=topology, engine=engine)
