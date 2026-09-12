# Grid telemetry validity and recovery

The controller requires a complete, valid grid measurement before normal control can write a setpoint. A measured **0 W is valid**. An invalid D-Bus variant, a missing active phase, a non-finite value, an unavailable meter or a changed measurement source is not a zero reading.

This controller supports the existing single-phase L1 and split-phase L1/L2 layouts. A reported three-phase layout is rejected with a diagnostic; this change does not add three-phase control.

## Measurement contract

The system service supplies `/Ac/Grid/NumberOfPhases` and the required phase powers. Grid input descriptors under `/Ac/In/0` and `/Ac/In/1` identify the source using `Source`, `ServiceName` and `DeviceInstance`. Source values 1 and 3 identify grid or shore input. Duplicate descriptors for the same meter are valid. Their `Connected` flags describe the inverter's active input, so they do not determine external meter health.

An external `com.victronenergy.grid.*` source must itself report `/Connected = 1`. Its `/NrOfPhases`, when present, must match the supported power layout. A previously observed phase capability cannot disappear and silently become optional. Related fields are acquired together using root D-Bus snapshots. Replies that span a source invalidation are discarded.

Venus calculates the system phase count from the highest phase with an available power value. It can therefore decrease when a phase disappears. Venus can also replace an absent external meter with a native inverter measurement. The controller retains its established source name, device instance and required phases for the lifetime of the process. It waits for that contract to recover instead of accepting a smaller phase count or another source. A restart of the same well-known D-Bus service requires fresh data but does not change the pinned identity.

Battery, solar and other D-Bus signals do not refresh grid validity. Unchanged grid values remain usable when successful periodic reads revalidate them. Reconciliation uses monotonic time and normally runs every 30 seconds. Required values expire after 40 seconds without revalidation, using the existing 30-second interval plus the 10-second signal-silence budget. Wall-clock corrections do not extend or prematurely expire that budget.

## Cold-start expectations

By default, the first complete valid observation establishes the measurement contract. This preserves installations that use native inverter grid measurements. **These defaults cannot identify a missing external meter before it has been observed in the current process.** Restarting during a meter outage can therefore establish the native fallback as the initial source.

For a site that requires a particular external meter and layout, set explicit expectations in `local_config.py` before installation:

```python
GRID_EXPECTED_SERVICE = "com.victronenergy.grid.YOUR_METER"
GRID_EXPECTED_PHASES = 2
```

Use the actual well-known `ServiceName` from the system's grid input descriptor, not its temporary unique bus owner such as `:1.42`. Confirm the meter identity and physical phase configuration while readings are healthy. `GRID_EXPECTED_PHASES` supports 1 or 2; its default 0 learns the initial layout. The default empty service name learns the initial source. These options validate input identity and topology; they do not change power limits, control coefficients or ESS modes.

Changing a configured meter or reducing the site's established phase layout requires an intentional configuration/restart operation after checking the new physical setup. This follow-up does not deploy configuration automatically.

## Outage and recovery timing

On the next control cycle after invalidation, the controller pauses normal setpoint writes, preserves pending manual requests and stops renewing both the valid-telemetry and accepted-setpoint watchdog heartbeats. It also discards stale grid-filter, derivative and legacy derived-grid EMA history so recovery cannot compare a new measurement with a sample from before the outage. A second inexpensive validity check immediately before writing catches invalidation during calculation. A D-Bus write already in flight cannot be recalled.

The existing watchdog policy remains in effect:

- Both control heartbeats must be older than `WATCHDOG_TIMEOUT_SECONDS`, normally 30 seconds.
- Three consecutive failed checks trigger the existing **0 W grid setpoint**. With the default five-second check interval, this is normally 40–45 seconds after the last valid control heartbeat, depending on check alignment.
- A rejected or failed fallback write is retried on subsequent watchdog checks. It is not recorded as a successful hardware change.
- Recovery requires two consecutive watchdog checks with currently valid telemetry. A brief good sample followed by another explicit invalidation cannot re-arm the watchdog.
- The existing recovery operation must successfully restore its saved setpoint before normal control resumes. The next cycle calculates a new setpoint or applies the preserved manual request. With the default interval, recovery qualification takes roughly 5–10 seconds after valid telemetry returns, plus any write retries.

If an outage produces no explicit invalidation and only stops revalidation, the 40-second freshness budget precedes watchdog timeout qualification. The fallback timing is deliberately not shortened by this change. Operators requiring a different timing policy should assess that separately.

No ESS mode, battery limit, control coefficient or power limit is changed by this mechanism. Dry-run mode still performs no hardware writes.

## Diagnostics and verification

The state API reports `grid_control_valid` and `grid_control_reason`. Logs report transitions into an unavailable measurement state and back to normal control. Diagnostic power values may retain the last reading during an outage; the validity field determines whether they can be used for control.

Before deploying, run the local regression suite. It covers measured zero, invalid arrays, non-finite values, legitimate single-phase input, missing active phases, unsupported three-phase input, external meter metadata, source and owner changes, stale replies, unrelated signal traffic, unchanged-value revalidation, filter reset races, pending manual requests and deterministic watchdog outage/recovery/write-rejection sequences.

This implementation has been validated with local tests and read-only inspection of Venus D-Bus contracts. Device rollout and an observed physical meter outage are separate operational checks; they are not performed by these tests.
