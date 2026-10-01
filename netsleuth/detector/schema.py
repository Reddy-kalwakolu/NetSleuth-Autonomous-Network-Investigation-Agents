"""The shape of an anomaly event, shared by every signal."""

import polars as pl

# ``offline_modems`` and ``modems`` are only filled in by the share offline signal.
ANOMALY_SCHEMA: dict[str, pl.DataType] = {
    "ts": pl.Datetime("us", "UTC"),
    "tick": pl.Int64(),
    "anomaly_id": pl.String(),
    "scope_device_id": pl.String(),
    "signal": pl.String(),
    "value": pl.Float64(),
    "baseline": pl.Float64(),
    "score": pl.Float64(),
    "offline_modems": pl.Int64(),
    "modems": pl.Int64(),
}
