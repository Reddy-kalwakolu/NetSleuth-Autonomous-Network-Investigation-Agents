"""Share of a node's modems that logged a T3 timeout in the last hour.

The event log travels in band, so this goes quiet during a full outage and matters most for
intermittent faults like ingress noise.
"""

import polars as pl

from netsleuth.detector.inventory import MODEM_NODE_CTE
from netsleuth.detector.onsets import onsets, to_anomalies, trailing_median
from netsleuth.detector.schema import ANOMALY_SCHEMA
from netsleuth.storage import StorageSession

SIGNAL = "t3_rate"
WINDOW_TICKS = 12
MIN_SHARE = 0.05
MIN_JUMP = 0.03

NODE_SIZES_SQL = (
    MODEM_NODE_CTE + "SELECT node_id, count(*) AS modems FROM modem_node GROUP BY node_id"
)
T3_COUNTS_SQL = (
    MODEM_NODE_CTE
    + """
SELECT e.tick, m.node_id, count(DISTINCT e.modem_id) AS t3_modems
FROM cm_events AS e JOIN modem_node AS m ON m.modem_id = e.modem_id
WHERE e.event = 'T3'
GROUP BY e.tick, m.node_id
"""
)
TICKS_SQL = "SELECT DISTINCT ts, tick FROM sg_status"


def detect_t3_rate(session: StorageSession) -> pl.DataFrame:
    if "cm_events" not in session.tables:
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    grid = session.query(TICKS_SQL).join(session.query(NODE_SIZES_SQL), how="cross")
    counts = session.query(T3_COUNTS_SQL)
    frame = (
        grid.join(counts, on=["tick", "node_id"], how="left")
        .with_columns(pl.col("t3_modems").fill_null(0))
        .sort("node_id", "tick")
        .with_columns(
            (
                pl.col("t3_modems").rolling_sum(WINDOW_TICKS, min_samples=1).over("node_id")
                / pl.col("modems")
            ).alias("value")
        )
        .with_columns(
            trailing_median("value", "node_id", WINDOW_TICKS).fill_null(0.0).alias("baseline")
        )
        .with_columns((pl.col("value") - pl.col("baseline")).alias("score"))
    )
    hit = (pl.col("value") >= MIN_SHARE) & (pl.col("score") >= MIN_JUMP)
    return to_anomalies(onsets(frame, "node_id", hit), "node_id", SIGNAL)
