#!/usr/bin/env python3
"""
Remote Web Dashboard for Inverter Control
Connects to Cerbo GX via MQTT, serves Vue.js dashboard via WebSocket
"""

import os
import json
import asyncio
import logging
import argparse
from typing import Set, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Configuration
MQTT_HOST = os.getenv('MQTT_HOST', '192.168.160.150')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))
WEB_PORT = int(os.getenv('WEB_PORT', '8080'))

# State
current_state: Dict[str, Any] = {}
console_lines: list = []
ws_clients: Set[WebSocket] = set()
mqtt_client: mqtt.Client = None
main_loop: asyncio.AbstractEventLoop = None


def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    """MQTT connected - subscribe to topics"""
    logger.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
    client.subscribe("inverter/state")
    client.subscribe("inverter/console")


def on_mqtt_message(client, userdata, msg):
    """MQTT message received"""
    global current_state, console_lines
    
    try:
        if msg.topic == "inverter/state":
            current_state = json.loads(msg.payload.decode())
            # Broadcast to WebSocket clients
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(broadcast_state(), main_loop)
        
        elif msg.topic == "inverter/console":
            line = msg.payload.decode()
            console_lines.append(line)
            if len(console_lines) > 50:
                console_lines.pop(0)
    except Exception as e:
        logger.error(f"MQTT message error: {e}")


async def broadcast_state():
    """Send state to all WebSocket clients"""
    if not ws_clients:
        return
    
    data = {**current_state, 'console': console_lines}
    message = json.dumps(data)
    
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    
    for ws in dead:
        ws_clients.discard(ws)


def send_command(cmd: str, payload: dict = None):
    """Send command to Cerbo via MQTT"""
    if mqtt_client and mqtt_client.is_connected():
        data = json.dumps(payload) if payload else ""
        mqtt_client.publish(f"inverter/cmd/{cmd}", data)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown"""
    global mqtt_client, main_loop
    
    # Store reference to the main event loop for use in MQTT callbacks
    main_loop = asyncio.get_running_loop()
    
    # Start MQTT client
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
        logger.info(f"Connecting to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
    except Exception as e:
        logger.error(f"MQTT connection failed: {e}")
    
    yield
    
    # Cleanup
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint"""
    await websocket.accept()
    ws_clients.add(websocket)
    logger.info(f"WebSocket client connected ({len(ws_clients)} total)")
    
    try:
        # Send current state immediately
        if current_state:
            await websocket.send_text(json.dumps({**current_state, 'console': console_lines}))
        
        while True:
            data = await websocket.receive_json()
            action = data.get('action')
            
            if action == 'toggle':
                send_command('toggle', {'entity': data.get('entity')})
            elif action == 'press':
                send_command('press', {'entity': data.get('entity')})
            elif action == 'setpoint':
                send_command('setpoint', {'value': data.get('value')})
            elif action == 'dry_run':
                send_command('dry_run')
            elif action == 'limits':
                send_command('limits', {'min': data.get('min'), 'max': data.get('max')})
            elif action == 'ess_mode':
                send_command('ess_mode')
            elif action == 'loop_interval':
                send_command('loop_interval', {'interval': data.get('interval')})
    
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)
        logger.info(f"WebSocket client disconnected ({len(ws_clients)} remaining)")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve dashboard"""
    return get_dashboard_html()


@app.get("/api/state")
async def api_state():
    """REST fallback"""
    return {**current_state, 'console': console_lines}


def get_dashboard_html() -> str:
    """Vue.js + uPlot dashboard"""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Inverter Control (Remote)</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/vue@3.4.21/dist/vue.global.prod.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/uplot@1.6.30/dist/uPlot.iife.min.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/uplot@1.6.30/dist/uPlot.min.css">
    <style>
        :root {
            --bg-light: #f5f5f5; --bg-card: #ffffff; --border: #e0e0e0;
            --text: #333333; --text-dim: #666666; --accent: #00a080;
            --solar: #e67e00; --grid: #3a7abd; --battery: #5cb318; --consumption: #d9534f;
        }
        body { background: var(--bg-light); color: var(--text); font-family: 'Segoe UI', sans-serif; }
        .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
        .card-header { background: transparent; border-bottom: 1px solid var(--border); font-weight: 600; text-transform: uppercase; font-size: 0.7rem; color: var(--text-dim); padding: 6px 12px; }
        .card-body { padding: 8px 12px; }
        .stat-value { font-size: 1.6rem; font-weight: 700; line-height: 1; }
        .stat-label { font-size: 0.65rem; color: var(--text-dim); text-transform: uppercase; }
        .stat-sub { font-size: 0.75rem; color: var(--text-dim); margin-top: 2px; }
        .toggle-btn { cursor: pointer; padding: 2px 6px; border-radius: 4px; font-size: 0.45rem; font-weight: 600; border: 1px solid var(--border); transition: all 0.15s; display: inline-block; margin: 1px; }
        .toggle-btn.on { background: #2e7d32; border-color: #4caf50; color: #fff; }
        .toggle-btn.off { background: #f0f0f0; color: #999; border-color: #ddd; }
        .toggle-btn:hover { transform: scale(1.02); filter: brightness(0.95); }
        .text-solar { color: var(--solar); } .text-grid { color: var(--grid); } .text-battery { color: var(--battery); } .text-consumption { color: var(--consumption); } .text-accent { color: var(--accent); }
        .daily-stats { font-size: 0.75rem; color: var(--text-dim); padding: 8px 12px; background: #fff; border: 1px solid var(--border); border-radius: 6px; font-family: monospace; }
        .chart-wrap { height: 200px; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
        .status-dot.online { background: #4caf50; } .status-dot.offline { background: #f44336; }
        .ws-status { position: fixed; top: 10px; right: 10px; padding: 4px 8px; border-radius: 4px; font-size: 0.7rem; }
        .ws-status.connected { background: #2e7d32; color: #fff; }
        .ws-status.disconnected { background: #c62828; color: #fff; }
    </style>
</head>
<body>
<div id="app" class="container-fluid p-2">
    <div class="ws-status" :class="wsConnected ? 'connected' : 'disconnected'">
        <i class="fas" :class="wsConnected ? 'fa-link' : 'fa-unlink'"></i>
        {{ wsConnected ? 'Live' : 'Reconnecting...' }}
    </div>

    <!-- Header -->
    <div class="card mb-2">
        <div class="card-body py-1 px-2">
            <div class="d-flex flex-wrap gap-1 align-items-center">
                <div class="toggle-btn" :class="state.dry_run ? 'on' : 'off'" @click="send('dry_run')">
                    <i class="fas fa-flask me-1"></i>DRY
                </div>
                <div class="toggle-btn" :class="essClass" @click="send('ess_mode')">
                    <i class="fas fa-bolt me-1"></i>{{ essText }}
                </div>
                <div class="vr mx-1" style="border-left:1px solid #ccc;height:16px;"></div>
                <div v-for="(val, key) in state.booleans" :key="key" 
                     class="toggle-btn" :class="val ? 'on' : 'off'"
                     @click="send('toggle', {entity: 'input_boolean.' + key})">
                    {{ formatKey(key) }}
                </div>
            </div>
        </div>
    </div>
    
    <!-- Daily stats -->
    <div class="daily-stats mb-2" v-html="dailyStatsHtml"></div>
    
    <!-- Main stats -->
    <div class="row g-2 mb-2">
        <div class="col-md-2">
            <div class="card h-100"><div class="card-body text-center">
                <div class="stat-label">Grid</div>
                <div class="stat-value text-grid">{{ formatPower(state.gt) }}</div>
                <div class="stat-sub">{{ formatPower(state.g1) }} | {{ formatPower(state.g2) }}</div>
            </div></div>
        </div>
        <div class="col-md-2">
            <div class="card h-100"><div class="card-body text-center">
                <div class="stat-label">Consumption</div>
                <div class="stat-value text-consumption">{{ formatPower(state.tt) }}</div>
                <div class="stat-sub">{{ formatPower(state.t1) }} | {{ formatPower(state.t2) }}</div>
            </div></div>
        </div>
        <div class="col-md-3">
            <div class="card h-100"><div class="card-body text-center">
                <div class="stat-label">Solar</div>
                <div class="stat-value text-solar">{{ formatPower(state.solar_total) }}</div>
                <div class="stat-sub">{{ solarDetail }}</div>
            </div></div>
        </div>
        <div class="col-md-3">
            <div class="card h-100"><div class="card-body text-center">
                <div class="stat-label">Battery</div>
                <div class="stat-value text-battery">{{ Math.floor(state.battery_soc || 0) }}%</div>
                <div class="stat-sub">{{ formatPower(state.battery_power) }} | {{ (state.battery_voltage || 0).toFixed(2) }}V {{ batteryIndividual }}</div>
            </div></div>
        </div>
        <div class="col-md-2">
            <div class="card h-100"><div class="card-body text-center">
                <div class="stat-label">Setpoint</div>
                <div class="stat-value text-accent">{{ formatPower(state.setpoint) }}</div>
                <div class="stat-sub">{{ state.inverter_state || '--' }}</div>
            </div></div>
        </div>
    </div>
    
    <!-- Chart -->
    <div class="row g-2 mb-2">
        <div class="col-md-8">
            <div class="card"><div class="card-body py-1">
                <div class="chart-wrap" ref="chartEl"></div>
            </div></div>
        </div>
        <div class="col-md-4">
            <!-- EV -->
            <div class="card mb-2" v-if="state.features?.ev !== false">
                <div class="card-header"><i class="fas fa-car me-2"></i>EV</div>
                <div class="card-body py-1">
                    <div class="d-flex justify-content-between">
                        <div><div class="stat-value text-solar">{{ evCharging }}</div><div class="stat-sub">Charging</div></div>
                        <div class="text-center"><div class="stat-value" style="color:#9e9e9e">{{ evPower }}</div><div class="stat-sub">VUE</div></div>
                        <div class="text-end"><div class="stat-value text-accent">{{ Math.floor(state.car_soc || 0) }}%</div><div class="stat-sub">SoC</div></div>
                    </div>
                </div>
            </div>
            <!-- Water -->
            <div class="card mb-2" v-if="state.features?.water !== false">
                <div class="card-header"><i class="fas fa-faucet me-2"></i>Water</div>
                <div class="card-body py-1">
                    <div class="d-flex justify-content-between align-items-center">
                        <div class="fw-bold" :style="{color: state.water_valve ? '#f44336' : '#4caf50'}">{{ state.water_level || 0 }} cm</div>
                        <div class="d-flex gap-1">
                            <div class="toggle-btn" :class="state.pump_switch ? 'on' : 'off'" @click="send('toggle', {entity:'switch.pump_switch'})">PUMP</div>
                            <div class="toggle-btn" :class="state.water_valve ? 'on' : 'off'" @click="send('toggle', {entity:'switch.778_40th_ave_sf_shutoff_valve'})">VALVE</div>
                        </div>
                    </div>
                </div>
            </div>
            <!-- Home -->
            <div class="card" v-if="state.features?.ha !== false">
                <div class="card-header"><i class="fas fa-home me-2"></i>Home</div>
                <div class="card-body py-1">
                    <div class="d-flex gap-1 flex-wrap">
                        <div class="toggle-btn" :class="state.home_recliner ? 'on' : 'off'" @click="send('toggle', {entity:'switch.recliner_recliner'})">RECLINER</div>
                        <div class="toggle-btn" :class="state.home_garage ? 'on' : 'off'" @click="send('toggle', {entity:'switch.garage_opener_l'})">GARAGE</div>
                        <div class="toggle-btn" :class="state.laundry_outlet ? 'on' : 'off'" @click="send('toggle', {entity:'switch.laundry_zigbee_switch'})">LAUNDRY</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Loads -->
    <div class="row g-2 mb-2" v-if="state.features?.ha_loads !== false && sortedLoads.length">
        <div class="col-12">
            <div class="card">
                <div class="card-header">Loads</div>
                <div class="card-body py-1" style="font-size:0.65rem;color:#666">
                    <div class="d-flex flex-wrap gap-3">
                        <div v-for="[name, val] in sortedLoads" :key="name">
                            <span>{{ name }}:</span> <span class="fw-bold">{{ Math.floor(val) }}W</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Status -->
    <div class="mt-2 text-center small" style="color:#666">
        <span class="status-dot" :class="state.ha_connected ? 'online' : 'offline'"></span>
        HA: {{ state.ha_connected ? 'Connected' : 'Disconnected' }}
        &nbsp;|&nbsp; Uptime: {{ formatUptime(state.uptime || 0) }}
        &nbsp;|&nbsp; MQTT: {{ mqttConnected ? 'OK' : 'Disconnected' }}
    </div>
</div>

<script>
const { createApp, ref, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

createApp({
    setup() {
        const state = ref({booleans: {}, features: {}, limits: {min: -2300, max: 2250}, console: []});
        const wsConnected = ref(false);
        const mqttConnected = ref(false);
        const chartEl = ref(null);
        let ws = null;
        let chart = null;
        let reconnectTimer = null;
        let historyData = {timestamps: [], grid: [], solar: [], battery: [], setpoint: []};
        
        function connect() {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${proto}//${location.host}/ws`);
            
            ws.onopen = () => { wsConnected.value = true; };
            ws.onclose = () => { 
                wsConnected.value = false; 
                reconnectTimer = setTimeout(connect, 2000);
            };
            ws.onmessage = (e) => {
                const data = JSON.parse(e.data);
                state.value = data;
                mqttConnected.value = true;
                
                // Update history
                if (data.gt !== undefined) {
                    const now = Date.now() / 1000;
                    historyData.timestamps.push(now);
                    historyData.grid.push(data.gt || 0);
                    historyData.solar.push(data.solar_total || 0);
                    historyData.battery.push(data.battery_power || 0);
                    historyData.setpoint.push(data.setpoint || 0);
                    
                    // Keep last 1800 points
                    if (historyData.timestamps.length > 1800) {
                        historyData.timestamps.shift();
                        historyData.grid.shift();
                        historyData.solar.shift();
                        historyData.battery.shift();
                        historyData.setpoint.shift();
                    }
                    updateChart();
                }
            };
        }
        
        function send(action, payload = {}) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({action, ...payload}));
            }
        }
        
        function formatPower(w) {
            const v = Math.abs(Math.floor(w || 0));
            const sign = w < 0 ? '-' : '';
            return v >= 1000 ? sign + (v/1000).toFixed(1) + 'kW' : sign + v + 'W';
        }
        function formatKey(k) { return k.replace(/_/g, ' ').toUpperCase(); }
        function formatUptime(s) {
            if (s < 60) return s + 's';
            if (s < 3600) return Math.floor(s/60) + 'm';
            const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
            return h + 'h ' + m + 'm';
        }
        
        const essClass = computed(() => {
            const m = state.value.ess_mode;
            if (!m) return 'off';
            if (m.mode_name === 'Off' || m.mode_name === 'Charger only') return 'off';
            return 'on';
        });
        const essText = computed(() => {
            const m = state.value.ess_mode;
            if (!m) return 'ESS';
            if (m.is_external) return 'External';
            return m.mode_name || 'ESS';
        });
        
        const solarDetail = computed(() => {
            const mppt = state.value.mppt_individual || [];
            const tas = state.value.tasmota_individual || [];
            return (mppt.length ? mppt.map(v => Math.floor(v) + 'W').join('|') : '--') + ' | ' + 
                   (tas.length ? tas.map(v => Math.floor(v) + 'W').join('|') : '--');
        });
        
        const evCharging = computed(() => {
            const kw = parseFloat(state.value.ev_charging_kw) || 0;
            return kw > 0 ? kw.toFixed(1) + 'kW' : '0';
        });
        const evPower = computed(() => formatPower(state.value.ev_power || 0));
        
        const sortedLoads = computed(() => {
            const loads = state.value.loads || {};
            return Object.entries(loads).filter(([_, v]) => v > 10).sort((a, b) => b[1] - a[1]);
        });
        
        const batteryIndividual = computed(() => {
            const b1 = state.value.battery1_soc;
            const b2 = state.value.battery2_soc;
            if (b1 !== undefined && b2 !== undefined) {
                return `[${Math.floor(b1)}%|${Math.floor(b2)}%]`;
            }
            return '';
        });
        
        const dailyStatsHtml = computed(() => {
            const ds = state.value.daily_stats || {};
            const s = state.value;
            
            const prod = (ds.produced_today || 0).toFixed(2);
            const dollars = (ds.produced_dollars || 0).toFixed(2);
            const grid = (ds.grid_kwh || 0).toFixed(2);
            const gridDollars = (ds.grid_dollars || 0).toFixed(2);
            
            // Solar breakdown: mppt + tasmota with individual values
            const mpptKwh = (ds.mppt_kwh || []).map(v => v.toFixed(2) + 'kW');
            const tasmotaKwh = (ds.tasmota_kwh || []).map(v => v.toFixed(2) + 'kW');
            const mpptDollars = (ds.mppt_dollars || []).map(v => v.toFixed(2));
            const tasmotaDollars = (ds.tasmota_dollars || []).map(v => v.toFixed(2));
            
            let solarBreakdown = '';
            if (mpptKwh.length || tasmotaKwh.length) {
                const allKwh = [...mpptKwh, ...tasmotaKwh].join('+');
                const allDollars = [...mpptDollars, ...tasmotaDollars].join('+');
                solarBreakdown = ` ${allKwh}(${allDollars})`;
            }
            
            // Battery stats
            const battIn = (ds.battery_in_kwh || 0).toFixed(2);
            const battInDollars = (ds.battery_in_dollars || 0).toFixed(2);
            const battOut = (ds.battery_out_kwh || 0).toFixed(2);
            const battOutDollars = (ds.battery_out_dollars || 0).toFixed(2);
            const battDelta = (ds.battery_delta_kwh || 0).toFixed(2);
            const battDeltaDollars = (ds.battery_delta_dollars || 0).toFixed(2);
            
            let result = `<span style="color:#e67e00">☀️ ${prod}kWh${solarBreakdown}</span> <span style="color:#5cb318">($${dollars})</span>`;
            result += ` | Grid: ${grid}kWh <span style="color:#d9534f">($${gridDollars})</span>`;
            
            if (ds.battery_in_kwh !== undefined) {
                result += ` | 🔋 In: ${battIn}kWh (${battInDollars}), Out: ${battOut}kWh (${battOutDollars}); Δ: ${battDelta}kWh (${battDeltaDollars})`;
            }
            
            return result;
        });
        
        function initChart() {
            if (!chartEl.value) return;
            const opts = {
                width: chartEl.value.clientWidth,
                height: 200,
                series: [
                    {},
                    {stroke: '#4a90d9', fill: 'rgba(74,144,217,0.05)', label: 'Grid'},
                    {stroke: '#f5a623', fill: 'rgba(245,166,35,0.05)', label: 'Solar'},
                    {stroke: '#7ed321', fill: 'rgba(126,211,33,0.05)', label: 'Battery'},
                    {stroke: '#00d4aa', dash: [5,5], label: 'Setpoint'},
                ],
                axes: [{show: false}, {grid: {stroke: '#e0e0e0'}, ticks: {stroke: '#ccc'}}],
                legend: {show: true},
                cursor: {show: false},
            };
            chart = new uPlot(opts, [[], [], [], [], []], chartEl.value);
        }
        
        function updateChart() {
            if (!chart) return;
            chart.setData([
                historyData.timestamps,
                historyData.grid,
                historyData.solar,
                historyData.battery,
                historyData.setpoint
            ]);
        }
        
        onMounted(() => {
            connect();
            nextTick(() => initChart());
            window.addEventListener('resize', () => {
                if (chart && chartEl.value) chart.setSize({width: chartEl.value.clientWidth, height: 200});
            });
        });
        
        onUnmounted(() => {
            if (ws) ws.close();
            if (reconnectTimer) clearTimeout(reconnectTimer);
        });
        
        return {
            state, wsConnected, mqttConnected, chartEl,
            essClass, essText, solarDetail, evCharging, evPower, sortedLoads, dailyStatsHtml, batteryIndividual,
            send, formatPower, formatKey, formatUptime
        };
    }
}).mount('#app');
</script>
</body>
</html>'''


def main():
    global MQTT_HOST, MQTT_PORT
    
    parser = argparse.ArgumentParser(description='Remote Web Dashboard for Inverter Control')
    parser.add_argument('--mqtt-host', default=MQTT_HOST, help='MQTT broker host')
    parser.add_argument('--mqtt-port', type=int, default=MQTT_PORT, help='MQTT broker port')
    parser.add_argument('--port', type=int, default=WEB_PORT, help='Web server port')
    parser.add_argument('--ssl-cert', help='SSL certificate file')
    parser.add_argument('--ssl-key', help='SSL key file')
    args = parser.parse_args()
    
    MQTT_HOST = args.mqtt_host
    MQTT_PORT = args.mqtt_port
    
    print(f"Starting Remote Dashboard")
    print(f"  MQTT: {MQTT_HOST}:{MQTT_PORT}")
    print(f"  Web:  http://0.0.0.0:{args.port}")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        ssl_certfile=args.ssl_cert,
        ssl_keyfile=args.ssl_key,
        log_level="info"
    )


if __name__ == "__main__":
    main()
