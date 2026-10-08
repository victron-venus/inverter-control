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

### Optional short hold before the maintained -10 W fallback

For an installation with a pinned external meter, configure a short delay in `local_config.py`:

```python
GRID_LOSS_HOLD_SECONDS = 3.0
```

`None` (the default) preserves the legacy watchdog policy below. A finite number from 0 to 30 enables the short hold; `0` requests the fallback on the first check without a hold. The delay starts at the first detected invalid observation, and repeated invalid reads or changed error reasons do not extend it. Detection includes source substitution, missing phases, an unavailable meter, and expired required readings; this setting does not change the telemetry freshness budget.

During the hold, the inverter retains the last successfully accepted command. The controller does **not** repeatedly calculate corrections from a frozen meter value. Diagnostic power readings can remain visible but are not normal-control input. If the process has not yet accepted a command, there is no known command to hold and it requests the fallback immediately.

When the delay expires, the shared watchdog requests **-10 W at the controlled AC input** and pauses normal regulation. This small export request is maintained to keep external ESS control active so that solar charging can continue during meter loss. It is not a zero-power command, an actual power measurement, or a guarantee of zero utility-meter flow; loads, PV, inverter protections, and other phases still determine physical flows.

An accepted fallback is refreshed approximately every **2 seconds**. Failed or rejected writes remain pending and retry no more often than once per **1 second**, including when the meter returns before a successful retry. These intervals use monotonic time and are subject to scheduling and D-Bus call latency. A failed refresh blocks recovery until a subsequent fallback write is accepted.

Both control validity gates check the hold deadline, normally once per 0.33-second cycle. The background watchdog also checks the meter-loss policy, waking at most about 2 seconds apart while it is enabled and potentially sooner for a pending retry. This protects a stalled control loop without changing the normal heartbeat-check interval. These are scheduling targets, not hard real-time guarantees. If the generic stalled-loop policy has already forced 0 W when meter loss is observed, the meter-loss policy requires an accepted -10 W write; it does not misreport the earlier zero as that fallback or replay the older command.

A valid return before the deadline cancels an unexpired hold, and normal calculation resumes with reset measurement history. Once the fallback has been requested, recovery requires an accepted fallback with no pending refresh and **two consecutive valid watchdog heartbeat checks** (normally about 5–10 seconds with the default interval). Explicit invalidation resets this qualification. Recovery leaves the accepted -10 W baseline in place; the next control cycle calculates from fresh readings or applies an eligible pending manual request. It never restores the command from before this meter-loss fallback. Safety writes and recovery transitions share a lock.

DRY suppresses ordinary automatic inverter setpoint writes and their automatic fallback; it is not a general actuator lockout. **An explicit persistent setpoint override is separate:** it has priority over both automatic regulation and the outage policy, operates even in DRY mode, and is maintained by the watchdog. During an override, the reported outage state is `overridden`. Stopping the override resumes the applicable current policy; it does not unconditionally send zero or restore a pre-override command. Only a trusted operator should be able to issue this command. See [control logic](../LOGIC.md) and [security design](security-design.md).

Neither outage policy changes ESS modes or substitutes for BMS/electrical protection.

### Legacy policy when the option is disabled

With `GRID_LOSS_HOLD_SECONDS = None`, both control heartbeats must be older than `WATCHDOG_TIMEOUT_SECONDS`, normally 30 seconds. Three consecutive failed checks trigger the existing 0 W setpoint, normally 40–45 seconds after the last valid heartbeat. Failed fallback writes are retried on subsequent checks. Recovery requires two consecutive checks with valid telemetry and a successful restoration of the saved setpoint before normal control resumes.

If telemetry only stops revalidating without explicit invalidation, its 40-second freshness budget precedes either outage policy. Set an explicit expected meter and phase count to protect startup during a meter outage.

## Native request deadlines

A slow D-Bus method is unavailable for that request; it does not by itself disconnect the shared native connection or remove grid signal subscriptions. The existing CLI fallback can retry the affected operation. Socket failures or an actually disconnected bus still trigger reconnection and subscription replay. A late failure from an older connection cannot disconnect its replacement.

Each synchronous native request has a monotonic deadline. Expired work still queued on the event loop is discarded before dispatch, and an operation already awaiting its reply is cancelled locally. With dbus-fast 2.21.1, the client also removes that request's reply handler to prevent accumulation when an endpoint never responds. Other pending requests and signal handlers remain registered. A synchronous request made on the native loop itself is refused without scheduling work.

Cancellation cannot recall a message already handed to dbus-fast's writer or sent to a device. A timed-out `SetValue` is therefore **unconfirmed**, even if the device later applies it; only the existing explicit success reply counts as acceptance. A late reply cannot turn that earlier failure into an accepted command. The controller's existing fallback and safety writes retain their acknowledgement checks. This change does not guarantee that an already transmitted command can be withdrawn or impose ordering on requests already being processed by a remote service.

## Diagnostics and verification

The published state reports `grid_control_valid`, `grid_control_reason`, `grid_loss_state`, `grid_loss_hold_seconds`, `grid_loss_elapsed`, and `grid_loss_remaining`. Outage states distinguish `holding`, `fallback_pending`, `fallback`, and `recovering`; `disabled` means the short-hold option is off, `normal` means the optional outage policy is inactive with valid input, and `overridden` means an explicit persistent setpoint override owns the output.

`grid_loss_fallback_applied` records an accepted outage fallback until a normal accepted command or override transition clears it. `grid_loss_fallback_setpoint` reports -10; `grid_loss_refresh_pending` distinguishes a failed/unconfirmed refresh from an accepted one, and `grid_loss_fallback_write_age` gives the age in seconds of the most recent accepted fallback write. The compatibility field `grid_loss_zero_applied` remains **false**: consumers must not present -10 W as an accepted zero command. These fields refresh even while normal control is paused. Logs report transitions into unavailable measurement state and back to normal control. Diagnostic power values may retain the last reading during an outage; validity determines whether they can be used for normal control.

`grid_primary_reason` describes the current primary observation. It can become `null` while the backup remains selected during its recovery hold. The additive `grid_source_transitions` array, also included in slim MQTT state, retains the last eight primary/backup selection edges in observation order. Each entry contains `selection_generation`, `observed_at_monotonic`, `observed_at_unix`, `from_source`, `to_source`, `using_backup`, `primary_valid` and `primary_reason`. Source identities are the known configured/pinned service names (or `null` when unknown); diagnostic text is capped at 512 characters.

These records are captured under the selector's existing lock, including edges observed by the background filter or the final check before a write. A quick primary recovery therefore leaves the original failure reason available for a later state publication. Returning from backup does not necessarily mean the primary recovered: inspect that entry's `primary_valid` and `primary_reason`, since an unavailable backup also ends its selection. The selection interval includes the recovery hold and is not the duration of a physical meter outage.

History and selection generations reset when the selector/process is recreated; the oldest entry is evicted after eight newer edges. Timestamps mark software observations, not the start of a physical failure. Monotonic time supports ordering within one boot; wall time supports correlation but can be adjusted. This is bounded in-memory diagnostic history, not a persistent event journal or an MQTT delivery guarantee. It changes no selection, freshness, hysteresis, watchdog or setpoint policy.

Before deploying, run the local regression suite. It covers measured zero, invalid arrays, non-finite values, legitimate single-phase input, missing active phases, unsupported three-phase input, external meter metadata, source and owner changes, stale replies, unrelated signal traffic, unchanged-value revalidation, filter reset races, pending manual requests and deterministic watchdog outage/recovery/write-rejection sequences, configurable deadlines, cold startup, short recovery, pending fallback retries and refresh, explicit override priority, dry-run behavior, and both control validity gates.

The repository regression tests validate these software transitions. Device rollout and an observed physical meter outage are separate operational checks; they are not performed by the tests, and the documentation does not attest to an installation-specific safety test.
