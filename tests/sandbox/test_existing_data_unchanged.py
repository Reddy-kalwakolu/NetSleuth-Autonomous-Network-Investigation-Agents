"""Fingerprints of every telemetry table that existed before milestone 3a, on a run that uses only
the primitives that existed then.

New data sources draw from their own random streams, so adding them must leave these tables
byte for byte the same. That is what keeps the sealed holdout and novel sets valid: their cases
were checked against this data, and nobody may look at them again to re-check.
"""

import hashlib

import polars as pl

from netsleuth.eval import (
    AmplifierFailureSpec,
    Case,
    FiberCutSpec,
    IngressNoiseSpec,
    PlannedMaintenanceSpec,
    build_fault,
)
from netsleuth.sandbox.engine import Engine
from netsleuth.sandbox.telemetry import TelemetryGenerator
from netsleuth.sandbox.topology import generate_topology

EXPECTED = {
    "cm_status": "df58e867b1bee1aeedaaa16c48334f6a20590f34788a196f11eabd4ec264b486",
    "cm_rf": "86961f01046e4e82bdb2cb08ef6ce6e88ad7184f2d36b5ea6503f6b57d353121",
    "sg_channels": "54cd1eced03f06464a81964f7a8d65061e4b8c3dc5d4a8f499040e5623d6bc88",
    "sg_status": "0033c39b74209631803a2fc4ae06bad0706b5f0b5e09bb14afd0324a462243d0",
    "node_optical": "6bdc267d78f6b26d068bc1b5f19e7a1ea535ba33c4397c48991bd8fc13344096",
    "cm_events": "439a68724dbdf6d218482d85700f9d7918f96d33aa2aa5fab8b5c5377a549bdf",
    "maintenance": "b0bd1a435178b8d30ee501130d3d904b9e24a16d768dbb1a405b542995784b71",
}


def test_tables_from_before_milestone_3a_are_byte_for_byte_unchanged() -> None:
    case = Case(
        case_id="fp",
        ticks=60,
        faults=[
            AmplifierFailureSpec(amp_id="amp-hub1-node04-a1", at_tick=10),
            AmplifierFailureSpec(amp_id="amp-hub1-node08-a2", at_tick=12, partial=True),
            FiberCutSpec(route="route-hub1-1", at_tick=40),
            IngressNoiseSpec(node_id="node-hub1-06", at_tick=0, start_hour=0, end_hour=6),
            PlannedMaintenanceSpec(node_id="node-hub1-07", start_tick=20, end_tick=30),
        ],
    )
    topology = generate_topology("dev", seed=0)
    faults = [build_fault(spec, topology, f"i{i}") for i, spec in enumerate(case.faults)]
    engine = Engine(topology, faults, seed=5)
    generator = TelemetryGenerator(engine)
    frames: dict[str, list[pl.DataFrame]] = {name: [] for name in EXPECTED}
    for tick in range(case.ticks):
        if tick:
            engine.step()
        out = generator.emit()
        for name in EXPECTED:
            frames[name].append(getattr(out, name))

    actual = {
        name: hashlib.sha256(
            pl.concat(parts, how="vertical_relaxed").write_csv().encode()
        ).hexdigest()
        for name, parts in frames.items()
    }

    assert actual == EXPECTED
