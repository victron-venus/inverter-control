# TODO — inverter-control: Known Issues & Repair Plan

**Incident 2026-08-27 (v1.23.0 deployed)** — control loop stuck in an infinite
`WATCHDOG: Cycle timeout` state after the native D-Bus client wedged. Verified
live on Cerbo GX (SSH): service running (pid 27145), heartbeat fresh, but every
control cycle burns the full 5 s SIGALRM budget and times out; DVCC line still
sneaks through every ~60 s (a cycle occasionally completes far enough). System
load 5–7 on 4 cores.

**Status update 2026-08-27 (work in progress):** service restarted on Cerbo
(`kill -9`, daemontools restarted it clean — no new `Cycle timeout` since).
Code fixes implemented and unit-tested locally (483 pass): see P1 below —
reconnect no longer self-deadlocks, `on_reconnect` seeding deferred off the hot
path, disconnect guarded against dbus_fast's one-shot coroutine, SIGALRM cycle
abort replaced with per-stage slow-warning, battery-chain SoC retains
last-known-good. **Pending: deploy to Cerbo + soak.**

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
- [ ] Investigate the `dbus -y` discovery timeout (2 s) — raise timeout / run on
      background thread / cache discovered map longer under load (load was 5–7).

## P2 — Downstream correctness after the fix
- [ ] Re-observe closed-loop under the fixed client: `perf.stage_ms.*`
      p50/p95 and `cycle_ms` p95 back under the ~330 ms budget, zero
      `WATCHDOG: Cycle timeout`, zero `Native D-Bus ... failed` bursts.
- [ ] Verify ESS remains in External control (`Hub4Mode=3`) and setpoints are
      actually applied (no `Native D-Bus set failed: vebus` spam).
- [ ] Confirm unit tests still pass (186 non-D-Bus tests; native tests use
      `test_mode=True`). Add a regression test covering: (a) `on_reconnect`
      seeding from the non-loop thread, (b) `call_busitem` from the loop thread
      doesn't deadlock, (c) `_mark_failure` on an already-disconnected bus
      doesn't raise "A coroutine object is required".

## P2 — Repo / release hygiene
- [ ] Commit the pending WIP (currently uncommitted `dvcc.py`, `victron.py`,
      `main.py` logging/throttle + inverter-state parse fix) separately from the
      native-client fix so the two are bisectable.
- [ ] Bump version in `pyproject.toml` + `version` file in sync; tag `v1.23.x`.

---

### Open questions / unknowns to confirm during repair
- Whether the wedge reproduces reliably on a fast path or only when
  `mqtt_chain1` is unavailable — add a targeted debug hook (e.g. log
  `get_ident()` vs `_loop_thread_id` on the reconnect seed) before/after the fix.
- Whether dbus-fast's `disconnect()` returning a non-coroutine is a documented
  behavior of the pinned 2.21.1 — confirm the guard covers it.
