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

On the next control cycle after invalidation, the controller pauses normal setpoint writes, preserves pending manual requests and stops renewing both valid-telemetry and accepted-setpoint watchdog heartbeats. It discards stale grid-filter, derivative and legacy derived-grid EMA history. A second inexpensive validity check immediately before writing catches invalidation during calculation. A D-Bus write already in flight cannot be recalled.

### Optional short hold before zero

For an installation with a pinned external meter, configure a short delay in `local_config.py`:

```python
GRID_LOSS_HOLD_SECONDS = 3.0
```

`None` (the default) preserves the legacy watchdog policy below. A finite number from 0 to 30 enables the short hold; `0` requests zero immediately. The delay starts at the first detected invalid observation and repeated invalid reads or changed error reasons do not extend it. Detection includes source substitution, missing phases, an unavailable meter and expired required readings; this setting does not change the telemetry freshness budget.

During the hold, the inverter retains the last successfully accepted command. The controller does **not** repeatedly calculate corrections from a frozen meter value. Diagnostic power readings can remain visible but are not control input. If the process has not yet accepted a command, there is no known command to hold and it requests zero immediately.

When the delay expires, the shared watchdog requests **0 W** and latches normal control off. Failed or rejected writes remain pending and are retried, even if the meter returns before the retry. An accepted zero is retained while the source is wrong or readings remain invalid. A zero inverter setpoint does not mean zero utility-meter power: house consumption and PV can still import or export during the outage.

Both control validity gates check the deadline, normally once per 0.33-second cycle. The background watchdog also checks it, so a stalled control loop is still covered. A deadline can be exceeded by scheduling and bounded D-Bus write latency; background-only enforcement can add up to `WATCHDOG_CHECK_INTERVAL`, normally five seconds. The separate stalled-loop failsafe can act sooner and is never postponed by the hold. If it has already triggered when meter loss is observed, its pending or accepted zero is adopted immediately and recovery will not replay the older command.

A valid return before the deadline cancels the hold and normal calculation resumes with reset measurement history. Once zero has been requested, recovery requires an accepted zero and two consecutive valid watchdog checks (normally about 5–10 seconds). An explicit invalidation resets that recovery qualification. Recovery leaves the command at zero, then the next control cycle calculates from fresh readings or applies a pending manual request. It never restores the command from before the outage. Safety writes and recovery transitions share a lock to prevent the background watchdog from racing this policy.

Dry-run mode writes no hardware commands. This mechanism does not change ESS modes, scheduling, battery policy, control coefficients or power limits.

### Legacy policy when the option is disabled

With `GRID_LOSS_HOLD_SECONDS = None`, both control heartbeats must be older than `WATCHDOG_TIMEOUT_SECONDS`, normally 30 seconds. Three consecutive failed checks trigger the existing 0 W setpoint, normally 40–45 seconds after the last valid heartbeat. Failed fallback writes are retried on subsequent checks. Recovery requires two consecutive checks with valid telemetry and a successful restoration of the saved setpoint before normal control resumes.

If telemetry only stops revalidating without explicit invalidation, its 40-second freshness budget precedes either outage policy. Set an explicit expected meter and phase count to protect startup during a meter outage.

## Diagnostics and verification

The state API reports `grid_control_valid`, `grid_control_reason`, `grid_loss_state`, `grid_loss_hold_seconds`, `grid_loss_elapsed` and `grid_loss_remaining`. Outage states distinguish `holding`, `zero_pending`, `zero` and `recovering`; `disabled` means legacy policy and `normal` means valid control input. `grid_loss_zero_applied` records an accepted outage zero until the next accepted normal command. These fields refresh even while normal control is paused. Logs report transitions into an unavailable measurement state and back to normal control. Diagnostic power values may retain the last reading during an outage; the validity field determines whether they can be used for control.

Before deploying, run the local regression suite. It covers measured zero, invalid arrays, non-finite values, legitimate single-phase input, missing active phases, unsupported three-phase input, external meter metadata, source and owner changes, stale replies, unrelated signal traffic, unchanged-value revalidation, filter reset races, pending manual requests and deterministic watchdog outage/recovery/write-rejection sequences, configurable deadlines, cold startup, short recovery, pending zero retries, dry-run behavior and both control validity gates.

This implementation has been validated with local tests and read-only inspection of Venus D-Bus contracts. Device rollout and an observed physical meter outage are separate operational checks; they are not performed by these tests.
