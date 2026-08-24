# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Water system migrated from Home Assistant to dbus-pump D-Bus services** (no HA):
  - New `inverter_control/water.py` reads `com.victronenergy.tank.ha_tank{N}` `/Level`
    and `com.victronenergy.pump.startstop{N}` `/State` with a 2 s TTL cache;
    a missing service yields "no data" instead of zeros
  - Config: `WATER_TANK_INSTANCE` / `WATER_PUMP_INSTANCE` / `WATER_VALVE_INSTANCE`
    (defaults 21/1/2) replace `HA_WATER_VALVE` / `HA_PUMP_SWITCH`; water no longer
    auto-disables when HA_TOKEN is absent
  - Console UI shows tank level in % (was cm) and colors by valve state
  - MQTT: `water_level` / `water_valve` / `pump_switch` are always relayed on
    `inverter/state` (removed from `MQTT_SLIM_EXCLUDE_KEYS`)

## [1.21.0] - 2026-08-21

### Changed
- **Tasmota daily yields are no longer computed synthetically**: `_poll_daily_yields`
  reads `/Ac/Energy/Daily` published by dbus-tasmota-pv (Tasmota's own `ENERGY.Today`
  counter) instead of subtracting a midnight reference from the lifetime counter.
  Removes the `_pv_midnight_kwh` tracker and its timezone/reset edge cases
- MPPT yesterday yield is now read from `/History/Daily/1/Yield`

### Added
- Yesterday production in `daily_stats` on `inverter/state`: `produced_yesterday`,
  `tasmota_yesterday[]`, `mppt_yesterday[]` (Tasmota yesterday comes from the new
  dbus-tasmota-pv 3.0 path `/Energy/Daily/Yesterday`; older module versions report 0)

## [1.20.0] - 2026-08-16

### Fixed
- **BrokenPipeError crash**: `print()` to multilog pipe could raise EPIPE after service restart; wrapped stdout/stderr in `_BrokenPipeSafeStream` that swallows EPIPE, keeping the control loop alive
- **Watchdog not restarting services**: `svc -k` only kills the process; runit never auto-restarts — added `svc -u` after the kill so the service actually comes back
- **Stale orphan processes on update**: when a service dir inode changes, svscan spawns a new supervise but the old one is never killed; they linger with `(deleted)` cwd and corrupt log pipes. New update.sh step removes symlinks, then kills all supervises + run processes under the install tree before replacing files

### Changed
- **Background battery cell data polling**: replaced ~72 per-cell `dbus-send` subprocess calls with a single tree query per chain (~55 ms); cached in background poller, control loop reads cache — eliminates the 5 s cycle watchdog timeouts that were happening every 30 s
- Watchdog disable backoff (600 s default) with sticky marker so it stops hammering a stale service and flooding the log
- Watchdog `VERSION` bumped to 1.1.0

### Added
- 4 unit tests for battery cell data cache (tree poller parsing, throttle, cache hit, stale fallback)

## [1.19.1] - 2026-08-15

### Added
- **Grid Smoothing with Home Load**: New config options `ENABLE_GRID_SMOOTHING_WITH_HOME`, `GRID_SMOOTHING_HOME_WEIGHT`, `GRID_SMOOTHING_DERIVED_ALPHA`
- **Derived Grid Estimate**: `derived_gt = home_total (Vue) - pv_total (MPPT + Tasmota)` blended with instantaneous CT meter at configurable weight (default 0.7)
- **EMA Smoothing**: Derived grid EMA-smoothed with alpha 0.1 before blending
- **SystemState fields**: Added `home_total`, `derived_gt`, `filtered_gt` to dataclass

### Changed
- `SetpointCalculator.calculate()` now blends derived and instantaneous grid before strategy execution
- `main.py` fetches `home_total` from HA sensors and computes `derived_gt`

### Fixed
- Dataclass field ordering for `SystemState` (non-default args before defaults)

## [1.19.0] - 2026-08-15

### Added
- **Background D-Bus Polling Thread**: 5 Hz (`_poll_loop`) caches full service trees; eliminates ~9 subprocess calls per 3 Hz control cycle
- **Async MQTT Publish**: Background queue with `publish_state()`/`publish_console()` non-blocking; `flush()` for tests
- **Cached hot-path methods**: `get_system_data()`, `get_mppt_data()`, `get_tasmota_pv_power()`, `get_battery_chain_socs()`, `get_inverter_state()`, `get_inverter_power()`, `get_ac_in_power()`
- **`GET_VALUE_METHOD` constant**: Replaces 6 duplicate string literals
- **Caching tests**: 4 new tests in `test_caching.py` (TTL, hits, expiry, call counts)

### Changed
- Control loop latency: **200–300 ms → 10–20 ms** on Cerbo GX (RPi 3)
- Cognitive complexity: All functions < 15 (was up to 17)
- Empty `except:` → `logger.debug()` with context
- Regex patterns simplified to avoid catastrophic backtracking
- `_parse_mppt_output()` extracted (3-line helper, was inline 17-branch block)

### Fixed
- SonarCloud: Empty except clauses, cognitive complexity, regex performance, duplicate literals

## [1.18.13] - 2026-08-14

### Changed
- Migrated CI to shared `venus-os-ci-toolkit` workflows (pinned SHA `8757623`)
- Auto-approve workflow updated for `4alvit` user/org
- Hardware watchdog failsafe (30 s heartbeat) resets ESS setpoint on telemetry loss

## [1.3.1] - 2026-03-29

### Added
- Home section with Recliner, Garage, Laundry controls
- Pending button state (black) until HA update
- Washer/Dryer sections with power/pause controls
- Large power values formatted as kW (e.g., 9.5kW)
- Dishwasher running time display

### Changed
- Merged Laundry section into Home section
- Improved button state handling

### Fixed
- Toggle buttons not responding
- DRY and ESS mode button colors
- Duration parsing for HH:MM:SS format

## [1.2.0] - 2026-03-27

### Added
- Optional EV, Water, Home Assistant sections
- Feature flags in config.py
- HTTP session pooling for HA
- Circuit breaker pattern for HA polling
- Graceful shutdown handling
- Periodic garbage collection

### Changed
- Improved 24/7 reliability
- Better error handling throughout

## [1.1.0] - 2026-03-26

### Added
- HTTPS support with SSL certificates
- Loop interval control in web UI
- Uptime display in footer
- Power limits override in web UI

### Changed
- Improved web interface design
- Better mobile responsiveness

## [1.0.0] - 2026-03-25

### Added
- Initial release
- Grid-zero feed-in control
- Split-phase compensation
- Web dashboard with real-time graphs
- Multiple operating modes
- Home Assistant integration
- D-Bus communication with Victron

[1.3.1]: https://github.com/victron-venus/inverter-control/releases/tag/v1.3.1
[1.3.0]: https://github.com/victron-venus/inverter-control/releases/tag/v1.3.0
[1.2.0]: https://github.com/victron-venus/inverter-control/releases/tag/v1.2.0
[1.1.0]: https://github.com/victron-venus/inverter-control/releases/tag/v1.1.0
[1.0.0]: https://github.com/victron-venus/inverter-control/releases/tag/v1.0.0
