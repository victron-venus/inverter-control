
  The main control cycle is in main.py in the run_cycle method. Let me analyze the flow:

  1. run_cycle() is called every LOOP_INTERVAL seconds (default ~0.33s)
  2. Each cycle does:
    - self.victron.get_system_data() - D-Bus call (fast - single call)
    - self.victron.get_mppt_data() - Multiple D-Bus calls (one per MPPT)
    - self.victron.get_tasmota_pv_power() - One D-Bus call per Tasmota device
    - self.victron.get_inverter_power() - D-Bus call
    - self.ha.get_vue_sensor() - Cached value (fast)
    - self.ha.get_boolean() - Cached value (fast)
    - self.calculator.calculate() - Pure logic (very fast)
    - self.handle_minimize_charging() - Optional delay
    - self.victron.set_grid_setpoint() - D-Bus call (setpoint)
    - self.update_state() - Multiple D-Bus calls for battery state, etc.
    - MQTT publish (if enabled)

  Key optimization opportunities:

  1. D-Bus call consolidation: Multiple separate dbus-send calls in get_mppt_data() and get_tasmota_pv_power() - could batch
  2. Cache TTLs: Some caches are 10s, 30s - could be tuned
  3. Batching: Many D-Bus calls could be combined using the same tree query pattern as get_system_data()
  4. Loop interval: Default is 0.33s - could be made configurable/adaptive
  5. Concurrent async I/O: Could use asyncio for parallel D-Bus calls
  6. Reference D-Bus calls: get_all_batteries() called in update_state() every cycle (with 10s cache)

  Let me analyze the specific bottlenecks from the code:

  1. victron.get_system_data() - single dbus-send call (fast, ~0.3s timeout)
  2. victron.get_mppt_data() - loops over MPPT services, 2 dbus-send calls per MPPT
  3. victron.get_tasmota_pv_power() - loops over TASMOTA_DBUS_SERVICES, 1 call per service
  4. victron.get_inverter_power() - 1 dbus-send call
  5. In update_state():
    - victron.get_battery_chain_socs() - 2 dbus-send calls
    - victron.get_inverter_state() - 1 dbus-send call
    - victron.get_all_batteries() (cached 10s) - multiple calls
    - victron.get_mppt_chargers() (cached 10s) - multiple calls
    - victron.get_ess_mode() (cached 5s) - 2 dbus-send calls
    - ha.get_all_booleans() - returns cached dict
    - ha.get_all_vue_sensors() - returns cached dict
  6. DVCC calculation (every 30s):
    - victron.get_battery_cell_data() - probes many cell paths

  Potential optimizations:

  1. Batch D-Bus calls: Use the same single-call pattern as get_system_data() for MPPT data and Tasmota data
  2. Reduce loop interval overhead: Current sleep at end of cycle, could use more precise timing
  3. Cache HA boolean readings: Already done in background thread
  4. Parallel D-Bus calls: Use threading/async to fetch multiple services concurrently
  5. Pre-fetch in background: Like HA does, run D-Bus reads in background thread
  6. Optimize the interval: If loop_interval is 0.33s but cycle takes ~0.1s, could potentially run faster
  7. Reduce gc.collect() frequency: Currently every 300s - probably fine

  Let me also check if there's a config.py to see the default LOOP_INTERVAL value.

  Actually, looking more carefully at the code, I see the main loop does:
  while True:
      if not controller.run_cycle():
          break
      ...
      time.sleep(controller.loop_interval)

  And run_cycle() does:
  - Signal alarm for 5s watchdog
  - D-Bus reads (multiple)
  - Calculations
  - D-Bus writes (setpoint)
  - State updates (more D-Bus reads)
  - MQTT publish
  - Heartbeat file writes
  - Sleep

  The total cycle time includes all these operations plus the sleep. If loop_interval is 0.33s and the cycle takes 0.1s, effective rate is ~2-3 Hz.

  Let me check the config for LOOP_INTERVAL.

⏺ Read(/Users/vmedvedev/victron/inverter-control/inverter_control/config.py)
Read 483 lines

∴ Now I have a comprehensive understanding of the codebase. Let me analyze the main control loop and identify optimization opportunities.

  Key findings:
  - LOOP_INTERVAL = 0.33 seconds (3 Hz control loop)
  - The cycle does multiple D-Bus calls sequentially via subprocess.run
  - HA polling runs in a separate thread at 1.5s interval
  - DVCC calculation runs every 30s with many cell voltage queries
  - The watchdog is a separate thread checking every 5s

  Optimization opportunities:

  1. D-Bus call batching: Multiple dbus-send calls could be combined into single GetValue calls for the entire service tree (like get_system_data() does)
  2. Async/parallel D-Bus reads: The sequential subprocess calls block the loop
  3. Cache TTL tuning: Some caches at 10s, 30s, 5s could be adjusted based on actual data volatility
  4. Background D-Bus polling: Like HA does, poll D-Bus in background thread and keep cached values
  5. Loop timing precision: The time.sleep() at end of loop could cause drift; use time.monotonic() for precise timing
  6. Remove redundant reads: Some values are read multiple times per cycle
  7. Eliminate sys_data.get() fallbacks: The get_battery_soc_local already takes sys_data to avoid extra reads
  8. Optimize MPPT/Tasmota reads: These could use the same single-call pattern as get_system_data()
  9. Batch battery cell reads: Currently probes ~32 cell paths sequentially every 30s
  10. Async MQTT publish: Currently blocks the loop
  11. Reduce lock contention: Multiple locks in hot paths
  12. **Use `dbus-sEngine
