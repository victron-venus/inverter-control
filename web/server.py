#!/usr/bin/env python3
"""
Web Server for Inverter Control
Dashboard with real-time data, controls, and graphs
"""

import json
import threading
import time
import ssl
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from typing import Dict, Any, Callable
from collections import deque
import os

logger = logging.getLogger('inverter-control')

# Will be set by main.py
state_getter: Callable[[], Dict[str, Any]] = lambda: {}
setpoint_setter: Callable[[int], bool] = lambda x: False
dry_run_toggler: Callable[[], bool] = lambda: False
limits_setter: Callable[[int, int], Dict[str, int]] = lambda mn, mx: {'min': mn, 'max': mx}
ess_mode_toggler: Callable[[], Dict[str, Any]] = lambda: {}
loop_interval_setter: Callable[[float], float] = lambda x: x
ha_client = None

# History for graphs
history = {
    'timestamps': deque(maxlen=1800),  # 1 hour at 2s intervals
    'grid': deque(maxlen=1800),
    'solar': deque(maxlen=1800),
    'battery': deque(maxlen=1800),
    'setpoint': deque(maxlen=1800),
    'consumption': deque(maxlen=1800),
}

console_log = deque(maxlen=50)


def add_history_point(data: Dict[str, Any]):
    """Add a data point to history"""
    history['timestamps'].append(time.time())
    history['grid'].append(data.get('gt', 0))
    history['solar'].append(data.get('solar_total', 0))
    history['battery'].append(data.get('battery_power', 0))
    history['setpoint'].append(data.get('setpoint', 0))
    history['consumption'].append(data.get('tt', 0))


def add_console_line(line: str):
    """Add line to console log"""
    console_log.append(line)


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for dashboard"""
    
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def send_json(self, data: Any, status: int = 200):
        """Send JSON response"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=list).encode())
    
    def send_html(self, content: str):
        """Send HTML response"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(content.encode())
    
    def do_OPTIONS(self):
        """Handle CORS preflight"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests"""
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path == '/api/state':
            self.send_json(state_getter())
        
        elif path == '/api/history':
            self.send_json({
                'timestamps': list(history['timestamps']),
                'grid': list(history['grid']),
                'solar': list(history['solar']),
                'battery': list(history['battery']),
                'setpoint': list(history['setpoint']),
                'consumption': list(history['consumption']),
            })
        
        elif path == '/api/console':
            self.send_json(list(console_log))
        
        else:
            self.send_html(get_dashboard_html())
    
    def do_POST(self):
        """Handle POST requests"""
        parsed = urlparse(self.path)
        path = parsed.path
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else '{}'
        
        try:
            data = json.loads(body) if body else {}
        except:
            data = {}
        
        if path == '/api/toggle':
            entity = data.get('entity')
            if entity and ha_client:
                success = ha_client.toggle_entity(entity)
                self.send_json({'success': success})
            else:
                self.send_json({'success': False}, 400)
        
        elif path == '/api/setpoint':
            value = data.get('value')
            if value is not None:
                success = setpoint_setter(int(value))
                self.send_json({'success': success})
            else:
                self.send_json({'success': False}, 400)
        
        elif path == '/api/dry-run':
            new_state = dry_run_toggler()
            self.send_json({'dry_run': new_state})
        
        elif path == '/api/limits':
            min_val = data.get('min', -2300)
            max_val = data.get('max', 2250)
            result = limits_setter(int(min_val), int(max_val))
            self.send_json(result)
        
        elif path == '/api/ess-mode':
            result = ess_mode_toggler()
            self.send_json(result)
        
        elif path == '/api/loop-interval':
            interval = data.get('interval', 0.33)
            new_interval = loop_interval_setter(float(interval))
            self.send_json({'loop_interval': new_interval})
        
        else:
            self.send_json({'error': 'Not found'}, 404)


def get_dashboard_html() -> str:
    """Generate dashboard HTML"""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Inverter Control</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-dark: #0a0a0a;
            --bg-card: #151515;
            --border: #2a2a2a;
            --text: #e0e0e0;
            --text-dim: #666;
            --accent: #00d4aa;
            --solar: #f5a623;
            --grid: #4a90d9;
            --battery: #7ed321;
            --consumption: #e74c3c;
        }
        body.light {
            --bg-dark: #f5f5f5;
            --bg-card: #ffffff;
            --border: #ddd;
            --text: #222;
            --text-dim: #555;
        }
        body.light #console { background: #f0f0f0; color: #000; }
        body.light .daily-stats { background: #e8e8e8; }
        body.light .toggle-btn { background: #e0e0e0; color: #333; border-color: #ccc; }
        body.light .toggle-btn.on { background: #4caf50; color: #fff; }
        body.light .toggle-btn.off { background: #ddd; color: #666; }
        body.light input, body.light .form-control { background: #fff !important; color: #333 !important; border-color: #ccc !important; }
        body { 
            background: var(--bg-dark); 
            color: var(--text); 
            font-family: 'Segoe UI', system-ui, sans-serif;
            min-height: 100vh;
        }
        .card { 
            background: var(--bg-card); 
            border: 1px solid var(--border); 
            border-radius: 8px;
        }
        .card-header {
            background: transparent;
            border-bottom: 1px solid var(--border);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.7rem;
            letter-spacing: 1px;
            color: var(--text-dim);
            padding: 6px 12px;
        }
        .card-body { padding: 8px 12px; }
        .stat-value { 
            font-size: 1.6rem; 
            font-weight: 700; 
            line-height: 1;
        }
        .stat-label { 
            font-size: 0.65rem; 
            color: var(--text-dim); 
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 2px;
        }
        .stat-sub { 
            font-size: 0.75rem; 
            color: var(--text-dim);
            margin-top: 2px;
        }
        .toggle-btn {
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.45rem;
            font-weight: 600;
            border: 1px solid var(--border);
            transition: all 0.2s;
            display: inline-block;
            margin: 1px;
        }
        .toggle-btn.on { 
            background: #2e7d32; 
            border-color: #4caf50;
            color: #fff; 
        }
        .toggle-btn.off { 
            background: #1a1a1a; 
            color: #555;
        }
        .toggle-btn.dry-on {
            background: #ff9800;
            border-color: #ffc107;
            color: #fff;
        }
        .toggle-btn:hover {
            transform: scale(1.02);
            filter: brightness(1.1);
        }
        #console {
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            font-size: 0.45rem;
            background: #000;
            color: #0f0;
            padding: 6px;
            height: 180px;
            overflow-y: auto;
            border-radius: 6px;
            border: 1px solid var(--border);
        }
        .text-solar { color: var(--solar); }
        .text-grid { color: var(--grid); }
        .text-battery { color: var(--battery); }
        .text-consumption { color: var(--consumption); }
        .text-accent { color: var(--accent); }
        .text-vue { color: #9e9e9e; }  /* Lighter grey for VUE in dark theme */
        .setpoint-control input {
            background: #1a1a1a;
            border: 1px solid var(--border);
            color: var(--text);
            border-radius: 6px;
            padding: 8px 12px;
            width: 120px;
        }
        .setpoint-control button {
            background: var(--accent);
            border: none;
            color: #000;
            border-radius: 6px;
            padding: 8px 16px;
            font-weight: 600;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 6px;
        }
        .status-dot.online { background: #4caf50; }
        .status-dot.offline { background: #f44336; }
        .chart-container {
            position: relative;
            height: 200px;
        }
        .water-indicator {
            font-size: 1.5rem;
            font-weight: 700;
        }
        .water-indicator.low { color: #f44336; }
        .water-indicator.ok { color: #4caf50; }
        .update-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #f44336;
            display: inline-block;
            margin-right: 8px;
            opacity: 0;
            transition: opacity 0.15s;
            box-shadow: 0 0 8px #f44336;
        }
        .update-indicator.pulse { opacity: 1; }
        .daily-stats {
            font-size: 0.75rem;
            color: var(--text-dim);
            padding: 8px 12px;
            background: #0d0d0d;
            border-radius: 6px;
            font-family: monospace;
        }
        .daily-stats .highlight { color: var(--solar); }
        .daily-stats .money { color: #4caf50; }
        .daily-stats .dim { color: #555; }
        .daily-stats .detail { color: #888; font-size: 0.7rem; }
        #loads { color: #888; font-size: 0.65rem; }
        #loads .loads-table { display: table; width: 100%; }
        #loads .loads-row { display: table-row; }
        #loads .loads-name { display: table-cell; text-align: left; padding-right: 8px; }
        #loads .loads-value { display: table-cell; text-align: right; font-family: monospace; min-width: 45px; }
        /* Smaller inputs for Manual Setpoint / Power Limits */
        .compact-controls input { font-size: 0.65rem !important; padding: 2px 6px !important; height: auto !important; }
        .compact-controls button { font-size: 0.65rem !important; padding: 2px 8px !important; }
        .compact-controls .card-header { font-size: 0.7rem; padding: 4px 8px; }
        .compact-controls .card-body { padding: 6px !important; }
        .compact-controls .text-muted { font-size: 0.6rem; }
        .range-hint { color: #999 !important; }
        .status-bar { color: #888; }
    </style>
</head>
<body>
    <div class="container-fluid p-2">
        <!-- Header with toggles and update indicator -->
        <div class="card mb-2">
            <div class="card-body py-1 px-2">
                <div class="d-flex flex-wrap gap-1 align-items-center">
                    <span id="update-indicator" class="update-indicator"></span>
                    <div id="dry-run-btn" class="toggle-btn" onclick="toggleDryRun()">
                        <i class="fas fa-flask me-1"></i>DRY
                    </div>
                    <div id="ess-mode-btn" class="toggle-btn" onclick="toggleEssMode()" title="Optimized without BatteryLife">
                        <i class="fas fa-bolt me-1"></i>Optimized
                    </div>
                    <div class="vr mx-1" style="border-left: 1px solid #333; height: 16px;"></div>
                    <div id="toggles" class="d-flex flex-wrap gap-1"></div>
                    <div class="ms-auto">
                        <div class="toggle-btn" onclick="toggleTheme()" id="theme-btn">
                            <i class="fas fa-sun"></i>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Daily stats bar -->
        <div class="daily-stats mb-2" id="daily-stats">
            Loading daily stats...
        </div>
        
        <!-- Main stats row -->
        <div class="row g-2 mb-2">
            <div class="col-md-2">
                <div class="card h-100">
                    <div class="card-body text-center">
                        <div class="stat-label">Grid</div>
                        <div class="stat-value text-grid" id="grid-power">--</div>
                        <div class="stat-sub" id="grid-detail">L1: -- | L2: --</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card h-100">
                    <div class="card-body text-center">
                        <div class="stat-label">Consumption</div>
                        <div class="stat-value text-consumption" id="consumption">--</div>
                        <div class="stat-sub" id="consumption-detail">L1: -- | L2: --</div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card h-100">
                    <div class="card-body text-center">
                        <div class="stat-label">Solar</div>
                        <div class="stat-value text-solar" id="solar-total">--</div>
                        <div class="stat-sub" id="solar-detail">MPPT: -- | Tasmota: --</div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card h-100">
                    <div class="card-body text-center">
                        <div class="stat-label">Battery</div>
                        <div class="stat-value text-battery" id="battery-soc">--%</div>
                        <div class="stat-sub" id="battery-detail">-- W | -- V</div>
                    </div>
                </div>
            </div>
            <div class="col-md-2">
                <div class="card h-100">
                    <div class="card-body text-center">
                        <div class="stat-label">Setpoint</div>
                        <div class="stat-value text-accent" id="setpoint">--</div>
                        <div class="stat-sub" id="inverter-state">--</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Chart and controls -->
        <div class="row g-2 mb-2">
            <div class="col-md-8">
                <div class="card">
                    <div class="card-body py-1">
                        <div class="chart-container">
                            <canvas id="powerChart"></canvas>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card mb-2" id="ev-section">
                    <div class="card-header"><i class="fas fa-car me-2"></i>EV</div>
                    <div class="card-body py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <div class="stat-value text-solar" id="ev-charging">--</div>
                                <div class="stat-sub">Charging</div>
                            </div>
                            <div class="text-center">
                                <div class="stat-value text-vue" id="ev-power">--</div>
                                <div class="stat-sub">VUE</div>
                            </div>
                            <div class="text-end">
                                <div class="stat-value text-accent" id="ev-soc">--%</div>
                                <div class="stat-sub">SoC</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card" id="water-section">
                    <div class="card-header"><i class="fas fa-faucet me-2"></i>Water</div>
                    <div class="card-body py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <div class="water-indicator" id="water-level">-- cm</div>
                            <div class="d-flex gap-1">
                                <div id="pump-switch" class="toggle-btn" onclick="toggle('switch.pump_switch')">PUMP</div>
                                <div id="water-valve" class="toggle-btn" onclick="toggle('switch.778_40th_ave_sf_shutoff_valve')">VALVE</div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="card" id="dishwasher-section" style="display:none;">
                    <div class="card-header"><i class="fas fa-sink me-2"></i>Dishwasher</div>
                    <div class="card-body py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <span>Running for</span>
                            <span id="dishwasher-duration" class="fw-bold">--</span>
                        </div>
                    </div>
                </div>
                <div class="card" id="washer-section" style="display:none;">
                    <div class="card-header"><i class="fas fa-soap me-2"></i>Washer</div>
                    <div class="card-body py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <span>Time left</span>
                            <span id="washer-time" class="fw-bold">--</span>
                        </div>
                    </div>
                </div>
                <div class="card" id="dryer-section" style="display:none;">
                    <div class="card-header"><i class="fas fa-wind me-2"></i>Dryer</div>
                    <div class="card-body py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <span>Time left</span>
                            <span id="dryer-time" class="fw-bold">--</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Console and loads -->
        <div class="row g-2 mb-2">
            <div class="col-md-8">
                <div class="card">
                    <div class="card-body p-1">
                        <div id="console"></div>
                    </div>
                </div>
            </div>
            <div class="col-md-4" id="loads-section">
                <div class="card">
                    <div class="card-header">Loads</div>
                    <div class="card-body py-1">
                        <div id="loads" class="small"></div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Manual setpoint, limits, and loop interval -->
        <div class="row g-2 compact-controls">
            <div class="col-md-4">
                <div class="card">
                    <div class="card-header">Manual Setpoint</div>
                    <div class="card-body">
                        <div class="d-flex gap-1 align-items-center">
                            <input type="number" id="manual-setpoint" class="form-control form-control-sm" placeholder="W" style="width:70px;background:#1a1a1a;border-color:#333;color:#ddd;">
                            <button class="btn btn-sm btn-success" onclick="setManualSetpoint()">Set</button>
                            <small class="range-hint ms-1" id="limits-display">[-2300, +2250]</small>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-md-5">
                <div class="card">
                    <div class="card-header">Power Limits Override</div>
                    <div class="card-body">
                        <div class="d-flex gap-1 align-items-center flex-wrap">
                            <span class="text-muted">Min:</span>
                            <input type="number" id="limit-min" class="form-control form-control-sm" value="-2300" style="width:60px;background:#1a1a1a;border-color:#333;color:#ddd;">
                            <span class="text-muted">Max:</span>
                            <input type="number" id="limit-max" class="form-control form-control-sm" value="2250" style="width:60px;background:#1a1a1a;border-color:#333;color:#ddd;">
                            <button class="btn btn-sm btn-warning" onclick="setLimits()">Apply</button>
                            <button class="btn btn-sm btn-secondary" onclick="resetLimits()">Reset</button>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card">
                    <div class="card-header">Loop Interval</div>
                    <div class="card-body">
                        <div class="d-flex gap-1 align-items-center">
                            <input type="number" id="loop-interval" class="form-control form-control-sm" step="0.1" min="0.1" max="5" value="0.33" style="width:60px;background:#1a1a1a;border-color:#333;color:#ddd;">
                            <span class="text-muted">s</span>
                            <button class="btn btn-sm btn-info" onclick="setLoopInterval()">Set</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Status bar -->
        <div class="mt-2 text-center small status-bar">
            <span class="status-dot" id="ha-status"></span>
            <span id="ha-status-text">HA: --</span>
            &nbsp;|&nbsp;
            <span id="last-update">Updated: --</span>
            &nbsp;|&nbsp;
            <span id="uptime">Uptime: --</span>
        </div>
    </div>

<script>
let chart;
let updateToggle = false;

function formatDuration(minutes) {
    // Format duration in minutes to human-readable string
    const mins = Math.floor(minutes);
    if (mins < 60) {
        return mins + ' min';
    }
    const hours = Math.floor(mins / 60);
    const remainMins = mins % 60;
    if (remainMins === 0) {
        return hours + 'h';
    }
    return hours + 'h ' + remainMins + 'm';
}

function formatPower(watts) {
    // Format power: use kW for values >= 1000W
    const w = Math.abs(Math.floor(watts));
    const sign = watts < 0 ? '-' : '';
    if (w >= 1000) {
        return sign + (w / 1000).toFixed(1) + 'kW';
    }
    return sign + w + 'W';
}

function initChart() {
    const ctx = document.getElementById('powerChart').getContext('2d');
    chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                { label: 'Grid', data: [], borderColor: '#4a90d9', backgroundColor: 'rgba(74,144,217,0.05)', fill: true, tension: 0.3, borderWidth: 1.5 },
                { label: 'Solar', data: [], borderColor: '#f5a623', backgroundColor: 'rgba(245,166,35,0.05)', fill: true, tension: 0.3, borderWidth: 1.5 },
                { label: 'Battery', data: [], borderColor: '#7ed321', backgroundColor: 'rgba(126,211,33,0.05)', fill: true, tension: 0.3, borderWidth: 1.5 },
                { label: 'Setpoint', data: [], borderColor: '#00d4aa', borderDash: [5,5], fill: false, tension: 0.3, borderWidth: 1 },
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 0 },
            scales: {
                x: { display: false },
                y: { grid: { color: '#222' }, ticks: { color: '#666' } }
            },
            plugins: {
                legend: { labels: { color: '#888', boxWidth: 12, padding: 10 } }
            },
            elements: { point: { radius: 0 } }
        }
    });
}

async function toggle(entity) {
    try {
        const res = await fetch('/api/toggle', { 
            method: 'POST', 
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({entity}) 
        });
        const data = await res.json();
        if (!data.success) {
            console.error('Toggle failed for', entity);
        }
    } catch (e) {
        console.error('Toggle error:', e);
    }
}

async function toggleDryRun() {
    try {
        const res = await fetch('/api/dry-run', { method: 'POST' });
        const data = await res.json();
        updateDryRunBtn(data.dry_run);
    } catch (e) {
        console.error('toggleDryRun error:', e);
        alert('Failed to toggle DRY mode: ' + e.message);
    }
}

function updateDryRunBtn(isDryRun) {
    const btn = document.getElementById('dry-run-btn');
    // Remove old state classes
    btn.classList.remove('on', 'off');
    if (isDryRun) {
        // DRY mode ON - green like other active buttons
        btn.classList.add('on');
    } else {
        // DRY mode OFF - grey like other inactive buttons
        btn.classList.add('off');
    }
}

async function toggleEssMode() {
    const res = await fetch('/api/ess-mode', { method: 'POST' });
    const data = await res.json();
    updateEssModeBtn(data);
}

function updateEssModeBtn(essMode) {
    const btn = document.getElementById('ess-mode-btn');
    if (!essMode) return;
    
    const modeName = essMode.mode_name || '';
    
    // Remove old state classes
    btn.classList.remove('on', 'off', 'dry-on');
    
    if (modeName === 'Off' || modeName === 'Charger only') {
        // Victron is off or not inverting - grey inactive
        btn.classList.add('off');
        btn.innerHTML = '<i class="fas fa-power-off me-1"></i>' + modeName;
        btn.title = 'Victron is ' + modeName;
    } else if (essMode.is_external) {
        // External control mode - active green
        btn.classList.add('on');
        btn.innerHTML = '<i class="fas fa-plug me-1"></i>External';
        btn.title = 'External control mode - click for Optimized without BatteryLife';
    } else {
        // Optimized mode - also active green (it's a working mode)
        btn.classList.add('on');
        btn.innerHTML = '<i class="fas fa-bolt me-1"></i>Optimized';
        btn.title = 'Optimized without BatteryLife - click for External control';
    }
}

function toggleTheme() {
    document.body.classList.toggle('light');
    const btn = document.getElementById('theme-btn');
    const isLight = document.body.classList.contains('light');
    btn.innerHTML = isLight ? '<i class="fas fa-moon"></i>' : '<i class="fas fa-sun"></i>';
    localStorage.setItem('theme', isLight ? 'light' : 'dark');
}

// Restore theme on load
if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light');
    document.getElementById('theme-btn').innerHTML = '<i class="fas fa-moon"></i>';
}

async function setManualSetpoint() {
    const val = document.getElementById('manual-setpoint').value;
    if (val) {
        await fetch('/api/setpoint', { method: 'POST', body: JSON.stringify({value: parseInt(val)}) });
        document.getElementById('manual-setpoint').value = '';
    }
}

async function setLimits() {
    const min = parseInt(document.getElementById('limit-min').value) || -2300;
    const max = parseInt(document.getElementById('limit-max').value) || 2250;
    const res = await fetch('/api/limits', { method: 'POST', body: JSON.stringify({min, max}) });
    const data = await res.json();
    document.getElementById('limits-display').textContent = `[${data.min}, +${data.max}]`;
}

function resetLimits() {
    document.getElementById('limit-min').value = -2300;
    document.getElementById('limit-max').value = 2250;
}

async function setLoopInterval() {
    const interval = parseFloat(document.getElementById('loop-interval').value) || 0.33;
    const res = await fetch('/api/loop-interval', { method: 'POST', body: JSON.stringify({interval}) });
    const data = await res.json();
    document.getElementById('loop-interval').value = data.loop_interval.toFixed(2);
    setLimits();
}

function pulseIndicator() {
    const ind = document.getElementById('update-indicator');
    updateToggle = !updateToggle;
    ind.className = 'update-indicator' + (updateToggle ? ' pulse' : '');
}

async function updateData() {
    try {
        const [stateRes, histRes, consoleRes] = await Promise.all([
            fetch('/api/state'),
            fetch('/api/history'),
            fetch('/api/console')
        ]);
        
        const state = await stateRes.json();
        const hist = await histRes.json();
        const console_lines = await consoleRes.json();
        
        // Pulse update indicator
        pulseIndicator();
        
        // Update stats
        document.getElementById('grid-power').textContent = formatPower(state.gt);
        document.getElementById('grid-detail').textContent = `${formatPower(state.g1)} | ${formatPower(state.g2)}`;
        document.getElementById('consumption').textContent = formatPower(state.tt);
        document.getElementById('consumption-detail').textContent = `${formatPower(state.t1)} | ${formatPower(state.t2)}`;
        document.getElementById('solar-total').textContent = formatPower(state.solar_total || 0);
        
        // Solar detail with individual MPPT and Tasmota values
        const mpptVals = state.mppt_individual || [];
        const tasVals = state.tasmota_individual || [];
        const mpptStr = mpptVals.length ? mpptVals.map(v => Math.floor(v) + 'W').join('|') : '--';
        const tasStr = tasVals.length ? tasVals.map(v => Math.floor(v) + 'W').join('|') : '--';
        document.getElementById('solar-detail').textContent = `${mpptStr} | ${tasStr}`;
        
        document.getElementById('battery-soc').textContent = (state.battery_soc || 0).toFixed(0) + '%';
        // Battery detail with individual SoC values
        const batSocs = state.battery_socs || [];
        const batSocStr = batSocs.length ? batSocs.map(s => Math.floor(s) + '%').join('|') : '';
        const batDetailExtra = batSocStr ? ` [${batSocStr}]` : '';
        document.getElementById('battery-detail').textContent = `${formatPower(state.battery_power || 0)} | ${(state.battery_voltage || 0).toFixed(2)}V${batDetailExtra}`;
        document.getElementById('setpoint').textContent = formatPower(state.setpoint);
        document.getElementById('inverter-state').textContent = state.inverter_state || '--';
        
        // Daily stats
        const ds = state.daily_stats || {};
        const prodToday = (ds.produced_today || 0).toFixed(2);
        const prodDollars = (ds.produced_dollars || 0).toFixed(2);
        const gridKwh = (ds.grid_kwh || 0).toFixed(2);
        const batIn = (ds.battery_in || 0).toFixed(2);
        const batOut = (ds.battery_out || 0).toFixed(2);
        const batInY = (ds.battery_in_yesterday || 0).toFixed(2);
        const batOutY = (ds.battery_out_yesterday || 0).toFixed(2);
        const batDelta = (parseFloat(batIn) - parseFloat(batOut)).toFixed(2);
        const batDeltaY = (parseFloat(batInY) - parseFloat(batOutY)).toFixed(2);
        const gridCost = (parseFloat(gridKwh) * 0.31).toFixed(2);
        
        // Solar detail for daily stats: tasmota1kW+tasmota2kW+pv_totalkW(mppt1+mppt2+mppt3)
        const tasDaily = ds.tasmota_daily || [];
        const mpptDaily = ds.mppt_daily || [];
        const pvTotalDaily = ds.pv_total_daily || 0;
        // Format: 2.41kW+2.60kW+11.96kW(3.15+4.93+4.07)
        let solarParts = [];
        tasDaily.forEach(v => { if (v > 0) solarParts.push(v.toFixed(2) + 'kW'); });
        const mpptDailyStr = mpptDaily.map(v => v.toFixed(2)).join('+');
        solarParts.push(pvTotalDaily.toFixed(2) + 'kW(' + mpptDailyStr + ')');
        const solarDetailStr = `<span class="detail">${solarParts.join('+')}</span>`;
        
        document.getElementById('daily-stats').innerHTML = 
            `<span class="highlight">☀️ ${prodToday}kWh</span> ${solarDetailStr} ` +
            `<span class="money">($${prodDollars})</span> | ` +
            `Grid: ${gridKwh}kWh <span class="money">($${gridCost})</span> | ` +
            `🔋 In: ${batIn}kWh <span class="dim">(${batInY})</span>, ` +
            `Out: ${batOut}kWh <span class="dim">(${batOutY})</span>; ` +
            `Δ: ${batDelta}kWh <span class="dim">(${batDeltaY})</span>`;
        
        // Feature visibility
        const features = state.features || {};
        document.getElementById('ev-section').style.display = features.ev !== false ? '' : 'none';
        document.getElementById('water-section').style.display = features.water !== false ? '' : 'none';
        document.getElementById('loads-section').style.display = features.ha_loads !== false ? '' : 'none';
        
        // EV
        if (features.ev !== false) {
        const evChargingKw = parseFloat(state.ev_charging_kw) || 0;
        document.getElementById('ev-charging').textContent = evChargingKw > 0 ? evChargingKw.toFixed(1) + 'kW' : '0';
        
        // Format EV VUE power - use kW for values >= 1000W
        const evPower = Math.floor(state.ev_power || 0);
        let evPowerText;
        if (evPower >= 1000) {
            evPowerText = (evPower / 1000).toFixed(1) + 'kW';
        } else {
            evPowerText = evPower + 'W';
        }
        document.getElementById('ev-power').textContent = evPowerText;
        document.getElementById('ev-soc').textContent = Math.floor(state.car_soc || 0) + '%';
        }
        
        // Water - green when valve OFF (safe), red when valve ON (open)
        if (features.water !== false) {
        const wl = parseInt(state.water_level) || 0;
        const wlEl = document.getElementById('water-level');
        wlEl.textContent = wl + ' cm';
        wlEl.className = 'water-indicator ' + (state.water_valve ? 'low' : 'ok');
        document.getElementById('water-valve').className = 'toggle-btn ' + (state.water_valve ? 'on' : 'off');
        document.getElementById('pump-switch').className = 'toggle-btn ' + (state.pump_switch ? 'on' : 'off');
        }
        
        // Dishwasher - show only when running
        if (features.dishwasher !== false && state.dishwasher_running) {
            document.getElementById('dishwasher-section').style.display = '';
            const duration = state.dishwasher_duration || 0;
            document.getElementById('dishwasher-duration').textContent = formatDuration(duration);
        } else {
            document.getElementById('dishwasher-section').style.display = 'none';
        }
        
        // Washer - show only when time remaining
        const washerTime = parseFloat(state.washer_time) || 0;
        if (features.washer !== false && washerTime > 0) {
            document.getElementById('washer-section').style.display = '';
            document.getElementById('washer-time').textContent = formatDuration(washerTime);
        } else {
            document.getElementById('washer-section').style.display = 'none';
        }
        
        // Dryer - show only when time remaining
        const dryerTime = parseFloat(state.dryer_time) || 0;
        if (features.dryer !== false && dryerTime > 0) {
            document.getElementById('dryer-section').style.display = '';
            document.getElementById('dryer-time').textContent = formatDuration(dryerTime);
        } else {
            document.getElementById('dryer-section').style.display = 'none';
        }
        
        // Toggles with friendly names
        const toggleNames = {
            'set_limit_to_ev_charger': 'LIMIT TO EV',
            'do_not_supply_charger': 'DO NOT SUPPLY EV',
            'only_charging': 'ONLY CHARGING',
            'no_feed': 'NO FEED',
            'house_support': 'HOUSE SUPPORT',
            'charge_battery': 'CHARGE BATTERY',
            'minimize_charging': 'MINIMIZE CHARGING'
        };
        let togglesHtml = '';
        const bools = state.booleans || {};
        for (const [key, val] of Object.entries(bools)) {
            const displayName = toggleNames[key] || key.replace(/_/g, ' ').toUpperCase();
            togglesHtml += `<div class="toggle-btn ${val ? 'on' : 'off'}" onclick="toggle('input_boolean.${key}')">${displayName}</div>`;
        }
        document.getElementById('toggles').innerHTML = togglesHtml;
        
        // Loads (sorted by value, exclude solar_shed)
        if (features.ha_loads !== false) {
        const loads = state.loads || {};
        const hiddenLoads = ['solar_shed'];
        const sortedLoads = Object.entries(loads)
            .filter(([name, val]) => val > 10 && !hiddenLoads.includes(name))
            .sort((a, b) => b[1] - a[1]);
        let loadsHtml = '<div class="loads-table">' + sortedLoads.map(([name, val]) => 
            `<div class="loads-row"><span class="loads-name">${name}</span><span class="loads-value">${Math.floor(val)}W</span></div>`
        ).join('') + '</div>';
        document.getElementById('loads').innerHTML = sortedLoads.length ? loadsHtml : '<div class="text-muted">No active loads</div>';
        }
        
        // HA Status with relative time
        if (features.ha === false) {
            document.getElementById('ha-status').className = 'status-dot offline';
            document.getElementById('ha-status-text').textContent = 'HA: Disabled';
        } else {
            document.getElementById('ha-status').className = 'status-dot ' + (state.ha_connected ? 'online' : 'offline');
            document.getElementById('ha-status-text').textContent = 'HA: ' + (state.ha_connected ? 'Connected' : 'Disconnected');
        }
        
        // Update relative time
        window.lastUpdateTime = Date.now();
        updateRelativeTime();
        
        // Uptime display
        const uptime = state.uptime || 0;
        document.getElementById('uptime').textContent = 'Uptime: ' + formatUptime(uptime);
        
        // Dry-run status
        updateDryRunBtn(state.dry_run);
        
        // ESS mode
        updateEssModeBtn(state.ess_mode);
        
        // Power limits
        const lim = state.limits || {min: -2300, max: 2250};
        document.getElementById('limits-display').textContent = `[${lim.min}, +${lim.max}]`;
        if (!document.activeElement || document.activeElement.id !== 'limit-min')
            document.getElementById('limit-min').value = lim.min;
        if (!document.activeElement || document.activeElement.id !== 'limit-max')
            document.getElementById('limit-max').value = lim.max;
        
        // Loop interval
        if (!document.activeElement || document.activeElement.id !== 'loop-interval')
            document.getElementById('loop-interval').value = (state.loop_interval || 0.33).toFixed(2);
        
        // Console
        document.getElementById('console').innerHTML = console_lines.map(l => `<div>${l}</div>`).join('');
        document.getElementById('console').scrollTop = 99999;
        
        // Chart
        if (hist.timestamps && hist.timestamps.length > 0) {
            const labels = hist.timestamps.map(t => '');
            chart.data.labels = labels;
            chart.data.datasets[0].data = hist.grid;
            chart.data.datasets[1].data = hist.solar;
            chart.data.datasets[2].data = hist.battery;
            chart.data.datasets[3].data = hist.setpoint;
            chart.update('none');
        }
    } catch(e) {
        console.error('Update failed:', e);
    }
}

// Format uptime (seconds) to human readable
function formatUptime(seconds) {
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    if (seconds < 86400) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return h + 'h ' + m + 'm';
    }
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    return d + 'd ' + h + 'h';
}

// Relative time for last update
window.lastUpdateTime = Date.now();
function updateRelativeTime() {
    const elapsed = Math.floor((Date.now() - window.lastUpdateTime) / 1000);
    let text;
    if (elapsed < 60) {
        text = elapsed + 's ago';
    } else {
        text = Math.floor(elapsed / 60) + 'm ' + (elapsed % 60) + 's ago';
    }
    document.getElementById('last-update').textContent = text;
}

initChart();
setInterval(updateData, 1500);
setInterval(updateRelativeTime, 1000);
updateData();
</script>
</body>
</html>'''


_server_instance = None

def start_web_server(
    get_state: Callable[[], Dict[str, Any]],
    set_setpoint: Callable[[int], bool],
    toggle_dry_run: Callable[[], bool],
    set_limits: Callable[[int, int], Dict[str, int]],
    toggle_ess: Callable[[], Dict[str, Any]],
    set_loop_interval: Callable[[float], float],
    ha: Any,
    host: str = '0.0.0.0',
    port: int = 8080,
    ssl_cert: str = None,
    ssl_key: str = None
):
    """Start the web server in a background thread
    
    If ssl_cert and ssl_key are provided, starts HTTPS server.
    """
    global state_getter, setpoint_setter, dry_run_toggler, limits_setter, ess_mode_toggler, loop_interval_setter, ha_client, _server_instance
    state_getter = get_state
    setpoint_setter = set_setpoint
    dry_run_toggler = toggle_dry_run
    limits_setter = set_limits
    ess_mode_toggler = toggle_ess
    loop_interval_setter = set_loop_interval
    ha_client = ha
    
    server = HTTPServer((host, port), DashboardHandler)
    server.timeout = 1  # Allow periodic checks for shutdown
    _server_instance = server
    
    # Wrap with SSL if certificate provided
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ssl_cert, ssl_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"  Web server: https://{host}:{port} (SSL)")
    else:
        print(f"  Web server: http://{host}:{port}")
    
    def server_thread():
        try:
            server.serve_forever()
        except Exception as e:
            logger.error(f"Web server thread crashed: {e}")
    
    thread = threading.Thread(target=server_thread, daemon=True)
    thread.start()
    return server


def stop_web_server():
    """Gracefully stop the web server"""
    global _server_instance
    if _server_instance:
        try:
            _server_instance.shutdown()
        except Exception:
            pass
        _server_instance = None
