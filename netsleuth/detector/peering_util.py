"""A peering link running near full. Measured on the backbone, so it is always present.

Ordinary evening peaks stay well under the threshold, so an anomaly here means the link itself is
the bottleneck, which shows up downstream as slow service everywhere and healthy RF.
"""

import polars as pl

from netsleuth.detector.onsets import onsets, to_anomalies, trailing_median
from netsleuth.detector.schema import ANOMALY_SCHEMA
from netsleuth.storage import StorageSession

SIGNAL = "peering_util"
BASELINE_TICKS = 12
MIN_UTIL_PCT = 90.0


def detect_peering_util(session: StorageSession) -> pl.DataFrame:
    if "peering_status" not in session.tables:
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    util = session.query("SELECT ts, tick, link_id, util_pct AS value FROM peering_status")
    frame = (
        util.sort("link_id", "tick")
        .with_columns(
            trailing_median("value", "link_id", BASELINE_TICKS)
            .fill_null(pl.col("value"))
            .alias("baseline")
        )
        .with_columns((pl.col("value") - pl.col("baseline")).alias("score"))
    )
    hit = pl.col("value") >= MIN_UTIL_PCT
    return to_anomalies(onsets(frame, "link_id", hit), "link_id", SIGNAL)
