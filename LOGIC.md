# Inverter Control Logic

## Overview

This document describes the control logic used to calculate the power setpoint sent to the Victron inverter system. The goal is to minimize grid import/export while respecting various operating modes.

## Setpoint Convention

For Victron in **External Control** mode:
- **Positive setpoint** = Consume power from grid (charge battery)
- **Negative setpoint** = Output power to house (discharge battery)

Example:
- `setpoint = -1000` → Inverter outputs 1000W to house
- `setpoint = +500` → Inverter consumes 500W from grid to charge battery

## Control Loop

The main control loop runs every ~0.33 seconds and performs these steps:

### Step 1: Gather Input Data

| Variable | Source | Description |
|----------|--------|-------------|
| `g1`, `g2` | D-Bus | Grid power per phase (W) |
| `gt` | D-Bus | Total grid power (W), positive = importing |
| `t1`, `t2`, `tt` | D-Bus | Consumption per phase and total (W) |
| `inv_power` | D-Bus | Current inverter AC output (W) |
| `mppt_total` | D-Bus | Solar from MPPT controllers (W) |
| `pv_inverter_total` | D-Bus | Solar from Tasmota microinverters (W) |
| `ev_power` | Home Assistant | EV charger consumption (W) |

### Step 2: Get Control Switches

All switches are in-process flags (`InverterController.get_boolean`).
On start they are all False — not restored from Settings or Home Assistant.
After start they change only via MQTT (`inverter/cmd/toggle` from Inverter Desktop
or HA as another MQTT client). Settings `/Settings/InverterControl/<Flag>` (0/1)
are registered (default 0) and written when a flag changes so Venus UI stays in
sync. Published on `inverter/state` as status (`booleans`).

| Switch | Purpose |
|--------|---------|
| `only_charging` | Don't discharge battery, use only solar |
| `do_not_supply_charger` | Don't power EV from battery |
| `no_feed` | Match Tasmota output to prevent grid export |
| `house_support` | Tasmota minus 300W for partial self-consumption |
| `charge_battery` | Force battery charging |

### Step 3: Calculate Effective Grid

When `do_not_supply_charger` is enabled:
```
effective_gt = gt - ev_power
```

This makes the algorithm "blind" to EV consumption, preventing it from discharging battery to power the EV.

### Step 4: Base Calculation

Target: Grid power ≈ 0 (grid-zero)

```
vanew = inv_power - effective_gt
```

Logic:
- If importing 500W from grid → increase output by 500W
- If exporting 200W to grid → decrease output by 200W

Stability zone: If `-30 < effective_gt < 50`, keep previous setpoint to prevent oscillation.

### Step 5: Apply Operating Modes

Modes are applied in priority order (lowest to highest):

#### 1. ONLY_CHARGING (Lowest Priority)
**Goal:** Don't discharge battery - output only what MPPT produces

```
output = mppt_total - SOLAR_OUTPUT_OFFSET
vanew = -max(0, output)
```

Flag: `[OC:XXX-60]`

#### 2. DO_NOT_SUPPLY_CHARGER
**Goal:** Don't let battery power the EV charger

```
max_output = max(0, mppt_total - SOLAR_OUTPUT_OFFSET)
if vanew < -max_output:
    vanew = -max_output
```

Flag: `[NoEV]`

#### 3. NO_FEED
**Goal:** Match Tasmota output to prevent grid export

```
vanew = pv_inverter_total
```

Positive setpoint consumes from grid what Tasmota exports.

Flag: `[NF]`

#### 4. HOUSE_SUPPORT
**Goal:** Partial self-consumption with Tasmota

```
vanew = pv_inverter_total - 300
```

Flag: `[HS]`

#### 5. CHARGE_BATTERY (Highest Priority)
**Goal:** Force maximum battery charging

```
vanew = 2200
```

Flag: `[CHG]`

### Step 6: Apply Safety Limits

```
vanew = max(POWER_LIMIT_MIN, min(POWER_LIMIT_MAX, vanew))
```

Default limits:
- `POWER_LIMIT_MIN = -2300` (max output)
- `POWER_LIMIT_MAX = +2250` (max charging)

## Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SOLAR_OUTPUT_OFFSET` | 60W | Reduce output by this amount to avoid grid export |
| `LOOP_INTERVAL` | 0.33s | Control loop interval |
| `POWER_LIMIT_MIN` | -2300W | Maximum output (discharge) |
| `POWER_LIMIT_MAX` | +2250W | Maximum charging |

## Console Output Flags

| Flag | Meaning |
|------|---------|
| `[~]` | Grid near zero, keeping stable |
| `[EV:XXX]` | EV power subtracted from grid calculation |
| `[OC:XXX-60]` | Only charging mode, MPPT minus offset |
| `[NoEV]` | EV exclusion limit applied |
| `[NF]` | No feed mode active |
| `[HS]` | House support mode active |
| `[CHG]` | Charge battery mode active |

## Error Handling

- Home Assistant disconnection: Uses last known values (cached)
- D-Bus errors: Automatic service rescan after consecutive failures
- Watchdog: Restarts service if web server becomes unresponsive
