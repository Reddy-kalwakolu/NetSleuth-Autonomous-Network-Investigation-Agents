"""Effect primitives and device state.

Primitives are plain data. Scenario files describe faults as lists of them, and the engine is the
only thing that applies them.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

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


Effect = Annotated[TakeDown | DegradeLevels, Field(discriminator="kind")]


class ScheduledEffect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at_tick: NonNegativeInt
    effect: Effect


def effect_targets(effect: TakeDown | DegradeLevels) -> tuple[str, ...]:
    match effect:
        case TakeDown(device_id=device_id):
            return (device_id,)
        case DegradeLevels(scope_id=scope_id):
            return (scope_id,)
