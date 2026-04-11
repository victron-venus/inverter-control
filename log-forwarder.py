#!/usr/bin/env python3
"""
Log forwarder for Cerbo GX to Loki.

Reads multilog directories and forwards logs to Loki via HTTP push API.
Designed for Venus OS with minimal dependencies.
"""

import json
import os
import time
import sys

try:
    import requests
    USE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    USE_REQUESTS = False

# Configuration
LOKI_URL = "http://192.168.167.25:3100/loki/api/v1/push"
STATE_FILE = "/tmp/log-forwarder-state.json"
POLL_INTERVAL = 5  # seconds
BATCH_SIZE = 100   # max lines per push
JOB_LABEL = "cerbo"

# Log sources: service_name -> log file path
LOG_SOURCES = {
    "inverter-control": "/var/log/inverter-control/current",
    "dbus-mqtt-chain1": "/var/log/dbus-mqtt-chain1/current",
    "dbus-mqtt-chain2": "/var/log/dbus-mqtt-chain2/current",
    "dbus-virtual-chain": "/var/log/dbus-virtual-chain/current",
}


def load_state():
    """Load file positions from state file."""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def save_state(state):
    """Save file positions to state file."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except IOError as e:
        print(f"Warning: Could not save state: {e}", file=sys.stderr)


def parse_multilog_timestamp(line):
    """
    Parse multilog @timestamp format.
    
    Multilog timestamps are in TAI64N format: @4000000067890abcdef12345
    The @ prefix followed by 24 hex characters.
    
    Returns (timestamp_ns, message) or (None, line) if no timestamp.
    """
    if not line.startswith('@') or len(line) < 25:
        return None, line
    
    try:
        # TAI64N: 8 bytes seconds + 4 bytes nanoseconds = 24 hex chars
        tai64n_hex = line[1:25]
        tai64_secs = int(tai64n_hex[:16], 16)
        nanosecs = int(tai64n_hex[16:24], 16)
        
        # TAI64 epoch is 2^62 seconds before Unix epoch
        # Unix timestamp = TAI64 - 2^62 - 10 (TAI-UTC offset, approximate)
        unix_secs = tai64_secs - (1 << 62) - 10
        timestamp_ns = unix_secs * 1_000_000_000 + nanosecs
        
        # Message is everything after the timestamp and space
        message = line[26:] if len(line) > 26 else ""
        return timestamp_ns, message
    except (ValueError, IndexError):
        return None, line


def read_new_lines(filepath, position, inode):
    """
    Read new lines from a file starting at position.
    
    Handles file rotation by checking inode.
    Returns (lines, new_position, new_inode).
    """
    lines = []
    new_position = position
    new_inode = inode
    
    try:
        stat = os.stat(filepath)
        new_inode = stat.st_ino
        
        # File rotated (different inode) - start from beginning
        if inode and new_inode != inode:
            new_position = 0
        
        # File truncated - start from beginning
        if stat.st_size < new_position:
            new_position = 0
        
        with open(filepath, 'r', errors='replace') as f:
            f.seek(new_position)
            for line in f:
                line = line.rstrip('\n\r')
                if line:
                    lines.append(line)
                if len(lines) >= BATCH_SIZE:
                    break
            new_position = f.tell()
            
    except (IOError, OSError) as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
    
    return lines, new_position, new_inode


def format_loki_payload(service_name, lines):
    """
    Format lines for Loki push API.
    
    Returns JSON payload for /loki/api/v1/push
    """
    values = []
    now_ns = int(time.time() * 1_000_000_000)
    
    for line in lines:
        timestamp_ns, message = parse_multilog_timestamp(line)
        if timestamp_ns is None:
            timestamp_ns = now_ns
        
        # Loki expects [timestamp_string, log_line]
        values.append([str(timestamp_ns), message])
    
    payload = {
        "streams": [
            {
                "stream": {
                    "job": JOB_LABEL,
                    "service": service_name,
                },
                "values": values
            }
        ]
    }
    
    return payload


def push_to_loki(payload):
    """Push logs to Loki via HTTP."""
    data = json.dumps(payload).encode('utf-8')
    headers = {"Content-Type": "application/json"}
    
    try:
        if USE_REQUESTS:
            resp = requests.post(LOKI_URL, data=data, headers=headers, timeout=10)
            resp.raise_for_status()
        else:
            req = urllib.request.Request(LOKI_URL, data=data, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    raise Exception(f"HTTP {resp.status}")
        return True
    except Exception as e:
        print(f"Error pushing to Loki: {e}", file=sys.stderr)
        return False


def process_logs():
    """Main processing loop iteration."""
    state = load_state()
    state_changed = False
    
    for service_name, filepath in LOG_SOURCES.items():
        if not os.path.exists(filepath):
            continue
        
        # Get current position and inode from state
        file_state = state.get(service_name, {})
        position = file_state.get('position', 0)
        inode = file_state.get('inode', None)
        
        # Read new lines
        lines, new_position, new_inode = read_new_lines(filepath, position, inode)
        
        if lines:
            payload = format_loki_payload(service_name, lines)
            if push_to_loki(payload):
                # Update state only on successful push
                state[service_name] = {
                    'position': new_position,
                    'inode': new_inode
                }
                state_changed = True
                print(f"Forwarded {len(lines)} lines from {service_name}")
            else:
                # Keep old position to retry
                print(f"Failed to forward {len(lines)} lines from {service_name}", file=sys.stderr)
        elif new_inode != inode:
            # File rotated but no new content yet
            state[service_name] = {
                'position': new_position,
                'inode': new_inode
            }
            state_changed = True
    
    if state_changed:
        save_state(state)


def main():
    """Main entry point."""
    print(f"Log forwarder starting...")
    print(f"Loki URL: {LOKI_URL}")
    print(f"State file: {STATE_FILE}")
    print(f"Poll interval: {POLL_INTERVAL}s")
    print(f"Monitoring: {', '.join(LOG_SOURCES.keys())}")
    print(f"Using: {'requests' if USE_REQUESTS else 'urllib'}")
    
    while True:
        try:
            process_logs()
        except Exception as e:
            print(f"Error in main loop: {e}", file=sys.stderr)
        
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
