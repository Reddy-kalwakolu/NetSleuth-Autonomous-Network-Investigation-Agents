"""Scenario files: YAML versions of evaluation cases.

A scenario names a network, seeds, a length and the faults to inject. Anything wrong with it
(a typo in a key, a device of the wrong kind, a fault after the run ends) fails when the
file loads, not halfway through a run.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from netsleuth.eval.cases import Case, CustomFaultSpec, build_fault
from netsleuth.sandbox.engine import Engine, EngineError, power_areas
from netsleuth.sandbox.topology import Node, Topology, generate_topology


class ScenarioError(ValueError):
    """A scenario file that can't be run."""


def load_case(path: Path) -> Case:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise ScenarioError(f"{path}: {error.strerror or error}") from error
    except yaml.YAMLError as error:
        raise ScenarioError(f"{path}: not valid YAML ({error})") from error
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: expected a mapping of case settings")

    data.setdefault("case_id", path.stem)
    try:
        case = Case.model_validate(data)
    except ValidationError as error:
        raise ScenarioError(f"{path}: {error}") from error

    topology = generate_topology(case.topology_size, seed=case.topology_seed)
    try:
        faults = [build_fault(spec, topology, f"check-{i}") for i, spec in enumerate(case.faults)]
        Engine(topology, faults, seed=case.seed)  # checks every effect's targets
    except (EngineError, ValidationError) as error:
        raise ScenarioError(
            f"{path}: {error} (the {case.topology_size} network, topology seed "
            f"{case.topology_seed})"
        ) from error
    for spec in case.faults:
        if isinstance(spec, CustomFaultSpec):
            _check_answer_key(path, spec, topology)
    for fault in faults:
        if fault.start_tick >= case.ticks:
            raise ScenarioError(
                f"{path}: the fault at tick {fault.start_tick} starts after the run ends "
                f"at tick {case.ticks - 1}"
            )
    return case


def _check_answer_key(path: Path, spec: CustomFaultSpec, topology: Topology) -> None:
    """A custom fault's answer has to name something real, of the kind its graded level says.
    It may name a different device than its effects hit, on purpose."""
    routes = {n.fiber_route for n in topology.of_type(Node)}
    root = spec.root_device_id
    if spec.graded_level == "fiber_route":
        if root not in routes:
            raise ScenarioError(f"{path}: {root} is not a fiber route in this network")
    elif spec.graded_level == "power_area":
        # A power area, or a device: scenarios sealed before areas could be answers name one.
        if root not in power_areas(topology) and root not in topology:
            raise ScenarioError(f"{path}: {root} is not a power area or a device in this network")
    elif root not in topology:
        raise ScenarioError(f"{path}: the answer names {root}, which is not in this network")
    target = spec.correct_action.target
    if target is not None and target not in topology and target not in routes:
        raise ScenarioError(f"{path}: the correct action targets {target}, which does not exist")
