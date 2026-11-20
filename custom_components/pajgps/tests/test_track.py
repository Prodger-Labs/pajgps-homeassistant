"""
Tests for the pure get_track logic. Standard library only, no network.

Run from anywhere:
    python3 custom_components/pajgps/tests/test_track.py

track.py is loaded by file path because importing the pajgps package needs
Home Assistant, and the whole point of the module is that it does not.
"""
import importlib.util
import pathlib
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "track.py"
_SPEC = importlib.util.spec_from_file_location("pajgps_track_under_test", _PATH)
t = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(t)

T0 = 1_800_000_000
H = 3600


def item(dateunix, lat=50.7, lng=-1.9, speed=12, battery=80, accuracy=5, **extra):
    d = {"id": str(dateunix), "lat": lat, "lng": lng, "dateunix": dateunix, "speed": speed,
         "battery_level": battery, "accuracy": accuracy, "direction": 0}
    d.update(extra)
    return d


def body(*items):
    return {"success": list(items)}


class ValidateWindowTests(unittest.TestCase):
    def test_valid_window_returns_ints(self):
        self.assertEqual(t.validate_window(T0, T0 + 2 * H + 600), (T0, T0 + 2 * H + 600))

    def test_exactly_24_hours_is_allowed(self):
        self.assertEqual(t.validate_window(T0, T0 + 24 * H), (T0, T0 + 24 * H))

    def test_24_hours_plus_one_second_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "longer than 24 hours"):
            t.validate_window(T0, T0 + 24 * H + 1)

    def test_end_equal_to_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "end must be after start"):
            t.validate_window(T0, T0)

    def test_end_before_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "end must be after start"):
            t.validate_window(T0, T0 - 1)

    def test_non_numbers_are_rejected(self):
        for bad in (("a", T0), (T0, None), (True, T0 + 5)):
            with self.assertRaisesRegex(ValueError, "unix timestamps"):
                t.validate_window(*bad)

    def test_max_hours_is_a_parameter(self):
        with self.assertRaises(ValueError):
            t.validate_window(T0, T0 + 2 * H + 1, max_hours=2)
        self.assertEqual(t.validate_window(T0, T0 + 2 * H, max_hours=2), (T0, T0 + 2 * H))


class ParseTrackBodyTests(unittest.TestCase):
    def test_point_shape_and_utc_iso(self):
        out = t.parse_track_body(body(item(T0, lat=50.5, lng=-1.5, speed=9, battery=71, accuracy=3)))
        self.assertEqual(out["count"], 1)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["dropped"], 0)
        p = out["points"][0]
        self.assertEqual(p, {"t": "2027-01-15T08:00:00+00:00", "lat": 50.5, "lon": -1.5,
                             "speed_kmh": 9, "battery": 71, "accuracy": 3})
        self.assertTrue(p["t"].endswith("+00:00"))

    def test_sorted_ascending(self):
        out = t.parse_track_body(body(item(T0 + 60), item(T0), item(T0 + 30)))
        self.assertEqual([p["t"][-14:-6] for p in out["points"]], ["08:00:00", "08:00:30", "08:01:00"])

    def test_duplicate_dateunix_removed(self):
        out = t.parse_track_body(body(item(T0, lat=1.0), item(T0, lat=2.0), item(T0 + 30)))
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["points"][0]["lat"], 1.0)  # first seen wins

    def test_missing_lat_dropped(self):
        bad = item(T0)
        del bad["lat"]
        out = t.parse_track_body(body(bad, item(T0 + 30)))
        self.assertEqual(out["count"], 1)

    def test_missing_lng_and_dateunix_dropped(self):
        no_lng = item(T0)
        del no_lng["lng"]
        no_time = item(T0 + 30)
        del no_time["dateunix"]
        self.assertEqual(t.parse_track_body(body(no_lng, no_time))["count"], 0)

    def test_string_numbers_are_non_numeric_and_dropped(self):
        out = t.parse_track_body(body(item(T0, lat="50.7"), item(T0 + 30, lng="-1.9"),
                                      item("1800000060"), item(T0 + 90)))
        self.assertEqual(out["count"], 1)

    def test_string_speed_becomes_none_but_point_kept(self):
        out = t.parse_track_body(body(item(T0, speed="fast", battery=None, accuracy="x")))
        p = out["points"][0]
        self.assertEqual((p["speed_kmh"], p["battery"], p["accuracy"]), (None, None, None))

    def test_latitude_91_dropped_and_boundaries_kept(self):
        out = t.parse_track_body(body(item(T0, lat=91), item(T0 + 30, lat=-90.0001),
                                      item(T0 + 60, lat=90), item(T0 + 90, lat=-90)))
        self.assertEqual(out["count"], 2)

    def test_longitude_out_of_range_dropped(self):
        out = t.parse_track_body(body(item(T0, lng=181), item(T0 + 30, lng=-180.5), item(T0 + 60, lng=180)))
        self.assertEqual(out["count"], 1)

    def test_millisecond_dateunix_rejected_not_divided(self):
        out = t.parse_track_body(body(item(T0 * 1000), item(T0)))
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["points"][0]["t"], "2027-01-15T08:00:00+00:00")

    def test_non_positive_dateunix_rejected(self):
        self.assertEqual(t.parse_track_body(body(item(0), item(-5)))["count"], 0)

    def test_bool_and_nan_rejected(self):
        out = t.parse_track_body(body(item(T0, lat=True), item(T0 + 30, lat=float("nan")),
                                      item(T0 + 60, speed=float("inf"))))
        self.assertEqual(out["count"], 1)
        self.assertIsNone(out["points"][0]["speed_kmh"])

    def test_empty_and_non_dict_bodies(self):
        empty = {"points": [], "count": 0, "truncated": False, "dropped": 0}
        for b in (None, [], "x", 5, {}, body()):
            self.assertEqual(t.parse_track_body(b), empty, b)

    def test_missing_or_malformed_success_key(self):
        empty = {"points": [], "count": 0, "truncated": False, "dropped": 0}
        for b in ({"error": "x"}, {"success": None}, {"success": "nope"}, {"success": {"a": 1}}):
            self.assertEqual(t.parse_track_body(b), empty, b)

    def test_non_dict_items_skipped(self):
        out = t.parse_track_body({"success": [None, 5, "x", item(T0)]})
        self.assertEqual(out["count"], 1)

    def test_truncation_keeps_earliest_and_sets_flag(self):
        items = [item(T0 + 30 * i) for i in range(10)][::-1]  # API order is descending
        out = t.parse_track_body(body(*items), max_points=4)
        self.assertEqual(out["count"], 4)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["dropped"], 6)
        self.assertEqual(out["points"][0]["t"], "2027-01-15T08:00:00+00:00")
        self.assertEqual(out["points"][-1]["t"], "2027-01-15T08:01:30+00:00")

    def test_exactly_max_points_is_not_truncated(self):
        out = t.parse_track_body(body(*[item(T0 + 30 * i) for i in range(4)]), max_points=4)
        self.assertEqual((out["count"], out["truncated"], out["dropped"]), (4, False, 0))

    def test_default_max_points_is_5000(self):
        out = t.parse_track_body(body(*[item(T0 + i) for i in range(5001)]))
        self.assertEqual((out["count"], out["truncated"], out["dropped"]), (5000, True, 1))

    def test_battery_falls_back_to_battery_key(self):
        it = item(T0)
        del it["battery_level"]
        it["battery"] = 55
        self.assertEqual(t.parse_track_body(body(it))["points"][0]["battery"], 55)

    def test_realistic_238_points_over_2h10(self):
        items = [item(T0 + 30 * i, lat=50.7 + i * 1e-5, speed=i % 40) for i in range(238)]
        out = t.parse_track_body(body(*items[::-1]))
        self.assertEqual((out["count"], out["truncated"]), (238, False))
        self.assertEqual(out["points"][-1]["t"], t.iso_utc(T0 + 30 * 237))


class IsoUtcTests(unittest.TestCase):
    def test_utc_suffix(self):
        self.assertEqual(t.iso_utc(0), "1970-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
