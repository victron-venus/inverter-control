# TODO — inverter-control Improvements

Analysis of v1.20.0 codebase (~8,700 lines). Prioritized by impact on reliability, performance, and maintainability on Cerbo GX (RPi 3).

---

## 1. Performance (High Priority)

### P1: Move uncached D-Bus reads to background poller ✅
**Problem:** `get_pv_inverter_daily_yields()` and `get_battery_daily_energy()` are called from `update_state()` every cycle (3 Hz) and make uncached subprocess reads directly. On a system with 3 MPPTs + 2 Tasmota inverters = 7+ subprocess calls per cycle in the main thread.
**Fix:** Add to `_poll_all()` in background thread. Cache results with ~2 s TTL.
**Files:** `inverter_control/victron.py` (lines 1466–1514)
**Effort:** M

### P2: Batch MPPT tree queries ✅
**Problem:** `_poll_mppt_data_tree()` runs a separate subprocess for each MPPT service (lines 361–380). 3 MPPTs = 3 subprocess calls = ~300–600 ms.
**Fix:** Single `dbus-send --print-reply /` with grep for MPPT patterns, same pattern as `get_system_data()`.
**Files:** `inverter_control/victron.py`
**Effort:** M

### P3: VUE sensor tree query ✅
**Problem:** `VUESensorDBusClient.update_all()` makes N subprocess calls per poll cycle (dbus.py lines 146–164). Each VUE sensor = separate call.
**Fix:** Batch tree query for acload services (discovery already uses tree query).
**Files:** `inverter_control/dbus.py`
**Effort:** S

### P4: Compile regexes once ✅
**Problem:** `_parse_numeric` (homeassistant.py:173) recompiles regex on every call. ~10 sensors × 0.67 Hz = 7 regex compilations/sec.
**Fix:** `re.compile()` at module level.
**Files:** `inverter_control/homeassistant.py`
**Effort:** S

### P5: Lazy-evaluate PORTAL_ID ✅
**Problem:** `config.py` line 161 — `_detect_portal_id()` spawns a subprocess at import time. On systems without D-Bus = 5 s timeout blocking startup.
**Fix:** `functools.lru_cache` or lazy `@property`.
**Files:** `inverter_control/config.py`
**Effort:** S

### P6: Review no_feed 2 s sleep ✅
**Problem:** `main.py` line 753 — `time.sleep(2)` blocks the control loop for 2 seconds in no_feed mode. Loop interval jumps from 0.33 s to 2.33 s.
**Fix:** Evaluate reducing to 1 s or making configurable. Or move to a separate polling interval.
**Files:** `main.py`
**Effort:** S

---

## 2. Code Quality

### Q1: Fix EMA no-op bug (logic.py:337–341) ✅
**Problem:** EMA smoothing of `derived_gt` is a no-op: `alpha * x + (1-alpha) * x == x`. The value is smoothed with itself. Actual smoothing only happens at the blend step (line 343), but that is not EMA.
**Fix:** Store previous smoothed value and apply EMA formula correctly.
**Files:** `inverter_control/logic.py` (lines 332–345)
**Effort:** S

### Q2: Consolidate duplicated Tasmota/Acload parsing ✅
**Problem:** `Ac/Power` regex parsing is duplicated between background poller and fallback paths for Tasmota (victron.py:415–444 vs 887–923) and Acload (446–496 vs 925–982).
**Fix:** Extract shared `_parse_power_from_tree_output()` helper.
**Files:** `inverter_control/victron.py`
**Effort:** S

### Q3: Auto-generate config dict ✅
**Problem:** `main.py` lines 357–375 — 24 constants manually mapped to a dict. Fragile: add a constant to `config.py`, forget to add to `main.py`.
**Fix:** `config.__all__` list or `config._EXPORTED_KEYS` with dict comprehension.
**Files:** `main.py`, `inverter_control/config.py`
**Effort:** S

### Q4: Fix typo `_vue_dbust_client` ✅
**Problem:** `homeassistant.py:107` — `_vue_dbust_client` (missing 's'). Consistent usage but confusing for readers.
**Fix:** Rename to `_vue_dbus_client` everywhere.
**Files:** `inverter_control/homeassistant.py`
**Effort:** S

### Q5: Dynamic DVCC_CELL_COUNT ✅
**Problem:** `main.py` line 410 — `DVCC_CELL_COUNT: 16` hardcoded. `VictronDBus` discovers actual cell counts per chain but never propagates this to the DVCC calculator.
**Fix:** Pass `len(cells)` from `get_battery_cell_data()` into DVCC.
**Files:** `main.py`, `inverter_control/dvcc.py`, `inverter_control/victron.py`
**Effort:** M

### Q6: Clean orphan files, move test_caching.py ✅
**Problem:** 12 orphan `.py,cover` files in `inverter_control/`. `test_caching.py` lives at repo root instead of `tests/`.
**Fix:** `find . -name '*.py,cover' -delete`, move `test_caching.py` → `tests/test_caching.py`.
**Files:** `test_caching.py`, `inverter_control/*.py,cover`
**Effort:** S

---

## 3. Testing

### T1: InverterController tests ✅
**Problem:** `main.py` — 950 lines, `InverterController` completely untested. This is the core orchestrator.
**Fix:** Mock victron/ha/mqtt, test `run_cycle()`, `calculate_setpoint()`, `update_state()`. Follow same pattern as `test_watchdog.py`.
**Files:** `tests/test_main.py` (new), `main.py`
**Effort:** L

### T2: VUESensorDBusClient tests ✅
**Problem:** `dbus.py` — 164 lines, completely untested.
**Fix:** Mock subprocess/dbus_fast, test discovery, `update_all`, fallback logic.
**Files:** `tests/test_dbus.py` (new), `inverter_control/dbus.py`
**Effort:** M

### T3: Isolated strategy tests ✅
**Problem:** 4 strategies (`no_feed`, `house_support`, `limit_to_ev`, `do_not_supply_charger`) not tested in isolation.
**Fix:** 2–3 tests per strategy: normal case, edge case, override behavior.
**Files:** `tests/test_logic.py`
**Effort:** S

### T4: Grid smoothing tests ✅
**Problem:** Grid smoothing with home load (v1.19.1) — no tests for `derived_gt` blending or EMA smoothing.
**Fix:** Test blend weight application, EMA convergence, fallback when `home_total` unavailable.
**Files:** `tests/test_logic.py`
**Effort:** S

### T5: Watchdog concurrency tests ✅
**Problem:** Only 4 tests for a safety-critical component. No race condition or thread safety tests.
**Fix:** Test rapid `mark_active`/`mark_stalled` calls, thread safety, `_pre_forced_*` state restoration.
**Files:** `tests/test_watchdog.py`
**Effort:** S

### T6: Coverage target > 80% ✅
**Problem:** Current coverage unknown (`.coverage` file exists but no report).
**Fix:** `pytest --cov=inverter_control --cov-report=term-missing`, establish baseline, add missing lines.
**Files:** `pyproject.toml` (pytest config)
**Effort:** M

---

## 4. Security

### S1: MQTT input validation ✅
**Problem:** MQTT command callbacks in `main.py` — `int(p.get("value", 0))` without validation. Non-numeric payload throws.
**Fix:** Validate type and range before conversion. Log rejected commands.
**Files:** `main.py` (MQTT callbacks)
**Effort:** S

### S2: Fix .gitignore pattern ✅
**Problem:** `C*E.md` overly broad — could match `CODE_OF_CONDUCT.md` (already tracked).
**Fix:** Replace with explicit `CLAUDE.md` and `SECURITY.md` entries.
**Files:** `.gitignore`
**Effort:** S

---

## 5. Architecture

### A1: Split main.py
**Problem:** 950 lines — controller, watchdog, CLI, signal handling, MQTT setup, console server in one file.
**Fix:**
- `inverter_control/controller.py` — `InverterController` class
- `inverter_control/watchdog.py` — `HardwareWatchdog` class
- `main.py` — CLI entry point + signal handling + loop only
**Files:** `main.py` → 3 files
**Effort:** L

### A2: Split victron.py
**Problem:** 1536 lines — polling, caching, parsing, SOC calculation, cell data, tree queries in one file.
**Fix:**
- `inverter_control/victron_dbus.py` — `VictronDBus` class (polling + caching)
- `inverter_control/victron_parse.py` — tree output parsers, SOC calculation
- `inverter_control/victron_cell.py` — cell voltage/temperature logic
**Files:** `inverter_control/victron.py` → 3 files
**Effort:** L

---

## Effort Summary

| Priority | Tasks | Total Effort |
|----------|-------|-------------|
| Performance (P1–P6) | 6 | 2M + 4S |
| Code Quality (Q1–Q6) | 6 | 1M + 5S |
| Testing (T1–T6) | 6 | 1L + 1M + 4S |
| Security (S1–S2) | 2 | 2S |
| Architecture (A1–A2) | 2 | 2L |

**Recommended order:** P1 → Q1 → T1 → P4/P5/S1 (quick wins) → P2/P3 → Q2–Q6 → T2–T6 → A1 → A2
