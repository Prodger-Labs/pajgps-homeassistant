"""
Decision logic for the history fallback. Pure: no Home Assistant, aiohttp or package imports.

The PAJ last-position endpoint (getalllastpositions) can serve a stale record for
tens of minutes while the history endpoint (last_points) already holds fresh GPS
points. This module decides, per device, when to ask the history endpoint and
which point to trust. The actual HTTP call is passed in as `fetch`.

dateunix is unix seconds, UTC. Milliseconds are NOT assumed or converted. A value
at or above MAX_PLAUSIBLE_DATEUNIX (about year 5138 in seconds, so almost
certainly milliseconds) is treated as invalid and ignored, never divided by 1000.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

STALE_AFTER_S = 90  # last-position record older than this is "stale"
MIN_ATTEMPT_GAP_S = 60  # minimum spacing between history calls per device
BACKOFF_AFTER_MISSES = 10  # consecutive attempts finding nothing newer
BACKOFF_ATTEMPT_GAP_S = 300  # spacing once backed off
WARN_GAP_S = 15 * 60  # at most one warning per device per this many seconds
MAX_PLAUSIBLE_DATEUNIX = 10**11  # seconds; larger means milliseconds, so invalid

SOURCE_LAST_POSITION = "last_position"
SOURCE_HISTORY = "history"

OUTCOME_SKIPPED = "skipped"
OUTCOME_NEWER = "newer"
OUTCOME_NOT_NEWER = "not_newer"
OUTCOME_EMPTY = "empty"
OUTCOME_ERROR = "error"

HISTORY_LAST_POINTS = 2
HISTORY_QUERY = {"lastPoints": HISTORY_LAST_POINTS, "gps": 1, "wifi": 0}


class HistoryParseError(Exception):
    """The history response body is not the expected {"success": [...]} shape."""


@dataclass(frozen=True)
class HistoryPoint:
    """One valid point from the history endpoint."""

    lat: float
    lng: float
    dateunix: int
    speed: int | None = None
    battery_level: int | None = None


@dataclass
class DeviceHistoryState:
    """Per-device bookkeeping. One instance per device, owned by the caller."""

    last_attempt: float | None = None
    misses: int = 0  # consecutive attempts that found nothing newer
    last_warn: float | None = None


@dataclass(frozen=True)
class HistoryResult:
    """What resolve_history decided."""

    source: str  # SOURCE_LAST_POSITION or SOURCE_HISTORY
    point: HistoryPoint | None  # set only when source is SOURCE_HISTORY
    outcome: str  # one of the OUTCOME_* values


def valid_dateunix(value: Any) -> int | None:
    """Return value as int unix seconds, or None if it is not a plausible seconds value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value <= 0 or value >= MAX_PLAUSIBLE_DATEUNIX:
        return None
    return int(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def parse_history_body(body: Any) -> list[HistoryPoint]:
    """
    Turn a last_points response into valid points.

    Returns [] for a well-formed empty list. Raises HistoryParseError when the body
    is not {"success": [...]}, or when it holds items but none are usable.
    Individual unusable items (bad coordinates, bad dateunix) are skipped.
    """
    if not isinstance(body, dict) or not isinstance(body.get("success"), list):
        raise HistoryParseError("body is not a dict with a success list")
    items = body["success"]
    points: list[HistoryPoint] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lat = _number(item.get("lat"))
        lng = _number(item.get("lng"))
        dateunix = valid_dateunix(item.get("dateunix"))
        if lat is None or lng is None or dateunix is None:
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            continue
        speed = _number(item.get("speed"))
        battery = _number(item.get("battery_level", item.get("battery")))
        points.append(
            HistoryPoint(
                lat=float(lat),
                lng=float(lng),
                dateunix=dateunix,
                speed=None if speed is None else int(speed),
                battery_level=None if battery is None else int(battery),
            )
        )
    if items and not points:
        raise HistoryParseError("no usable points in success list")
    return points


def newest_point(points: list[HistoryPoint]) -> HistoryPoint | None:
    """Newest point by dateunix. The API sorts by id, so we do not rely on order."""
    if not points:
        return None
    return max(points, key=lambda p: p.dateunix)


def attempt_gap(state: DeviceHistoryState) -> int:
    """Seconds required between attempts: 60 normally, 300 once backed off."""
    if state.misses >= BACKOFF_AFTER_MISSES:
        return BACKOFF_ATTEMPT_GAP_S
    return MIN_ATTEMPT_GAP_S


def should_attempt(state: DeviceHistoryState, age: float, now: float) -> bool:
    """True when the record is stale and the attempt spacing has elapsed."""
    if age <= STALE_AFTER_S:
        return False
    if state.last_attempt is None:
        return True
    return (now - state.last_attempt) >= attempt_gap(state)


def _warn_once_per_window(state: DeviceHistoryState, now: float, device_id: Any, why: str) -> None:
    if state.last_warn is None or (now - state.last_warn) >= WARN_GAP_S:
        state.last_warn = now
        _LOGGER.warning("PAJ history lookup failed for device %s: %s", device_id, why)
    else:
        _LOGGER.debug("PAJ history lookup failed for device %s: %s", device_id, why)


async def resolve_history(
    state: DeviceHistoryState,
    device_id: Any,
    last_dateunix: Any,
    fetch: Callable[[Any], Awaitable[Any]],
    now: float,
) -> HistoryResult:
    """
    Decide whether to use a history point instead of the last-position record.

    `fetch(device_id)` performs the last_points call and returns the parsed JSON body.
    Never raises (cancellation excepted). On any problem the result is the
    last-position record.
    """
    fallback = HistoryResult(SOURCE_LAST_POSITION, None, OUTCOME_SKIPPED)

    last_fix = valid_dateunix(last_dateunix)
    if last_fix is None:
        _LOGGER.debug("Device %s: last-position dateunix missing or implausible, no history lookup", device_id)
        return fallback

    age = now - last_fix
    if age <= STALE_AFTER_S:
        # The endpoint is serving fresh data. A later stale spell starts from scratch.
        state.misses = 0
        return fallback
    if not should_attempt(state, age, now):
        return fallback

    state.last_attempt = now
    try:
        body = await fetch(device_id)
        points = parse_history_body(body)
        newest = newest_point(points)
    except Exception as err:  # noqa: BLE001 - nothing may escape into the normal path
        state.misses += 1
        _warn_once_per_window(state, now, device_id, f"{type(err).__name__}: {err}")
        return HistoryResult(SOURCE_LAST_POSITION, None, OUTCOME_ERROR)

    if newest is None:
        state.misses += 1
        _LOGGER.debug("Device %s: history returned no points", device_id)
        return HistoryResult(SOURCE_LAST_POSITION, None, OUTCOME_EMPTY)

    if newest.dateunix > last_fix:
        state.misses = 0
        _LOGGER.debug(
            "Device %s: last position is %ss old, using history point from %s",
            device_id, int(age), newest.dateunix,
        )
        return HistoryResult(SOURCE_HISTORY, newest, OUTCOME_NEWER)

    state.misses += 1
    _LOGGER.debug("Device %s: history point not newer than last position", device_id)
    return HistoryResult(SOURCE_LAST_POSITION, None, OUTCOME_NOT_NEWER)
