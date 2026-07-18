# ADR-001: Grid-Zero Control Architecture

**Status:** Accepted
**Date:** 2024-01-15
**Deciders:** @4alvit

## Context

We need to maintain zero grid feed-in/consumption for a split-phase (120/240V) system with:
- Single MultiPlus-II inverter
- JBD BMS battery chain (multiple BMS modules)
- Multiple solar sources (MPPT + Tasmota microinverters)
- EV charger that should not be powered by battery

## Decision

Use a Python-based control loop that:
1. Subscribes to MQTT for grid meter data (from Home Assistant Shelly integration)
2. Calculates setpoint using EMA-filtered grid power + burst correction + D-term braking
3. Writes setpoint to Victron D-Bus (external control mode)
4. Operates in 3 cycles/second for responsive grid control

## Consequences

**Positive:**
- Fine-grained control over grid power
- Supports complex scenarios (EV exclusion, split-phase, multiple solar sources)
- Works with existing Victron hardware (no additional Gateway required)

**Negative:**
- Custom solution requires maintenance
- D-Bus dependency means only runs on Venus OS
- Single point of failure if Python service crashes

## Alternatives Considered

1. **Victron ESS with External Control** - Native but limited scheduling
2. **Home Assistant Energy Management** - Cloud dependency, latency issues
3. **Dedicated Hardware Controller** - Additional cost, complexity

---

# ADR-002: MQTT Bridge Architecture

**Status:** Accepted
**Date:** 2024-03-01
**Deciders:** @4alvit

## Context

Dashboard needs real-time data from Cerbo GX but WebSocket connection is unstable over WAN.

## Decision

- Use MQTT bridge from Cerbo to remote dashboard
- Dashboard subscribes to `inverter/state` and `inverter/console`
- Commands published to `inverter/cmd/*`
- For slim bandwidth: MQTT_SLIM_STATE option excludes HA mirror fields

## Consequences

- Dashboard can be hosted anywhere with MQTT access
- Reduced bandwidth vs WebSocket streaming
- Additional MQTT topic management overhead

---

# ADR-003: DVCC for JBD BMS Protection

**Status:** Accepted
**Date:** 2024-06-15
**Deciders:** @4alvit

## Context

JBD BMS doesn't communicate with Victron DVCC protocol. Cells can be damaged by over-charge/discharge if Victron doesn't know cell limits.

## Decision

Implement software DVCC in dbus-mqtt-battery:
- Parse cell voltages from BMS
- Calculate CCL (Charge Current Limit) based on max cell voltage
- Calculate DCL (Discharge Current Limit) based on min cell voltage
- Apply temperature and SoC derating
- Publish limits to Victron D-Bus `/Info/MaxChargeCurrent` etc.

## Consequences

- Battery protected before BMS emergency cutoff
- Balancers have time to work (prevents early shutdowns)
- Adds complexity to dbus-mqtt-battery

---

# ADR-004: PackageManager for Venus OS Deployment

**Status:** Accepted
**Date:** 2024-02-01
**Deciders:** @4alvit

## Context

Services need to run on Cerbo GX and auto-update when new versions are released.

## Decision

Use SetupHelper PackageManager:
- Services installed under `/data/<package>/`
- `setup` script handles installation
- PackageManager auto-downloads from GitHub on updates
- Health checks via svstat

## Consequences

- Auto-updates work out of the box
- Rollback via `cp -a /data/$PKG /data/$PKG.rollback` before install
- Dependency on internet for updates