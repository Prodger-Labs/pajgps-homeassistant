"""
Pure helpers for the get_track service. No Home Assistant, aiohttp or package imports.

The PAJ date_range endpoint returns every stored point for a device between two
unix timestamps (one point about every 30 s). This module checks the requested
window and turns the response body into a clean, sorted, de-duplicated track.

dateunix is unix seconds, UTC. Milliseconds are NOT assumed or converted. A value
at or above MAX_PLAUSIBLE_DATEUNIX (about year 5138 in seconds, so almost
certainly milliseconds) is rejected, never divided by 1000. This is the same
decision as history.py, repeated here so this module stays import-free.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

MAX_WINDOW_HOURS = 24
MAX_POINTS = 5000
MAX_PLAUSIBLE_DATEUNIX = 10**11  # seconds; larger means milliseconds, so invalid


def _number(value: Any) -> float | int | None:
    """Return value if it is a finite real number (not a bool or a string), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def iso_utc(unix_seconds: float | int) -> str:
    """ISO 8601 UTC string with a +00:00 suffix, for example 2026-11-19T09:00:00+00:00."""
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat()


def validate_window(start_unix: Any, end_unix: Any, max_hours: float = MAX_WINDOW_HOURS) -> tuple[int, int]:
    """
    Check a requested window and return it as whole unix seconds (start, end).

    Raises ValueError with a clear message when a bound is not a number, when end
    is not after start, or when the window is longer than max_hours.
    """
    start = _number(start_unix)
    end = _number(end_unix)
    if start is None or end is None:
        raise ValueError("start and end must be unix timestamps in seconds")
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > max_hours * 3600:
        raise ValueError(f"window is longer than {max_hours:g} hours, choose a shorter trip")
    return int(start), int(end)


def parse_track_body(body: Any, max_points: int = MAX_POINTS) -> dict:
    """
    Turn a date_range response body into a track.

    Returns {"points": [...], "count": n, "truncated": bool, "dropped": n}.
    Each point is {"t", "lat", "lon", "speed_kmh", "battery", "accuracy"} where t is
    ISO 8601 UTC. Points are sorted ascending by time and de-duplicated by
    dateunix (the first one seen wins). Items with a missing or non-numeric lat,
    lon or dateunix, a coordinate out of range, or a dateunix that is not a
    plausible seconds value are skipped silently. A missing or malformed
    `success` key gives zero points, never an exception.

    When there are more than max_points valid points the earliest max_points are
    kept, truncated is True and dropped is how many valid points were cut off.
    Skipped invalid items are not counted in dropped.
    """
    empty = {"points": [], "count": 0, "truncated": False, "dropped": 0}
    if not isinstance(body, dict) or not isinstance(body.get("success"), list):
        return empty

    by_time: dict[int, dict] = {}
    for item in body["success"]:
        if not isinstance(item, dict):
            continue
        lat = _number(item.get("lat"))
        lon = _number(item.get("lng"))
        dateunix = _number(item.get("dateunix"))
        if lat is None or lon is None or dateunix is None:
            continue
        if dateunix <= 0 or dateunix >= MAX_PLAUSIBLE_DATEUNIX:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        key = int(dateunix)
        if key in by_time:
            continue
        battery = _number(item.get("battery_level", item.get("battery")))
        by_time[key] = {
            "t": iso_utc(key),
            "lat": float(lat),
            "lon": float(lon),
            "speed_kmh": _number(item.get("speed")),
            "battery": battery,
            "accuracy": _number(item.get("accuracy")),
        }

    points = [by_time[key] for key in sorted(by_time)]
    dropped = max(0, len(points) - max_points)
    if dropped:
        points = points[:max_points]
    return {"points": points, "count": len(points), "truncated": dropped > 0, "dropped": dropped}
