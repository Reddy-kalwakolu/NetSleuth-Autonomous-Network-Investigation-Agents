"""Anomaly detection over telemetry. Each signal is independent and deliberately simple."""

import polars as pl

from netsleuth.detector.rf_drop import detect_rf_drop
from netsleuth.detector.schema import ANOMALY_SCHEMA
from netsleuth.detector.sg_snr import detect_sg_snr
from netsleuth.detector.share_offline import (
    BASELINE_TICKS,
    MIN_OFFLINE_MODEMS,
    MIN_SHARE_JUMP,
    detect_share_offline,
)
from netsleuth.detector.t3_rate import detect_t3_rate
from netsleuth.storage import StorageSession

DETECTORS = (detect_share_offline, detect_sg_snr, detect_t3_rate, detect_rf_drop)


def detect_anomalies(session: StorageSession) -> pl.DataFrame:
    """Every signal's anomaly events up to the session's ``as_of``."""
    found = [detector(session) for detector in DETECTORS]
    return pl.concat(found).sort("ts", "scope_device_id")


__all__ = [
    "ANOMALY_SCHEMA",
    "BASELINE_TICKS",
    "DETECTORS",
    "MIN_OFFLINE_MODEMS",
    "MIN_SHARE_JUMP",
    "detect_anomalies",
]
