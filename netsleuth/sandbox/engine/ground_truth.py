"""Writes the answer key for a run.

The file goes to the ground truth folder, which only the evaluation harness reads. Tools and agents
never see it.
"""

import json
from pathlib import Path
from typing import Any

from netsleuth.sandbox.engine.engine import Engine


def write_ground_truth(engine: Engine, run_id: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    record: dict[str, Any] = {
        "run_id": run_id,
        "seed": engine.seed,
        "start_time": engine.start.isoformat(),
        "tick_minutes": engine.tick_minutes,
        "faults": [
            {
                "fault_id": f.fault_id,
                "incident_id": f.incident_id,
                "category": f.category,
                "root_device_id": f.root_device_id,
                "graded_level": f.graded_level,
                "variant": f.variant,
                "correct_action": f.correct_action.model_dump(),
                "start_tick": f.start_tick,
                "start_time": engine.time_of(f.start_tick).isoformat(),
            }
            for f in engine.faults
        ],
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path
