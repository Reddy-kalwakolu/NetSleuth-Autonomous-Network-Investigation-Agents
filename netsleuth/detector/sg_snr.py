"""Upstream SNR on a service group's low channels falling well below its own recent level.

Measured at the CMTS, so it is always present. It is the clearest sign of ingress noise.
"""

import polars as pl

from netsleuth.detector.onsets import onsets, to_anomalies, trailing_median
from netsleuth.detector.schema import ANOMALY_SCHEMA
from netsleuth.storage import StorageSession

SIGNAL = "sg_snr"
BASELINE_TICKS = 12
MIN_SNR_DROP_DB = 4.0

LOW_CHANNEL_SNR_SQL = """
SELECT ts, tick, sg_id, min(us_snr_db) AS value
FROM sg_channels
WHERE channel IN ('us1', 'us2')
GROUP BY ts, tick, sg_id
"""


def detect_sg_snr(session: StorageSession) -> pl.DataFrame:
    snr = session.query(LOW_CHANNEL_SNR_SQL)
    if snr.is_empty():
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    frame = (
        snr.sort("sg_id", "tick")
        .with_columns(
            trailing_median("value", "sg_id", BASELINE_TICKS)
            .fill_null(pl.col("value"))
            .alias("baseline")
        )
        .with_columns((pl.col("baseline") - pl.col("value")).alias("score"))
    )
    hit = pl.col("score") >= MIN_SNR_DROP_DB
    return to_anomalies(onsets(frame, "sg_id", hit), "sg_id", SIGNAL)
