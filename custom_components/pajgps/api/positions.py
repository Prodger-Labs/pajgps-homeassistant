"""
Low-level position and elevation data fetching from the PajGPS and Open-Meteo APIs.

Responsible for:
- Fetching last known positions for all devices
- Fetching a device's track between two timestamps (get_track service)
- Fetching elevation data from the Open-Meteo API for a given coordinate
- Detecting which devices have moved since the last update
"""
import logging

import aiohttp

from custom_components.pajgps.requests import make_request, ApiResponseError
from custom_components.pajgps.models import PajGPSPositionData
from custom_components.pajgps import elevation, history

_LOGGER = logging.getLogger(__name__)

API_URL = "https://connect.paj-gps.de/api/v1/"
ELEVATION_API_URL = "https://api.open-meteo.com/v1/elevation"


async def fetch_positions(device_ids: list[int], headers: dict) -> tuple[list[PajGPSPositionData], dict | None]:
    """
    Fetch the last known position for every device in device_ids.

    Returns a tuple of (positions, raw_json).
    On error returns ([], None).

    Corresponding CURL command:
    curl -X 'POST' \
      'https://connect.paj-gps.de/api/v1/trackerdata/getalllastpositions' \
      -d '{"deviceIDs": [<ids>], "fromLastPoint": false}'
    """
    url = API_URL + "trackerdata/getalllastpositions"
    payload = {"deviceIDs": device_ids, "fromLastPoint": False}
    raw_json = None
    try:
        raw_json = await make_request("POST", url, headers, payload=payload)
    except ApiResponseError as e:
        _LOGGER.error("Error while getting tracking data: %s", e)
        return [], None
    except TimeoutError:
        _LOGGER.warning("Timeout while getting tracking data")
        return [], None
    except KeyError as e:
        _LOGGER.error("Missing key in tracking data response: %s", e)
        return [], None

    if not raw_json or "success" not in raw_json:
        _LOGGER.error("Unexpected response format in tracking data: %s", raw_json)
        return [], raw_json

    positions = [
        PajGPSPositionData(
            device["iddevice"],
            device["lat"],
            device["lng"],
            device["direction"],
            device["speed"],
            # Upstream 0.7.1 reads "battery_level"; the example JSON and 0.7.0 use "battery".
            # Accept either so a key change on the API side cannot break the poll.
            device.get("battery_level", device.get("battery")),
            fix_time=history.valid_dateunix(device.get("dateunix")),
        )
        for device in raw_json["success"]
    ]
    return positions, raw_json


async def fetch_history_body(device_id: int, headers: dict):
    """
    Fetch the two newest GPS points for a device from the history endpoint.

    Returns the parsed JSON body. Raises on any error: the caller
    (history.resolve_history) owns the error handling. A single attempt with a
    5 s timeout, so a dead history call cannot hold up the 30 s poll.

    Corresponding CURL command:
    curl 'https://connect.paj-gps.de/api/v1/trackerdata/<id>/last_points?lastPoints=2&gps=1&wifi=0'
    """
    url = API_URL + f"trackerdata/{device_id}/last_points"
    return await make_request(
        "GET", url, headers, params=history.HISTORY_QUERY, timeout=5, max_attempts=1
    )


async def fetch_track_body(
    device_id: int, date_start: int, date_end: int, headers: dict, gps: int = 1, wifi: int = 0
):
    """
    Fetch every stored point for a device between two unix timestamps (seconds, UTC).

    Returns the parsed JSON body. Raises on any error: the caller (the get_track
    service) owns the error handling. A single attempt with a 20 s timeout, as a
    window of a few hours holds a few hundred points.

    Corresponding CURL command:
    curl 'https://connect.paj-gps.de/api/v1/trackerdata/<id>/date_range?dateStart=<s>&dateEnd=<s>&gps=1&wifi=0'
    """
    url = API_URL + f"trackerdata/{device_id}/date_range"
    params = {"dateStart": date_start, "dateEnd": date_end, "gps": gps, "wifi": wifi}
    return await make_request("GET", url, headers, params=params, timeout=20, max_attempts=1)


async def fetch_elevation(device_id: int, position: PajGPSPositionData) -> float | None:
    """
    Fetch the elevation (in metres) for the given position from the Open-Meteo API.

    Rounds lat/lng to ~100 m precision before the request to improve cache hit rate.
    One attempt with a short timeout and no retries: elevation is optional, and the
    caller (PajGPSData._update_elevation_for) owns the retry timing and the logging.
    Returns None on any error, and logs it at debug level only.

    Example request:
    https://api.open-meteo.com/v1/elevation?latitude=52.52&longitude=13.41
    """
    # Round to about 100 metres precision to improve cache hit rate on the remote API
    lat = round(position.lat, 5)
    lng = round(position.lng, 5)
    params = {"latitude": lat, "longitude": lng}
    headers = {"accept": "application/json"}

    try:
        raw_json = await make_request(
            "GET", ELEVATION_API_URL, headers, params=params,
            timeout=elevation.REQUEST_TIMEOUT_S, max_attempts=1,
        )
    except (TimeoutError, ValueError, ApiResponseError, aiohttp.ClientError) as e:
        # Open-Meteo answers 502 or {"error": true, "reason": "The service is overloaded"}
        # now and then. Both are expected, not a bug.
        _LOGGER.debug(
            "Failed to get elevation for device %s at (%s, %s): %s: %s",
            device_id, lat, lng, type(e).__name__, e,
        )
        return None
    except Exception as e:
        _LOGGER.error(
            "Unexpected error while getting elevation for device %s at (%s, %s): %s: %s",
            device_id, lat, lng, type(e).__name__, e,
        )
        return None

    if isinstance(raw_json, dict) and isinstance(raw_json.get("elevation"), list) and raw_json["elevation"]:
        value = raw_json["elevation"][0]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value

    _LOGGER.warning(
        "Unexpected elevation response format for device %s at (%s, %s): %s",
        device_id, lat, lng, raw_json,
    )
    return None


def find_moved_device_ids(
    new_positions: list[PajGPSPositionData],
    old_positions: list[PajGPSPositionData],
) -> list[int]:
    """
    Return device IDs whose lat/lng changed compared to the previous update,
    or whose elevation has never been fetched.
    """
    old_by_id = {p.device_id: p for p in old_positions}
    moved = []
    for new in new_positions:
        old = old_by_id.get(new.device_id)
        if old is None:
            continue
        if old.lat != new.lat or old.lng != new.lng or new.elevation is None:
            moved.append(new.device_id)
    return moved

