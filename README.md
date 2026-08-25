# Inverter Control

[![CI](https://github.com/victron-venus/inverter-control/actions/workflows/ci.yml/badge.svg)](https://github.com/victron-venus/inverter-control/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Release](https://img.shields.io/github/v/release/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/releases)
[![Downloads](https://img.shields.io/github/downloads/victron-venus/inverter-control/total)](https://github.com/victron-venus/inverter-control/releases)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![Venus OS](https://img.shields.io/badge/Venus%20OS-3.x-blue)](https://github.com/victronenergy/venus)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://github.com/victron-venus/inverter-control)
[![GitHub watchers](https://img.shields.io/github/watchers/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/watchers)
[![GitHub contributors](https://img.shields.io/github/contributors/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/graphs/contributors)
[![GitHub issues](https://img.shields.io/github/issues/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/issues)
[![GitHub closed issues](https://img.shields.io/github/issues-closed/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/issues?q=is%3Aissue+is%3Aclosed)
[![GitHub pull requests](https://img.shields.io/github/issues-pr/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/pulls)
[![GitHub last commit](https://img.shields.io/github/last-commit/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/commits/main)
[![Code size](https://img.shields.io/github/languages/code-size/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control)
[![Repo size](https://img.shields.io/github/repo-size/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control)
[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://github.com/victron-venus/inverter-control/graphs/commit-activity)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/victron-venus/inverter-control/pulls)
[![Made with Python](https://img.shields.io/badge/Made%20with-Python-1f425f.svg)](https://www.python.org/)
[![Victron Community](https://img.shields.io/badge/Victron-Community-blue)](https://community.victronenergy.com/)


---

⭐ **If this project helps you, please star it!** Stars help others discover it and motivate continued development.

---

Grid-zero feed-in controller for Victron systems with split-phase compensation.

---

## Release Channels & CI/CD

- **Stable Releases**: Tagged as `vX.Y.Z` (e.g., `v1.0.0`). Package archive (`inverter-control-*.tar.gz`) published automatically to GitHub Releases.
- **Pre-releases**: Tagged with `-rc.N` or `-beta.N`. Automatically marked as Pre-release on GitHub Releases to protect live grid-control systems.

---

## Completed Features

- ✅ **CI/CD Releases**: Pre-release detection and package archive workflows configured
- ✅ **Hardware Watchdog Failsafe**: 30-second heartbeat watchdog automatically resets Victron ESS setpoint to fallback mode (0W / pass-through) if MQTT or D-Bus telemetry stops updating (PR #63, commit 0212a4c)
- ✅ **Background D-Bus Polling** (v1.19.0): 5 Hz polling thread eliminates ~9 subprocess calls per control cycle; control loop latency 200–300 ms → 10–20 ms on Cerbo GX (RPi 3)
- ✅ **Async MQTT Publish** (v1.19.0): Non-blocking publish via background queue; control loop no longer stalls on broker latency
- ✅ **Aggressive Grid Smoothing with Home Load** (v1.19.1): Blends derived grid (Vue total load − PV production) with instantaneous CT meter at configurable weight for stable setpoints

---

> **Disclaimer**: Most grid-zero goals can be achieved using Victron's built-in **ESS Optimized (without BatteryLife)** mode. This project exists for specific edge cases requiring custom logic (split-phase compensation, EV charger exclusion, multiple solar sources, etc.). This code was developed for a particular setup and is unlikely to work as a drop-in solution — treat it as a learning resource or starting point for your own implementation.

## Documentation

- [System Architecture](.github/docs/system-architecture.md) - Data flow diagrams, runbook
- [ADR](.github/docs/adr-001-grid-zero-architecture.md) - Architecture Decision Records

### Where this project came from

This repository did not start as a polished Python package. Roughly **three years ago** it began as the smallest thing that could work: a **single shell pipeline** glued together with `mosquitto_sub`, a few arithmetic hacks, and a helper script. No repository structure, no D-Bus abstraction, no Home Assistant — just “read a number from MQTT, clamp it, hand it to the inverter.”

The original one-liner looked conceptually like this (host, topic, and credentials are redacted; `***` stands in for a password or token):

```bash
# Proof-of-concept from ~2023 — do not run as-is; values and paths were local.
mosquitto_sub -L "mqtt://mqtt:***@10.10.10.10/home/power_main" | while read -r line; do
  # va = “current” setpoint, s = “main” sensor, b = computed next setpoint
  b=$(( va - s/3 + 2 ))
  [ "$b" -gt 2000 ]  && b=2000
  [ "$b" -le -2000 ] && b=-2000
  [ "$b" -le 0 ]     && b=0
  [ "$s" -eq 0 ]     && b="${va}"
  echo -n "$(date) => current:${va} main:${s} new:${b} "
  va="${b}"
  ~/inverter.py "${va}"
done
```

What it was trying to do, in plain language:

- **Subscribe** to a Home Assistant (or broker) topic that published something like “main” grid or power telemetry (`power_main`).
- **Derive** a new inverter setpoint `b` from the difference between a remembered value `va` and the live reading `s` (the `s/3+2` term was a crude proportional tweak).
- **Clamp** the result into a safe band (±2000 W in this sketch) and avoid sending meaningless negatives in some cases.
- **Delegate** the actual Victron write to a tiny `~/inverter.py` helper — the predecessor of today’s D-Bus layer.

For the curious, the same idea in **one dense line** (again: redacted broker URL; line breaks only for readability — the spirit was “pipe MQTT into a tiny state machine, then `inverter.py`”):

```bash
mosquitto_sub -L "mqtt://mqtt:***@10.10.10.10/home/power_main" \
| while read -r _; do
    b=$((va-s/3+2)); [ $b -gt 2000 ]&&b=2000; [ $b -le -2000 ]&&b=-2000
    [ $b -le 0 ]&&b=0; [ $s -eq 0 ]&&b="${va}"
    echo -n "$(date) => current:$va main:$s new:$b "; va="${b}"; ~/inverter.py ${va}
  done
```

That pipeline was enough to prove the idea on a bench or a single meter. It was also fragile: no persistence across reboots, no split-phase awareness, no EV or laundry logic, and no story for MPPT + Tasmota + multiple battery chains. Everything you see now — structured config, `victron.py`, MQTT bridge, optional dashboard, monitoring hooks — grew out of replacing that one-liner piece by piece while keeping the same core goal: **keep the grid where we want it without sacrificing the weird parts of a real house.**

If you are browsing this repo for inspiration, that history is intentional: **start simple, measure, then automate.** The current code is the same instinct with years of production bruises folded in.

## Overview

This Python application controls a Victron inverter to maintain zero grid feed-in/consumption while supporting various operating modes. It's designed for split-phase (120/240V) systems where L2 loads need to be compensated by L1 export.

```mermaid
flowchart TD
    Solar1["Solar Panels\n(Standard)"] -->|"DC"| MPPT1["Victron MPPT\nControllers"]
    MPPT1 <-->|"DC"| Battery["Battery 48V"]
    MPPT1 -.->|"D-Bus\nTelemetry"| Script["This Script\non Cerbo GX"]
    MPPT1 <-->|"DC"| Inverter["Victron\nInverter"]

    Solar2["Solar Panels\n(Tasmota)"] -->|"DC"| MPPT2["Inline MPPT\nInverter\n(w/ Tasmota)"]
    MPPT2 -->|"AC"| Tasmota["Tasmota\nSmart Plug"]
    Tasmota -->|"AC"| Grid["AC Grid"]

    Battery <-->|"DC"| Inverter
    Inverter <-->|"AC L1"| Grid
    Grid -->|"AC L1"| Loads1["Loads L1"]
    Grid -->|"AC L2"| Loads2["Loads L2\n(no inverter)"]

    Tasmota -.->|"D-Bus\n(dbustasmota-pv)"| Script
    HA["Home Assistant"] -.->|"HTTP\npolling"| Script

    Script -->|"D-Bus\nSetpoint"| Inverter
    Script -->|"MQTT"| Dashboard["Remote\nDashboard"]
```


## Features

- **Grid-Zero Control**: Maintains net zero power at the utility meter
- **Split-Phase Compensation**: Exports on L1 to offset L2 consumption
- **Multiple Operating Modes**:
  - Normal: Automatic grid-zero targeting
  - Only Charging: Use solar only, don't discharge battery
  - No Feed: Only use Tasmota PV, no battery
  - House Support: Tasmota PV minus 300W
  - Charge Battery: Force battery charging
  - Do Not Supply Charger: EV charges from grid only
- **Minimize Charging**: Auto-control dump loads to consume excess solar
- **Home Assistant Integration**: Sensor data and switch control
- **Fast Control Loop**: 3 updates per second via D-Bus
- **Background D-Bus Polling** (v1.19.0+): 5 Hz thread caches D-Bus data; hot-path methods read instantly (< 1 ms) — eliminates subprocess overhead on Cerbo GX
- **Async MQTT Publish** (v1.19.0+): Non-blocking publish queue; control loop never stalls on broker
- **Home Load Grid Smoothing** (v1.19.1+): Blends derived grid (Home total load − PV production) from HA/Vue with instantaneous CT meter at configurable weight (default 0.7) for stable setpoints despite CT jitter

## Architecture

```
inverter-control/              # Git repo root
├── main.py                    # Entry point — control loop, CLI, MQTT setup
├── inverter_control/          # Python package
│   ├── __init__.py
│   ├── config.py              # Non-sensitive parameters (tuning, limits, flags)
│   ├── local_config.py        # Sensitive config — NOT in git (see local_config.example.py)
│   ├── logic.py               # SetpointCalculator, strategies, EMA, burst, D-term
│   ├── victron.py             # D-Bus I/O — background 5 Hz polling thread, cached reads (< 1 ms)
│   ├── homeassistant.py       # HA API polling with circuit breaker
│   ├── mqtt_bridge.py         # MQTT subscribe/publish (async queue, non-blocking)
│   ├── console_ui.py          # Terminal dashboard renderer
│   ├── console_server.py      # TCP server (port 9999) for remote console
│   ├── keepalive.py           # Setpoint keepalive during restart
│   ├── ui_config.py           # Dashboard layout configuration
│   └── log-forwarder.py       # Forwards daemontools logs to syslog
├── setup                      # SetupHelper-compatible installer (run by PackageManager)
├── gitHubInfo                 # GitHub user:branch for PackageManager auto-download
├── version                    # Current version (read by PackageManager)
├── deploy.sh                  # SSH deploy to Cerbo/Pi (dev workflow)
├── update.sh                  # Deploy/update via deploy.sh (stream + run on device)
├── local_config.example.py    # Template for local_config.py
├── tests/
│   └── test_logic.py          # Unit tests for control logic
├── service/
│   ├── inverter-control/      # daemontools service runner
│   ├── log-forwarder/         # Log forwarder service
│   └── watchdog/              # External process watchdog
├── LOGIC.md                   # Control logic documentation (EN)
├── LOGIC_RUS.md               # Control logic documentation (RU)
└── release.sh                 # Tag, push, create GitHub release
```

## Configuration

1. Copy `local_config.example.py` to `local_config.py`
2. Edit `local_config.py` with your actual values:

```python
# Home Assistant connection
HA_URL = "http://YOUR_HA_IP:8123"
HA_TOKEN = "your_long_lived_access_token"

# Victron Portal ID (VRM) is auto-detected at runtime
# (/sbin/get-unique-id, fallback: eth0 MAC) — nothing to configure

# Time-of-use expensive grid window (local hours; -1/-1 disables).
# Solar-forecast pre-charge is suppressed inside [START, END).
# On Cerbo these are also created as GUI-editable Settings entries under
# /Settings/InverterControl — edit them from the Venus Settings menu (no SSH
# needed); those values take precedence over local_config.
TOU_EXPENSIVE_START_HOUR = 15  # 3 PM
TOU_EXPENSIVE_END_HOUR = 24  # midnight

# HA Sensors, VUE sensors, booleans, etc.
# See local_config.example.py for full template
```

3. Edit `config.py` for non-sensitive parameters:

```python
# Power limits (protect outlet from overheating)
POWER_LIMIT_MAX = 2250  # Max feed-in (W)
POWER_LIMIT_MIN = -2300  # Max export (W)

# Control loop timing
LOOP_INTERVAL = 0.33  # 3 times per second
```

## Optional Features

Features can be enabled/disabled in `config.py`. They auto-disable if `HA_TOKEN` is not configured:

```python
ENABLE_EV = True  # EV charging monitoring (car SoC, charger power)
ENABLE_WATER = True  # Water level, pump and valve control
ENABLE_HA_LOADS = True  # Home Assistant loads monitoring (Vue sensors)
ENABLE_HA = True  # Home Assistant integration entirely
```

When disabled:
- Console output omits the corresponding sections
- No HA API calls are made for disabled features

This allows running the inverter control standalone without Home Assistant.

## Installation

### Option 1: SetupHelper / PackageManager (Recommended)

The easiest way to install is via [SetupHelper](https://github.com/kwindrem/SetupHelper) PackageManager. The `setup` script in this repo is PackageManager-compatible and handles service creation, file placement, and restarts.

1. **Install SetupHelper** (if not already installed):
   ```bash
   wget -qO - https://github.com/kwindrem/SetupHelper/archive/latest.tar.gz | tar -xzf - -C /data
   mv /data/SetupHelper-latest /data/SetupHelper
   /data/SetupHelper/setup
   ```

2. **Add package via GUI**:
   - Settings → PackageManager → Inactive packages → **new**
   - Package name: `inverter-control`
   - GitHub user: `victron-venus`
   - Branch: `main`
   - Proceed → Download → Install

3. **Configure secrets** (from your local machine):
   ```bash
   cp local_config.example.py local_config.py
   # Edit local_config.py with your TOU hours, HA token, sensor names, etc.
   scp local_config.py root@cerbo:/data/setupOptions/inverter-control/
   ```
   Config in `/data/setupOptions/` survives package reinstalls; a copy directly in `/data/inverter-control/` also works (kept on update once installed).

4. **Done!** PackageManager will auto-download updates from `main` and reinstall on Venus OS updates.

#### How PackageManager Works

PackageManager discovers packages by scanning `/data/` for directories containing both a `version` file and a `setup` script. The `setup` script (sourced from this repo) is executed with the `INSTALL` action by SetupHelper, which:

- Creates `/data/inverter-control/` and copies `main.py` + `inverter_control/` package
- Installs `local_config.py` from `/data/setupOptions/inverter-control/`; if absent but the install dir already has one, keeps it; otherwise installs `local_config.example.py` as a starting point
- Installs the daemontools services (`inverter-control`, `log-forwarder`, `watchdog`): persistent run/log dirs under `/data/inverter-control/service/`, symlinked into tmpfs `/service`, plus an idempotent `/data/rc.local` block that recreates the symlinks after reboot
- Restarts the service

The `gitHubInfo` file tells PackageManager where to download from:
```
victron-venus:main
```
This means: download `https://github.com/victron-venus/inverter-control/archive/main.tar.gz`

### Option 2: Deploy Script (Development)

For development or testing, use `deploy.sh`:

```bash
./deploy.sh Cerbo    # 'Cerbo' is SSH host alias in ~/.ssh/config
```

This copies `main.py`, the `inverter_control/` package, `setup`, and `gitHubInfo` to the device, then restarts the service.

### Option 3: Manual Install (from the Cerbo)

Download and install straight on the device — `setup install auto` is fully self-sufficient (copies files, creates the daemontools services, adds the rc.local boot-persistence block):

```bash
ssh root@cerbo
cd /data && wget -qO- https://github.com/victron-venus/inverter-control/archive/main.tar.gz | tar -xzf - && mv inverter-control-main inverter-control
/data/inverter-control/setup install auto
```

Notes:
- Run interactively over SSH? Plain `/data/inverter-control/setup` prompts for the action instead of needing `install auto`.
- Config goes to `/data/setupOptions/inverter-control/local_config.py` (see step 3 above); without one, `local_config.example.py` is installed as a starting point.
- An existing installation is backed up to `/data/inverter-control.rollback` before reinstalling.
- Uninstall: `/data/inverter-control/setup uninstall`.

## Usage

### Service Management

```bash
# Check status
svstat /service/inverter-control

# Restart
svc -t /service/inverter-control

# Stop / Start
svc -d /service/inverter-control
svc -u /service/inverter-control

# View logs
tail -f /var/log/inverter-control/current | tai64nlocal
```


### One-shot Mode

```bash
# Set specific setpoint and exit
python3 main.py 1500

# Dry run (don't send commands)
python3 main.py --dry-run
```

## Operating Modes

### Normal Mode
- Targets zero grid power
- Automatically adjusts based on consumption and solar

### Only Charging (`[OC]`)
- During daytime low electricity rates
- Don't discharge battery
- Use MPPT solar only, minus offset

### No Feed (`[NF]`)
- Only use Tasmota PV inverters
- Don't discharge main battery
- Setpoint = Tasmota PV power

### House Support (`[HS]`)
- Tasmota PV minus 300W
- Supports house loads partially

### Charge Battery (`[CHG]`)
- Force setpoint to 2200W
- Maximum battery charging

### Do Not Supply Charger (`[NoEV]`)
- EV charges from grid only
- Battery doesn't supply EV charger
- Grid calculation excludes EV consumption

### Minimize Charging (`[MC]`)
- Automatically turns on/off dump loads
- Uses excess solar instead of grid export

## Console Output Format

```
HH:MM:SS[flags]>setpoint(prev) g:total(L1+L2)net  tt(L1+L2) tt:home [State]battW,soc%,b1%,b2% solar loads water car
```

Example:
```
14:23:45[OC:850-60]>-790(0) g:45(23+22)50  567(300+267) tt:580 [External control]-150W,85%,82%,83% 890(120+130+640) 45f 150l 42cm 78%
```

Flags:
- `[~]` - Grid near zero, keeping stable
- `[EV:XXX]` - EV power excluded from grid calculation
- `[OC:XXX-60]` - Only charging mode (MPPT minus offset)
- `[NF]` - No feed mode
- `[HS]` - House support mode
- `[NoEV]` - EV charger exclusion limit applied
- `[CHG]` - Charge battery mode
- `[MC+/-]` - Minimize charging load changes
- `[B:+320]` - Burst correction applied (sudden spike response)
- `[D:+33]` - D-term braking (prevents overshoot when approaching zero fast)
- `[!ΔNNN]` - Software fuse triggered (delta exceeded limit)

## Grid Metering Options

For accurate grid-zero control, you need real-time power measurement at the grid entry point. Here are the options:

### Recommended: Shelly with CT Clamp

Any Shelly device with external CT (current transformer) clamp input works well:
- **Shelly Pro 3EM** - 3-phase, Ethernet + WiFi, local MQTT
- **Shelly EM** - Single phase, WiFi, local MQTT
- Low latency (~100ms), fully local, no cloud dependency

### Emporia Vue

Vue energy monitors can work but have significant limitations:

| Version | Pros | Cons |
|---------|------|------|
| **Vue 2** | Affordable, easy setup | Cloud-only by default (us-east-2 = high latency), 2.4GHz WiFi only |
| **Vue 3** | Has Ethernet port | ESPHome reflash may not work with Ethernet, falls back to WiFi |

**Vue with ESPHome**: You can reflash Vue 2/3 with ESPHome for local MQTT, eliminating cloud latency. However:
- Vue 2: No Ethernet, 2.4GHz WiFi can introduce jitter
- Vue 3: Ethernet support in ESPHome is experimental, may not work

### Victron Energy Meters

Official Victron solutions like **VM-3P75CT** (3-phase CT meter):
- **Pros**: Native D-Bus integration, no additional software needed
- **Cons**:
  - Expensive (~$300+)
  - Requires Ethernet cable to electrical panel (often in garage)
  - Reports instantaneous values which can make control loop less stable than averaged readings

### dbus-esphome-grid-sensor

ESP32-based CT sensor with D-Bus service that registers as a standard `com.victronenergy.grid` device in Venus OS.
- **Pros**:
  - ~$25 DIY cost
  - Native D-Bus integration (no additional software needed)
  - Open source, calibratable with known load
  - ESPHome firmware (OTA updates, WiFi fallback, MQTT discovery)
  - Docker deployment available for Venus OS
- **Cons**:
  - ±2-5% accuracy (suitable for grid-zero control)
  - Requires ESP32 + CT sensor hardware assembly
  - Single phase only (L1) - for split-phase you need two sensors or a 2CT version

### Practical Recommendation

For most setups, **Shelly with CT clamp** offers the best balance:
1. Local MQTT with sub-100ms latency
2. Ethernet option (Pro models) for reliability
3. Affordable (~$50-80)
4. Easy integration with this controller

If already using Vue with cloud, it still works but expect:
- 500-2000ms latency from us-east-2 cloud
- Occasional missed readings
- Less responsive grid-zero tracking

## Grid Smoothing with Home Load (v1.19.1+)

When `ENABLE_GRID_SMOOTHING_WITH_HOME = True` (in `config.py`), the controller blends a derived grid estimate with the instantaneous CT meter for dramatically more stable setpoints.

### How it works

```
pv_total = MPPT DC power + Tasmota AC power
derived_gt = home_total (from Vue via HA cloud) - pv_total
# derived_gt: positive = import, negative = export

effective_gt = GRID_SMOOTHING_HOME_WEIGHT * derived_gt
             + (1 - GRID_SMOOTHING_HOME_WEIGHT) * instantaneous_gt
```

The derived grid is also EMA-smoothed with `GRID_SMOOTHING_DERIVED_ALPHA` (default 0.1) before blending.

### Config options

| Option | Default | Description |
|--------|---------|-------------|
| `ENABLE_GRID_SMOOTHING_WITH_HOME` | `False` | Enable blending derived grid with CT meter |
| `GRID_SMOOTHING_HOME_WEIGHT` | `0.7` | Weight for derived grid (0.0–1.0, higher = more stable) |
| `GRID_SMOOTHING_DERIVED_ALPHA` | `0.1` | EMA alpha for derived grid (slower = smoother) |

### Why it helps

- **Instantaneous CT meters** (VM-3P75CT, Shelly, etc.) report noisy, jittery values that cause setpoint oscillation
- **Home total from Vue** (via HA cloud, ~1 s latency) is rock-stable — it's a billing-grade accumulator
- **Blending at 0.7 weight** gives you the stability of cloud data with the responsiveness of local CT
- Bash scripts using this approach historically produced the most economical setpoints

### Requirements

- `ENABLE_HA_LOADS = True` in `config.py`
- `HA_SENSORS` in `site_config.py` must include Vue total house consumption sensor
- `VUE_SENSORS` in `site_config.py` must map to correct HA entities

## Troubleshooting

### Service not starting

```bash
# Check service status
svstat /service/inverter-control

# View recent logs
tail -50 /var/log/inverter-control.log

# Check for import errors (common after refactor)
cd /data/inverter-control && python3 -c "from inverter_control.config import LOOP_INTERVAL; print('OK')"
```

### ImportError after package refactor (v1.18.1+)

After moving modules into `inverter_control/` subdirectory, the service crashes with `ImportError` if the package is not deployed. Symptoms: service starts, shows banner, immediately exits.

**Cause**: `deploy.sh` or PackageManager did not copy the `inverter_control/` directory.

**Fix**:
```bash
# Check if package exists
ls /data/inverter-control/inverter_control/

# If missing, redeploy
./deploy.sh Cerbo

# Or manually
scp -r inverter_control/ root@cerbo:/data/inverter-control/
ssh root@cerbo "svc -t /service/inverter-control"
```

### PackageManager not discovering the package

PackageManager's `AddStoredPackages()` requires both a `version` file AND a `setup` script in `/data/inverter-control/`.

**Check**:
```bash
ls -la /data/inverter-control/version /data/inverter-control/setup
cat /data/inverter-control/gitHubInfo   # should show: victron-venus:main
```

**Common issues**:
- `setup` file missing → PackageManager skips the directory silently
- `gitHubInfo` points to `latest` tag (which may be ancient) → should be `main`
- `DO_NOT_AUTO_ADD` flag in `/data/setupOptions/inverter-control/` → manual removal marker

**Fix**:
```bash
# Copy setup and gitHubInfo
scp setup gitHubInfo root@cerbo:/data/inverter-control/
ssh root@cerbo "chmod +x /data/inverter-control/setup"

# Restart PackageManager to re-scan
svc -t /service/PackageManager
```

**Verify**:
```bash
tail -20 /var/log/PackageManager/current | grep inverter
# Should show: adding inverter-control / checking inverter-control
```

### Dashboard shows stale data (not updating)

**Symptom**: Console dashboard shows data on startup but never refreshes.

**Cause**: Service is not running, or D-Bus polling loop crashed.

```bash
svstat /service/inverter-control        # Check if up
tail -20 /var/log/inverter-control.log  # Check for errors
```

### Home Assistant circuit breaker

After 5 consecutive HA poll failures, the circuit breaker opens for 60 seconds. This is normal — HA restarts, network blips, etc.

```bash
grep "circuit breaker" /var/log/inverter-control.log
```

If persistent: check `HA_URL` and `HA_TOKEN` in `site_config.py`.

### D-Bus errors

```bash
# Check VE.Bus service
dbus -y | grep vebus

# Check system data
dbus -y com.victronenergy.system / GetValue
```

### MQTT connection

```bash
# Check MQTT broker is running
mosquitto_sub -t '$SYS/broker/uptime' -C 1

# Check inverter-control MQTT logs
grep MQTT /var/log/inverter-control.log | tail -10
```

### Secrets import conflict (known issue)

Python 3.6+ has a built-in `secrets` module. Our `site_config.py` relies on local import priority (current directory wins). If the working directory is wrong, Python imports the stdlib `secrets` instead, and all HA/EV features silently disable.

**Symptoms**: HA features disabled, `ENABLE_HA = False` in logs.

**Workaround**: Ensure the service `cd`s to `/data/inverter-control/` before running (the `run` script handles this).

**Long-term fix**: Config was renamed to `local_config.py` (v2.x+).

### TCP Console Server (Port 9999)

The TCP console server binds to `0.0.0.0:9999` and provides **read-only** access to live inverter data via a simple telnet-style interface.

**Security Options** (choose one):

#### Option 1: Firewall (Recommended)

Block external access — only allow from trusted subnets:

```bash
# On Cerbo GX / Venus OS
iptables -A INPUT -p tcp --dport 9999 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 9999 -j DROP
```

Or via VenOS rc.local for persistence:

```bash
# Add to /data/rc.local
iptables -I INPUT -p tcp --dport 9999 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 9999 -j DROP
```

#### Option 2: SSH Tunnel (For Remote Access)

Instead of exposing port 9999, use SSH port forwarding:

```bash
# From your local machine
ssh -L 9999:localhost:9999 root@cerbo
# Then connect your client to localhost:9999
```

#### Option 3: Disable If Unused

```python
# In local_config.py
ENABLE_CONSOLE_SERVER = False  # Disable TCP console entirely
```

**Note**: The console server is read-only — it cannot send commands to the controller. No authentication is required by design (minimizing attack surface). If you need remote access, use SSH tunnel.

## Dependencies

- Python 3.x (included in Venus OS)
- requests (for HA API)
- D-Bus (for Victron communication)

## Related Projects

This project is part of the Victron Venus OS integration suite:

| Project | Description |
|---------|-------------|
| **inverter-control** (this) | Advanced ESS external control system with grid-zero targeting |
| [inverter-dashboard](https://github.com/victron-venus/inverter-dashboard) | Real-time web dashboard (Python/FastAPI) via MQTT |
| [inverter-dashboard-go](https://github.com/victron-venus/inverter-dashboard-go) | High-performance Go rewrite of the web dashboard |
| [inverter-desktop](https://github.com/victron-venus/inverter-desktop) | Native desktop application (Rust/Tauri) for system monitoring |
| [dbus-mqtt-battery](https://github.com/victron-venus/dbus-mqtt-battery) | MQTT to D-Bus bridge for JBD BMS battery integration |
| [dbus-tasmota-pv](https://github.com/victron-venus/dbus-tasmota-pv) | Tasmota smart plug integration as a PV inverter on D-Bus |
| [esphome-jbd-bms-mqtt](https://github.com/victron-venus/esphome-jbd-bms-mqtt) | ESP32 Bluetooth monitor for JBD BMS batteries |
| [inverter-monitoring](https://github.com/victron-venus/inverter-monitoring) | TIG (Telegraf, InfluxDB, Grafana) monitoring stack |
| [terraform-github-victron](https://github.com/4alvit/terraform-github-victron) | Infrastructure as Code for the GitHub organization |

## Development Workflow

### Auto-Commit Script

Use `commit.sh` for automated commit and PR creation:

```bash
# Create commit message in commit.txt
echo "Add new feature X" > commit.txt
echo "" >> commit.txt
echo "Detailed description of changes" >> commit.txt

# Run commit script
./commit.sh
```

The script will:
- Create feature branch if on main
- Commit changes
- Push branch
- Create PR with auto-merge label
- Enable auto-merge after CI checks pass

### Auto-Merge

For maintainers, PRs created with `auto-merge` label automatically merge after:
- All GitHub Actions checks pass
- Status checks: CI, Python Security Scan

Configure branch protection rules in GitHub settings to require these checks.

## Release History

### v1.19.1 (2026-08-15) — Aggressive Grid Smoothing with Home Load

- **New**: `ENABLE_GRID_SMOOTHING_WITH_HOME`, `GRID_SMOOTHING_HOME_WEIGHT`, `GRID_SMOOTHING_DERIVED_ALPHA` config options
- **Logic**: Blends derived grid (Vue home_total − PV production) with instantaneous CT meter at 0.7 weight
- **Stability**: Derived grid EMA-smoothed at 0.1 alpha; eliminates CT jitter while keeping responsiveness
- **Result**: Most economical setpoints historically; stable grid-zero tracking despite noisy CT meters

### v1.19.0 (2026-08-15) — Background D-Bus Polling + Async MQTT

- **Performance**: Background 5 Hz thread caches full D-Bus service trees; hot-path reads < 1 ms
- **Latency**: Control loop 200–300 ms → 10–20 ms on Cerbo GX (RPi 3)
- **MQTT**: Non-blocking publish queue; control loop never stalls on broker latency
- **Quality**: Cognitive complexity < 15; empty `except` → logging; regex backtracking fixed; `GET_VALUE_METHOD` constant

### v1.18.x — Stabilization & CI Migration

- Migrated CI to shared `venus-os-ci-toolkit` workflows (pinned SHAs)
- Auto-approve workflow for Dependabot PRs
- Hardware watchdog failsafe (30 s heartbeat)
- PackageManager-compatible installer

## Author

Created by [@4alvit](https://github.com/4alvit)

## License

MIT License

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature-name`)
3. Commit your changes
4. Push to the branch (`git push origin feature-name`)
5. Create a Pull Request

## Support

For issues specific to:
- **D-Bus errors**: Verify VE.Bus service and Venus OS version
- **Home Assistant**: Test token and sensor availability
- **Grid metering**: Check Shelly/Vue connection and MQTT latency
- **Operating modes**: Review mode-specific logic implementation
- **This project**: Open an issue in this repository

**Note:** This is a community project and is not affiliated with Victron Energy.
