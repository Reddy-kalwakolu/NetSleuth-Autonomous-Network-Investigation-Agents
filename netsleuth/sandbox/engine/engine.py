"""The state engine: a tick clock, device state, and reachability.

A device is unreachable if it, or anything on its path to the hub, is down. Unreachable devices
send no telemetry of their own, which is how a dead amplifier shows up in the data: as silence.

Reachability only changes when a device's status changes, so it is recomputed then rather than
every tick. That keeps month long runs at the ``scale`` size fast.

Power: a utility outage cuts the homes in a power area, so their modems drop, and puts the area's
power supplies on battery. A supply whose battery runs out stops powering the node and amplifiers
it feeds, and everything behind them goes dark. Batteries recharge once power is back.

Forking arrives with the closed loop.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from netsleuth.sandbox.engine.clock import DEFAULT_START, TICK_MINUTES, hour_in_window
from netsleuth.sandbox.engine.faults import Fault
from netsleuth.sandbox.engine.primitives import (
    HEALTHY,
    AddUpstreamNoise,
    AnyEffect,
    ConfigChange,
    CutFiberRoute,
    DegradeLevels,
    DeviceState,
    EngineError,
    MaintenanceWindow,
    PeeringLoad,
    Restore,
    TakeDown,
    UtilityOutage,
    effect_targets,
)
from netsleuth.sandbox.topology import Cmts, Modem, Node, ServiceGroup, Topology
from netsleuth.sandbox.topology.models import PeeringLink, PowerSupply

RECHARGE_HOURS = 8  # a drained battery is full again this long after utility power returns


@dataclass(frozen=True)
class PowerSupplyState:
    ac_ok: bool
    on_battery: bool
    battery_min_left: float


@dataclass(frozen=True)
class ChangeLogEntry:
    change_id: str
    target_id: str
    description: str
    tick: int


@dataclass(frozen=True)
class CalendarEntry:
    window_id: str
    scope_id: str
    start_tick: int
    end_tick: int
    published_tick: int


class Engine:
    def __init__(
        self,
        topology: Topology,
        faults: Sequence[Fault],
        seed: int,
        *,
        start: datetime = DEFAULT_START,
        tick_minutes: int = TICK_MINUTES,
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

        # Power. Areas are known from the homes and supplies placed in them.
        self._supplies = topology.of_type(PowerSupply)
        self._modems_in_area: dict[str, list[str]] = defaultdict(list)
        for modem in topology.of_type(Modem):
            self._modems_in_area[modem.power_area].append(modem.device_id)
        self._areas = set(self._modems_in_area) | {p.power_area for p in self._supplies}
        self._area_out_until: dict[str, int] = {}
        self._battery = {p.device_id: float(p.battery_runtime_min) for p in self._supplies}
        self._drained: set[str] = set()
        self._power_events: list[tuple[int, str, str]] = []

        self._config_snr: dict[str, float] = defaultdict(float)
        self._change_log: list[ChangeLogEntry] = []
        self._peering: dict[str, list[PeeringLoad]] = defaultdict(list)

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

    def area_out(self, power_area: str) -> bool:
        return power_area in self._area_out_until

    def power_supply_state(self, ps_id: str) -> PowerSupplyState:
        supply = self.topology[ps_id]
        assert isinstance(supply, PowerSupply)
        ac_ok = not self.area_out(supply.power_area)
        left = self._battery[ps_id]
        return PowerSupplyState(
            ac_ok=ac_ok, on_battery=not ac_ok and left > 0, battery_min_left=left
        )

    @property
    def power_events(self) -> tuple[tuple[int, str, str], ...]:
        """``(tick, power area, "outage_start" or "restored")``, oldest first."""
        return tuple(self._power_events)

    @property
    def change_log(self) -> tuple[ChangeLogEntry, ...]:
        return tuple(self._change_log)

    def config_snr_db(self, service_group_id: str) -> float:
        """Upstream SNR lost on every channel of a service group to configuration pushes."""
        return self._config_snr.get(service_group_id, 0.0)

    def peering_load_pct(self, link_id: str) -> float | None:
        """The utilization an active peering load pushes a link toward, if one is active now."""
        now = self.time_of(self.tick)
        hour = now.hour + now.minute / 60
        active = [
            p.peak_util_pct
            for p in self._peering.get(link_id, [])
            if hour_in_window(hour, p.start_hour, p.end_hour)
        ]
        return max(active) if active else None

    def us_noise_db(self, service_group_id: str) -> float:
        """Upstream SNR lost to ingress on a service group at the current tick."""
        now = self.time_of(self.tick)
        hour = now.hour + now.minute / 60
        return sum(
            n.snr_drop_db
            for n in self._noise.get(service_group_id, [])
            if hour_in_window(hour, n.start_hour, n.end_hour)
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
            case UtilityOutage(power_area=area) if area not in self._areas:
                raise EngineError(f"{fault_id} cuts power to unknown power area {area}")
            case ConfigChange(target_id=target) if not isinstance(
                self.topology[target], (Cmts, ServiceGroup)
            ):
                raise EngineError(f"{fault_id}: a config push targets a CMTS or service group")
            case PeeringLoad(link_id=link) if not isinstance(self.topology[link], PeeringLink):
                raise EngineError(f"{fault_id}: peering load needs a peering link, not {link}")
            case _:
                pass

    def _apply_current_tick(self) -> None:
        # Batteries account for the 5 minutes since the last tick, before this tick's effects.
        status_changed = self._restore_power()
        status_changed |= self._update_batteries()
        effects = self._schedule.pop(self.tick, [])
        for effect in effects:
            status_changed |= self._apply(effect)
        if effects or status_changed:
            self.revision += 1
        if status_changed:
            self._recompute_reachability()

    def _restore_power(self) -> bool:
        back = [a for a, until in self._area_out_until.items() if until <= self.tick]
        for area in sorted(back):
            del self._area_out_until[area]
            self._power_events.append((self.tick, area, "restored"))
        return bool(back)

    def _update_batteries(self) -> bool:
        """Drain the supplies that are on battery by one tick, recharge the rest. Returns whether
        a supply ran out or came back."""
        changed = False
        for supply in self._supplies:
            ps_id, full = supply.device_id, float(supply.battery_runtime_min)
            if self.area_out(supply.power_area):
                self._battery[ps_id] = max(0.0, self._battery[ps_id] - self.tick_minutes)
                if self._battery[ps_id] == 0.0 and ps_id not in self._drained:
                    self._drained.add(ps_id)
                    changed = True
            else:
                if ps_id in self._drained:
                    self._drained.discard(ps_id)
                    changed = True
                step = full * self.tick_minutes / (RECHARGE_HOURS * 60)
                self._battery[ps_id] = min(full, self._battery[ps_id] + step)
        return changed

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
            case UtilityOutage(power_area=area, duration_ticks=duration):
                if area not in self._area_out_until:
                    self._power_events.append((self.tick, area, "outage_start"))
                self._area_out_until[area] = max(
                    self._area_out_until.get(area, 0), self.tick + duration
                )
                return True
            case ConfigChange():
                self._change_log.append(
                    ChangeLogEntry(
                        change_id=effect.change_id,
                        target_id=effect.target_id,
                        description=effect.description,
                        tick=self.tick,
                    )
                )
                target = self.topology[effect.target_id]
                groups = (
                    [target]
                    if isinstance(target, ServiceGroup)
                    else [
                        d
                        for d in self.topology.subtree(target.device_id)
                        if isinstance(d, ServiceGroup)
                    ]
                )
                for sg in groups:
                    self._config_snr[sg.device_id] += effect.snr_drop_db
                return False
            case PeeringLoad():
                self._peering[effect.link_id].append(effect)
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
        roots += [
            active
            for supply in self._supplies
            if supply.device_id in self._drained
            for active in supply.feeds
        ]
        cut: set[str] = set()
        for device_id in roots:
            if device_id not in cut:
                cut.add(device_id)
                cut.update(d.device_id for d in self.topology.subtree(device_id))
        for area in self._area_out_until:  # homes without power: just the modems
            cut.update(self._modems_in_area.get(area, ()))
        self._unreachable = frozenset(cut)
