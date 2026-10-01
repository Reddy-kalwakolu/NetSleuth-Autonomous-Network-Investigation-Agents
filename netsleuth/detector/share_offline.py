"""Share of modems offline per fiber node.

It reads only what the storage layer gives it, through a session with a time cutoff, the way a
separate detection team would consume the data lake. Offline counts come from the CMTS view
(``cm_status``), which is always present, so a dark part of the plant still shows up. Each modem's
node comes from the stored inventory, so inventory drift can mislead it, as it would in production.

A node is flagged when its share offline rises at least ``MIN_SHARE_JUMP`` above its own recent
median and at least ``MIN_OFFLINE_MODEMS`` modems are down. A lasting outage raises one anomaly, at
its onset. Partial failures, where nothing goes offline, are left to the RF signal.
"""

import polars as pl

from netsleuth.detector.inventory import MODEM_NODE_CTE
from netsleuth.detector.onsets import onsets, to_anomalies, trailing_median
from netsleuth.detector.schema import ANOMALY_SCHEMA
from netsleuth.storage import StorageSession

SIGNAL = "share_offline"
BASELINE_TICKS = 12  # one hour of history at 5 minute ticks
MIN_SHARE_JUMP = 0.02
MIN_OFFLINE_MODEMS = 5

NODE_OFFLINE_SQL = (
    MODEM_NODE_CTE
    + """
SELECT
    s.ts,
    s.tick,
    m.node_id,
    count(*) AS modems,
    count(*) FILTER (WHERE NOT s.online) AS offline_modems
FROM cm_status AS s
JOIN modem_node AS m ON m.modem_id = s.modem_id
GROUP BY s.ts, s.tick, m.node_id
"""
)


def detect_share_offline(session: StorageSession) -> pl.DataFrame:
    counts = session.query(NODE_OFFLINE_SQL)
    if counts.is_empty():
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    frame = (
        counts.with_columns((pl.col("offline_modems") / pl.col("modems")).alias("value"))
        .sort("node_id", "tick")
        .with_columns(
            trailing_median("value", "node_id", BASELINE_TICKS).fill_null(0.0).alias("baseline")
        )
        .with_columns((pl.col("value") - pl.col("baseline")).alias("score"))
    )
    hit = (pl.col("score") >= MIN_SHARE_JUMP) & (pl.col("offline_modems") >= MIN_OFFLINE_MODEMS)
    return to_anomalies(onsets(frame, "node_id", hit), "node_id", SIGNAL)
