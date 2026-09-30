"""A deliberately simple anomaly detector: share of modems offline per fiber node.

It reads only what the storage layer gives it, through a session with a time cutoff, the way a
separate detection team would consume the data lake. Offline counts come from the CMTS view
(``cm_status``), which is always present, so a dark part of the plant still shows up. Each modem's
node comes from the stored inventory, so inventory drift can mislead it, as it would in production.

A node is flagged when its share offline rises at least ``MIN_SHARE_JUMP`` above its own recent
median and at least ``MIN_OFFLINE_MODEMS`` modems are down. A lasting outage raises one anomaly, at
its onset.

It misses things on purpose: partial failures, where nothing goes offline, need RF signals. The
T4 timeout rate joins it with the modem event log, for intermittent faults.
"""

import polars as pl

from netsleuth.storage import StorageSession

SIGNAL = "share_offline"
BASELINE_TICKS = 12  # one hour of history at 5 minute ticks
MIN_SHARE_JUMP = 0.02
MIN_OFFLINE_MODEMS = 5

# Walks each modem up the stored inventory to its node, then counts offline modems per node and
# tick. Plain SQL with a recursive CTE, so the Athena backend can run it too.
NODE_OFFLINE_SQL = """
WITH RECURSIVE up (modem_id, device_id) AS (
    SELECT device_id, parent_id FROM topology_devices WHERE device_type = 'modem'
    UNION ALL
    SELECT up.modem_id, d.parent_id
    FROM up JOIN topology_devices AS d ON d.device_id = up.device_id
    WHERE d.device_type <> 'node'
),
modem_node AS (
    SELECT up.modem_id, up.device_id AS node_id
    FROM up JOIN topology_devices AS n ON n.device_id = up.device_id
    WHERE n.device_type = 'node'
)
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


def detect_anomalies(session: StorageSession) -> pl.DataFrame:
    """Anomaly events for every node, up to the session's ``as_of``."""
    counts = session.query(NODE_OFFLINE_SQL)
    if counts.is_empty():
        return pl.DataFrame(schema=ANOMALY_SCHEMA)

    per_node = pl.col("node_id")
    flagged = (
        counts.sort("node_id", "tick")
        .with_columns((pl.col("offline_modems") / pl.col("modems")).alias("value"))
        .with_columns(
            pl.col("value")
            .shift(1)
            .rolling_median(BASELINE_TICKS, min_samples=1)
            .over(per_node)
            .fill_null(0.0)
            .alias("baseline")
        )
        .with_columns((pl.col("value") - pl.col("baseline")).alias("score"))
        .with_columns(
            (
                (pl.col("score") >= MIN_SHARE_JUMP)
                & (pl.col("offline_modems") >= MIN_OFFLINE_MODEMS)
            ).alias("hit")
        )
        .with_columns(pl.col("hit").shift(1).over(per_node).fill_null(False).alias("was_hit"))
        .filter(pl.col("hit") & ~pl.col("was_hit"))
    )

    return flagged.select(
        "ts",
        "tick",
        pl.format("an-{}-t{}", "node_id", "tick").alias("anomaly_id"),
        pl.col("node_id").alias("scope_device_id"),
        pl.lit(SIGNAL).alias("signal"),
        "value",
        "baseline",
        "score",
        "offline_modems",
        "modems",
    ).cast(pl.Schema(ANOMALY_SCHEMA))
