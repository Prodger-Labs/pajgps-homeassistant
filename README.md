#  PAJ GPS Tracker Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/skipperro/pajgps-homeassistant.svg)](https://GitHub.com/skipperro/pajgps-homeassistant/releases/)
![](https://img.shields.io/badge/dynamic/json?color=41BDF5&logo=home-assistant&label=integration%20usage&suffix=%20installs&cacheSeconds=15600&url=https://analytics.home-assistant.io/custom_integrations.json&query=$.pajgps.total)
![PAJ GPS](https://raw.githubusercontent.com/Skipperro/pajgps-homeassistant/main/images/logo.png)

> [!IMPORTANT]  
> This integration was moved to Home Assistant Core in version 2026.6.
>
> It's now available for install as core integration, without the need for HACS.
> Any further development of this integration will only be done directly in Home Assistant repository.
>
> This repository is now obosolete!

## The `patched` branch (Prodger-Labs fork)

This fork keeps its changes on `patched`, which is the default branch. `main` is left as it was when this fork was made (upstream plus one requirements commit).

What is different from upstream:
- The availability check follows redirects on HEAD (`allow_redirects=True`).
- `device_tracker` exposes `fix_time` (the API's own `dateunix`, ISO 8601 UTC) and `position_source`.
- History fallback. The last-position endpoint can serve a stale record for tens of minutes. When the record is over 90 s old, the integration asks `GET /api/v1/trackerdata/{id}/last_points?lastPoints=2&gps=1&wifi=0` (at most once a minute per device, once every 5 minutes after 10 fruitless tries) and uses the newest point if it is newer. `position_source` is `history` or `last_position`. The poll interval is unchanged at 30 s. The logic lives in `custom_components/pajgps/history.py` and has no Home Assistant imports.

Tests (no network, no credentials):

    python3 custom_components/pajgps/tests/test_history.py
    python3 custom_components/pajgps/tests/test_history_glue.py
    python3 custom_components/pajgps/tests/test_track.py
    python3 custom_components/pajgps/tests/test_track_service.py

### Services

`pajgps.get_track` returns every point the PAJ cloud holds for a tracker between two times, oldest first. Use it after a trip to work out distance and hours from the true track instead of the latest point. It returns a response, so call it with `response_variable`:

```yaml
action: pajgps.get_track
data:
  device_id: 1242185
  start: "2026-11-19 09:00:00"
  end: "2026-11-19 11:10:00"
response_variable: trip
```

`start` and `end` take ISO 8601. With an offset (`2026-11-19T09:00:00+00:00`) they are used as written. Without one they are Home Assistant's local time. `device_id` is optional when the account has one tracker. `include_wifi` (default false) also asks PAJ for Wi-Fi located points.

The response has `device_id`, `start`, `end` (ISO 8601 UTC), `count`, `truncated`, `dropped` and `points`. Each point is `t` (ISO 8601 UTC), `lat`, `lon`, `speed_kmh`, `battery` and `accuracy`. Points are sorted by time and de-duplicated. Points without usable coordinates or time are skipped.

Limits:
- The window is at most 24 hours. A longer one raises an error.
- At most 5000 points come back. If there are more, the earliest 5000 are kept, `truncated` is true and `dropped` says how many valid points were cut.
- `speed_kmh` is in km/h, as PAJ reports it.
- One request, 20 s timeout, no retries. A failed call raises an error with no URL or token in it.

Nothing tracks upstream. Skipperro's repo is obsolete (its README says the integration moved into Home Assistant Core) and last changed on 4 June 2026, so this fork is maintained here and no check, ticket or reminder watches it. If upstream ever changes in a way that matters, bring it in by hand: `git fetch upstream` and merge or cherry-pick into `patched`, then run the test files under `custom_components/pajgps/tests/`.

Never push to, or open a pull request or issue on, `skipperro/pajgps-homeassistant`. Nothing goes back upstream. Set the push URL of the `upstream` remote to `DISABLED` to make that physical.

## Introduction
Welcome to the PAJ GPS Tracker Integration for Home Assistant!<br>
This integration enables you to effortlessly incorporate PAJ GPS devices from www.paj-gps.de into your Home Assistant setup.

## Disclaimer

- This integration is created with acknowledgement and limited support from PAJ GPS but **this integration is not official software from PAJ UG**.<br>
- It's a custom integration created entirely by me (Skipperro), and thus PAJ UG is not responsible for any damage/issues caused by this integration, nor it offers any end-user support for it.

## Moving into the Home Assistant core repository

This integration is in the process of refactoring and adapting in order to be submitted for inclusion in the Home Assistant core repository. Once it is accepted and merged, it will be available as a built-in integration, eliminating the need for manual installation.
This also means that it will no longer be necessary to add this repository to HACS, and the integration will be available for installation directly from the Home Assistant interface.

## Features
- [x] Device tracking (Longitude, Latitude)
- [x] Device battery level
- [x] Device speed
- [x] Notifications (SOS, low battery, etc.)
- [x] Elevation
- [x] Support for multiple accounts

## Planned features
- [ ] Device status (moving, stopped, etc.)
- [ ] Geofencing

## Supported devices

While this integration was primarily tested with the **Allround Finder 2G 2.0**, it leverages the standard API provided by PAJ. Therefore, it should be compatible with other PAJ GPS devices. If you encounter any issues with different devices, please report them.
## Installation

1. **Make a proper setup of your PAJ GPS device**. You need to have an account on www.v2.finder-portal.com and your device must be properly configured and connected to the platform.
2. Install this integration with HACS (adding this repository may be required), or copy the contents of this
repository into the `custom_components/pajgps` directory.
2. Restart Home Assistant.
3. Start the configuration flow:
   - [![Start Config Flow](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=pajgps)
   - Or: Go to `Configuration` -> `Integrations` and click the `+ Add Integration`. Select `PAJ GPS` from the list.
   - If the integration is not found try to refresh the HA page without using cache (Ctrl+F5).
4. Provide your email and password used to login on www.v2.finder-portal.com. This data will be saved only in your Home Assistant and is required to generate API token.
5. Device Tracker Entities will be created for all your devices.

## Configuration

The integration will automatically discover all your devices connected to your account on www.v2.finder-portal.com. 
They will be added as entities to Home Assistant based on their ID from the API (not the number on the device).

There is no need to configure anything else.
