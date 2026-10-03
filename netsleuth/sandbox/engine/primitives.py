"""Effect primitives and device state.

Primitives are plain data. Scenario files describe faults as lists of them, and the engine is the
only thing that applies them.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

DeviceStatus = Literal["healthy", "degraded", "down"]


class EngineError(ValueError):
    """A scenario or fault the engine can't run."""


@dataclass(frozen=True, slots=True)
class DeviceState:
    status: DeviceStatus = "healthy"
    severity: float = 0.0


HEALTHY = DeviceState()


class _Primitive(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TakeDown(_Primitive):
    """The device goes down. Everything behind it becomes unreachable."""

    kind: Literal["take_down"] = "take_down"
    device_id: str


class DegradeLevels(_Primitive):
    """Shifts signal levels for the scope device and everything behind it.

    ``ds_db`` moves downstream receive power, ``us_db`` moves upstream transmit power. The scope
    device itself is marked degraded, since that's where the problem sits.
    """

    kind: Literal["degrade_levels"] = "degrade_levels"
    scope_id: str
    ds_db: float
    us_db: float


class Restore(_Primitive):
    """The device comes back up. Everything behind it becomes reachable again."""

    kind: Literal["restore"] = "restore"
    device_id: str


class CutFiberRoute(_Primitive):
    """Every node on the route loses its fiber link. The nodes themselves are fine."""

    kind: Literal["cut_fiber_route"] = "cut_fiber_route"
    route: str


class AddUpstreamNoise(_Primitive):
    """Ingress on a service group's upstream, active every day between two local hours.

    ``end_hour`` is exclusive, and a window with ``start_hour > end_hour`` wraps midnight.
    """

    kind: Literal["add_us_noise"] = "add_us_noise"
    service_group_id: str
    snr_drop_db: PositiveFloat
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=24)


class MaintenanceWindow(_Primitive):
    """Publishes a planned work window to the maintenance calendar. The work itself is
    scheduled separately, as ``take_down`` and ``restore``."""

    kind: Literal["maintenance_window"] = "maintenance_window"
    window_id: str
    scope_id: str
    start_tick: NonNegativeInt
    end_tick: NonNegativeInt

    @model_validator(mode="after")
    def _ends_after_start(self) -> "MaintenanceWindow":
        if self.end_tick <= self.start_tick:
            raise ValueError("a maintenance window must end after it starts")
        return self


class UtilityOutage(_Primitive):
    """Homes and power supplies in a power area lose utility power for ``duration_ticks``.
    Supplies carry their actives on battery until it runs out."""

    kind: Literal["utility_outage"] = "utility_outage"
    power_area: str
    duration_ticks: PositiveInt


class ConfigChange(_Primitive):
    """A configuration push to a CMTS or service group. It writes a change log entry, and from
    that moment every upstream channel of every service group under the target loses SNR."""

    kind: Literal["config_change"] = "config_change"
    change_id: str
    target_id: str
    snr_drop_db: PositiveFloat
    description: str = "configuration push"


class PeeringLoad(_Primitive):
    """Extra traffic on a peering link every day between two local hours, pushing it toward
    ``peak_util_pct``. ``end_hour`` is exclusive, and a window can wrap midnight."""

    kind: Literal["peering_load"] = "peering_load"
    link_id: str
    peak_util_pct: float = Field(gt=0, le=100)
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=24)


AnyEffect = (
    TakeDown
    | DegradeLevels
    | Restore
    | CutFiberRoute
    | AddUpstreamNoise
    | MaintenanceWindow
    | UtilityOutage
    | ConfigChange
    | PeeringLoad
)
Effect = Annotated[AnyEffect, Field(discriminator="kind")]


class ScheduledEffect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at_tick: NonNegativeInt
    effect: Effect


def effect_targets(effect: AnyEffect) -> tuple[str, ...]:
    """Device IDs the effect names. Fiber routes and power areas aren't devices and are checked
    separately."""
    match effect:
        case TakeDown(device_id=device_id) | Restore(device_id=device_id):
            return (device_id,)
        case DegradeLevels(scope_id=scope_id) | MaintenanceWindow(scope_id=scope_id):
            return (scope_id,)
        case AddUpstreamNoise(service_group_id=service_group_id):
            return (service_group_id,)
        case ConfigChange(target_id=target_id):
            return (target_id,)
        case PeeringLoad(link_id=link_id):
            return (link_id,)
        case CutFiberRoute() | UtilityOutage():
            return ()
