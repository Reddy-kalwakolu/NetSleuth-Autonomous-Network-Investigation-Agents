"""Modems whose downstream power fell well below their own recent level, per node.

This is how a partial amplifier failure shows up: nothing goes offline, but levels sag behind it.
"""

import polars as pl

from netsleuth.detector.inventory import MODEM_NODE_CTE
from netsleuth.detector.onsets import onsets, to_anomalies
from netsleuth.detector.schema import ANOMALY_SCHEMA
from netsleuth.storage import StorageSession

SIGNAL = "rf_level_drop"
BASELINE_POLLS = 4
# Two polls are enough: noise never fakes a 4 dB drop, and waiting for four would blind the
# signal to a failure in the first hour of a run.
MIN_BASELINE_POLLS = 2
MIN_DROP_DB = 4.0
MIN_DROPPED_MODEMS = 5
MIN_DROPPED_SHARE = 0.02

MODEM_RF_SQL = (
    MODEM_NODE_CTE
    + """
SELECT r.ts, r.tick, r.modem_id, m.node_id, r.ds_rx_power_dbmv
FROM cm_rf AS r JOIN modem_node AS m ON m.modem_id = r.modem_id
"""
)


def detect_rf_drop(session: StorageSession) -> pl.DataFrame:
    rf = session.query(MODEM_RF_SQL)
    if rf.is_empty():
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    # Each modem is compared with its own recent polls, never with its neighbours.
    per_modem = rf.sort("modem_id", "tick").with_columns(
        (
            pl.col("ds_rx_power_dbmv")
            .shift(1)
            .rolling_median(BASELINE_POLLS, min_samples=MIN_BASELINE_POLLS)
            .over("modem_id")
            - pl.col("ds_rx_power_dbmv")
        ).alias("drop_db")
    )
    per_node = (
        per_modem.group_by("ts", "tick", "node_id")
        .agg(
            (pl.col("drop_db") >= MIN_DROP_DB).sum().alias("dropped"),
            pl.len().alias("polled"),
        )
        .with_columns(
            (pl.col("dropped") / pl.col("polled")).alias("value"),
            pl.lit(0.0).alias("baseline"),
        )
        .with_columns(pl.col("value").alias("score"))
    )
    hit = (pl.col("dropped") >= MIN_DROPPED_MODEMS) & (pl.col("value") >= MIN_DROPPED_SHARE)
    return to_anomalies(onsets(per_node, "node_id", hit), "node_id", SIGNAL)
