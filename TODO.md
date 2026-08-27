# TODO — inverter-control: Known Issues & Repair Plan

**Incident 2026-08-27 (v1.23.0 deployed)** — control loop stuck in an infinite
`WATCHDOG: Cycle timeout` state after the native D-Bus client wedged. Verified
live on Cerbo GX (SSH): service running (pid 27145), heartbeat fresh, but every
control cycle burns the full 5 s SIGALRM budget and times out; DVCC line still
sneaks through every ~60 s (a cycle occasionally completes far enough). System
load 5–7 on 4 cores.

**Status update 2026-08-27 (in progress):** PR #168 merged (`84b4951`), then on
`main`: `perf(controller)` `7e331b6` (state-dict build throttled off the 3 Hz hot
path) and `fix(victron)` `f0a16fc` (discovery timeout 2→5 s + `_discovery_lock`).
**Deployed to Cerbo 20:46 and verified closed-loop:** zero `Cycle timeout`, zero
native failures, load 5–7 → **1.55**, ESS `Hub4Mode=3`, live setpoint changing
(-610W → -642W). Residual: `update_state` p95 ~405ms is individual native D-Bus
read latency, not the dict build (see P2 below) — the setpoint path is healthy
(`setpoint_write` p95 42ms). All 483 tests pass, ruff clean. Remaining: release
hygiene (version bump + tag).

---

## Incident summary

Timeline (from `/var/log/inverter-control.log`):
- 20:12:56 — clean start (MQTT connected, prometheus up, control loop starting).
- 20:15:06 — `Native D-Bus com.victronenergy.battery.mqtt_chain1 GetValue//Soc
  failed` → `_mark_failure()` → `bus.disconnect()` fails:
  `Native D-Bus disconnect failed: A coroutine object is required`.
- 20:15:18 … ongoing — every control cycle trips `WATCHDOG: Cycle timeout`
  (`signal.alarm(5)` → `WatchdogTimeoutError` in `controller.run_cycle`).
- Also seen in the same window: `Native D-Bus on_reconnect handler failed:
  Control cycle watchdog timeout` (20:10:45) and repeated
  `D-Bus service discovery failed: Command '['dbus', '-y']' timed out after
  2 seconds` (20:04:44, 20:12:15, 20:12:54).

## Root cause

**The native D-Bus client's reconnect path deadlocks/wedges the event-loop
thread on the first disconnect, and the SIGALRM-based cycle watchdog then makes
every hot-path cycle burn the full 5 s before being cancelled.**

- `NativeDbusClient._mark_failure()` (dbus_native.py:176) strips `self._bus`
  and calls `asyncio.run_coroutine_threadsafe(bus.disconnect(), self._loop)`
  (dbus_native.py:183). In `dbus-fast`, `disconnect()` is a coroutine only the
  first time; once the bus is already torn down it returns a non-coroutine, so
  `run_coroutine_threadsafe` raises "A coroutine object is required" — the
  secondary error in the log. The stale/duplicate disconnect is unguarded.
- On reconnect, `_connect()` → `_replay_subscriptions()` → `on_reconnect()` =
  `_seed_fast_values()` runs **on the event-loop thread** (dbus_native.py:107-117,
  133-137; victron.py:261,288). `_seed_fast_values()` calls
  `self._native.get_value()` (victron.py:293), which calls
  `asyncio.run_coroutine_threadsafe(bus.call(message), self._loop).result(...)`
  (dbus_native.py:230) **from inside the same event-loop thread** → the loop is
  busy in `_connect()` and can never schedule the submitted call → the loop
  thread blocks for the timeout on every seeded value and never re-enters its
  select loop.
- After that wedge, every native read in the hot path blocks; `run_cycle`'s
  `signal.alarm(5)` (controller.py:762) is the only thing that rescues the main
  thread each cycle → `WATCHDOG: Cycle timeout`, forever. The SIGALRM exception
  ALSO leaks into cross-thread callback work (the `on_reconnect handler failed:
  ... watchdog timeout` line), because a SIGALRM handler can interrupt a
  `concurrent.futures.Future.result()` wait shared across threads.
- `signal.alarm`/SIGALRM is fundamentally the wrong tool for "is this
  synchronous native call stuck?" — it cannot interrupt a genuine C-level block,
  and it stomps unrelated threads' futures when they share a wait.

Contributing: `dbus -y` discovery timing out (2 s) repeatedly — the D-Bus daemon
is slow/heavily loaded; and frequent `Native D-Bus set failed` for
`vebus /Hub4/L1/AcPowerSetpoint` even before the hard wedge.

---

## Immediate remediation (done / first actions — Cerbo, ops)

- [ ] **Restart the service now** to clear the wedged native loop:
  `kill -9 27145` (daemontools restarts it; `svc -t` does NOT promptly restart —
  use kill -9, see CHANGELOG [1.21.2] ops note). Verify: `tail -f
  /var/log/inverter-control.log` shows the fresh-start banner and NO
  `Cycle timeout` lines.
- [ ] Write down the exact reproduce trigger for the fix branch:
  `mqtt_chain1 /Soc` read failing while native reconnect fires. Until fixed,
  treat any `GetValue//Soc failed` + `disconnect failed: A coroutine object is
  required` back-to-back as the wedge precursor.

---

## P1 — Fix the native D-Bus client so a disconnect can never wedge the loop thread

- [x] **Never call `run_coroutine_threadsafe(...).result()` from the event-loop
      thread.** Added `_call_on_loop`/`_submit_on_loop` + `_loop_thread_id` guard
      (dbus_native.py); used by `call_busitem`/`_send_add_match`/sender-map
      refresh. Regression test: `test_loop_self_call_does_not_deadlock`.
- [x] **Don't run `_seed_fast_values()` synchronously inside the reconnect.**
      `on_reconnect` now runs on a dedicated worker thread
      (`_run_reconnect_hook`), so the control cycle / hot path is never held by
      reconnect seeding. Regression test: `test_reconnect_hook_does_not_block_caller`.
- [x] **Guard the duplicate `bus.disconnect()`** (`_try_disconnect`): only
      disconnect a coroutine-returning open bus, bounded 0.2s, never raises
      "A coroutine object is required". Regression test:
      `test_mark_failure_noncoroutine_disconnect_is_safe`.
- [ ] (bonus) Hard watchdog that rebuilds the `MessageBus`+loop if a sentinel
      callback isn't processed within N seconds — not needed once the above
      landed; revisit only if a fresh wedge shows up.

## P1 — Replace the SIGALRM cycle watchdog with a safe mechanism

- [x] Removed `signal.alarm(5)` / `WatchdogTimeoutError` from `run_cycle`
      (controller.py). Native/CLI calls are already time-boxed by their own
      timeouts; a cycle abort via signal corrupted cross-thread futures on the
      reconnect path (2026-08-27).
- [x] Per-stage **slow-warning** (`STAGE_SLOW_MS=300`, `_stage()` logs a warning
      when a stage exceeds it) replaces the force-abort, keeping stage_ms
      visibility without a signal.
- [x] Non-invasive failsafe unchanged (reacts to missing setpoint/D-Bus writes,
      no signal involved).

## P1 — Make the D-Bus path resilient so a single flaky battery service can't stall the loop

- [x] `_query_battery_chain_socs` (victron.py) retains **last-known-good** SoC
      per chain and falls back to it (not a fabricated 0.0%) on a transient
      read failure; existing per-service backoff short-circuits repeat calls.
- [x] Investigate the `dbus -y` discovery timeout: raised `DISCOVERY_TIMEOUT`
      2 s → 5 s (it timed out under the load 5-7 incident), and serialized
      discovery with a non-blocking `_discovery_lock` so startup /
      `NameOwnerChanged` / poll-thread rescan can't run overlapping subprocesses
      or race the service maps (commit `f0a16fc`). Discovery is off the control
      loop already, so a few s here doesn't delay setpoints.

## P2 — Post-fix observation (2026-08-27 deploy): slow control-cycle stages

The new per-stage slow-warning (STAGE_SLOW_MS=300) surfaced a **pre-existing**
latency, previously hidden: `update_state` regularly takes 300–670 ms and
`calculate_setpoint` 300–420 ms at 3 Hz (load ~3.9 on 4 cores). This is NOT the
wedge (no cycle-timeout, setpoints still written) but it means the real loop
stays above the ~330 ms budget. Not a regression — the old build had the same
work, just no visibility.

- [x] Profile `update_state` build: measured live on Cerbo via prometheus —
      `update_state` p50 ~1ms but p95 ~377ms, max ~1.1s; `cycle_ms` p95 ~579ms.
      All getters are TTL-cached; the tail spikes come from 2s/5s/10s TTLs
      occasionally expiring in the same cycle, each doing a D-Bus read under load.
- [x] Reduce redundant work: `UPDATE_STATE_INTERVAL=0.5` — the telemetry state-dict
      (feeds web UI/MQTT, not the setpoint decision) is now rebuilt at most every
      0.5 s instead of every 333 ms cycle (commit `7e331b6`). Loop control path
      untouched.
- [x] Re-measure `perf.stage_ms.update_state.{p50,p95}` after redeploy (2026-08-27,
      deploy at 20:46): `update_state` p50 ~0ms, p95 ~405ms, max ~1.25s;
      `cycle_ms` p95 ~629ms. Throttling cut the *frequency* of slow runs but NOT
      their magnitude — each slow build is still ~400ms because individual native
      D-Bus reads spike (also `get_system_data` max ~103ms). Conclusion: the tail
      is native D-Bus read latency (daemon/native-client), NOT the dict build.
      The control decision path is healthy regardless: `setpoint_write`
      p50 6ms / p95 42ms, `calculate_setpoint` p95 ~226ms.

## P2 — Downstream correctness after the fix
- [x] Re-observe closed-loop under the fixed client (deploy 20:46): zero
      `WATCHDOG: Cycle timeout`, zero `Native D-Bus ... failed` bursts, system
      load down to 1.55 (was 3.9 post-wedge-fix, 5–7 during incident). `cycle_ms`
      p50 ~20ms; the p95 ~629ms tail is the telemetry-path D-Bus latency above.
- [x] Verify ESS remains in External control (`Hub4Mode=3` confirmed) and setpoints
      are actually applied — live AcPowerSetpoint read twice 2 s apart changed
      -610W → -642W (active closed-loop), `setpoint_write` p95 42ms, and only 1
      cumulative failed write since restart (no `Native D-Bus set failed` spam).
- [x] Confirm unit tests pass: 483 pass (incl. the native regression tests from
      PR #168 — (a) `on_reconnect`/seeding from the non-loop thread,
      (b) `call_busitem` from the loop thread doesn't deadlock,
      (c) `_mark_failure` on an already-disconnected bus is safe).
      `test_mode=True` used throughout for D-Bus isolation.

## P2 — Repo / release hygiene
- [x] Commit the pending WIP (log throttling + inverter-state parse fix) separately
      from the native-client fix — done in PR #168 (`95ac30e`).
- [ ] Bump version in `pyproject.toml` + `version` file in sync; tag `v1.23.x`.

---

### Open questions / unknowns to confirm during repair
- Whether the wedge reproduces reliably on a fast path or only when
  `mqtt_chain1` is unavailable — add a targeted debug hook (e.g. log
  `get_ident()` vs `_loop_thread_id` on the reconnect seed) before/after the fix.
- Whether dbus-fast's `disconnect()` returning a non-coroutine is a documented
  behavior of the pinned 2.21.1 — confirm the guard covers it.
