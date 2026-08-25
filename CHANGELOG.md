# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.21.2] - 2026-08-25

Post-incident hardening release (Cerbo GX investigation 2026-08-24/25: grid not
held at zero, vebus passthru dips, watchdog restart storm). Covers everything
since v1.21.1.

### Added
- **Persistent `dbus_fast` connection** for Get/Set hot path, replacing per-call
  `dbus-send` subprocesses; signal-driven fast inputs via BusItem change
  signals with slow tree-poll reconciliation
- **Rolling control-loop latency metrics**: cycle/setpoint-write/snapshot-age
  p50/p95/p99 in the MQTT `perf` block, missed-deadline counter, process
  CPU/RSS on Linux (#113, #114)
- **Per-stage cycle timing** (`perf.stage_ms.{stage}.{p50,p95,max}`) and a
  deadline-anchored main-loop sleep (#140); Prometheus exposition on
  `:9102/metrics` via `INVERTER_METRICS_PORT` (#136)
- **Signal-path health truth**: `signals_healthy` is now re-derived from
  observed traffic (10s silence invalidates), transition logging, and the
  dbus-send spawn counter published as a storm canary (#141)
- **Loud ESS warning**: sustained "ESS not in External control" (>5 min while
  live) raises an MQTT warning notification (desktop banner) and re-warns
  hourly; recovery emits info (#142)
- Real battery yesterday charge/discharge from D-Bus history (#117)
- `/api/v1/forecast` webhook storing daily solar forecast into MQTT state (#119)
- Retained `inverter/portal` MQTT topic with the auto-detected VRM Portal ID (#121)
- Background GridFilter thread for the CT grid value + decoupled heartbeat
  writer thread (#132)

### Changed
- **Water system migrated from Home Assistant to dbus-pump D-Bus services**
  (`WATER_TANK/PUMP/VALVE_INSTANCE` config; water no longer requires HA) (#120)
- **Watchdog failsafe forces setpoint only** — no longer flips ESS out of
  External control on stall (each Hub4 3↔1 flap caused a vebus passthru dip
  and reset BatteryLife State); recovery restores the prior setpoint
  unconditionally. `WATCHDOG_TIMEOUT_SECONDS` / `WATCHDOG_CHECK_INTERVAL`
  knobs added (#144)
- **External watchdog**: timeout default 60→300s, new `WATCHDOG_ALERT_ONLY=1`
  mode, dead service names dropped from `SERVICES` (#145)
- **Grid-zero deadband HIGH tightened +80W → +30W** (LOW stays −50W);
  rollback via local_config.py (#146)
- derived_gt smoothing moved into the GridFilter thread
  (`GRID_SMOOTHING_DERIVED_TAU`, default 3.2s; 0 = legacy alpha path) (#147)

### Fixed
- Unhealthy signal-path fallback throttled from 5Hz to 1s — the dbus-send
  spawn storm behind the watchdog restart loop (#141)
- ThreadPoolExecutor crash on empty MPPT service lists (#133)
- Service runs unbuffered (`python3 -u`) and logs via multilog (#134, #135)
- setup script refuses interactive prompt on non-TTY stdin (#137)
- `PUSH_LOCAL_CONFIG` is opt-in in deploy path (#138)
- Missing daemontools log/run for watchdog and log-forwarder (#139)
- `service/` and `services/` trees unified (single source of truth) (#143)
- `release.sh` now syncs pyproject.toml with the version file and asserts
  agreement before tagging

### Ops note
`svc -t` does not promptly restart the service under daemontools (graceful
SIGTERM handling) — use `kill -9 <pid>` and let runit respawn it.

### Added
- Retained `inverter/portal` MQTT topic: the auto-detected VRM Portal ID is
  published on every broker connect, so remote consumers (desktop app) can
  discover the `N/<portal>/...` water/alarm topics with no manual config.

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
