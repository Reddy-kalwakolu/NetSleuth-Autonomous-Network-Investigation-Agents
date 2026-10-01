"""The evaluation harness: run a case end to end, then score a system against the ground truth.

For each case: simulate and write the data, write the ground truth to its own folder, run the
detector, and hand each anomaly to the system under test in a storage session cut off at the
anomaly's detection time. Only the harness reads the ground truth.

Until the incident grouper exists, each anomaly stands in for one incident, and a fault is matched
to the earliest unused anomaly on the node that contains it, at or after the fault started.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netsleuth.detector import detect_anomalies
from netsleuth.diagnosis import Diagnosis
from netsleuth.eval.cases import Case, simulate_case
from netsleuth.eval.metrics import category_score, location_score
from netsleuth.sandbox.topology import Node, Topology
from netsleuth.storage import DuckDBStorage, RunWriter, StorageSession

System = Callable[[StorageSession, Mapping[str, Any]], Diagnosis]


@dataclass(frozen=True)
class FaultScore:
    fault_id: str
    true_category: str
    true_device_id: str
    detected: bool
    predicted_category: str | None
    predicted_device_id: str | None
    category_score: float
    location_score: float


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    system: str
    faults: tuple[FaultScore, ...]
    false_alarms: int


def run_case(
    case: Case,
    system: System,
    data_dir: Path,
    ground_truth_dir: Path,
    system_name: str | None = None,
) -> CaseResult:
    sim = simulate_case(case, data_dir, ground_truth_dir)
    storage = DuckDBStorage(data_dir)

    with storage.session(sim.run_id, sim.engine.time_of(case.ticks - 1)) as session:
        anomalies = detect_anomalies(session)
    with RunWriter(data_dir, sim.run_id) as writer:
        if anomalies.height:
            writer.append("anomaly_events", anomalies)

    truth = json.loads((ground_truth_dir / f"{sim.run_id}.json").read_text(encoding="utf-8"))
    rows = sorted(anomalies.iter_rows(named=True), key=lambda r: r["tick"])
    used: set[str] = set()
    scores = []
    for fault in truth["faults"]:
        node_id = _node_containing(sim.topology, fault["root_device_id"])
        match = next(
            (
                r
                for r in rows
                if r["scope_device_id"] == node_id
                and r["tick"] >= fault["start_tick"]
                and r["anomaly_id"] not in used
            ),
            None,
        )
        if match is None:
            scores.append(_missed(fault))
            continue
        used.add(match["anomaly_id"])
        with storage.session(sim.run_id, match["ts"]) as session:
            diagnosis = system(session, match)
        scores.append(
            FaultScore(
                fault_id=fault["fault_id"],
                true_category=fault["category"],
                true_device_id=fault["root_device_id"],
                detected=True,
                predicted_category=diagnosis.root_cause_category,
                predicted_device_id=diagnosis.root_cause_device_id,
                category_score=category_score(diagnosis.root_cause_category, fault["category"]),
                location_score=location_score(
                    sim.topology, diagnosis.root_cause_device_id, fault["root_device_id"]
                ),
            )
        )

    name = system_name or system.__name__.removesuffix("_baseline")
    return CaseResult(
        case_id=case.case_id,
        system=name,
        faults=tuple(scores),
        false_alarms=anomalies.height - len(used),
    )


def format_scores(results: Sequence[CaseResult]) -> str:
    lines = []
    all_scores = [s for r in results for s in r.faults]
    for result in results:
        lines.append(f"case {result.case_id}  [{result.system}]")
        if not result.faults:
            lines.append("  no faults injected")
        for s in result.faults:
            predicted = (
                f"predicted {s.predicted_category} @ {s.predicted_device_id}"
                if s.detected
                else "not detected"
            )
            lines.append(
                f"  {s.true_category} @ {s.true_device_id}  {predicted}  "
                f"category {s.category_score:.2f}  location {s.location_score:.2f}"
            )
        lines.append(f"  false alarms {result.false_alarms}")
    if all_scores:
        n = len(all_scores)
        lines.append(
            f"overall: {n} faults, {sum(s.detected for s in all_scores)} detected, "
            f"category {sum(s.category_score for s in all_scores) / n:.2f}, "
            f"location {sum(s.location_score for s in all_scores) / n:.2f}, "
            f"false alarms {sum(r.false_alarms for r in results)}"
        )
    return "\n".join(lines)


def _node_containing(topology: Topology, device_id: str) -> str:
    device = topology[device_id]
    if isinstance(device, Node):
        return device.device_id
    return next(a.device_id for a in topology.ancestors(device_id) if isinstance(a, Node))


def _missed(fault: Mapping[str, Any]) -> FaultScore:
    return FaultScore(
        fault_id=fault["fault_id"],
        true_category=fault["category"],
        true_device_id=fault["root_device_id"],
        detected=False,
        predicted_category=None,
        predicted_device_id=None,
        category_score=0.0,
        location_score=0.0,
    )
