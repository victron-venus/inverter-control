#!/bin/sh
#
# Deployment keepalive - maintains the last grid setpoint on the inverter
# while the main service is stopped during an update.
#
# Usage:
#   keepalive.sh start   - start the keepalive daemon (background)
#   keepalive.sh stop    - stop a running daemon
#   keepalive.sh status  - check if daemon is running
#
# The daemon reads the current setpoint from D-Bus and re-writes it every
# second, preventing the inverter from falling into passthrough mode (which
# also kills MPPT generation). It exits automatically when the main service
# is detected running again, or after TIMEOUT seconds as a safety net.

set -eu

HEARTBEAT_FILE="/run/inverter-control/heartbeat"
PID_FILE="/run/inverter-control/keepalive.pid"
STATE_DIR="/run/inverter-control"
INTERVAL=1
TIMEOUT=120

discover_vebus() {
    dbus -y 2>/dev/null | while IFS= read -r line; do
        case "$line" in
            *com.victronenergy.vebus*) echo "$line"; return ;;
        esac
    done
}

read_setpoint() {
    dbus-send --system --print-reply=literal \
        --dest="$1" \
        /Hub4/L1/AcPowerSetpoint \
        com.victronenergy.BusItem.GetValue 2>/dev/null \
    | grep -E '^\s+int16 ' \
    | awk '{print $2}'
}

write_setpoint() {
    dbus-send --system --type=method_call \
        --dest="$1" \
        /Hub4/L1/AcPowerSetpoint \
        com.victronenergy.BusItem.SetValue \
        "variant:int16:$2" >/dev/null 2>&1
}

is_main_running() {
    [ -f "$HEARTBEAT_FILE" ]
}

# ---------------------------------------------------------------------------
# Hidden daemon mode (--daemon): this is the actual keepalive loop, forked
# from the start command. It lives until the main service is detected or
# TIMEOUT expires.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--daemon" ]; then
    VEBUS="$2"
    SETPOINT="$3"
    elapsed=0
    while [ "$elapsed" -lt "$TIMEOUT" ]; do
        if is_main_running; then
            rm -f "$PID_FILE"
            exit 0
        fi
        write_setpoint "$VEBUS" "$SETPOINT"
        sleep "$INTERVAL"
        elapsed=$((elapsed + INTERVAL))
    done
    rm -f "$PID_FILE"
    exit 0
fi

# ---------------------------------------------------------------------------
# Public commands
# ---------------------------------------------------------------------------
do_start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[keepalive] already running (pid $(cat "$PID_FILE"))"
        return 0
    fi

    mkdir -p "$STATE_DIR"

    VEBUS="$(discover_vebus)" || true
    if [ -z "$VEBUS" ]; then
        echo "[keepalive] WARNING: vebus service not found, skipping"
        return 0
    fi

    SETPOINT="$(read_setpoint "$VEBUS")" || true
    if [ -z "$SETPOINT" ]; then
        echo "[keepalive] WARNING: could not read setpoint, skipping"
        return 0
    fi

    echo "[keepalive] vebus=$VEBUS setpoint=${SETPOINT}W"

    SCRIPT="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    nohup sh -c "\"$SCRIPT\" --daemon '$VEBUS' '$SETPOINT'" \
        >/dev/null 2>&1 &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$PID_FILE"
    echo "[keepalive] started (pid $DAEMON_PID)"
}

do_stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "[keepalive] not running"
        return 0
    fi
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null && echo "[keepalive] stopped (pid $PID)"
    else
        echo "[keepalive] stale pid $PID, cleaning up"
    fi
    rm -f "$PID_FILE"
}

do_status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[keepalive] running (pid $(cat "$PID_FILE"))"
        return 0
    fi
    echo "[keepalive] not running"
    return 1
}

case "${1:-}" in
    start)  do_start  ;;
    stop)   do_stop   ;;
    status) do_status ;;
    *)
        echo "Usage: $0 {start|stop|status}" >&2
        exit 1
        ;;
esac
