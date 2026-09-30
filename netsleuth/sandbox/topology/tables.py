"""Flatten a topology into the ``topology_devices`` and ``topology_edges`` tables."""

import polars as pl

from netsleuth.sandbox.topology.models import PowerSupply
from netsleuth.sandbox.topology.topology import Topology

# One wide table for all device types. Columns a type doesn't use are null.
DEVICE_SCHEMA: dict[str, pl.DataType] = {
    "device_id": pl.String(),
    "device_type": pl.String(),
    "parent_id": pl.String(),
    "partner": pl.String(),
    "capacity_gbps": pl.Int64(),
    "region": pl.String(),
    "city": pl.String(),
    "vendor": pl.String(),
    "model": pl.String(),
    "architecture": pl.String(),
    "line_cards": pl.Int64(),
    "line_card": pl.Int64(),
    "us_channels": pl.Int64(),
    "ds_channels": pl.Int64(),
    "split": pl.String(),
    "homes_passed": pl.Int64(),
    "fiber_route": pl.String(),
    "cascade_position": pl.Int64(),
    "reports_telemetry": pl.Boolean(),
    "port_count": pl.Int64(),
    "firmware": pl.String(),
    "docsis_version": pl.String(),
    "customer_id": pl.String(),
    "power_area": pl.String(),
    "battery_runtime_min": pl.Int64(),
    "x_km": pl.Float64(),
    "y_km": pl.Float64(),
}

EDGE_SCHEMA: dict[str, pl.DataType] = {
    "from_id": pl.String(),
    "to_id": pl.String(),
    "edge_type": pl.String(),  # "contains" (parent to child) or "powers" (power supply to active)
}

# Stored as edges, not as a column.
_EDGE_FIELDS = {"feeds"}


def to_frames(topology: Topology) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return ``(topology_devices, topology_edges)``."""
    device_rows = []
    edge_rows = []
    for device in topology:
        row = device.model_dump(exclude=_EDGE_FIELDS)
        unknown = row.keys() - DEVICE_SCHEMA.keys()
        if unknown:
            raise ValueError(f"DEVICE_SCHEMA is missing columns for {sorted(unknown)}")
        device_rows.append(row)

        if device.parent_id is not None:
            edge_rows.append(
                {"from_id": device.parent_id, "to_id": device.device_id, "edge_type": "contains"}
            )
        if isinstance(device, PowerSupply):
            edge_rows.extend(
                {"from_id": device.device_id, "to_id": active, "edge_type": "powers"}
                for active in device.feeds
            )

    devices = pl.DataFrame(device_rows, schema=DEVICE_SCHEMA)
    edges = pl.DataFrame(edge_rows, schema=EDGE_SCHEMA)
    return devices, edges
