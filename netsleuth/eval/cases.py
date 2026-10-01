"""Evaluation cases and the simulation that produces their data.

A case names a topology, seeds, a length and the faults to inject. Scenario files load into cases.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from netsleuth.config import TopologySize
from netsleuth.sandbox.engine import (
    CorrectAction,
    Engine,
    Fault,
    GradedLevel,
    RootCauseCategory,
    ScheduledEffect,
    amplifier_failure,
    fiber_cut,
    ingress_noise,
    planned_maintenance,
    write_ground_truth,
)
from netsleuth.sandbox.telemetry import TelemetryGenerator
from netsleuth.sandbox.topology import Topology, generate_topology, to_frames
from netsleuth.storage import RunWriter


class AmplifierFailureSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["amplifier_failure"] = "amplifier_failure"
    amp_id: str
    at_tick: NonNegativeInt
    partial: bool = False


class FiberCutSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["fiber_cut"] = "fiber_cut"
    at_tick: NonNegativeInt
    route: str | None = None
    node_id: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "FiberCutSpec":
        if (self.route is None) == (self.node_id is None):
            raise ValueError("a fiber cut needs either route or node_id, not both")
        return self


class IngressNoiseSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["ingress_noise"] = "ingress_noise"
    node_id: str
    at_tick: NonNegativeInt
    snr_drop_db: PositiveFloat = 10.0
    start_hour: int = Field(default=17, ge=0, le=23)
    end_hour: int = Field(default=23, ge=0, le=24)


class PlannedMaintenanceSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["planned_maintenance"] = "planned_maintenance"
    node_id: str
    start_tick: NonNegativeInt
    end_tick: NonNegativeInt
    publish_tick: NonNegativeInt = 0

    @model_validator(mode="after")
    def _ends_after_start(self) -> "PlannedMaintenanceSpec":
        if self.end_tick <= self.start_tick:
            raise ValueError("a maintenance window must end after it starts")
        return self


class CustomFaultSpec(BaseModel):
    """A fault written directly as primitives, with its answer spelled out. This is how
    someone else can write sealed scenarios without touching Python."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["custom"] = "custom"
    category: RootCauseCategory
    root_device_id: str
    graded_level: GradedLevel
    correct_action: CorrectAction
    effects: tuple[ScheduledEffect, ...]
    variant: str | None = None
    onset_tick: NonNegativeInt | None = None
    end_tick: NonNegativeInt | None = None


FaultSpec = Annotated[
    AmplifierFailureSpec
    | FiberCutSpec
    | IngressNoiseSpec
    | PlannedMaintenanceSpec
    | CustomFaultSpec,
    Field(discriminator="kind"),
]


def build_fault(spec: FaultSpec, topology: Topology, incident_id: str) -> Fault:
    match spec:
        case AmplifierFailureSpec():
            return amplifier_failure(
                topology,
                spec.amp_id,
                at_tick=spec.at_tick,
                incident_id=incident_id,
                partial=spec.partial,
            )
        case FiberCutSpec():
            return fiber_cut(
                topology,
                at_tick=spec.at_tick,
                incident_id=incident_id,
                route=spec.route,
                node_id=spec.node_id,
            )
        case IngressNoiseSpec():
            return ingress_noise(
                topology,
                spec.node_id,
                at_tick=spec.at_tick,
                incident_id=incident_id,
                snr_drop_db=spec.snr_drop_db,
                start_hour=spec.start_hour,
                end_hour=spec.end_hour,
            )
        case PlannedMaintenanceSpec():
            return planned_maintenance(
                topology,
                spec.node_id,
                start_tick=spec.start_tick,
                end_tick=spec.end_tick,
                incident_id=incident_id,
                publish_tick=spec.publish_tick,
            )
        case CustomFaultSpec():
            return Fault(
                fault_id=f"custom-{incident_id}",
                incident_id=incident_id,
                category=spec.category,
                root_device_id=spec.root_device_id,
                graded_level=spec.graded_level,
                correct_action=spec.correct_action,
                effects=spec.effects,
                variant=spec.variant,
                onset_tick=spec.onset_tick,
                end_tick=spec.end_tick,
            )


class Case(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    topology_size: TopologySize = "dev"
    topology_seed: int = 0
    seed: int = 0
    ticks: PositiveInt = 48
    faults: list[FaultSpec] = Field(default_factory=list)


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
        build_fault(spec, topology, f"inc-{case.case_id}-{i}")
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
