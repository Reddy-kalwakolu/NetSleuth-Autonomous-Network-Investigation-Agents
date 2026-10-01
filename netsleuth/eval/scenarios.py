"""Scenario files: YAML versions of evaluation cases.

A scenario names a network, seeds, a length and the faults to inject. Anything wrong with it
(a typo in a key, a device that isn't an amplifier, a fault after the run ends) fails when the
file loads, not halfway through a run.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from netsleuth.eval.cases import Case
from netsleuth.sandbox.topology import Amplifier, generate_topology


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
    for fault in case.faults:
        if fault.amp_id not in topology or not isinstance(topology[fault.amp_id], Amplifier):
            raise ScenarioError(
                f"{path}: {fault.amp_id} is not an amplifier in the {case.topology_size} "
                f"network with topology seed {case.topology_seed}"
            )
        if fault.at_tick >= case.ticks:
            raise ScenarioError(
                f"{path}: the fault at tick {fault.at_tick} starts after the run ends "
                f"at tick {case.ticks - 1}"
            )
    return case
