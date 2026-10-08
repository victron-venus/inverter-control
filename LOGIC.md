# Inverter Control Logic

## Overview

This document summarizes the automatic setpoint calculator in
[`inverter_control/logic.py`](inverter_control/logic.py) and its integration in
[`controller.py`](inverter_control/controller.py). It targets low grid
import/export for the supported single-phase L1 and split-phase L1/L2 layouts.
It is not a guarantee of zero grid flow or a replacement for inverter/BMS and
installation protection. Three-phase control is not supported.

## Setpoint Convention

For Victron **External Control** mode, the daemon writes
`/Hub4/L1/AcPowerSetpoint`:

- A positive value requests import at the controlled AC input.
- A negative value requests export at that input.

For example, `-1000` requests 1000 W export and `+500` requests 500 W import.
These are requested setpoints, not measurements or guarantees of battery power.
House loads, solar production, inverter limits, and other phases determine the
actual flows. Confirm the ESS mode and observed measurements on the installation.

## Control Loop

The configured loop interval is 0.33 seconds. Execution and I/O can take longer;
this is not a hard real-time deadline.

### Step 1: Gather and validate input data

| Variable | Source | Description |
|----------|--------|-------------|
| `g1`, `g2`, `gt` | D-Bus grid snapshot | Phase and total power; positive total means import |
| `t1`, `t2`, `tt` | D-Bus | Phase and total consumption |
| `inv_power` | VE.Bus `/Devices/0/Ac/Inverter/P` | Cached inverter power used by the base calculation |
| `mppt_total` | D-Bus MPPT services | Sum of reported MPPT power |
| `pv_inverter_total` | D-Bus PV-inverter services | Sum of reported PV-inverter power |
| `ev_power` | D-Bus wallbox adapter | EV charging power from the auxiliary reader |

Normal calculation requires a valid selected grid snapshot. A measured zero is
valid; missing, stale, non-finite, or wrong-source grid values are not replaced
with zero. The controller rechecks source/sample identity and validity before
writing. See [grid telemetry safety](docs/grid-telemetry-safety.md) for source
pinning, optional backup selection, expiry, and recovery behavior.

Auxiliary inputs have their own limitations. An unavailable wallbox value is
represented as zero for the calculator, so EV exclusion cannot be relied upon
without a working measurement. HA/Vue caches do not provide a universal
per-sensor freshness guarantee; a cached value alone is not evidence of health.

### Step 2: Get control switches

Inverter switches are in-process flags. They start **off on each daemon
restart** and are not restored from Settings or Home Assistant. MQTT commands
change flags; Settings are a display mirror. `inverter/state` publishes flags
as `booleans` and button definitions as `ui_config.header_toggles`. The canonical
keys and labels live in `inverter_control/control_flags.py`.

The calculator uses `only_charging`, `do_not_supply_charger`,
`set_limit_to_ev_charger`, `no_feed`, `house_support`, and `charge_battery`.
`minimize_charging` separately controls configured HA dump loads; toggling it
does not itself establish that HA or its measurements are available. See the
[MQTT control contract](docs/mqtt-control-flags.md).

### Step 3: Calculate effective grid and filtering

With `do_not_supply_charger` enabled and measured EV power greater than 100 W:

```text
effective_gt = gt - ev_power
```

Otherwise the EV subtraction is zero. This excludes the measured EV component
from the grid-error target; it does not identify the physical source of energy
at the charger.

The default background grid filter uses a time-based EMA with a 2-second time
constant. The calculator consumes that filtered snapshot without filtering it
a second time. If the background filter is disabled, the legacy per-cycle EMA
uses `EMA_ALPHA` (default 0.3).

Optional home-load smoothing is **disabled by default**. When enabled and an
eligible home-total value is present, `home_total - mppt_total -
pv_inverter_total` is filtered and blended with the grid reading (default home
weight 0.7), with the same EV exclusion applied. This estimate depends on sensor
coverage, battery flows, and source timing; validate it for the site. It is not
used while the backup grid source is selected.

### Step 4: Base calculation

Outside the deadband, the normal strategy's simplified equation is:

```text
raw_setpoint = inv_power - filtered_effective_gt * damping
```

Default damping is 0.7 for import and 1.0 for export. Inside the strict deadband
`-50 < filtered_effective_gt < 30`, the strategy starts from the previous
accepted setpoint. At exactly zero it clears creep and holds. Otherwise it
accumulates a bounded creep correction (`CREEP_RATE=0.5`, `CREEP_MAX=100` by
default; export accumulation is twice as fast). Set `CREEP_RATE=0` to disable
that accumulation.

Burst correction reacts to a sufficiently large difference between the
instantaneous and filtered effective grid values. A derivative term can apply
near zero; it is suppressed during a filtered deadband hold. These corrections
run before the higher-priority operating modes below.

### Step 5: Apply operating modes

Strategies run in this order, from lower to higher priority. Later strategies
may replace the earlier result; the resulting value still passes through
convergence and automatic limits. Multiple flags do not create simultaneous
physical guarantees.

1. **Only charging** limits export to an estimate of available MPPT output:
   `min_setpoint = -max(0, int(mppt_total * 0.94) - 60)` with default efficiency
   and offset. If the current result is more negative, it is raised to that
   floor; the strategy does not otherwise force the result to the floor.
2. **Do not supply charger** applies that same MPPT-based export floor when the
   flag is enabled and measured EV power is greater than 100 W.
3. **Limit to EV charger** (`set_limit_to_ev_charger`) replaces the result with
   `-max(0, int(mppt_total * 0.94) - 500)` when either garage or EV power exceeds
   1000 W. Garage power comes from the Vue cache and may be stale.
4. **No feed** sets the raw result to `int(pv_inverter_total)`.
5. **House support** sets it to `int(pv_inverter_total - 300)`.
6. **Charge battery** sets it to `2200`. A valid queued pre-charge request can
   select this strategy for a cycle, subject to its expiry and tariff-window
   checks; an explicit manual charge flag is a separate operator choice.

The names express policy intent. For example, `no_feed` is a setpoint rule
based on measured PV, not a certified anti-export system.

### Step 6: Apply convergence and automatic limits

The calculator normally applies 90% of the difference between its raw result
and the previous accepted setpoint (integer truncation). A burst uses 100%.
Then it clamps the result to `POWER_LIMIT_MIN` / `POWER_LIMIT_MAX`, limits the
per-cycle change to `SETPOINT_DELTA_LIMIT`, and clamps again. The final clamp
allows a newly tightened absolute bound to override the slew limit.

Defaults are `POWER_LIMIT_MIN=-2300 W`, `POWER_LIMIT_MAX=2250 W`, and
`SETPOINT_DELTA_LIMIT=2000 W`. The controller rechecks the current absolute bounds
under the hardware-write lock before an automatic write. Its accepted baseline
changes only after the write is acknowledged.

Optional [submeter trim](docs/submeter-trim.md) can add a small correction after
automatic control settles; it is disabled by default and has additional
source, timing, mode, and bound checks.

**Explicit persistent setpoint override is a separate operator interface.** It
has priority over automatic regulation, works independently of DRY mode, and
accepts an int32 value rather than the automatic power range. It must only be
available to a trusted operator who understands the equipment limits. Stopping
an override resumes the applicable automatic/outage policy; it does not
unconditionally send zero. DRY suppresses ordinary automatic inverter setpoint
writes; it does not disable every ESS/HA actuator or prevent an authorized MQTT
client from changing DRY. See the [dry-run boundary](docs/security-design.md#dry-run-is-not-a-hardware-isolation-boundary).

## Configuration and diagnostics

The actual defaults and permitted configuration values are in
[`config.py`](inverter_control/config.py). The formulas above use those defaults;
site overrides may change them.

Console markers include `[~...]` for deadband/creep, `[EV:...]` for exclusion,
`[OC:...]` or `[OC~]` for only-charging, `[NoEV:...]`, `[LimEV:...]`, `[NF]`,
`[HS]`, `[CHG]`, `[B:...]` for burst, `[D:...]` for derivative correction,
`[!Δ...]` for slew limiting, and `[TRIM:...]` for optional trim. A marker reports
calculator behavior, not proof of the resulting physical power flow.

## Error handling and watchdogs

HA failures trigger bounded requests and a circuit breaker. Cached values can
remain visible and usable by optional logic; the cache has no general
per-entity age cutoff. Keep optional home-load blending disabled unless its
inputs and outage behavior have been validated for the site.

D-Bus reconnection, service discovery, and explicit grid-validity checks govern
normal-control recovery. They cannot establish the truth of measurements
published by a compromised local service.

The in-process `HardwareWatchdog` monitors accepted-setpoint and valid-telemetry
liveness. A generic stalled-control fallback requests 0 W. With an explicit
`GRID_LOSS_HOLD_SECONDS` setting, detected meter loss holds the last accepted
command briefly, then maintains **-10 W** at the controlled AC input and retries
failed writes. The full [outage policy](docs/grid-telemetry-safety.md) describes
recovery and the default legacy timing. A write already sent to the device
cannot be recalled by a later validity check.

The separate [`service/watchdog/run`](service/watchdog/run) observes process
heartbeat files and requests service restarts with backoff. It does not monitor
an HTTP dashboard, and a fresh process heartbeat alone does not prove successful
hardware control. Neither watchdog replaces electrical or BMS protection.
