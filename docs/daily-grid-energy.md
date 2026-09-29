# Physical grid import and export energy

The controller publishes `daily_stats.grid_energy` for the selected physical
`com.victronenergy.grid.*` meter. It uses the total cumulative D-Bus counters
`/Ac/Energy/Forward` (import) and `/Ac/Energy/Reverse` (export), in kWh, from the
existing full meter snapshot. Phase counters, instantaneous power, Home
Assistant and tariff prices do not contribute to these values. The legacy
`daily_stats.grid_kwh` remains `null`.

A VM-3P75CT exposes both total counters. It does not expose an
on-device daily total or historical midnight reading through its D-Bus tree.
Starting observation in the middle of a day therefore cannot reconstruct the
whole day's consumption. Previously captured daytime readings and VRM logger
upload queues are not substitutes for a verified midnight baseline.

## Published contract

`grid_energy` is an object with these fields:

- `date`: full local ISO date (`YYYY-MM-DD`), or `null` before a valid baseline.
- `time_zone`: the Cerbo `/Settings/System/TimeZone` IANA zone, or `null`.
- `import_kwh`, `export_kwh`: separate nonnegative measured deltas in kWh;
  `null` when unknown, stale or reset. A measured zero remains zero.
- `observed_at`: Unix seconds, including fractional seconds, at completion of
  the latest accepted meter read. This is not the last time a counter changed.
- `started_at`: Unix seconds for the baseline. A partial interval starts at
  its first valid read; a complete interval starts at local midnight.
- `complete`: whether the baseline covers the whole named local day.
- `source`: `{service, device_instance, serial}`; serial may be `null` for
  meters that do not supply it. The system-selected device instance must
  match the meter's own instance. Duplicate references to the same physical
  meter are allowed; ambiguous selection is unavailable.
- `status`: `complete`, `partial`, `stale`, `unknown` or `reset`.
- `reason`: a diagnostic such as `midnight_verified`, `incomplete_day`,
  `meter_unavailable`, `stale_reading`, `counter_reset`, `source_changed`,
  `timezone_changed`, `clock_changed` or `persistence_unavailable`.

Clients should label `complete` as **Today** and `partial` as **Since HH:mm**,
using `started_at` in `time_zone`, with an explanation of the measured period.
Clients must hide numbers for all other statuses and independently enforce the
90-second freshness limit when MQTT delivery stops. This contract measures
energy only: it does not calculate energy charges or export credits.

## Day boundaries, gaps and resets

A fresh, unchanged pair of total counter readings bracketing midnight proves
that those counters did not advance across the boundary. Only this case marks
the new day complete. Both readings must be at most 30 seconds apart, from the
same meter and timezone, with consistent wall and monotonic clocks and no
intervening read failure. If either counter advances across the boundary, its
unknown cross-boundary delta is discarded; the first post-midnight read starts
a partial interval. There is no interpolation or power-based apportionment.

The ledger uses the full local date and IANA zone, so year rollover and 23/25-hour
DST days retain their correct calendar boundaries. An observation gap or restart
across midnight starts partial coverage. A same-day gap can recover the original
baseline because cumulative counters include energy consumed while disconnected.
A restored baseline is hidden until a fresh matching meter read validates it.

A counter decrease, meter identity change, timezone change or wall-clock jump
reseeds the baseline. The first reset observation has status `reset` and no
numbers; the next valid observation exposes the new partial interval. Missing
or disconnected meters and replies acquired across a source-owner change cannot
revive old values. The meter provides no reset-generation or counter timestamp;
a reset that occurs entirely between observations and catches up above the
previous counter cannot be detected from these counters alone.

## Persistence and control isolation

The ledger is saved at:

```text
/data/setupOptions/inverter-control/grid-energy-state.json
```

This is separate from the source checkout and survives ordinary deployment,
package updates and reinstall. It contains one small baseline/latest observation
pair, date, timezone, meter identity and local generation, without credentials
or tariff configuration. Loading accepts only a regular file up to 16 KiB,
rejects symlinks, validates finite JSON numeric types, and treats malformed or
unreadable state as unknown. The file is replaced atomically with mode `0600`.

Writes happen at most once per minute on the existing display snapshot worker.
The control loop and D-Bus signal handlers perform no ledger filesystem I/O;
cache reads and new observations do not wait for a disk write. Failed writes
preserve the last complete file and report `persistence_unavailable` while
in-memory measurement continues. Recent baseline/reset changes may be lost if
the process exits before the next successful save; restored state is always
revalidated against fresh identity, date and counters before use.

Timezone subscriptions are optional: failed matches never invalidate control
health, and retries/settings reads run only on the display worker. The display
has a separate timezone cache and never clears the existing control/battery
cache. Native settings reads occur at initial seeding or recovery; without native
D-Bus, the display worker refreshes the setting at most once per minute.

The 90-second freshness budget allows for the existing 30-second reconciliation
interval and delivery delay; midnight proof separately requires a 30-second
bracket. The implementation adds no meter reads: native and CLI fallback both reuse the
existing full-tree reconciliation reply. Counter signals alone do not prove
freshness for values that remain unchanged. No control setting, charging policy,
inverter setpoint or tariff is modified by this display measurement.
