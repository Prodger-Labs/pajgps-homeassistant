"""
Main class for PajGPS data handling.
Singleton class to handle data fetching and storing for all the sensors to read from.
This class is responsible for fetching data from the PajGPS API and storing it.
It also acts as a gateway for the sensors to make only few API calls which results all sensors can read
instead of each sensor making its own API calls.
"""
import asyncio
import logging
import random
import time
from datetime import timedelta
import aiohttp
from custom_components.pajgps.const import DOMAIN, VERSION
from custom_components.pajgps.api import auth, devices, alerts, sensors, positions
from custom_components.pajgps import elevation, history, models, requests

# An entity update (random start delay of up to 1 s, then this) must finish well inside
# Home Assistant's 10 s "update is taking over 10 seconds" warning. If PAJ is slower than
# this the refresh carries on in the background and the entities keep their last data.
UPDATE_WAIT_BUDGET_S = 7
CLOSE_WAIT_S = 5  # how long async_close lets background tasks finish before cancelling them
_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)
API_URL = "https://connect.paj-gps.de/api/v1/"

PajGPSDataInstances: dict[str, "PajGPSData"] = {}

class PajGPSData:
    """Main class for PajGPS data handling."""

    # Basic properties
    guid: str
    entry_name: str

    # Session properties
    _session: aiohttp.ClientSession | None
    _background_tasks: set[asyncio.Task]

    # Credentials properties
    email: str
    password: str
    token: str | None
    last_token_update: float
    token_ttl: int = 60 * 5  # 5 minutes

    # Update properties
    last_update: float
    data_ttl: int = int(SCAN_INTERVAL.total_seconds() / 2)  # update requests done more often than this many seconds will be ignored
    mark_alerts_as_read: bool
    update_lock: asyncio.Lock
    force_battery: bool
    fetch_elevation: bool
    total_update_time_ms: float  # Total time of last full update in milliseconds

    # Pure json responses from API
    devices_json: dict
    alerts_json: dict
    positions_json: dict

    # Deserialized data
    devices: list[models.PajGPSDevice]
    alerts: list[models.PajGPSAlert]
    positions: list[models.PajGPSPositionData]
    sensors: list[models.PajGPSSensorData]

    # Per-device history-fallback bookkeeping (attempt spacing, backoff, warn limiter)
    history_states: dict[int, history.DeviceHistoryState]

    # Per-device elevation cache and retry state (last good value, back-off, warn limiter)
    elevation_states: dict[int, elevation.DeviceElevationState]


    def __init__(self, guid: str, entry_name: str, email: str, password: str, mark_alerts_as_read: bool, fetch_elevation: bool, force_battery: bool) -> None:
        """
        Initialize the PajGPSData class.
        """

        self.guid = guid
        self.entry_name = entry_name
        self.email = email
        self.password = password
        self.mark_alerts_as_read = mark_alerts_as_read
        self.fetch_elevation = fetch_elevation
        self.force_battery = force_battery
        self._session = None
        self._background_tasks = set()
        self.update_lock = asyncio.Lock()
        self.devices = []
        self.alerts = []
        self.positions = []
        self.sensors = []
        self.history_states = {}
        self.elevation_states = {}
        self.token = None
        self.last_token_update = 0.0
        self.last_update = time.time() - 60
        self.total_update_time_ms = 0.0


    async def async_close(self) -> None:
        """Close the session and cleanup resources."""
        # Give pending background tasks a moment to finish, then cancel what is left
        # (a refresh stuck on a hung PAJ call must not hold up unloading the entry)
        if self._background_tasks:
            tasks = set(self._background_tasks)
            _LOGGER.debug("Waiting for %s background tasks to complete", len(tasks))
            _, pending = await asyncio.wait(tasks, timeout=CLOSE_WAIT_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Close the session
        if self._session and not self._session.closed:
            await self._session.close()
            # Give the underlying connections time to close
            await asyncio.sleep(0.25)
            self._session = None
            _LOGGER.debug("Session closed successfully")

    @classmethod
    def get_instance(cls, guid: str, entry_name: str, email: str, password: str, mark_alerts_as_read: bool, fetch_elevation: bool, force_battery: bool) -> "PajGPSData":
        """
        Get or create a singleton instance of PajGPSData for the given entry_name.
        """
        if guid not in PajGPSDataInstances:
            PajGPSDataInstances[guid] = cls(guid, entry_name, email, password, mark_alerts_as_read, fetch_elevation, force_battery)
        return PajGPSDataInstances[guid]

    @classmethod
    async def clean_instances(cls) -> None:
        """
        Clean all instances of PajGPSData.
        This is used for testing purposes to reset the singleton instances.
        """
        for instance in PajGPSDataInstances.values():
            await instance.async_close()
        PajGPSDataInstances.clear()

    async def refresh_token(self, forced: bool = False) -> None:
        """Refresh the bearer token via api/auth, updating local state."""
        self.token, self.last_token_update = await auth.refresh_token(
            current_token=self.token,
            last_token_update=self.last_token_update,
            token_ttl=self.token_ttl,
            email=self.email,
            password=self.password,
            forced=forced,
        )

    def clean_data(self):
        self.devices = []
        self.alerts = []
        self.positions = []

    def get_standard_headers(self) -> dict:
        """Get standard headers for API requests."""
        return auth.get_standard_headers(self.token)


    async def update_pajgps_data(self, forced: bool = False) -> None:
        """
        Orchestrate a full data refresh.
        Skips if data is still fresh or an update is already in progress.

        Entity polls (forced=False) never wait for the shared update lock and never wait
        longer than UPDATE_WAIT_BUDGET_S: if PAJ is slow the refresh carries on in the
        background and the entity keeps its last data until the next poll. Forced updates
        (setup, options flow) wait for the full refresh.
        """
        if not await self._should_run_update(forced):
            return

        if forced:
            await self._locked_update(forced)
            return

        task = asyncio.create_task(self._locked_update(forced))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        done, _ = await asyncio.wait({task}, timeout=UPDATE_WAIT_BUDGET_S)
        if done:
            task.result()
        else:
            _LOGGER.debug("PAJ refresh still running after %s s, carrying on in the background", UPDATE_WAIT_BUDGET_S)
            task.add_done_callback(self._log_abandoned_update)

    @staticmethod
    def _log_abandoned_update(task: asyncio.Task) -> None:
        """Log how a refresh ended when no entity was left waiting for it."""
        if not task.cancelled() and (err := task.exception()) is not None:
            _LOGGER.warning("PAJ refresh failed: %s: %s", type(err).__name__, err)

    async def _locked_update(self, forced: bool) -> None:
        """Run one refresh under the shared lock. A polling caller gives up if the lock is held."""
        # No await between this check and taking the lock, so two polls cannot both pass it.
        if not forced and self.update_lock.locked():
            return

        async with self.update_lock:
            # Re-check TTL after acquiring the lock (without sleep or lock check)
            # to discard updates that were queued while a previous update was running.
            if not forced and (time.time() - self.last_update) < self.data_ttl:
                return

            self.last_update = time.time()

            if not await self._is_infrastructure_ready():
                # Delay next retry by 1 minute to avoid flooding warnings
                self.last_update = time.time() + 60
                return

            await self._fetch_all_data()

    async def _should_run_update(self, forced: bool) -> bool:
        """Return True if enough time has passed since last update, or if the update is forced."""
        if forced:
            return True
        if self.update_lock.locked():
            _LOGGER.debug("Update already in progress, skipping this update")
            return False
        # Wait a small random delay to avoid thundering-herd when multiple sensors trigger at once
        await asyncio.sleep(random.random())
        return (time.time() - self.last_update) >= self.data_ttl

    async def _is_infrastructure_ready(self) -> bool:
        """Ensure the API is reachable and the auth token is valid."""
        if not await requests.check_pajgps_availability():
            _LOGGER.warning("API is not reachable, skipping update")
            return False
        await self.refresh_token()
        return True

    async def _fetch_all_data(self) -> None:
        """Fetch all device data from the API and record the total duration."""
        start = time.perf_counter()

        # Devices must be fetched first — other calls depend on the device list
        await self.update_devices_data()

        await asyncio.gather(
            self.update_position_data(),
            self.update_alerts_data(),
            self.update_sensors_data(),
        )

        self._record_update_duration(start)

    def _record_update_duration(self, start: float) -> None:
        """Persist the measured update duration on self and all sensor entries."""
        duration_ms = (time.perf_counter() - start) * 1000
        self.total_update_time_ms = duration_ms

        # Sanity check: ignore values outside the 0–30 s range
        if 0 < duration_ms < 30_000:
            for sensor in self.sensors:
                sensor.total_update_time_ms = duration_ms


    def get_device(self, device_id: int) -> models.PajGPSDevice | None:
        """Get device by id."""
        for device in self.devices:
            if device.id == device_id:
                return device
        return None

    def get_device_ids(self) -> list[int]:
        """Get device ids."""
        return [device.id for device in self.devices]

    def get_device_info(self, device_id: int) -> dict | None:
        """Get device info by id."""
        for device in self.devices:
            if device.id == device_id:
                return {
                    "identifiers": {
                        (DOMAIN, f"{self.guid}_{device.id}")
                    },
                    "name": f"{device.name}",
                    "manufacturer": "PAJ GPS",
                    "model": device.model,
                    "sw_version": VERSION,
                }
        return None

    def get_position(self, device_id: int) -> models.PajGPSPositionData | None:
        """Get position data by device id."""
        for position in self.positions:
            if position.device_id == device_id:
                return position
        return None

    def get_sensors(self, device_id: int) -> models.PajGPSSensorData | None:
        """Get sensor data by device id."""
        for sensor in self.sensors:
            if sensor.device_id == device_id:
                return sensor
        return None

    def get_alerts(self, device_id: int) -> list[models.PajGPSAlert]:
        """Get alerts by device id."""
        return [alert for alert in self.alerts if alert.device_id == device_id]

    async def update_position_data(self) -> None:
        """Fetch last positions for all devices and schedule elevation updates for moved devices."""
        new_positions, raw_json = await positions.fetch_positions(
            self.get_device_ids(), self.get_standard_headers()
        )

        if raw_json is None:
            _LOGGER.warning("Keeping stale position data due to fetch error")
            return

        self.positions_json = raw_json

        new_positions = await self._apply_history(new_positions)

        self._apply_elevation(new_positions)

        self.positions = new_positions

    async def _apply_history(
        self, new_positions: list[models.PajGPSPositionData]
    ) -> list[models.PajGPSPositionData]:
        """
        Replace a stale last-position record with a newer history point.

        Never raises: on any problem the last-position records are returned unchanged.
        """
        headers = self.get_standard_headers()

        async def fetch(device_id: int):
            return await positions.fetch_history_body(device_id, headers)

        async def resolve(pos: models.PajGPSPositionData) -> models.PajGPSPositionData:
            try:
                state = self.history_states.setdefault(pos.device_id, history.DeviceHistoryState())
                result = await history.resolve_history(state, pos.device_id, pos.fix_time, fetch, time.time())
                if result.source != history.SOURCE_HISTORY or result.point is None:
                    return pos
                pt = result.point
                return models.PajGPSPositionData(
                    pos.device_id,
                    pt.lat,
                    pt.lng,
                    pos.direction,
                    pt.speed if pt.speed is not None else pos.speed,
                    pt.battery_level if pt.battery_level is not None else pos.battery_level,
                    fix_time=pt.dateunix,
                    position_source=history.SOURCE_HISTORY,
                )
            except Exception as err:  # noqa: BLE001 - history must never break the normal path
                _LOGGER.debug("History fallback failed for device %s: %s", pos.device_id, err)
                return pos

        return list(await asyncio.gather(*(resolve(p) for p in new_positions)))

    def _apply_elevation(self, new_positions: list[models.PajGPSPositionData]) -> None:
        """
        Carry the last good elevation onto fresh positions and start a background lookup if due.

        Never waits and never touches the update lock: the lookup is its own task with a hard
        time box, and a failed one just keeps the previous value.
        """
        if not self.fetch_elevation:
            return
        now = time.time()
        for position in new_positions:
            state = self.elevation_states.setdefault(position.device_id, elevation.DeviceElevationState())
            if state.value is not None:
                position.elevation = state.value
            if not elevation.should_fetch(state, position.lat, position.lng, now):
                continue
            elevation.mark_started(state, now)
            task = asyncio.create_task(self._update_elevation_for(position.device_id, position))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _update_elevation_for(self, device_id: int, position: models.PajGPSPositionData) -> None:
        """Fetch elevation for a single position within a hard time box and store the result."""
        state = self.elevation_states.setdefault(device_id, elevation.DeviceElevationState())
        lat, lng = position.lat, position.lng
        try:
            value = await asyncio.wait_for(
                positions.fetch_elevation(device_id, position), timeout=elevation.TOTAL_TIMEOUT_S
            )
        except (asyncio.TimeoutError, TimeoutError):
            value = None
        except asyncio.CancelledError:
            state.in_flight = False
            raise
        if value is None:
            if elevation.record_failure(state, time.time()):
                _LOGGER.warning(
                    "Failed to fetch elevation for device %s, keeping previous elevation if any (%s failures in a row)",
                    device_id, state.failures,
                )
            else:
                _LOGGER.debug("Failed to fetch elevation for device %s (%s failures in a row)", device_id, state.failures)
            return
        elevation.record_success(state, value, lat, lng, time.time())
        live = self.get_position(device_id)
        (live if live is not None else position).elevation = state.value


    async def update_devices_data(self) -> None:
        """Fetch device list from the API and update self.devices."""
        new_devices, raw_json = await devices.fetch_devices(self.get_standard_headers())
        if raw_json is not None:
            self.devices_json = raw_json
        if new_devices:
            self.devices = new_devices
        elif raw_json is None:
            _LOGGER.warning("Keeping stale device data due to fetch error")

    async def update_sensors_data(self) -> None:
        """Fetch sensor data for all known devices and update self.sensors."""
        new_sensors = await sensors.fetch_sensors(self.devices, self.get_standard_headers())
        if new_sensors:
            self.sensors = new_sensors
        else:
            _LOGGER.warning("Keeping stale sensor data due to fetch error")

    async def update_alerts_data(self) -> None:
        """Fetch unread alerts and optionally schedule marking them as read."""
        new_alerts, raw_json = await alerts.fetch_alerts(self.get_standard_headers())

        if raw_json is None:
            _LOGGER.warning("Keeping stale alert data due to fetch error")
            return

        self.alerts_json = raw_json
        self.alerts = new_alerts

        if new_alerts and self.mark_alerts_as_read:
            alert_ids = [alert.alert_type for alert in new_alerts]
            task = asyncio.create_task(self.consume_alerts(alert_ids))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def consume_alerts(self, alert_ids: list[int]) -> None:
        """Mark the given alert types as read in the API."""
        await alerts.consume_alerts(alert_ids, self.get_standard_headers())

    async def change_alert_state(self, device_id: int, alert_type: int, state: bool) -> None:
        """Enable or disable an alert type for a device."""
        device = self.get_device(device_id)
        if device is None:
            _LOGGER.error("Device not found: %s", device_id)
            return
        await alerts.change_alert_state(device, alert_type, state, self.get_standard_headers())
