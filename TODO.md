# TODO — inverter-control: Known Issues & Bottlenecks

Post-incident review 2026-08-24/25 (v1.21.1 + PRs #132–#138). Findings come from a
production investigation on Cerbo GX: grid not held near zero, vebus falling into
passthru, and a watchdog restart storm (360 restarts of this service).

---

## P1 — Control loop latency (root cause of everything else)

- [ ] **Cycle time is 10–20x over budget.** Measured via the new `perf` block in
      MQTT state: `cycle_ms p50 ≈ 3.8s, p95 ≈ 8.2s` vs `LOOP_INTERVAL = 0.33s`,
      `missed_deadlines` accumulating. Hot-path reads are cached since #132, so
      profile what remains: per-cycle `update_state()` getters
      (`get_mppt_chargers()` thread pool, battery SoC/cell queries), console line
      rendering, D-Bus reconcile polls. Add per-stage timing to `CycleMetrics`.
- [ ] **Telemetry snapshot age.** `snapshot_age_ms p50 ≈ 520ms, max ≈ 4.4s`.
      Background poll cadence too slow or `_signals_healthy()` falling back to
      subprocess polling. When the PropertiesChanged signal path is unhealthy the
      system degrades into a dbus-send spawn storm — check why it ever becomes
      unhealthy and add a metric for signal-path state.
- [ ] **System load.** Cerbo load average ~3.4 on 4 cores with this service plus
      dbus-modbus-client, PackageManager, observability agent running. Every
      blocking call in the control cycle competes with that.

## P1 — ESS mode mismatch (grid-zero currently off)

- [ ] System was manually switched to **Optimized (BatteryLife)** during the
      incident: `is_external=false`, so GX ignores every `AcPowerSetpoint` the
      controller writes. Return to **External control** once stable.
- [ ] Add a loud log/MQTT warning when `dry_run=false` but ESS is not in
      external mode for more than N minutes — silent no-op control is a trap.

## P2 — SmartShunt phantom battery

- [ ] `com.victronenergy.battery.ttyUSB4` (instance **289**) reports SOC stuck at
      **100%** (never synchronized) and has no cell data. Lowest instance wins,
      so GX treats it as *the* system battery → DVCC runs with
      `ccl/dcl_reason="no_cell_data"` static limits, and aggregate
      `battery_soc ≈ 70%` while real chains sit at ~34%. Sync its SOC, or remove
      it from battery enumeration / raise its instance.

## P2 — BatteryLife State=4 (discharge disabled)

- [ ] `/Settings/CGwacs/BatteryLife/State = 4` ("Discharge disabled") while
      chains are at ~34% and MinimumSocLimit=15%. Likely stale hysteresis; reset
      by toggling BatteryLife mode off/on and observe.
- [ ] On this firmware `MinimumSocLevel` / `SustainableSocLevel` are NOT readable
      via D-Bus GetValue (UnknownObject) — values only visible through retained
      MQTT `N/<portal>/settings/0/Settings/CGwacs/BatteryLife/*`. Document this;
      don't debug blind next time.

## P2 — dbus-mqtt chain services restart as a group

- [ ] `dbus-mqtt-chain1`, `dbus-mqtt-chain2`, `dbus-virtual-chain` restarted
      together (~same second). Each group restart is a BMS dropout → vebus dips
      into passthru. Find the shared trigger (flashmq? memory? SetupHelper?) and
      add restart backoff + alerting.

## P3 — Watchdog design review

- [ ] External watchdog (`service/watchdog/run`): timeout raised to **300s**
      during the incident (was the kill-loop trigger at 60s). A restart cannot
      fix slow cycles — only process death. Consider alert-only mode now that
      heartbeat writing lives in its own thread (#132).
- [ ] Its `SERVICES` list mentions `mqtt-battery` / `tasmota-pv`, names that
      don't match any `/service/*` dir — dead checks, clean up.
- [ ] Internal `HardwareWatchdog._apply_failsafe()` does
      `set_ess_mode(external=False)` and restores it on recovery → ESS mode
      flapping on transient stalls, each flap itself causes a passthru dip.
      Review whether forcing setpoint=0 alone is sufficient.
- [ ] Ops gotcha: `svc -t` does not promptly restart the service (graceful
      SIGTERM handler); use `kill -9 <pid>`. Document or shorten shutdown.

## P3 — Control tuning

- [ ] Deadband `-50..+80W`: anything up to **+80W import** is considered "at
      zero". Tighten HIGH to 20–30W if tighter grid-zero is wanted.
- [ ] Burst threshold (150W) vs deadband interplay: spikes below threshold get
      only damping+rate-limit convergence; revisit gains once cycles are fast.
- [ ] `derived_gt` (Vue blend) smoothing still runs per-cycle inside
      `logic.py`; consider moving into the GridFilter thread for one consistent
      notion of "smoothed grid".

## P3 — Repo hygiene / release

- [ ] Duplicate `service/` and `services/` dirs with diverging run files — unify
      (bit us: `-u` flag initially landed in only one of them).
- [ ] Cut a release: `version` still says 1.21.1 across merged #132–#138. Follow
      CLAUDE.md three-way sync (version file + pyproject.toml + git tag).
- [ ] The setup-script tty guard (#137) should be replicated to other packages'
      `setup` scripts (dbus-pump, etc.). The mcp-venus-os fix (#34: main-branch
      tarball + scriptAction env + `</dev/null`) requires an MCP server restart
      to take effect.
- [ ] Wire alerts on `perf.missed_deadlines` / `cycle_ms.p95` from the MQTT
      state topic (data is already published; nothing consumes it).
