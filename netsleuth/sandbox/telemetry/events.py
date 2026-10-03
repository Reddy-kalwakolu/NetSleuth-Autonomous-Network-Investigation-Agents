"""Data sources added in milestone 3a: power supply status, utility power events, the change log,
peering link status and customer tickets.

They draw from their own random stream, seeded from the run's seed alone. Nothing here touches
the engine's main stream or the generator's event stream, so every table that existed before
stays exactly the same for runs that don't use the new primitives.

* Power supply transponders report over the plant, through the node or amplifier they sit with,
  so a supply goes silent when that active is unreachable, including when its own battery dies.
* The utility power feed, the change log and peering link counters come from outside the plant
  and are always present.
* Tickets follow customer impact. Customers whose service died call within the hour more often
  than not, once per outage. People whose home has no power rarely call about their internet.
  Noisy upstream brings a trickle of "intermittent" calls, a congested peering link brings "slow"
  calls from everywhere at once, and there is always some unrelated background.
"""

import numpy as np
import polars as pl

from netsleuth.sandbox.engine import Engine
from netsleuth.sandbox.topology import Modem
from netsleuth.sandbox.topology.models import PeeringLink, PowerSupply

STREAM = 3  # this module's random stream, next to the run's seed

BATTERY_FULL_V = 54.0
BATTERY_EMPTY_V = 46.0
BATTERY_NOISE_V = 0.1

PEERING_BASE = (20.0, 35.0)  # off peak utilization, percent
PEERING_EVENING = 35.0  # extra utilization at evening peak
PEERING_NOISE = 1.5
LATENCY_BASE_MS = (8.0, 15.0)
LATENCY_MS_PER_PCT_OVER = 4.0  # queueing delay once a link is past 85%
LATENCY_KNEE_PCT = 85.0
DROP_KNEE_PCT = 95.0
DROP_PCT_PER_PCT_OVER = 0.5
CONGESTED_PCT = 92.0

CALL_DELAY_TICKS = 2  # nobody calls the moment service drops
NO_SERVICE_CALL_PER_TICK = 0.04
NO_POWER_CALL_PER_TICK = 0.002
INTERMITTENT_CALL_PER_TICK = 0.0005
SLOW_CALL_PER_TICK = 0.0004
NOISY_SNR_DB = 30.0
BACKGROUND_TICKETS_PER_TICK = 0.15
BACKGROUND_KINDS = ("no_service", "slow_service", "intermittent", "billing")

PS_STATUS_SCHEMA: dict[str, pl.DataType] = {
    "ps_id": pl.String(),
    "power_area": pl.String(),
    "ac_ok": pl.Boolean(),
    "on_battery": pl.Boolean(),
    "battery_min_left": pl.Float64(),
    "battery_v": pl.Float64(),
}
POWER_EVENTS_SCHEMA: dict[str, pl.DataType] = {"power_area": pl.String(), "event": pl.String()}
CHANGE_LOG_SCHEMA: dict[str, pl.DataType] = {
    "change_id": pl.String(),
    "target_id": pl.String(),
    "description": pl.String(),
}
PEERING_SCHEMA: dict[str, pl.DataType] = {
    "link_id": pl.String(),
    "util_pct": pl.Float64(),
    "latency_ms": pl.Float64(),
    "drop_pct": pl.Float64(),
}
TICKETS_SCHEMA: dict[str, pl.DataType] = {
    "ticket_id": pl.String(),
    "customer_id": pl.String(),
    "modem_id": pl.String(),
    "kind": pl.String(),
}


class EventSources:
    def __init__(self, engine: Engine, modem_ids: list[str], modem_sg: np.ndarray) -> None:
        self.engine = engine
        topo = engine.topology
        self.rng = np.random.default_rng([engine.seed, STREAM])

        self._supplies = topo.of_type(PowerSupply)
        self._links = [link.device_id for link in topo.of_type(PeeringLink)]
        self._link_base = self.rng.uniform(*PEERING_BASE, len(self._links))
        self._link_latency = self.rng.uniform(*LATENCY_BASE_MS, len(self._links))

        self._modem_ids = np.array(modem_ids)
        self._modem_sg = modem_sg
        modems = {m.device_id: m for m in topo.of_type(Modem)}
        self._customer_ids = np.array([modems[m].customer_id for m in modem_ids])
        self._modem_area = [modems[m].power_area for m in modem_ids]
        self._areas = sorted(set(self._modem_area))
        n = len(modem_ids)
        self._down_since = np.full(n, -1, dtype=np.int64)  # tick service dropped, or -1
        self._called = np.zeros(n, dtype=bool)  # already called about this outage
        self._called_noise = np.zeros(n, dtype=bool)
        self._called_slow = np.zeros(n, dtype=bool)
        self._ticket_count = 0

    def emit(
        self, tick: int, evening: float, up: np.ndarray, sg_low_snr: np.ndarray
    ) -> dict[str, pl.DataFrame]:
        return {
            "ps_status": self._ps_status(),
            "power_events": pl.DataFrame(
                [
                    {"power_area": area, "event": event}
                    for t, area, event in self.engine.power_events
                    if t == tick
                ],
                schema=POWER_EVENTS_SCHEMA,
            ),
            "change_log": pl.DataFrame(
                [
                    {
                        "change_id": c.change_id,
                        "target_id": c.target_id,
                        "description": c.description,
                    }
                    for c in self.engine.change_log
                    if c.tick == tick
                ],
                schema=CHANGE_LOG_SCHEMA,
            ),
            **self._peering_and_tickets(tick, evening, up, sg_low_snr),
        }

    def _ps_status(self) -> pl.DataFrame:
        unreachable = self.engine.unreachable
        rows = []
        noise = self.rng.normal(0, BATTERY_NOISE_V, len(self._supplies))
        for supply, jitter in zip(self._supplies, noise, strict=True):
            if supply.feeds and supply.feeds[0] in unreachable:
                continue  # its transponder talks through that active
            state = self.engine.power_supply_state(supply.device_id)
            charge = state.battery_min_left / supply.battery_runtime_min
            volts = (
                BATTERY_FULL_V
                if state.ac_ok
                else BATTERY_EMPTY_V + (BATTERY_FULL_V - BATTERY_EMPTY_V) * charge
            )
            rows.append(
                {
                    "ps_id": supply.device_id,
                    "power_area": supply.power_area,
                    "ac_ok": state.ac_ok,
                    "on_battery": state.on_battery,
                    "battery_min_left": state.battery_min_left,
                    "battery_v": volts + jitter,
                }
            )
        return pl.DataFrame(rows, schema=PS_STATUS_SCHEMA)

    def _peering_and_tickets(
        self, tick: int, evening: float, up: np.ndarray, sg_low_snr: np.ndarray
    ) -> dict[str, pl.DataFrame]:
        rng = self.rng
        k = len(self._links)
        normal = self._link_base + PEERING_EVENING * evening + rng.normal(0, PEERING_NOISE, k)
        loads = [self.engine.peering_load_pct(link) for link in self._links]
        util = np.clip(
            [
                normal[i] if load is None else load + rng.normal(0, 0.5)
                for i, load in enumerate(loads)
            ],
            0,
            100,
        )
        latency = self._link_latency + LATENCY_MS_PER_PCT_OVER * np.maximum(
            0.0, util - LATENCY_KNEE_PCT
        )
        drops = DROP_PCT_PER_PCT_OVER * np.maximum(0.0, util - DROP_KNEE_PCT)
        peering = pl.DataFrame(
            {"link_id": self._links, "util_pct": util, "latency_ms": latency, "drop_pct": drops},
            schema=PEERING_SCHEMA,
        )

        # Tickets. One uniform draw per modem per tick, so the stream never depends on state.
        n = len(self._modem_ids)
        draw = rng.random(n)
        dark = ~up
        newly_down = dark & (self._down_since < 0)
        self._down_since[newly_down] = tick
        back = up & (self._down_since >= 0)
        self._down_since[back] = -1
        self._called[back] = False

        no_power_areas = {a for a in self._areas if self.engine.area_out(a)}
        no_power = np.array([a in no_power_areas for a in self._modem_area], dtype=bool)
        waited = dark & (self._down_since >= 0) & (tick - self._down_since >= CALL_DELAY_TICKS)
        chance = np.where(no_power, NO_POWER_CALL_PER_TICK, NO_SERVICE_CALL_PER_TICK)
        no_service = waited & ~self._called & (draw < chance)
        self._called |= no_service

        noisy = up & (sg_low_snr[self._modem_sg] < NOISY_SNR_DB) & ~self._called_noise
        intermittent = noisy & (draw < INTERMITTENT_CALL_PER_TICK)
        self._called_noise |= intermittent

        congested = bool((util >= CONGESTED_PCT).any())
        slow = up & ~self._called_slow & congested & (draw < SLOW_CALL_PER_TICK)
        self._called_slow |= slow

        picked: list[tuple[int, str]] = [(int(i), "no_service") for i in np.flatnonzero(no_service)]
        picked += [(int(i), "intermittent") for i in np.flatnonzero(intermittent)]
        picked += [(int(i), "slow_service") for i in np.flatnonzero(slow)]
        background = int(rng.poisson(BACKGROUND_TICKETS_PER_TICK))
        for i, kind in zip(
            rng.integers(0, n, background),
            rng.integers(0, len(BACKGROUND_KINDS), background),
            strict=True,
        ):
            picked.append((int(i), BACKGROUND_KINDS[int(kind)]))

        rows = []
        for i, kind in picked:
            self._ticket_count += 1
            rows.append(
                {
                    "ticket_id": f"tk-{self._ticket_count:06d}",
                    "customer_id": str(self._customer_ids[i]),
                    "modem_id": str(self._modem_ids[i]),
                    "kind": kind,
                }
            )
        return {"peering_status": peering, "tickets": pl.DataFrame(rows, schema=TICKETS_SCHEMA)}
