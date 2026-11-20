"""
Tests for the pure history-fallback logic. Standard library only, no network.

Run from anywhere:
    python3 custom_components/pajgps/tests/test_history.py

history.py is loaded by file path because importing the pajgps package needs
Home Assistant, and the whole point of the module is that it does not.
"""
import asyncio
import importlib.util
import logging
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "history.py"
_SPEC = importlib.util.spec_from_file_location("pajgps_history_under_test", _PATH)
h = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(h)
h._LOGGER.setLevel(logging.CRITICAL)  # keep test output quiet; assertLogs overrides this

NOW = 1_800_000_000.0


def point(dateunix, lat=50.7, lng=-1.9, speed=3, battery=80, **extra):
    item = {"id": dateunix, "lat": lat, "lng": lng, "dateunix": dateunix,
            "speed": speed, "battery_level": battery, "direction": 0}
    item.update(extra)
    return item


def body(*items):
    return {"success": list(items)}


class FakeApi:
    """Stands in for the last_points call. Records every call."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def __call__(self, device_id):
        self.calls.append(device_id)
        if self.error is not None:
            raise self.error
        return self.result


def run(coro):
    return asyncio.run(coro)


def resolve(state, api, age, now=NOW, device_id=7):
    return run(h.resolve_history(state, device_id, now - age, api, now))


class DecisionTests(unittest.TestCase):
    def test_stale_record_and_newer_history_point_gives_history(self):
        api = FakeApi(body(point(NOW - 120, lat=50.8), point(NOW - 30, lat=50.9, speed=9, battery=71)))
        res = resolve(h.DeviceHistoryState(), api, age=600)
        self.assertEqual(res.source, "history")
        self.assertEqual(res.outcome, h.OUTCOME_NEWER)
        self.assertEqual((res.point.lat, res.point.speed, res.point.battery_level), (50.9, 9, 71))
        self.assertEqual(res.point.dateunix, NOW - 30)
        self.assertEqual(api.calls, [7])

    def test_newest_point_chosen_by_dateunix_not_by_list_order(self):
        newest = point(NOW - 10, lat=51.0)
        for order in ([newest, point(NOW - 40)], [point(NOW - 40), newest]):
            res = resolve(h.DeviceHistoryState(), FakeApi(body(*order)), age=600)
            self.assertEqual(res.point.lat, 51.0)

    def test_history_older_than_last_position_gives_last_position(self):
        res = resolve(h.DeviceHistoryState(), FakeApi(body(point(NOW - 700))), age=600)
        self.assertEqual(res.source, "last_position")
        self.assertIsNone(res.point)
        self.assertEqual(res.outcome, h.OUTCOME_NOT_NEWER)

    def test_history_equal_to_last_position_gives_last_position(self):
        res = resolve(h.DeviceHistoryState(), FakeApi(body(point(NOW - 600))), age=600)
        self.assertEqual(res.source, "last_position")
        self.assertEqual(res.outcome, h.OUTCOME_NOT_NEWER)

    def test_history_one_second_newer_wins(self):
        res = resolve(h.DeviceHistoryState(), FakeApi(body(point(NOW - 599))), age=600)
        self.assertEqual(res.source, "history")

    def test_missing_speed_and_battery_are_none_so_caller_can_keep_old_values(self):
        raw = {"lat": 50.0, "lng": -1.0, "dateunix": NOW - 5}
        res = resolve(h.DeviceHistoryState(), FakeApi(body(raw)), age=600)
        self.assertEqual(res.source, "history")
        self.assertIsNone(res.point.speed)
        self.assertIsNone(res.point.battery_level)

    def test_source_labels(self):
        self.assertEqual(h.SOURCE_HISTORY, "history")
        self.assertEqual(h.SOURCE_LAST_POSITION, "last_position")
        fresh = resolve(h.DeviceHistoryState(), FakeApi(body(point(NOW))), age=10)
        self.assertEqual(fresh.source, "last_position")

    def test_battery_key_may_be_battery_or_battery_level(self):
        raw = {"lat": 50.0, "lng": -1.0, "dateunix": NOW - 5, "battery": 61}
        res = resolve(h.DeviceHistoryState(), FakeApi(body(raw)), age=600)
        self.assertEqual(res.point.battery_level, 61)

    def test_query_is_last_points_2_gps_1_wifi_0(self):
        self.assertEqual(h.HISTORY_QUERY, {"lastPoints": 2, "gps": 1, "wifi": 0})


class FailureTests(unittest.TestCase):
    def assert_falls_back(self, api, outcome):
        state = h.DeviceHistoryState()
        res = resolve(state, api, age=600)
        self.assertEqual(res.source, "last_position")
        self.assertIsNone(res.point)
        self.assertEqual(res.outcome, outcome)
        self.assertEqual(state.misses, 1)
        self.assertEqual(len(api.calls), 1)

    def test_fetch_raising_runtime_error_is_swallowed(self):
        self.assert_falls_back(FakeApi(error=RuntimeError("boom")), h.OUTCOME_ERROR)

    def test_fetch_raising_timeout_is_swallowed(self):
        self.assert_falls_back(FakeApi(error=TimeoutError()), h.OUTCOME_ERROR)

    def test_fetch_raising_value_error_is_swallowed(self):
        self.assert_falls_back(FakeApi(error=ValueError("html page")), h.OUTCOME_ERROR)

    def test_empty_success_list(self):
        self.assert_falls_back(FakeApi(body()), h.OUTCOME_EMPTY)

    def test_malformed_bodies(self):
        for bad in (None, [], "oops", {"error": "nope"}, {"success": None},
                    {"success": {"a": 1}}, {"success": "x"}, {"success": [1, "a", None]},
                    {"success": [{"lat": "x", "lng": 1, "dateunix": NOW - 5}]},
                    {"success": [{"lat": 95, "lng": 1, "dateunix": NOW - 5}]},
                    {"success": [{"lat": 1, "lng": 200, "dateunix": NOW - 5}]},
                    {"success": [{"lat": 1, "lng": 1}]},
                    {"success": [{"lat": True, "lng": 1, "dateunix": NOW - 5}]},
                    {"success": [{"lat": float("nan"), "lng": 1, "dateunix": NOW - 5}]}):
            with self.subTest(bad=bad):
                self.assert_falls_back(FakeApi(bad), h.OUTCOME_ERROR)

    def test_one_bad_item_does_not_spoil_a_good_one(self):
        res = resolve(h.DeviceHistoryState(), FakeApi(body("junk", point(NOW - 5))), age=600)
        self.assertEqual(res.source, "history")

    def test_cancellation_is_not_swallowed(self):
        with self.assertRaises(asyncio.CancelledError):
            resolve(h.DeviceHistoryState(), FakeApi(error=asyncio.CancelledError()), age=600)

    def test_warning_rate_limited_to_once_per_15_minutes(self):
        state = h.DeviceHistoryState()
        api = FakeApi(error=RuntimeError("boom"))
        with self.assertLogs(h._LOGGER, level="DEBUG") as cm:
            for i in range(5):  # attempts 60 s apart, all within 15 minutes
                resolve(state, api, age=600, now=NOW + i * 60)
        warnings = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len([r for r in cm.records if r.levelname == "DEBUG"]), 4)
        with self.assertLogs(h._LOGGER, level="DEBUG") as cm:
            resolve(state, api, age=600, now=NOW + 15 * 60)
        self.assertEqual(len([r for r in cm.records if r.levelname == "WARNING"]), 1)

    def test_empty_and_not_newer_never_warn(self):
        for result in (body(), body(point(NOW - 900))):
            with self.assertLogs(h._LOGGER, level="DEBUG") as cm:
                resolve(h.DeviceHistoryState(), FakeApi(result), age=600)
            self.assertEqual([r for r in cm.records if r.levelname == "WARNING"], [])


class TimingTests(unittest.TestCase):
    def test_age_threshold_89_no_call_91_call(self):
        for age, expected_calls in ((89, 0), (90, 0), (91, 1)):
            with self.subTest(age=age):
                api = FakeApi(body(point(NOW)))
                res = resolve(h.DeviceHistoryState(), api, age=age)
                self.assertEqual(len(api.calls), expected_calls)
                if expected_calls == 0:
                    self.assertEqual(res.outcome, h.OUTCOME_SKIPPED)

    def test_attempt_spacing_60_seconds(self):
        state = h.DeviceHistoryState()
        api = FakeApi(body(point(NOW - 5000)))  # never newer
        resolve(state, api, age=600, now=NOW)
        resolve(state, api, age=600, now=NOW + 59)
        self.assertEqual(len(api.calls), 1)
        resolve(state, api, age=600, now=NOW + 60)
        self.assertEqual(len(api.calls), 2)

    def test_failed_attempt_also_starts_the_spacing_clock(self):
        state = h.DeviceHistoryState()
        api = FakeApi(error=RuntimeError("x"))
        resolve(state, api, age=600, now=NOW)
        resolve(state, api, age=600, now=NOW + 30)
        self.assertEqual(len(api.calls), 1)

    def _drive_to_backoff(self, state, api):
        t = NOW
        for _ in range(10):
            resolve(state, api, age=600, now=t)
            t += 60
        return t - 60  # time of the 10th attempt

    def test_ten_misses_back_off_to_five_minutes(self):
        state = h.DeviceHistoryState()
        api = FakeApi(body(point(NOW - 5000)))
        last = self._drive_to_backoff(state, api)
        self.assertEqual(len(api.calls), 10)
        self.assertEqual(state.misses, 10)
        resolve(state, api, age=600, now=last + 60)
        resolve(state, api, age=600, now=last + 299)
        self.assertEqual(len(api.calls), 10)
        resolve(state, api, age=600, now=last + 300)
        self.assertEqual(len(api.calls), 11)

    def test_nine_misses_do_not_back_off(self):
        state = h.DeviceHistoryState()
        api = FakeApi(body(point(NOW - 5000)))
        t = NOW
        for _ in range(9):
            resolve(state, api, age=600, now=t)
            t += 60
        self.assertEqual(h.attempt_gap(state), 60)
        resolve(state, api, age=600, now=t)
        self.assertEqual(len(api.calls), 10)

    def test_errors_count_towards_backoff(self):
        state = h.DeviceHistoryState()
        api = FakeApi(error=RuntimeError("x"))
        last = self._drive_to_backoff(state, api)
        resolve(state, api, age=600, now=last + 60)
        self.assertEqual(len(api.calls), 10)

    def test_success_resets_backoff_to_60_seconds(self):
        state = h.DeviceHistoryState()
        api = FakeApi(body(point(NOW - 5000)))
        last = self._drive_to_backoff(state, api)
        api.result = body(point(NOW + 1000))  # now something newer exists
        t = last + 300
        res = resolve(state, api, age=600, now=t)
        self.assertEqual(res.source, "history")
        self.assertEqual(state.misses, 0)
        # Back to 60 s spacing, not 300 s.
        api.result = body(point(NOW - 5000))
        resolve(state, api, age=600, now=t + 60)
        self.assertEqual(len(api.calls), 12)

    def test_fresh_last_position_resets_the_miss_counter(self):
        # Deliberate reading of the spec: once the endpoint serves fresh data the
        # stale spell is over, so the next one starts at 60 s spacing.
        state = h.DeviceHistoryState(last_attempt=NOW, misses=12)
        resolve(state, FakeApi(body()), age=5, now=NOW + 10)
        self.assertEqual(state.misses, 0)

    def test_state_is_per_device(self):
        a, b = h.DeviceHistoryState(), h.DeviceHistoryState()
        api = FakeApi(body(point(NOW - 5000)))
        resolve(a, api, age=600, now=NOW, device_id=1)
        resolve(b, api, age=600, now=NOW, device_id=2)
        self.assertEqual(api.calls, [1, 2])


class MillisecondTests(unittest.TestCase):
    """dateunix is seconds. Milliseconds are never assumed or converted."""

    MS = int(NOW * 1000)

    def test_valid_dateunix(self):
        self.assertEqual(h.valid_dateunix(1_800_000_000), 1_800_000_000)
        self.assertEqual(h.valid_dateunix(1_800_000_000.0), 1_800_000_000)
        for bad in (self.MS, 0, -1, None, "1800000000", True, float("inf"), float("nan")):
            with self.subTest(bad=bad):
                self.assertIsNone(h.valid_dateunix(bad))

    def test_millisecond_last_position_makes_no_call(self):
        api = FakeApi(body(point(NOW)))
        res = run(h.resolve_history(h.DeviceHistoryState(), 7, self.MS, api, NOW))
        self.assertEqual(api.calls, [])
        self.assertEqual(res.source, "last_position")

    def test_missing_last_position_dateunix_makes_no_call(self):
        api = FakeApi(body(point(NOW)))
        res = run(h.resolve_history(h.DeviceHistoryState(), 7, None, api, NOW))
        self.assertEqual(api.calls, [])
        self.assertEqual(res.outcome, h.OUTCOME_SKIPPED)

    def test_millisecond_history_point_is_ignored_not_divided(self):
        # If it were divided by 1000 this would look newer and win.
        res = resolve(h.DeviceHistoryState(), FakeApi(body(point(self.MS))), age=600)
        self.assertEqual(res.source, "last_position")
        self.assertEqual(res.outcome, h.OUTCOME_ERROR)

    def test_millisecond_point_beside_a_good_one_is_skipped(self):
        res = resolve(h.DeviceHistoryState(), FakeApi(body(point(self.MS), point(NOW - 20))), age=600)
        self.assertEqual(res.source, "history")
        self.assertEqual(res.point.dateunix, NOW - 20)


if __name__ == "__main__":
    unittest.main()
