# TODO — inverter-control: Known Issues & Bottlenecks

Post-incident review 2026-08-24/25 (v1.21.1 + PRs #132–#138). Findings come from a
production investigation on Cerbo GX: grid not held near zero, vebus falling into
passthru, and a watchdog restart storm (360 restarts of this service).

Status update 2026-08-25: code-side items closed by v1.21.2 (#140–#148); Cerbo-side
ops done same day (notes inline). Remaining items at the bottom.

---

## P1 — Control loop latency (root cause of everything else)

- [x] **Per-stage cycle timing** — `run_cycle` records monotonic deltas into
      `CycleMetrics.record_stage()`; `perf.stage_ms.{stage}.{p50,p95,max}` in MQTT,
      `inverter_control_stage_ms` in Prometheus (#140).
- [x] **Deadline-anchored loop sleep** — slow cycles shorten/skip the sleep instead of
      drifting; stall past a slot realigns (#140).
- [x] **Signal-path health truth + storm throttle** — `_signals_healthy()` is re-derived
      from observed traffic (10s silence invalidates → resubscribe), unhealthy tree polls
      throttled 5Hz→1s, retry 60s→10s; `signals_healthy` + `dbus_subprocess_calls` published
      to perf/prom as the storm canary (#141). Alert-rule examples: `docs/prometheus-alerts.md`.
- [ ] **System load.** Cerbo load average ~3.4 on 4 cores with this service plus
      dbus-modbus-client, PackageManager, observability agent running. Re-measure now that
      the spawn storm is gone and stage_ms has soaked; profile the heaviest remaining stages.

## P1 — ESS mode mismatch

- [x] System was manually switched to Optimized during the incident — **restored to
      External control** (`Hub4Mode=3`) on 2026-08-25. Forensics: the 3→1 flip at ~00:35
      was the old watchdog failsafe's `set_ess_mode(external=False)` during a stall
      (see `/var/log/localsettings/current` change log) — removed by #144.
- [x] Loud warning when live but not external >5 min: warning-level MQTT notification +
      hourly re-warn, info on recovery (#142).

## P2 — SmartShunt phantom battery

- [x] `com.victronenergy.battery.ttyUSB4` (instance 289) removed from battery enumeration:
      `svc -d /service/vedirect-interface.ttyUSB4` on 2026-08-25 — it is a REAL SmartShunt
      500A whose SOC never synchronized (shunt, not BMS). Active BMS is now mqtt_chain1;
      DVCC reasons normal. **Caveat:** daemontools down does not survive a GX reboot —
      make permanent (raise instance / unplug / code filter) if its readings aren't needed.

## P2 — BatteryLife State=4 (discharge disabled)

- [x] Self-resolved with forensics: State 4→10 when hub4control took over after the
      watchdog-induced Hub4Mode flip (~00:35); verified State=10 (normal self-consumption)
      after External control was restored. Note: don't write `BatteryLife/State` directly
      while in External control — check the localsettings change log first next time.

## P2 — dbus-mqtt chain services restart as a group

- [x] Diagnosed: the group restart (chain1/chain2/virtual-chain all up 23200s together)
      coincides with `*** Venus OS v3.75 booted ***` — they restart because **the GX
      rebooted**, no shared crash trigger found. flashmq uptime 13h+. Revisit only if a
      non-reboot group restart shows up in logs.

## P3 — Watchdog design review

- [x] External watchdog: timeout default 60→300s, new `WATCHDOG_ALERT_ONLY=1`, dead names
      dropped from SERVICES, v1.2.0 (#145).
- [x] Internal failsafe forces setpoint only — ESS-mode force removed; recovery restores
      the prior setpoint unconditionally; knobs `WATCHDOG_TIMEOUT_SECONDS` /
      `WATCHDOG_CHECK_INTERVAL` (#144).
- [x] Ops gotcha documented: `svc -t` does not promptly restart the service — use
      `kill -9 <pid>` (CHANGELOG [1.21.2] ops note).

## P3 — Control tuning

- [x] Deadband HIGH tightened 80→30W (LOW stays −50W) (#146).
- [x] `derived_gt` smoothing moved into the GridFilter thread (`GRID_SMOOTHING_DERIVED_TAU`,
      default 3.2s; 0 = legacy alpha path) (#147).
- [ ] Burst threshold (150W) vs deadband interplay: revisit gains once stage_ms/cycle_ms
      numbers from the new release have soaked for a few days.

## P3 — Repo hygiene / release

- [x] `service/` and `services/` unified (#143).
- [x] Release cut: **v1.21.2** (version file + pyproject synced; `release.sh` now bumps
      pyproject too and asserts agreement before tagging; CHANGELOG covers #110–#147).
- [x] tty guard replication: verified N/A — none of dbus-mqtt-battery / dbus-tasmota-pv /
      dbus-virtual-battery have interactive prompts; dbus-pump has no setup script.
- [x] Alert wiring: example Prometheus rules committed in `docs/prometheus-alerts.md`.
- [ ] Grafana/Alertmanager side of those rules lives in venus-os-observability (separate repo).

---

### Follow-ups (new items from this review)

- [ ] Make the ttyUSB4 disable survive GX reboots (see P2 above).
- [ ] Observe v1.21.2 on hardware: `perf.stage_ms` p50/p95 per stage, cycle_ms p95 back
      under the 330ms budget, zero missed deadlines over 24h.
- [ ] Deploy note: #146 (deadband) and #147 (derived filter) both change closed-loop
      behavior near zero — deploy/observe separately if possible.
