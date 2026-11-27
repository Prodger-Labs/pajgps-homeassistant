"""
Glue tests: an entity update must never wait on the shared update lock, and a
slow or failing elevation lookup must never hold anything.

Background (27 Nov 2026): Home Assistant warned "Update of sensor.paj_gps_...
is taking over 10 seconds" whenever PAJ's availability HEAD check hung. Every
PAJ entity polls update_pajgps_data(), the first one took the shared lock and
sat in the 15 s HEAD timeout, and the rest queued behind the lock. Elevation
(Open-Meteo, often 502 "overloaded") runs as a background task and must stay
off that path.

Needs the pinned Home Assistant requirements (or the stubs in
test_history_glue.py). No network: every call is replaced with a fake.

Run from anywhere:
    python3 custom_components/pajgps/tests/test_update_budget_glue.py
"""
import asyncio
import logging
import pathlib
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

# Reuse the Home Assistant stubs (or the real package) set up by the history glue tests.
from custom_components.pajgps.tests import test_history_glue  # noqa: E402,F401
from custom_components.pajgps import models, pajgps_data, requests  # noqa: E402
from custom_components.pajgps.api import positions  # noqa: E402

logging.getLogger("custom_components.pajgps").setLevel(logging.CRITICAL)

BUDGET = 0.3  # shrunk from 7 s so the tests run fast


def make_data(fetch_elevation=False):
    data = pajgps_data.PajGPSData("g", "n", "e", "p", False, fetch_elevation, False)
    data.token = "tok"
    return data


async def hang(*_args, **_kwargs):
    await asyncio.sleep(3600)


class UpdateBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches = [
            patch.object(pajgps_data.random, "random", return_value=0.0),
            patch.object(pajgps_data, "UPDATE_WAIT_BUDGET_S", BUDGET),
            patch.object(pajgps_data, "CLOSE_WAIT_S", 0.2),
            patch.object(requests, "check_pajgps_availability", new=hang),
        ]
        for p in self.patches:
            p.start()
        self.data = make_data()

    async def asyncTearDown(self):
        await self.data.async_close()
        for p in self.patches:
            p.stop()

    async def test_entity_update_returns_within_budget_when_paj_hangs(self):
        started = time.monotonic()
        await asyncio.wait_for(self.data.update_pajgps_data(), timeout=BUDGET + 2)
        self.assertLess(time.monotonic() - started, BUDGET + 1)

    async def test_second_entity_does_not_wait_for_the_lock(self):
        # Both entities pass the "is the lock held" check before either takes it. The one
        # that loses must give up at once, not queue behind the lock for the whole budget.
        started = time.monotonic()
        finished = []

        async def poll():
            await self.data.update_pajgps_data()
            finished.append(time.monotonic() - started)

        await asyncio.wait_for(asyncio.gather(poll(), poll()), timeout=BUDGET + 2)
        self.assertLess(min(finished), BUDGET / 2)
        self.assertLess(max(finished), BUDGET + 1)

    async def test_forced_update_still_waits_for_the_fetch(self):
        # Setup and the options flow force an update and need real data, so they
        # are not cut short.
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.data.update_pajgps_data(True), timeout=BUDGET + 0.5)

    async def test_close_does_not_hang_on_a_stuck_update(self):
        await asyncio.wait_for(self.data.update_pajgps_data(), timeout=BUDGET + 2)
        await asyncio.wait_for(self.data.async_close(), timeout=pajgps_data.CLOSE_WAIT_S + 3)


class UpdateErrorPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fast_failing_fetch_still_raises_to_the_entity(self):
        data = make_data()
        with patch.object(pajgps_data.random, "random", return_value=0.0), \
                patch.object(requests, "check_pajgps_availability", new=AsyncMock(return_value=True)), \
                patch.object(data, "refresh_token", new=AsyncMock()), \
                patch.object(data, "_fetch_all_data", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                await data.update_pajgps_data()
        await data.async_close()


def pos(device_id=7, lat=50.71294, lng=-1.99165):
    return models.PajGPSPositionData(device_id, lat, lng, 0, 0, 80, fix_time=1_800_000_000)


class ElevationIsOffThePathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = make_data(fetch_elevation=True)
        self.calls = 0

        async def fetch_positions(*_a, **_k):
            return [pos()], {"success": []}

        async def fake_history(self_, positions_in):
            return positions_in

        self.patches = [
            patch.object(pajgps_data, "CLOSE_WAIT_S", 0.2),
            patch.object(positions, "fetch_positions", new=fetch_positions),
            patch.object(pajgps_data.PajGPSData, "_apply_history", new=fake_history),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        await self.data.async_close()
        for p in self.patches:
            p.stop()

    async def test_hanging_elevation_does_not_delay_the_poll_or_hold_the_lock(self):
        async def fetch_elevation(*_a, **_k):
            self.calls += 1
            await asyncio.sleep(3600)

        with patch.object(positions, "fetch_elevation", new=fetch_elevation):
            started = time.monotonic()
            async with self.data.update_lock:  # what _fetch_all_data holds
                await asyncio.wait_for(self.data.update_position_data(), timeout=2)
            self.assertLess(time.monotonic() - started, 1)
            await asyncio.sleep(0.05)
            self.assertEqual(self.calls, 1)
            self.assertFalse(self.data.update_lock.locked())

    async def test_elevation_lookup_is_time_boxed(self):
        async def fetch_elevation(*_a, **_k):
            await asyncio.sleep(3600)

        with patch.object(positions, "fetch_elevation", new=fetch_elevation), \
                patch.object(pajgps_data.elevation, "TOTAL_TIMEOUT_S", 0.05):
            await self.data.update_position_data()
            await asyncio.wait_for(asyncio.gather(*self.data._background_tasks), timeout=2)
        state = self.data.elevation_states[7]
        self.assertFalse(state.in_flight)
        self.assertEqual(state.failures, 1)

    async def test_last_value_survives_the_next_poll(self):
        fetch = AsyncMock(return_value=12.4)
        with patch.object(positions, "fetch_elevation", new=fetch):
            await self.data.update_position_data()
            await asyncio.gather(*self.data._background_tasks)
            await self.data.update_position_data()  # fresh PajGPSPositionData objects
        self.assertEqual(self.data.get_position(7).elevation, 12)
        self.assertEqual(fetch.await_count, 1)

    async def test_failure_keeps_the_previous_value_and_backs_off(self):
        results = iter([12.4, None, 99.0])
        fetch = AsyncMock(side_effect=lambda *a, **k: next(results))
        clock = [1_800_000_000.0]
        with patch.object(positions, "fetch_elevation", new=fetch), \
                patch.object(pajgps_data.time, "time", side_effect=lambda: clock[0]):
            await self.data.update_position_data()
            await asyncio.gather(*self.data._background_tasks)
            # Moved to a new 100 m cell, past the minimum delay: one failing lookup.
            self.data.positions[0].lat += 0.01
            clock[0] += 400
            moved = [pos(lat=50.72294)]
            with patch.object(positions, "fetch_positions", new=AsyncMock(return_value=(moved, {"success": []}))):
                await self.data.update_position_data()
                await asyncio.gather(*self.data._background_tasks)
                self.assertEqual(fetch.await_count, 2)
                # Still moving but inside the back-off: no new request, old value kept.
                clock[0] += 30
                await self.data.update_position_data()
                await asyncio.gather(*self.data._background_tasks)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(self.data.get_position(7).elevation, 12)


class FetchElevationTests(unittest.IsolatedAsyncioTestCase):
    """The Open-Meteo call: one short attempt, and an overloaded service is not an error."""

    def fake(self, result=None, error=None):
        calls = []

        async def request(method, url, headers, **kw):
            calls.append((method, url, kw))
            if error:
                raise error
            return result

        return calls, request

    async def test_good_answer_and_a_single_short_attempt(self):
        calls, request = self.fake({"elevation": [12.3]})
        with patch.object(positions, "make_request", new=request):
            self.assertEqual(await positions.fetch_elevation(7, pos()), 12.3)
        kw = calls[0][2]
        self.assertEqual((kw["max_attempts"], kw["timeout"]), (1, 5))
        self.assertEqual(kw["params"], {"latitude": 50.71294, "longitude": -1.99165})

    async def test_overloaded_service_is_quiet(self):
        body = {"error": True, "reason": "The service is overloaded"}
        for error in (requests.ApiResponseError(body), ValueError("HTTP 502 with text/html"), TimeoutError()):
            _, request = self.fake(error=error)
            with patch.object(positions, "make_request", new=request), \
                    self.assertNoLogs("custom_components.pajgps", level="WARNING"):
                self.assertIsNone(await positions.fetch_elevation(7, pos()), error)

    async def test_unexpected_answer_still_warns(self):
        for body in ({"elevation": []}, {"elevation": ["x"]}, {"nope": 1}, None):
            _, request = self.fake(body)
            with patch.object(positions, "make_request", new=request), \
                    self.assertLogs("custom_components.pajgps", level="WARNING"):
                self.assertIsNone(await positions.fetch_elevation(7, pos()), body)


if __name__ == "__main__":
    unittest.main()
