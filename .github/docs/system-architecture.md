# System Architecture

## Data Flow Diagram

```mermaid
flowchart TB
    subgraph Solar["☀️ Solar Sources"]
        MPPT["MPPT Charger\n(Victron)"]
        TAS["Tasmota PV\n(2x smart plugs)"]
    end

    subgraph Battery["🔋 Battery System"]
        JBD["JBD BMS\n(4S LiFePO4)"]
        INV["MultiPlus-II\n(Inverter/Rectifier)"]
    end

    subgraph Measurement["📊 Measurements"]
        SHELLY["Shelly Pro 3EM\n(Grid Meter)"]
        VUE["Emporia Vue\n(Circuit Monitor)"]
    end

    subgraph ESP["📡 ESP32 Bridge"]
        ESP32["ESPHome\n(JBD BLE Proxy)"]
        MQTT_BR["MQTT Broker\n(Venus OS)"]
    end

    subgraph Control["⚙️ Control Loop"]
        HA["Home Assistant\n(Sensors, Logic)"]
        INV_CTRL["inverter-control\n(PID Loop)"]
    end

    subgraph Dbus["D-Bus"]
        SYS["System D-Bus"]
        VBUS["Ve.Bus D-Bus"]
    end

    subgraph Remote["🖥️ Remote Access"]
        DASH["inverter-dashboard\n(Web UI)"]
        MQTT_REM["Remote MQTT\n(User Broker)"]
    end

    %% Solar to Battery
    MPPT -->|"DC Power"| INV
    TAS -->|"Grid AC"| SYS

    %% Battery connections
    JBD -->|"BLE"| ESP32
    ESP32 -->|"MQTT:battery/*"| MQTT_BR
    MQTT_BR -->|"D-Bus"| SYS

    %% Grid measurement
    SHELLY -->|"MQTT"| HA
    HA -->|"HTTP"| SYS
    SHELLY -->|"D-Bus"| SYS

    %% Control flow
    SYS -->|"Grid Power"| HA
    HA -->|"Grid/Consumption"| INV_CTRL
    INV_CTRL -->|"ESS Mode / Setpoint"| VBUS
    INV -->|"AC Power"| SYS
    VBUS <-->|"Inverter State"| INV

    %% Circuit monitoring
    VUE -->|"Cloud/HTTP"| HA
    HA -->|"Load Data"| INV_CTRL

    %% Remote access
    MQTT_BR -->|"inverter/state"| MQTT_REM
    MQTT_REM -->|"WebSocket"| DASH
    DASH -->|"MQTT cmd"| MQTT_REM
    MQTT_REM -->|"inverter/cmd"| INV_CTRL

    %% Legend
    style JBD fill:#ff6b6b,color:#fff
    style INV_CTRL fill:#4ecdc4,color:#fff
    style SYS fill:#95e1d3,color:#000
```

## Service Dependencies

```mermaid
graph LR
    subgraph VenusOS["Venus OS Services"]
        ICM["inverter-control"]
        DMB["dbus-mqtt-battery"]
        DTP["dbus-tasmota-pv"]
        MQTT["MQTT Broker"]
        PW["PackageManager"]
    end

    subgraph External["External Services"]
        HA["Home Assistant"]
        DASH["inverter-dashboard"]
    end

    DMB -->|"DVCC Limits"| SYS
    DTP -->|"PV Power"| SYS
    SYS -->|"ESS Control"| ICM
    ICM -->|"MQTT"| MQTT
    MQTT -->|"Subscribe"| DASH
    HA -->|"Sensors"| ICM
```

## Network Topology

```mermaid
graph TB
    subgraph Local["🏠 LAN 192.168.x.x"]
        CERBO["Cerbo GX\n192.168.160.150"]
        MQTT_C["MQTT :1883"]
        SHELLY["Shelly Pro 3EM"]
        TAS1["Tasmota :120"]
        TAS2["Tasmota :121"]
        ESP["ESP32"]
    end

    subgraph Remote["☁️ Remote"]
        HA["Home Assistant\n(Self-hosted)"]
        DASH["Dashboard\n(Any HTTP host)"]
        GH["GitHub\n(Auto-update)"]
    end

    CERBO --> MQTT_C
    MQTT_C --> DASH
    SHELLY -->|"MQTT"| MQTT_C
    TAS1 --> MQTT_C
    TAS2 --> MQTT_C
    ESP -->|"MQTT:battery"| MQTT_C
    HA -->|"HTTP :8123"| CERBO
    GH -->|"Download"| CERBO
```

## Control Loop Timing

```mermaid
sequenceDiagram
    participant G as Grid Meter<br/>(Shelly)
    participant HA as Home Assistant
    participant IC as inverter-control
    participant VBUS as Ve.Bus
    participant INV as MultiPlus

    G->>HA: MQTT: grid_power = 150W
    HA->>IC: HTTP: sensors
    IC->>IC: Calculate setpoint
    Note over IC: EMA smoothing<br/>Burst correction<br/>D-term braking
    IC->>VBUS: Setpoint = 127W
    VBUS->>INV: Adjust power
    INV-->>VBUS: Grid → 0W
    VBUS-->>G: Meter reads 0W
```

## Runbook: Troubleshooting

### ⚠️ Battery Disconnected

**Symptoms:**
- Dashboard shows stale battery data
- DVCC limits not updating
- `/System/StaleData = 1` in D-Bus

**Actions:**
```bash
# Check ESP32 connection
ssh cerbo
tail -f /var/log/dbus-mqtt-battery/current | tai64nlocal

# Check MQTT subscription
mosquitto_sub -v -t "battery/#" | head -20

# Restart service
svc -t /service/dbus-mqtt-battery

# Check backup
cp -a /data/dbus-mqtt-battery.rollback /data/dbus-mqtt-battery
svc -t /service/dbus-mqtt-battery
```

### ⚠️ Grid Failure

**Symptoms:**
- ESS mode shows "Passthru"
- No grid export/import control
- Console shows `[PT]` flag

**Actions:**
```bash
# Verify grid meter
dbus -y com.victronenergy.grid.meter0

# Check ESS mode
svxadmin display

# Manual restart
svc -t /service/inverter-control

# Check D-Bus connectivity
python3 -c "
import dbus
bus = dbus.SystemBus()
obj = bus.get_object('com.victronenergy.system', '/System')
print(obj.process_names())
"
```

### ⚠️ MQTT Broker Crash

**Symptoms:**
- Dashboard shows "connecting..."
- Services report MQTT errors
- inverter/state topic not updating

**Actions:**
```bash
# Restart MQTT
svc -t /service/mqtt-broker

# Verify connection
mosquitto_sub -v -t "\$SYS/#" -C 1

# Check service logs
tail -50 /var/log/inverter-control/current | tai64nlocal
```

### ⚠️ Watchdog Triggered Restart

**Symptoms:**
- Service was restarted by watchdog
- `/tmp/inverter-control.heartbeat` timestamp old
- Alert: "ROLLBACK: service failed health check"

**Actions:**
```bash
# Check heartbeat
cat /tmp/inverter-control.heartbeat
date -r /tmp/inverter-control.heartbeat

# View service status
svstat /service/inverter-control

# Check for repeated restarts
ls -la /tmp/.watchdog_restarts/

# Disable watchdog for testing
svc -d /service/watchdog

# Re-enable after fix
svc -u /service/watchdog
```

### ⚠️ Dashboard Not Loading

**Symptoms:**
- Web UI shows blank or timeout
- WebSocket error in browser console

**Actions:**
```bash
# Check Docker container
ssh nas
docker ps | grep inverter-dashboard
docker logs inverter-dashboard

# Check MQTT connection
docker exec inverter-dashboard sh -c 'nc -zv MQTT_HOST 1883'

# Restart container
docker restart inverter-dashboard

# Check TLS certs
ls -la /volume1/docker/inverter-dashboard/config/
```

---

## Emergency Contacts

| Issue | First Action | Escalation |
|-------|-------------|------------|
| Battery BMS alarm | Check ESP32 BLE connection | Replace BMS |
| Grid meter failure | Use backup Shelly | Install VM-3P75CT |
| Inverter fault | Check VE.Bus status | Call Victron support |
| Data loss | Restore from backup | Check InfluxDB |