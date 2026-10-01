"""The rule every signal shares: flag the first tick a condition holds, not every tick after."""

import polars as pl

from netsleuth.detector.schema import ANOMALY_SCHEMA


def trailing_median(column: str, key: str, window: int) -> pl.Expr:
    """Median of the previous ``window`` values for the same key, not counting the current one."""
    return pl.col(column).shift(1).rolling_median(window, min_samples=1).over(key)


def onsets(frame: pl.DataFrame, key: str, hit: pl.Expr) -> pl.DataFrame:
    return (
        frame.sort(key, "tick")
        .with_columns(hit.alias("_hit"))
        .with_columns(pl.col("_hit").shift(1).over(key).fill_null(False).alias("_was_hit"))
        .filter(pl.col("_hit") & ~pl.col("_was_hit"))
        .drop("_hit", "_was_hit")
    )


def to_anomalies(frame: pl.DataFrame, key: str, signal: str) -> pl.DataFrame:
    """Shape onset rows (with ``ts, tick, value, baseline, score``) into anomaly events."""

    def optional(column: str) -> pl.Expr:
        return pl.col(column) if column in frame.columns else pl.lit(None)

    return frame.select(
        "ts",
        "tick",
        pl.format("an-{}-{}-t{}", pl.lit(signal), key, "tick").alias("anomaly_id"),
        pl.col(key).alias("scope_device_id"),
        pl.lit(signal).alias("signal"),
        "value",
        "baseline",
        "score",
        optional("offline_modems").alias("offline_modems"),
        optional("modems").alias("modems"),
    ).cast(pl.Schema(ANOMALY_SCHEMA))
