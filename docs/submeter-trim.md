# Slow submeter zero trim

The primary meter remains responsible for fast load changes. The optional trim
removes a persistent residual seen by a separately configured whole-grid
submeter while the normal controller is holding its accepted command. It does
not assume a fixed difference between meters or treat reported unloaded AC
output as a physical load.

## Enabling

The feature is disabled by default. Site configuration must explicitly set:

```python
SUBMETER_TRIM_ENABLED = True
CREEP_RATE = 0
GRID_BACKUP_SERVICE = "com.victronenergy.acload.YOUR_WHOLE_GRID_SERVICE"
```

Use a signed aggregate grid measurement: positive import, negative export.
The selected source must provide `/LastUpdate` measurement timestamps. Backup
takeover does not need to be enabled; the selected source is monitored even
when `USE_GRID_SUBMETER_AS_BACKUP` is false.

The existing startup policy clears all seven operating flags. Do not restart
an installation with active charging, EV or other restrictions merely to
enable this feature. Establish a suitable ordinary operating window first.
Enabling the feature is a field trial, not a promise of a particular error.

## Control contract

- Only normal primary-meter operation is eligible. Every special operating
  flag, manual command/override, watchdog recovery, backup takeover, nonexternal
  ESS mode, legacy creep or derived-grid blending inhibits trim.
  Enabling home smoothing without a configured or discovered Vue `total`
  source does not activate blending and does not inhibit trim. Once that
  source is present in the cache, even at zero or unknown power, trim is
  conservatively blocked; source presence is rechecked before every trim write.
- The ordinary calculator must already have selected a true hold, inside its
  grid deadband, without burst or derivative correction. Trim is applied after
  that decision so it cannot reactivate the raw derivative or disappear in
  the calculator's integer convergence.
- Each distinct source timestamp contributes once. Repeated, reordered,
  inconsistent, stale, future or foreign-source reports cannot drive a step.
  Primary and submeter service generations separate measurement histories.
- A command must settle for at least 10 seconds. Reports measured earlier than
  6 seconds after the command change are excluded. Five reports spanning at
  least 10 seconds are needed; the window is limited to 20 seconds. Source age
  is limited to 8 seconds. Submeter spread above 40 W, filtered-grid spread
  above 30 W, or a raw-to-filtered deviation above 80 W rejects a window.
- Outside a ±5 W mean-error band, the correction is one quarter of the error,
  limited to 4 W per accepted step. A new settled window is needed after each
  step. There is no per-cycle growing accumulator. Net trim is limited to 60 W
  per accepted fast-control/source baseline, and outages or rejected writes
  do not replenish that allowance.
- Power limits and the existing per-cycle delta limit still apply. A step
  which would reach a power limit is rejected instead of clipped. Fresh
  source, mode and age checks run again under the hardware-write lock.
  A step predicted to cross the primary controller's hold boundary is also
  inhibited so the slow and fast controls do not deliberately fight each other.
- Only the watchdog's accepted-write callback commits a step. Rejected or
  interrupted writes do not advance the accepted command, trim budget or
  settling time. Dry-run, limit and operating-mode changes require rewarming.

These constants are conservative trial settings. With three-second reports
and delivery delay, a fresh post-adjustment window normally takes about
20 seconds; a nominal 10-second settling guard is not a 10-second step timer.

## Observability and acceptance

`inverter/state.submeter_trim` exposes enablement, inhibition reason, window
sample count/mean, pending delta, accepted command/time/delta and cumulative
trim. `[TRIM:+N]` or `[TRIM:-N]` in the console identifies a proposed fine step;
accepted telemetry and hardware readback establish that it actually applied.

Compare distinct submeter observations under similar settled load conditions.
Report absolute error, fraction within ±10 W, command movement and recovery
from real load changes. A mean near zero alone can hide alternating import
and export. Replaying recorded telemetry can test eligibility and safety, but
cannot show how the physical system would react to hypothetical commands.

Stop a trial on unexplained growing oscillation, invalid measurements or write
failures and restore the exact preceding runtime/configuration. Preserve power
limits and active operating modes. Do not repeatedly retune several control
mechanisms in the same comparison.
