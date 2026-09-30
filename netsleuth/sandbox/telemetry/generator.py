"""Telemetry for one tick of the simulated network.

Every value is a per device baseline (drawn once per run), plus a daily pattern, plus noise, plus
whatever the active faults do. Two kinds of source follow different rules:

* Measured at the CMTS, in the hub: always present. That's every modem's online state, how loud the
  CMTS hears each modem, upstream SNR per channel, and service group load.
* Sent back over the cable plant: present only while the device is reachable. That's modem RF,
  codeword counters and node optical levels. A dead amplifier shows up as missing rows.

Everything is computed with numpy across all devices at once, and anything derived from fault state
is cached until the engine applies another effect, so month long runs at the ``scale`` size stay
fast.
"""

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import polars as pl

from netsleuth.sandbox.engine import Engine
from netsleuth.sandbox.topology import Modem, Node, ServiceGroup

RF_POLL_EVERY_TICKS = 3  # modem RF every 15 minutes at 5 minute ticks
MAX_US_TX_DBMV = 57.0  # modems can't transmit louder than this

# Per device baselines. Chosen so noise and daily drift keep healthy values inside the healthy
# ranges: downstream power -7 to +7 dBmV, downstream MER 36 to 42 dB, upstream transmit 35 to 49
# dBmV, CMTS receive 0 +/- 2 dBmV, upstream MER and SNR 30 dB or better, optical -3 to +2 dBm.
DS_POWER_BASE = (-4.5, 4.5)
DS_MER_BASE = (37.5, 40.5)
US_TX_BASE = (38.0, 46.0)
US_MER_BASE = (33.0, 38.0)
US_SNR_BASE = (34.0, 38.0)
OPTICAL_BASE = (-2.0, 1.0)

DS_POWER_NOISE = 0.25
DS_MER_NOISE = 0.25
US_TX_NOISE = 0.25
CMTS_RX_NOISE = 0.3
US_MER_NOISE = 0.3
US_SNR_NOISE = 0.3
OPTICAL_NOISE = 0.1
TEMP_NOISE = 0.5
US_UTIL_NOISE = 1.5
DS_UTIL_NOISE = 2.0

THERMAL_DRIFT_DB = 0.5  # coax loses a little more signal in the afternoon heat
DS_MER_LOSS_PER_DB = 0.6  # downstream MER lost per dB of downstream signal lost
CMTS_MER_LOSS_PER_DB = 1.5  # upstream MER lost per dB a modem falls short of what it needs

CORRECTED_PER_POLL = 40.0
UNCORRECTABLE_PER_POLL = 0.05

NODE_TEMP_BASE_C = 20.0
NODE_TEMP_SWING_C = 15.0


@dataclass(frozen=True)
class TickTelemetry:
    tick: int
    ts: datetime
    cm_status: pl.DataFrame
    cm_rf: pl.DataFrame
    sg_channels: pl.DataFrame
    sg_status: pl.DataFrame
    node_optical: pl.DataFrame


TS = pl.Datetime("us", "UTC")

CM_RF_SCHEMA: dict[str, pl.DataType] = {
    "ts": TS,
    "tick": pl.Int64(),
    "modem_id": pl.String(),
    "ds_rx_power_dbmv": pl.Float64(),
    "ds_mer_db": pl.Float64(),
    "us_tx_power_dbmv": pl.Float64(),
    "corrected_cw_total": pl.Int64(),
    "uncorrectable_cw_total": pl.Int64(),
}


def evening_peak(hour: float) -> float:
    """1 at 21:00, lowest in the morning. Drives traffic."""
    return 0.5 * (1 + math.cos(2 * math.pi * (hour - 21) / 24))


def afternoon_heat(hour: float) -> float:
    """1 at 15:00, lowest at night. Drives temperature and thermal drift."""
    return 0.5 * (1 + math.cos(2 * math.pi * (hour - 15) / 24))


class TelemetryGenerator:
    def __init__(self, engine: Engine, rf_poll_every: int = RF_POLL_EVERY_TICKS) -> None:
        self.engine = engine
        self.rf_poll_every = rf_poll_every
        topo = engine.topology
        rng = engine.rng

        modems = topo.of_type(Modem)
        self._modem_ids = [m.device_id for m in modems]
        self._sgs = topo.of_type(ServiceGroup)
        sg_index = {sg.device_id: i for i, sg in enumerate(self._sgs)}
        self._modem_sg = np.array(
            [
                sg_index[
                    next(a.device_id for a in topo.ancestors(m) if a.device_type == "service_group")
                ]
                for m in self._modem_ids
            ],
            dtype=np.int64,
        )
        self._sg_ids = [sg.device_id for sg in self._sgs]
        self._modem_sg_ids = [self._sg_ids[i] for i in self._modem_sg]
        self._modems_total = np.bincount(self._modem_sg, minlength=len(self._sgs))

        n = len(modems)
        self._ds_base = rng.uniform(*DS_POWER_BASE, n)
        self._ds_mer_base = rng.uniform(*DS_MER_BASE, n)
        self._tx_base = rng.uniform(*US_TX_BASE, n)
        self._us_mer_base = rng.uniform(*US_MER_BASE, n)

        self._channel_sg = [sg.device_id for sg in self._sgs for _ in range(sg.us_channels)]
        self._channel_names = [f"us{c + 1}" for sg in self._sgs for c in range(sg.us_channels)]
        self._snr_base = rng.uniform(*US_SNR_BASE, len(self._channel_sg))

        self._nodes = topo.of_type(Node)
        self._node_ids = [node.device_id for node in self._nodes]
        self._optical_base = rng.uniform(*OPTICAL_BASE, len(self._nodes))

        self._corrected = np.zeros(n, dtype=np.int64)
        self._uncorrectable = np.zeros(n, dtype=np.int64)

        self._revision = -1
        self._modem_up = np.ones(n, dtype=bool)
        self._node_up = np.ones(len(self._nodes), dtype=bool)
        self._ds_offset = np.zeros(n)
        self._us_offset = np.zeros(n)

    def _refresh_fault_state(self) -> None:
        if self.engine.revision == self._revision:
            return
        unreachable = self.engine.unreachable
        self._modem_up = np.array([m not in unreachable for m in self._modem_ids], dtype=bool)
        self._node_up = np.array([nd not in unreachable for nd in self._node_ids], dtype=bool)
        offsets = [self.engine.level_offsets(m) for m in self._modem_ids]
        self._ds_offset = np.array([ds for ds, _ in offsets])
        self._us_offset = np.array([us for _, us in offsets])
        self._revision = self.engine.revision

    def emit(self) -> TickTelemetry:
        """Telemetry for the engine's current tick."""
        self._refresh_fault_state()
        engine = self.engine
        rng = engine.rng
        tick = engine.tick
        ts = engine.time_of(tick)
        hour = ts.hour + ts.minute / 60
        evening = evening_peak(hour)
        heat = afternoon_heat(hour)
        n = len(self._modem_ids)
        up = self._modem_up

        # Modem levels. Noise is drawn for every modem every tick, so the random stream doesn't
        # depend on which devices happen to be up.
        ds_power = (
            self._ds_base
            - THERMAL_DRIFT_DB * heat
            + self._ds_offset
            + rng.normal(0, DS_POWER_NOISE, n)
        )
        ds_mer = (
            self._ds_mer_base
            + rng.normal(0, DS_MER_NOISE, n)
            - DS_MER_LOSS_PER_DB * np.maximum(0.0, -self._ds_offset)
        )
        needed_tx = (
            self._tx_base
            + THERMAL_DRIFT_DB * heat
            + self._us_offset
            + rng.normal(0, US_TX_NOISE, n)
        )
        us_tx = np.minimum(needed_tx, MAX_US_TX_DBMV)
        shortfall = np.maximum(0.0, needed_tx - MAX_US_TX_DBMV)
        cmts_rx = rng.normal(0, CMTS_RX_NOISE, n) - shortfall
        us_mer = (
            self._us_mer_base + rng.normal(0, US_MER_NOISE, n) - CMTS_MER_LOSS_PER_DB * shortfall
        )

        cm_status = pl.DataFrame(
            {
                "modem_id": self._modem_ids,
                "sg_id": self._modem_sg_ids,
                "online": up,
                "us_rx_power_dbmv": np.where(up, cmts_rx, np.nan),
                "us_rx_mer_db": np.where(up, us_mer, np.nan),
            }
        ).with_columns(pl.col("us_rx_power_dbmv", "us_rx_mer_db").fill_nan(None))

        # Service group load falls with the share of modems still online.
        online = np.bincount(self._modem_sg, weights=up, minlength=len(self._sgs))
        share = np.divide(
            online, self._modems_total, out=np.zeros(len(self._sgs)), where=self._modems_total > 0
        )
        sg_count = len(self._sgs)
        us_util = np.clip(
            share * (8 + 40 * evening) + rng.normal(0, US_UTIL_NOISE, sg_count), 0, 100
        )
        ds_util = np.clip(
            share * (15 + 55 * evening) + rng.normal(0, DS_UTIL_NOISE, sg_count), 0, 100
        )
        sg_status = pl.DataFrame(
            {
                "sg_id": self._sg_ids,
                "us_util_pct": us_util,
                "ds_util_pct": ds_util,
                "modems_online": online.astype(np.int64),
                "modems_total": self._modems_total,
            }
        )

        sg_channels = pl.DataFrame(
            {
                "sg_id": self._channel_sg,
                "channel": self._channel_names,
                "us_snr_db": self._snr_base + rng.normal(0, US_SNR_NOISE, len(self._channel_sg)),
            }
        )

        node_count = len(self._nodes)
        node_optical = pl.DataFrame(
            {
                "node_id": self._node_ids,
                "optical_rx_dbm": self._optical_base + rng.normal(0, OPTICAL_NOISE, node_count),
                "temperature_c": NODE_TEMP_BASE_C
                + NODE_TEMP_SWING_C * heat
                + rng.normal(0, TEMP_NOISE, node_count),
            }
        ).filter(pl.Series(self._node_up))

        # Codeword counters are cumulative. They climb faster as downstream MER falls, only count
        # while the modem is reachable, and are read when the modem is polled.
        if tick % self.rf_poll_every == 0:
            corrected_rate = CORRECTED_PER_POLL * np.exp(0.8 * np.maximum(0.0, 36.0 - ds_mer))
            uncorrectable_rate = UNCORRECTABLE_PER_POLL * np.exp(
                1.2 * np.maximum(0.0, 35.0 - ds_mer)
            )
            self._corrected += np.where(up, rng.poisson(corrected_rate), 0)
            self._uncorrectable += np.where(up, rng.poisson(uncorrectable_rate), 0)
            cm_rf = pl.DataFrame(
                {
                    "modem_id": self._modem_ids,
                    "ds_rx_power_dbmv": ds_power,
                    "ds_mer_db": ds_mer,
                    "us_tx_power_dbmv": us_tx,
                    # Copies: polars can share memory with numpy, and the totals keep growing.
                    "corrected_cw_total": self._corrected.copy(),
                    "uncorrectable_cw_total": self._uncorrectable.copy(),
                }
            ).filter(pl.Series(up))
        else:
            cm_rf = pl.DataFrame(
                schema={k: v for k, v in CM_RF_SCHEMA.items() if k not in ("ts", "tick")}
            )

        return TickTelemetry(
            tick=tick,
            ts=ts,
            cm_status=_stamp(cm_status, tick, ts),
            cm_rf=_stamp(cm_rf, tick, ts),
            sg_channels=_stamp(sg_channels, tick, ts),
            sg_status=_stamp(sg_status, tick, ts),
            node_optical=_stamp(node_optical, tick, ts),
        )


def _stamp(frame: pl.DataFrame, tick: int, ts: datetime) -> pl.DataFrame:
    """Put ``ts`` and ``tick`` first on every row."""
    return frame.select(
        pl.lit(ts, dtype=TS).alias("ts"),
        pl.lit(tick, dtype=pl.Int64()).alias("tick"),
        pl.all(),
    )
