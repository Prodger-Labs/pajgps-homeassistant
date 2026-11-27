"""
Decision logic for elevation lookups. Pure: no Home Assistant, aiohttp or package imports.

Elevation comes from Open-Meteo, a free service that often answers 502 or
"The service is overloaded". It is a nice-to-have, so a lookup must be cheap to
skip and must never get in the way of the position poll. This module keeps, per
device, the last good value and when to try again. The HTTP call itself is made
by the caller, in a background task, inside a hard time box.

Rules:
- The last good value is kept and reused for every later poll, so the elevation
  sensor does not blank while a new lookup is pending or failing.
- A new lookup is made only when the device has no value yet or has moved to a
  different cell of about 100 m, and never sooner than MIN_UPDATE_DELAY_S after
  the previous attempt.
- Every failure doubles the wait before the next attempt, up to MAX_BACKOFF_S.
  A success resets it.
- At most one lookup per device is in flight.
- Failures are logged at warning once per WARN_GAP_S and at debug otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_UPDATE_DELAY_S = 5 * 60  # minimum spacing between lookups for one device
MAX_BACKOFF_S = 60 * 60  # longest wait after repeated failures
TOTAL_TIMEOUT_S = 6.0  # hard cap on one lookup, however the HTTP layer behaves
REQUEST_TIMEOUT_S = 5  # one attempt, no retries
WARN_GAP_S = 15 * 60  # at most one warning per device per this many seconds
CELL_DECIMALS = 3  # 0.001 degrees is about 110 m


@dataclass
class DeviceElevationState:
    """Per-device bookkeeping. One instance per device, owned by the caller."""

    value: int | None = None  # last good elevation in metres
    value_cell: tuple[float, float] | None = None  # cell the value was read for
    next_attempt_at: float = 0.0  # earliest time (unix seconds) for the next lookup
    failures: int = 0  # consecutive failed lookups
    in_flight: bool = False
    last_warned_at: float | None = None


def cell(lat: float, lng: float) -> tuple[float, float]:
    """The roughly 100 m cell a position falls in."""
    return (round(lat, CELL_DECIMALS), round(lng, CELL_DECIMALS))


def backoff(failures: int) -> float:
    """Seconds to wait after this many consecutive failures."""
    if failures <= 0:
        return MIN_UPDATE_DELAY_S
    return min(MIN_UPDATE_DELAY_S * (2 ** (failures - 1)), MAX_BACKOFF_S)


def should_fetch(state: DeviceElevationState, lat: float, lng: float, now: float) -> bool:
    """True when a lookup is worth making for this position right now."""
    if state.in_flight:
        return False
    if now < state.next_attempt_at:
        return False
    if state.value is None:
        return True
    return state.value_cell != cell(lat, lng)


def mark_started(state: DeviceElevationState, now: float) -> None:
    """Record that a lookup has been launched."""
    state.in_flight = True
    state.next_attempt_at = now + MIN_UPDATE_DELAY_S


def record_success(state: DeviceElevationState, value: float, lat: float, lng: float, now: float) -> None:
    """Store a good value for the cell the lookup was made for."""
    state.in_flight = False
    state.value = round(value)
    state.value_cell = cell(lat, lng)
    state.failures = 0
    state.next_attempt_at = now + MIN_UPDATE_DELAY_S


def record_failure(state: DeviceElevationState, now: float) -> bool:
    """
    Record a failed or timed-out lookup. The previous value is kept.

    Returns True when this failure should be logged at warning level.
    """
    state.in_flight = False
    state.failures += 1
    state.next_attempt_at = now + backoff(state.failures)
    if state.last_warned_at is None or (now - state.last_warned_at) >= WARN_GAP_S:
        state.last_warned_at = now
        return True
    return False
