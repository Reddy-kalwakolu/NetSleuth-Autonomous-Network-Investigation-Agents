"""The simulation clock: when a run starts, how long a tick is, and daily time windows."""

from datetime import UTC, datetime, timedelta

DEFAULT_START = datetime(2026, 9, 1, tzinfo=UTC)
TICK_MINUTES = 5


def hour_in_window(hour: float, start_hour: int, end_hour: int) -> bool:
    """Whether a time of day falls in ``[start_hour, end_hour)``. A window with ``start_hour >
    end_hour`` wraps midnight."""
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def first_tick_in_window(
    at_tick: int,
    start_hour: int,
    end_hour: int,
    *,
    start: datetime = DEFAULT_START,
    tick_minutes: int = TICK_MINUTES,
) -> int:
    """The first tick at or after ``at_tick`` whose time of day is inside the window."""
    ticks_per_day = 24 * 60 // tick_minutes
    for tick in range(at_tick, at_tick + ticks_per_day + 1):
        now = start + timedelta(minutes=tick * tick_minutes)
        if hour_in_window(now.hour + now.minute / 60, start_hour, end_hour):
            return tick
    return at_tick  # an empty window never fires; report the scheduled tick
