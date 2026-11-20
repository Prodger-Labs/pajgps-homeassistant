import asyncio
import logging

import aiohttp
import voluptuous as vol
from homeassistant import config_entries, core
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from . import track
from .api import positions
from .pajgps_data import PajGPSData, PajGPSDataInstances
from .const import DOMAIN
from .requests import ApiResponseError

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]
_LOGGER = logging.getLogger(__name__)

SERVICE_GET_TRACK = "get_track"

GET_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
        vol.Optional("device_id"): cv.positive_int,
        vol.Optional("include_wifi", default=False): cv.boolean,
    }
)


def _parse_time(value: str, field: str) -> int:
    """
    Turn an ISO 8601 string into unix seconds.

    A string with an offset is taken as written. A string without one is Home
    Assistant's local time zone: dt_util.as_utc applies the configured default
    zone to a naive datetime.
    """
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ServiceValidationError(
            f"{field} is not a valid date and time, use ISO 8601 such as 2026-11-19T09:00:00+00:00"
        )
    return int(dt_util.as_utc(parsed).timestamp())


def _pick_device(device_id: int | None) -> tuple[PajGPSData, int]:
    """Find the account instance and device id to query, or raise ServiceValidationError."""
    known = [(inst, dev.id) for inst in PajGPSDataInstances.values() for dev in inst.devices]
    ids = sorted({dev_id for _, dev_id in known})
    if not known:
        raise ServiceValidationError("No PAJ GPS devices are loaded yet, try again in a minute")
    if device_id is None:
        if len(ids) != 1:
            raise ServiceValidationError(f"device_id is required, choose one of {ids}")
        return known[0]
    for inst, dev_id in known:
        if dev_id == device_id:
            return inst, dev_id
    raise ServiceValidationError(f"device_id {device_id} not found, choose one of {ids}")


async def async_handle_get_track(call: ServiceCall) -> dict:
    """Return a device's points between start and end, for use with a response variable."""
    start = _parse_time(call.data["start"], "start")
    end = _parse_time(call.data["end"], "end")
    try:
        start, end = track.validate_window(start, end)
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err

    data, device_id = _pick_device(call.data.get("device_id"))
    wifi = 1 if call.data.get("include_wifi", False) else 0

    # Messages below are fixed text on purpose: the request URL, query string and
    # headers (which hold the bearer token) must never reach the user or the log.
    try:
        await data.refresh_token()  # no-op while the token is still fresh
        if not data.token:
            raise HomeAssistantError("PAJ GPS login failed, cannot fetch the track")
        body = await positions.fetch_track_body(
            device_id, start, end, data.get_standard_headers(), wifi=wifi
        )
    except HomeAssistantError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise HomeAssistantError("PAJ GPS did not answer in time, try again") from None
    except ApiResponseError:
        raise HomeAssistantError("PAJ GPS refused the track request") from None
    except (aiohttp.ClientError, ValueError) as err:
        raise HomeAssistantError(
            f"PAJ GPS track request failed ({type(err).__name__})"
        ) from None

    if not isinstance(body, dict) or not isinstance(body.get("success"), list):
        raise HomeAssistantError("PAJ GPS returned an unusable track response")

    result = track.parse_track_body(body)
    return {
        "device_id": device_id,
        "start": track.iso_utc(start),
        "end": track.iso_utc(end),
        "count": result["count"],
        "truncated": result["truncated"],
        "dropped": result["dropped"],
        "points": result["points"],
    }


async def async_setup(hass: core.HomeAssistant, config: dict) -> bool:
    """Set up the integration."""
    hass.data.setdefault(DOMAIN, {})
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TRACK,
        async_handle_get_track,
        schema=GET_TRACK_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True

async def async_setup_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Set up platform from a ConfigEntry."""
    entry.async_on_unload(
        entry.add_update_listener(_async_update_listener)
    )

    await async_initialize_data(entry)

    # Forward the setup to the device_tracker platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_initialize_data(entry: config_entries.ConfigEntry):
    """Initialize the PajGPS data object."""
    try:
        # Create a new PajGPSData object
        data = PajGPSData.get_instance(entry.data["guid"], entry.data["entry_name"], entry.data["email"], entry.data["password"], entry.data["mark_alerts_as_read"], entry.data["fetch_elevation"], entry.data["force_battery"])
        # Initialize the data object
        await data.update_pajgps_data(True)
    except Exception as e:
        _LOGGER.error(f"Failed to initialize PajGPS data: {e}")

async def async_remove_config_entry_device(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry, device_entry
) -> bool:
    """Remove a device from the integration."""
    return True

    _LOGGER.warning("Device not found: %s", device_entry.id)
    return False

async def _async_update_listener(hass: HomeAssistant, config_entry):
    """Handle config options update."""
    # Reload the integration when the options change.
    await hass.config_entries.async_reload(config_entry.entry_id)

async def options_update_listener(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
):
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)

async def async_unload_entry(
    hass: core.HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Unload a config entry."""
    # Close the session before unloading
    guid = entry.data.get("guid")
    if guid:
        from .pajgps_data import PajGPSDataInstances
        instance = PajGPSDataInstances.get(guid)
        if instance:
            await instance.async_close()
            PajGPSDataInstances.pop(guid, None)

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
