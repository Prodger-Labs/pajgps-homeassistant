"""
Glue tests: how pajgps_data, positions and device_tracker use history.py.

Standard library plus aiohttp (imported by requests.py). Home Assistant is not
installed here, so the few homeassistant modules the package imports are stubbed
before import. No network: make_request is replaced with a fake.

Run from anywhere:
    python3 custom_components/pajgps/tests/test_history_glue.py
"""
import asyncio
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class _TrackerEntity:
    pass


try:
    import homeassistant  # noqa: F401  real Home Assistant present, use it
except ImportError:
    _stub("homeassistant")
    _stub("homeassistant.config_entries")
    _stub("homeassistant.core", HomeAssistant=object, ServiceCall=object,
          SupportsResponse=types.SimpleNamespace(ONLY="only"))
    _stub("homeassistant.exceptions", HomeAssistantError=type("HomeAssistantError", (Exception,), {}))
    _stub("homeassistant.util")
    _stub("homeassistant.util.dt")
    _stub("voluptuous", Schema=lambda s: s, Required=lambda k: k, Optional=lambda k, default=None: k)
    _stub("homeassistant.const", Platform=types.SimpleNamespace(
        DEVICE_TRACKER="device_tracker", SENSOR="sensor", BINARY_SENSOR="binary_sensor", SWITCH="switch"))
    _stub("homeassistant.components")
    _stub("homeassistant.components.device_tracker")
    _stub("homeassistant.components.device_tracker.config_entry", TrackerEntity=_TrackerEntity)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.entity", DeviceInfo=dict)
    _stub("homeassistant.helpers.config_validation", string=str, boolean=bool, positive_int=int)
    sys.modules["homeassistant.exceptions"].ServiceValidationError = type(
        "ServiceValidationError", (sys.modules["homeassistant.exceptions"].HomeAssistantError,), {})
    sys.modules["homeassistant.helpers"].config_validation = sys.modules["homeassistant.helpers.config_validation"]
    sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]
    sys.modules["homeassistant"].config_entries = sys.modules["homeassistant.config_entries"]
    sys.modules["homeassistant"].core = sys.modules["homeassistant.core"]

import logging  # noqa: E402

from custom_components.pajgps import history, models  # noqa: E402
from custom_components.pajgps import pajgps_data  # noqa: E402
from custom_components.pajgps import device_tracker  # noqa: E402
from custom_components.pajgps.api import positions  # noqa: E402

logging.getLogger("custom_components.pajgps").setLevel(logging.CRITICAL)
logging.getLogger(history.__name__).setLevel(logging.CRITICAL)

NOW = 1_800_000_000.0


def pos(device_id=7, fix_age=600, lat=50.0, lng=-1.0, speed=1, battery=50):
    return models.PajGPSPositionData(device_id, lat, lng, 90, speed, battery, fix_time=int(NOW - fix_age))


def hist_body(dateunix, lat=51.0, lng=-2.0, speed=8, battery=44):
    return {"success": [{"id": 1, "lat": lat, "lng": lng, "dateunix": dateunix,
                         "speed": speed, "battery_level": battery}]}


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def __call__(self, method, url, headers, **kw):
        self.calls.append((method, url, headers, kw))
        if self.error:
            raise self.error
        return self.result


def make_data():
    d = pajgps_data.PajGPSData("g", "n", "e", "p", False, False, False)
    d.token = "tok"
    return d


class ApplyHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def run_apply(self, fake, *positions_in):
        data = make_data()
        with patch.object(positions, "make_request", new=fake), \
                patch.object(pajgps_data.time, "time", return_value=NOW):
            return data, await data._apply_history(list(positions_in))

    async def test_history_point_replaces_stale_record(self):
        fake = FakeRequest(hist_body(int(NOW - 20)))
        _, out = await self.run_apply(fake, pos())
        p = out[0]
        self.assertEqual((p.lat, p.lng, p.speed, p.battery_level), (51.0, -2.0, 8, 44))
        self.assertEqual(p.fix_time, int(NOW - 20))
        self.assertEqual(p.position_source, "history")
        self.assertEqual(p.direction, 90)  # not in the spec'd field list, kept from last-position

    async def test_request_shape(self):
        fake = FakeRequest(hist_body(int(NOW - 20)))
        await self.run_apply(fake, pos(device_id=42))
        method, url, headers, kw = fake.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/v1/trackerdata/42/last_points"), url)
        self.assertEqual(kw["params"], {"lastPoints": 2, "gps": 1, "wifi": 0})
        self.assertEqual(kw["max_attempts"], 1)
        self.assertEqual(headers["Authorization"], "Bearer tok")

    async def test_older_history_keeps_last_position(self):
        original = pos()
        _, out = await self.run_apply(FakeRequest(hist_body(int(NOW - 900))), original)
        self.assertIs(out[0], original)
        self.assertEqual(out[0].position_source, "last_position")

    async def test_request_error_keeps_last_position(self):
        original = pos()
        _, out = await self.run_apply(FakeRequest(error=TimeoutError()), original)
        self.assertIs(out[0], original)

    async def test_fresh_record_makes_no_call(self):
        fake = FakeRequest(hist_body(int(NOW)))
        original = pos(fix_age=30)
        _, out = await self.run_apply(fake, original)
        self.assertEqual(fake.calls, [])
        self.assertIs(out[0], original)

    async def test_one_device_failing_does_not_affect_another(self):
        async def fake(method, url, headers, **kw):
            if "/42/" in url:
                raise RuntimeError("boom")
            return hist_body(int(NOW - 20))
        data = make_data()
        a, b = pos(device_id=42), pos(device_id=43)
        with patch.object(positions, "make_request", new=fake), \
                patch.object(pajgps_data.time, "time", return_value=NOW):
            out = await data._apply_history([a, b])
        self.assertIs(out[0], a)
        self.assertEqual(out[1].position_source, "history")

    async def test_missing_speed_and_battery_keep_old_values(self):
        body = {"success": [{"lat": 51.0, "lng": -2.0, "dateunix": int(NOW - 20)}]}
        _, out = await self.run_apply(FakeRequest(body), pos(speed=5, battery=33))
        self.assertEqual((out[0].speed, out[0].battery_level), (5, 33))

    async def test_state_is_kept_per_device_on_the_data_object(self):
        data, _ = await self.run_apply(FakeRequest(hist_body(int(NOW - 900))), pos(device_id=7), pos(device_id=8))
        self.assertEqual(set(data.history_states), {7, 8})
        self.assertEqual(data.history_states[7].misses, 1)


class FetchPositionsTests(unittest.IsolatedAsyncioTestCase):
    def raw(self, dateunix):
        return {"success": [{"iddevice": 7, "lat": 1.0, "lng": 2.0, "direction": 0,
                             "speed": 0, "battery_level": 90, "dateunix": dateunix}]}

    async def test_fix_time_and_default_source(self):
        with patch.object(positions, "make_request", new=FakeRequest(self.raw(1_800_000_000))):
            out, _ = await positions.fetch_positions([7], {})
        self.assertEqual(out[0].fix_time, 1_800_000_000)
        self.assertEqual(out[0].position_source, "last_position")

    async def test_battery_key_battery_or_battery_level(self):
        for key in ("battery", "battery_level"):
            raw = self.raw(1_800_000_000)
            item = raw["success"][0]
            item.pop("battery_level")
            item[key] = 77
            with patch.object(positions, "make_request", new=FakeRequest(raw)):
                out, _ = await positions.fetch_positions([7], {})
            self.assertEqual(out[0].battery_level, 77, key)

    async def test_millisecond_dateunix_is_dropped_not_converted(self):
        with patch.object(positions, "make_request", new=FakeRequest(self.raw(1_800_000_000_000))):
            out, _ = await positions.fetch_positions([7], {})
        self.assertIsNone(out[0].fix_time)


class TrackerAttributeTests(unittest.IsolatedAsyncioTestCase):
    def tracker(self, position):
        t = object.__new__(device_tracker.PajGPSPositionSensor)
        t._device_id = 7
        t._pajgps_data = types.SimpleNamespace(
            update_pajgps_data=lambda: asyncio.sleep(0), get_position=lambda _id: position)
        return t

    async def test_attributes_for_history_and_last_position(self):
        for source in ("history", "last_position"):
            p = pos(fix_age=20)
            p.position_source = source
            t = self.tracker(p)
            await t.async_update()
            self.assertEqual(t.extra_state_attributes["position_source"], source)
            self.assertEqual(t.extra_state_attributes["fix_time"], "2027-01-15T07:59:40+00:00")
            self.assertEqual((t.latitude, t.longitude), (50.0, -1.0))

    async def test_no_position_gives_no_attributes(self):
        t = self.tracker(None)
        await t.async_update()
        self.assertIsNone(t.extra_state_attributes)

    async def test_missing_fix_time_still_reports_source(self):
        p = pos()
        p.fix_time = None
        t = self.tracker(p)
        await t.async_update()
        self.assertEqual(t.extra_state_attributes, {"position_source": "last_position"})


if __name__ == "__main__":
    unittest.main()
