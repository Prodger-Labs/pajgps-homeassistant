"""
Glue tests for the pajgps.get_track service (the handler in __init__.py, positions.fetch_track_body).

Standard library plus aiohttp (imported by requests.py). Home Assistant is not
installed here, so the few homeassistant modules the package imports are stubbed
before import. No network: make_request is replaced with a fake, and the token
refresh is replaced so no login is attempted.

Run from anywhere:
    python3 custom_components/pajgps/tests/test_track_service.py
"""
import datetime
import logging
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=5))  # stands in for Home Assistant's configured zone


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class _TrackerEntity:
    pass


try:
    import homeassistant  # noqa: F401  real Home Assistant present, use it
    STUBBED = False
except ImportError:
    STUBBED = True

    def _parse_datetime(value):
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    def _as_utc(dattim):  # same rule as homeassistant.util.dt.as_utc
        if dattim.tzinfo is None:
            dattim = dattim.replace(tzinfo=LOCAL_TZ)
        return dattim.astimezone(datetime.timezone.utc)

    _ha_error = type("HomeAssistantError", (Exception,), {})
    _stub("homeassistant")
    _stub("homeassistant.config_entries")
    _stub("homeassistant.core", HomeAssistant=object, ServiceCall=object,
          SupportsResponse=types.SimpleNamespace(ONLY="only"))
    _stub("homeassistant.const", Platform=types.SimpleNamespace(
        DEVICE_TRACKER="device_tracker", SENSOR="sensor", BINARY_SENSOR="binary_sensor", SWITCH="switch"))
    _stub("homeassistant.exceptions", HomeAssistantError=_ha_error,
          ServiceValidationError=type("ServiceValidationError", (_ha_error,), {}))
    _stub("homeassistant.util")
    _stub("homeassistant.util.dt", parse_datetime=_parse_datetime, as_utc=_as_utc)
    _stub("homeassistant.components")
    _stub("homeassistant.components.device_tracker")
    _stub("homeassistant.components.device_tracker.config_entry", TrackerEntity=_TrackerEntity)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.entity", DeviceInfo=dict)
    _stub("homeassistant.helpers.config_validation", string=str, boolean=bool, positive_int=int)
    _stub("voluptuous", Schema=lambda s: s, Required=lambda k: k, Optional=lambda k, default=None: k)
    sys.modules["homeassistant"].config_entries = sys.modules["homeassistant.config_entries"]
    sys.modules["homeassistant"].core = sys.modules["homeassistant.core"]
    sys.modules["homeassistant.helpers"].config_validation = sys.modules["homeassistant.helpers.config_validation"]
    sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]

import custom_components.pajgps as integration  # noqa: E402
from custom_components.pajgps import pajgps_data  # noqa: E402
from custom_components.pajgps.api import positions  # noqa: E402
from custom_components.pajgps.requests import ApiResponseError  # noqa: E402
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: E402

logging.getLogger("custom_components.pajgps").setLevel(logging.CRITICAL)

T0 = 1_800_000_000  # 2027-01-15T08:00:00Z
START_Z = "2027-01-15T08:00:00+00:00"
END_Z = "2027-01-15T10:10:00+00:00"  # 2 h 10 min later, the real-world trip length


def item(dateunix, lat=50.7, lng=-1.9, speed=12):
    return {"id": str(dateunix), "lat": lat, "lng": lng, "dateunix": dateunix, "speed": speed,
            "battery_level": 80, "accuracy": 4}


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def __call__(self, method, url, headers, **kw):
        self.calls.append((method, url, headers, kw))
        if self.error:
            raise self.error
        return self.result


def call(**data):
    return types.SimpleNamespace(data=data)


def make_data(guid, *device_ids, token="secret-token-123"):
    d = pajgps_data.PajGPSData(guid, "n", "e", "p", False, False, False)
    d.token = token
    d.devices = [types.SimpleNamespace(id=i) for i in device_ids]

    async def fresh():  # stands in for refresh_token: no login, no network
        d.refresh_calls = getattr(d, "refresh_calls", 0) + 1
    d.refresh_token = fresh
    return d


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pajgps_data.PajGPSDataInstances.clear()
        self.addCleanup(pajgps_data.PajGPSDataInstances.clear)

    def add(self, *device_ids, guid="g1", **kw):
        d = make_data(guid, *device_ids, **kw)
        pajgps_data.PajGPSDataInstances[guid] = d
        return d

    async def run_handler(self, fake, **data):
        with patch.object(positions, "make_request", new=fake):
            return await integration.async_handle_get_track(call(**data))


class HappyPathTests(Base):
    async def test_single_device_is_auto_selected_and_response_shape(self):
        self.add(1242185)
        fake = FakeRequest({"success": [item(T0 + 30), item(T0)]})
        out = await self.run_handler(fake, start=START_Z, end=END_Z)
        self.assertEqual(set(out), {"device_id", "start", "end", "count", "truncated", "dropped", "points"})
        self.assertEqual(out["device_id"], 1242185)
        self.assertEqual((out["start"], out["end"]), (START_Z, END_Z))
        self.assertEqual((out["count"], out["truncated"], out["dropped"]), (2, False, 0))
        self.assertEqual([p["t"] for p in out["points"]], [START_Z, "2027-01-15T08:00:30+00:00"])
        self.assertEqual(set(out["points"][0]), {"t", "lat", "lon", "speed_kmh", "battery", "accuracy"})

    async def test_request_shape(self):
        data = self.add(42)
        fake = FakeRequest({"success": []})
        await self.run_handler(fake, start=START_Z, end=END_Z, device_id=42)
        method, url, headers, kw = fake.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/v1/trackerdata/42/date_range"), url)
        self.assertEqual(kw["params"], {"dateStart": T0, "dateEnd": T0 + 7800, "gps": 1, "wifi": 0})
        self.assertEqual((kw["timeout"], kw["max_attempts"]), (20, 1))
        self.assertEqual(headers["Authorization"], "Bearer secret-token-123")
        self.assertEqual(data.refresh_calls, 1)

    async def test_include_wifi_sets_wifi_1(self):
        self.add(42)
        fake = FakeRequest({"success": []})
        await self.run_handler(fake, start=START_Z, end=END_Z, include_wifi=True)
        self.assertEqual(fake.calls[0][3]["params"]["wifi"], 1)

    async def test_token_refreshed_before_headers_are_built(self):
        data = self.add(42, token=None)

        async def refresh():
            data.token = "fresh-token"
        data.refresh_token = refresh
        fake = FakeRequest({"success": []})
        await self.run_handler(fake, start=START_Z, end=END_Z)
        self.assertEqual(fake.calls[0][2]["Authorization"], "Bearer fresh-token")

    async def test_empty_success_gives_zero_points(self):
        self.add(42)
        out = await self.run_handler(FakeRequest({"success": []}), start=START_Z, end=END_Z)
        self.assertEqual((out["count"], out["points"]), (0, []))

    async def test_device_chosen_across_accounts(self):
        self.add(1, guid="a")
        self.add(2, guid="b")
        fake = FakeRequest({"success": []})
        out = await self.run_handler(fake, start=START_Z, end=END_Z, device_id=2)
        self.assertEqual(out["device_id"], 2)
        self.assertTrue(fake.calls[0][1].endswith("/2/date_range"))


class DeviceSelectionTests(Base):
    async def test_several_devices_without_device_id_raises_and_lists_ids(self):
        self.add(30, 10, 20)
        fake = FakeRequest({"success": []})
        with self.assertRaises(ServiceValidationError) as cm:
            await self.run_handler(fake, start=START_Z, end=END_Z)
        self.assertIn("[10, 20, 30]", str(cm.exception))
        self.assertEqual(fake.calls, [])

    async def test_unknown_device_id_raises_and_lists_ids(self):
        self.add(10, 20)
        with self.assertRaises(ServiceValidationError) as cm:
            await self.run_handler(FakeRequest({"success": []}), start=START_Z, end=END_Z, device_id=99)
        self.assertIn("99", str(cm.exception))
        self.assertIn("[10, 20]", str(cm.exception))

    async def test_no_devices_loaded_raises_validation_error(self):
        with self.assertRaises(ServiceValidationError):
            await self.run_handler(FakeRequest({"success": []}), start=START_Z, end=END_Z)


class WindowAndTimeTests(Base):
    async def test_naive_start_end_are_local_time(self):
        self.add(42)
        fake = FakeRequest({"success": []})
        out = await self.run_handler(fake, start="2027-01-15 13:00:00", end="2027-01-15T15:10:00")
        # local zone is UTC+5 here, so 13:00 local is 08:00Z
        self.assertEqual((out["start"], out["end"]), (START_Z, END_Z))
        self.assertEqual(fake.calls[0][3]["params"]["dateStart"], T0)

    async def test_offset_start_end_are_taken_as_written(self):
        self.add(42)
        out = await self.run_handler(FakeRequest({"success": []}),
                                     start="2027-01-15T09:00:00+01:00", end="2027-01-15T10:10:00Z")
        self.assertEqual((out["start"], out["end"]), (START_Z, END_Z))

    async def test_bad_inputs_raise_validation_error_and_make_no_call(self):
        self.add(42)
        cases = [
            ("not a date", END_Z),
            (START_Z, "tomorrow"),
            (END_Z, START_Z),  # end before start
            (START_Z, START_Z),  # zero length
            (START_Z, "2027-01-16T08:00:01+00:00"),  # 24 h + 1 s
        ]
        for start, end in cases:
            fake = FakeRequest({"success": []})
            with self.assertRaises(ServiceValidationError, msg=(start, end)):
                await self.run_handler(fake, start=start, end=end)
            self.assertEqual(fake.calls, [])

    async def test_exactly_24_hours_is_accepted(self):
        self.add(42)
        out = await self.run_handler(FakeRequest({"success": []}),
                                     start=START_Z, end="2027-01-16T08:00:00+00:00")
        self.assertEqual(out["count"], 0)


class FailureTests(Base):
    async def assert_clean_failure(self, error=None, result=None):
        self.add(42)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with self.assertRaises(HomeAssistantError) as cm:
                await self.run_handler(FakeRequest(result=result, error=error), start=START_Z, end=END_Z)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertNotIsInstance(cm.exception, ServiceValidationError)
        text = str(cm.exception) + " ".join(r.getMessage() for r in records)
        for secret in ("secret-token-123", "Bearer", "Authorization", "dateStart", "dateEnd", "http", "X-CSRF"):
            self.assertNotIn(secret, text)
        return str(cm.exception)

    async def test_leaky_value_error_maps_to_clean_message(self):
        msg = await self.assert_clean_failure(error=ValueError(
            "HTTP 500 with text/html from https://connect.paj-gps.de/x?dateStart=1 Bearer secret-token-123"))
        self.assertIn("ValueError", msg)

    async def test_timeout(self):
        self.assertIn("in time", await self.assert_clean_failure(error=TimeoutError("secret-token-123")))

    async def test_api_error_response(self):
        await self.assert_clean_failure(error=ApiResponseError({"error": "Bearer secret-token-123"}))

    async def test_client_error(self):
        import aiohttp
        await self.assert_clean_failure(error=aiohttp.ClientConnectionError("secret-token-123"))

    async def test_unusable_body(self):
        for body in (None, [], {"error": "x"}, {"success": None}, "<html>"):
            await self.assert_clean_failure(result=body)

    async def test_missing_token_after_refresh(self):
        self.add(42, token=None)
        fake = FakeRequest({"success": []})
        with self.assertRaises(HomeAssistantError):
            await self.run_handler(fake, start=START_Z, end=END_Z)
        self.assertEqual(fake.calls, [])


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_registered_once_in_async_setup_with_response(self):
        registered = []
        hass = types.SimpleNamespace(
            data={}, services=types.SimpleNamespace(
                async_register=lambda *a, **kw: registered.append((a, kw))))
        self.assertTrue(await integration.async_setup(hass, {}))
        self.assertEqual(len(registered), 1)
        args, kw = registered[0]
        self.assertEqual(args, ("pajgps", "get_track", integration.async_handle_get_track))
        self.assertIs(kw["schema"], integration.GET_TRACK_SCHEMA)
        self.assertEqual(kw["supports_response"], integration.SupportsResponse.ONLY)


if __name__ == "__main__":
    unittest.main()
