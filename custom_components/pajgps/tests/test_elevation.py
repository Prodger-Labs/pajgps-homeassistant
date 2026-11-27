"""
Tests for the pure elevation decision logic. Standard library only, no network.

Run from anywhere:
    python3 custom_components/pajgps/tests/test_elevation.py

elevation.py is loaded by file path because importing the pajgps package needs
Home Assistant, and the whole point of the module is that it does not.
"""
import importlib.util
import pathlib
import sys
import unittest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "elevation.py"
_SPEC = importlib.util.spec_from_file_location("pajgps_elevation_under_test", _PATH)
e = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = e  # dataclasses look the module up by name
_SPEC.loader.exec_module(e)

NOW = 1_800_000_000.0
LAT, LNG = 50.71294, -1.99165


class ShouldFetchTests(unittest.TestCase):
    def test_first_lookup_for_a_device(self):
        self.assertTrue(e.should_fetch(e.DeviceElevationState(), LAT, LNG, NOW))

    def test_in_flight_blocks_a_second_lookup(self):
        s = e.DeviceElevationState(in_flight=True)
        self.assertFalse(e.should_fetch(s, LAT, LNG, NOW))

    def test_same_cell_with_a_value_is_never_refetched(self):
        s = e.DeviceElevationState()
        e.record_success(s, 12.4, LAT, LNG, NOW)
        self.assertFalse(e.should_fetch(s, LAT + 0.0001, LNG - 0.0001, NOW + 86400))

    def test_moved_cell_waits_for_the_minimum_delay(self):
        s = e.DeviceElevationState()
        e.record_success(s, 12.4, LAT, LNG, NOW)
        self.assertFalse(e.should_fetch(s, LAT + 0.01, LNG, NOW + e.MIN_UPDATE_DELAY_S - 1))
        self.assertTrue(e.should_fetch(s, LAT + 0.01, LNG, NOW + e.MIN_UPDATE_DELAY_S))

    def test_started_lookup_counts_as_an_attempt_for_spacing(self):
        s = e.DeviceElevationState()
        e.mark_started(s, NOW)
        s.in_flight = False
        self.assertFalse(e.should_fetch(s, LAT, LNG, NOW + 1))

    def test_a_missing_value_is_retried_after_the_delay_even_in_the_same_cell(self):
        s = e.DeviceElevationState()
        e.mark_started(s, NOW)
        e.record_failure(s, NOW + 5)
        self.assertTrue(e.should_fetch(s, LAT, LNG, NOW + 5 + e.MIN_UPDATE_DELAY_S))


class RecordTests(unittest.TestCase):
    def test_success_stores_a_rounded_value_and_resets_failures(self):
        s = e.DeviceElevationState(failures=4, in_flight=True)
        e.record_success(s, 12.6, LAT, LNG, NOW)
        self.assertEqual((s.value, s.failures, s.in_flight), (13, 0, False))
        self.assertEqual(s.value_cell, e.cell(LAT, LNG))

    def test_failure_keeps_the_previous_value(self):
        s = e.DeviceElevationState()
        e.record_success(s, 12.4, LAT, LNG, NOW)
        e.record_failure(s, NOW + 400)
        self.assertEqual(s.value, 12)
        self.assertEqual(s.failures, 1)
        self.assertFalse(s.in_flight)

    def test_backoff_doubles_and_is_capped(self):
        waits = [e.backoff(n) for n in range(1, 10)]
        self.assertEqual(waits[:4], [300, 600, 1200, 2400])
        self.assertEqual(max(waits), e.MAX_BACKOFF_S)
        self.assertEqual(e.backoff(0), e.MIN_UPDATE_DELAY_S)

    def test_failure_pushes_the_next_attempt_out_by_the_backoff(self):
        s = e.DeviceElevationState()
        e.record_failure(s, NOW)
        e.record_failure(s, NOW)
        self.assertEqual(s.next_attempt_at, NOW + 600)
        self.assertFalse(e.should_fetch(s, LAT, LNG, NOW + 599))
        self.assertTrue(e.should_fetch(s, LAT, LNG, NOW + 600))

    def test_warning_once_per_window(self):
        s = e.DeviceElevationState()
        self.assertTrue(e.record_failure(s, NOW))
        self.assertFalse(e.record_failure(s, NOW + 60))
        self.assertFalse(e.record_failure(s, NOW + e.WARN_GAP_S - 1))
        self.assertTrue(e.record_failure(s, NOW + e.WARN_GAP_S))


class CellTests(unittest.TestCase):
    def test_cell_is_about_a_hundred_metres(self):
        self.assertEqual(e.cell(50.71294, -1.99165), (50.713, -1.992))
        self.assertEqual(e.cell(50.71301, -1.99173), e.cell(50.71294, -1.99165))
        self.assertNotEqual(e.cell(50.714, -1.992), e.cell(50.712, -1.992))


if __name__ == "__main__":
    unittest.main()
