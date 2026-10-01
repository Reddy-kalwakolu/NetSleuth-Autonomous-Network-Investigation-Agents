"""The state engine: a tick clock, device state, and reachability.

A device is unreachable if it, or anything on its path to the hub, is down. Unreachable devices
send no telemetry of their own, which is how a dead amplifier shows up in the data: as silence.

Reachability only changes when a device's status changes, so it is recomputed then rather than
every tick. That keeps month long runs at the ``scale`` size fast.

Power (utility outages, batteries) arrives with the power primitives, and forking arrives with the
closed loop.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from netsleuth.sandbox.engine.faults import Fault
from netsleuth.sandbox.engine.primitives import (
    HEALTHY,
    AddUpstreamNoise,
    AnyEffect,
    CutFiberRoute,
    DegradeLevels,
    DeviceState,
    EngineError,
    MaintenanceWindow,
    Restore,
    TakeDown,
    effect_targets,
)
from netsleuth.sandbox.topology import Node, ServiceGroup, Topology

DEFAULT_START = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CalendarEntry:
    window_id: str
    scope_id: str
    start_tick: int
    end_tick: int
    published_tick: int


def _hour_in_window(hour: float, start_hour: int, end_hour: int) -> bool:
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # wraps midnight


class Engine:
    def __init__(
        self,
        topology: Topology,
        faults: Sequence[Fault],
        seed: int,
        *,
        start: datetime = DEFAULT_START,
        tick_minutes: int = 5,
    ) -> None:
        self.topology = topology
        self.faults = tuple(faults)
        self.seed = seed
        self.start = start
        self.tick_minutes = tick_minutes
        self.rng = np.random.default_rng(seed)
        self.tick = 0
        # Goes up whenever an effect is applied, so readers can cache anything derived from state.
        self.revision = 0

        self._states: dict[str, DeviceState] = {}
        self._offsets: dict[str, tuple[float, float]] = {}
        self._unreachable: frozenset[str] = frozenset()
        self._schedule: dict[int, list[AnyEffect]] = defaultdict(list)
        self._cut_routes: set[str] = set()
        self._noise: dict[str, list[AddUpstreamNoise]] = defaultdict(list)
        self._calendar: list[CalendarEntry] = []
        self._nodes_on_route: dict[str, list[str]] = defaultdict(list)
        for node in topology.of_type(Node):
            self._nodes_on_route[node.fiber_route].append(node.device_id)

        for fault in self.faults:
            for scheduled in fault.effects:
                self._validate(fault.fault_id, scheduled.effect)
                self._schedule[scheduled.at_tick].append(scheduled.effect)

        self._apply_current_tick()

    # ---------- clock ----------

    def step(self) -> None:
        self.tick += 1
        self._apply_current_tick()

    def run_until(self, tick: int) -> None:
        if tick < self.tick:
            raise EngineError(f"can't go back from tick {self.tick} to {tick}")
        while self.tick < tick:
            self.step()

    def time_of(self, tick: int) -> datetime:
        return self.start + timedelta(minutes=tick * self.tick_minutes)

    # ---------- queries ----------

    @property
    def calendar(self) -> tuple[CalendarEntry, ...]:
        return tuple(self._calendar)

    def us_noise_db(self, service_group_id: str) -> float:
        """Upstream SNR lost to ingress on a service group at the current tick."""
        now = self.time_of(self.tick)
        hour = now.hour + now.minute / 60
        return sum(
            n.snr_drop_db
            for n in self._noise.get(service_group_id, [])
            if _hour_in_window(hour, n.start_hour, n.end_hour)
        )

    @property
    def unreachable(self) -> frozenset[str]:
        return self._unreachable

    def state(self, device_id: str) -> DeviceState:
        return self._states.get(device_id, HEALTHY)

    def level_offsets(self, device_id: str) -> tuple[float, float]:
        """Total ``(downstream dB, upstream dB)`` shift from every scope the device sits under."""
        ds, us = self._offsets.get(device_id, (0.0, 0.0))
        for ancestor in self.topology.ancestors(device_id):
            a_ds, a_us = self._offsets.get(ancestor.device_id, (0.0, 0.0))
            ds += a_ds
            us += a_us
        return ds, us

    # ---------- applying effects ----------

    def _validate(self, fault_id: str, effect: AnyEffect) -> None:
        for target in effect_targets(effect):
            if target not in self.topology:
                raise EngineError(f"{fault_id} targets unknown device {target}")
        match effect:
            case CutFiberRoute(route=route) if route not in self._nodes_on_route:
                raise EngineError(f"{fault_id} cuts unknown fiber route {route}")
            case AddUpstreamNoise(service_group_id=sg_id) if not isinstance(
                self.topology[sg_id], ServiceGroup
            ):
                raise EngineError(f"{fault_id}: upstream noise needs a service group, not {sg_id}")
            case _:
                pass

    def _apply_current_tick(self) -> None:
        status_changed = False
        effects = self._schedule.pop(self.tick, [])
        for effect in effects:
            status_changed |= self._apply(effect)
        if effects:
            self.revision += 1
        if status_changed:
            self._recompute_reachability()

    def _apply(self, effect: AnyEffect) -> bool:
        """Apply one effect. Returns whether any device status changed."""
        match effect:
            case TakeDown(device_id=device_id):
                self._states[device_id] = DeviceState(status="down", severity=1.0)
                return True
            case DegradeLevels(scope_id=scope_id, ds_db=ds_db, us_db=us_db):
                ds, us = self._offsets.get(scope_id, (0.0, 0.0))
                self._offsets[scope_id] = (ds + ds_db, us + us_db)
                if self.state(scope_id).status == "healthy":
                    self._states[scope_id] = DeviceState(
                        status="degraded", severity=max(abs(ds_db), abs(us_db))
                    )
                    return True
                return False
            case Restore(device_id=device_id):
                self._states.pop(device_id, None)
                return True
            case CutFiberRoute(route=route):
                self._cut_routes.add(route)
                return True
            case AddUpstreamNoise():
                self._noise[effect.service_group_id].append(effect)
                return False
            case MaintenanceWindow():
                self._calendar.append(
                    CalendarEntry(
                        window_id=effect.window_id,
                        scope_id=effect.scope_id,
                        start_tick=effect.start_tick,
                        end_tick=effect.end_tick,
                        published_tick=self.tick,
                    )
                )
                return False

    def _recompute_reachability(self) -> None:
        roots = [d for d, state in self._states.items() if state.status == "down"]
        roots += [n for route in self._cut_routes for n in self._nodes_on_route[route]]
        cut: set[str] = set()
        for device_id in roots:
            if device_id not in cut:
                cut.add(device_id)
                cut.update(d.device_id for d in self.topology.subtree(device_id))
        self._unreachable = frozenset(cut)
