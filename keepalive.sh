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
#
# Note: GetValue replies look like "   variant       int32 -2300" on Venus OS
# (BusyBox), so the parser must accept the variant wrapper and both int16 and
# int32 payload types.

set -eu

HEARTBEAT_FILE="/run/inverter-control/heartbeat"
PID_FILE="/run/inverter-control/keepalive.pid"
LOG_FILE="/run/inverter-control/keepalive.log"
STATE_DIR="/run/inverter-control"
INTERVAL=1
TIMEOUT=120

discover_vebus() {
    dbus -y 2>/dev/null | while IFS= read -r line; do
        case "$line" in
            *com.victronenergy.vebus*) echo "$line"; return ;;
            *) : ;;
        esac
    done
}

read_setpoint() {
    service="$1"
    dbus-send --system --print-reply=literal \
        --dest="$service" \
        /Hub4/L1/AcPowerSetpoint \
        com.victronenergy.BusItem.GetValue 2>/dev/null \
    | awk '$1 == "variant" && ($2 == "int16" || $2 == "int32") { print $3 }'
}

write_setpoint() {
    service="$1"
    value="$2"
    dbus-send --system --type=method_call \
        --dest="$service" \
        /Hub4/L1/AcPowerSetpoint \
        com.victronenergy.BusItem.SetValue \
        "variant:int16:$value" >/dev/null 2>&1
}

# The heartbeat file survives a killed service (nothing removes it), so
# presence alone is not proof the main service is alive - check mtime.
HEARTBEAT_FRESH_SEC=3

is_main_running() {
    [ -f "$HEARTBEAT_FILE" ] || return 1
    now=$(date +%s)
    mtime=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null) || return 1
    [ $((now - mtime)) -lt "$HEARTBEAT_FRESH_SEC" ]
}

# ---------------------------------------------------------------------------
# Hidden daemon mode (--daemon): this is the actual keepalive loop, forked
# from the start command. It lives until the main service is detected or
# TIMEOUT expires.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--daemon" ]; then
    VEBUS="$2"
    elapsed=0
    while [ "$elapsed" -lt "$TIMEOUT" ]; do
        if is_main_running; then
            rm -f "$PID_FILE"
            exit 0
        fi
        # Re-read each tick: keeps the value current even while the main
        # service is still running during the start window, and skips the
        # write cleanly if the read ever fails. Both D-Bus calls are best
        # effort: a transient failure must not kill the daemon (set -eu).
        SETPOINT=$(read_setpoint "$VEBUS") || true
        if [ -n "$SETPOINT" ]; then
            write_setpoint "$VEBUS" "$SETPOINT" || true
        fi
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
    # Fresh log per deployment attempt (old failures would otherwise read
    # as current ones).
    : > "$LOG_FILE"

    VEBUS="$(discover_vebus)" || true
    if [ -z "$VEBUS" ]; then
        echo "[keepalive] WARNING: vebus service not found, skipping"
        return 0
    fi

    SETPOINT="$(read_setpoint "$VEBUS")" || true
    if [ -n "$SETPOINT" ]; then
        echo "[keepalive] vebus=$VEBUS setpoint=${SETPOINT}W"
    else
        # Not fatal: the daemon retries the read every tick.
        echo "[keepalive] vebus=$VEBUS setpoint unreadable yet, daemon will retry"
    fi

    # Drop any leftover heartbeat so the daemon cannot mistake the pre-update
    # service state for "already restarted". If the service is genuinely
    # still alive it rewrites the file within a second and the daemon exits,
    # which is exactly what we want in that case.
    rm -f "$HEARTBEAT_FILE"

    SCRIPT="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    # Run via sh explicitly: the deploy tarball does not guarantee the
    # executable bit survives macOS -> Venus extraction.
    nohup sh "$SCRIPT" --daemon "$VEBUS" >>"$LOG_FILE" 2>&1 &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$PID_FILE"
    # The daemon may legitimately exit instantly (service still alive and
    # rewriting its heartbeat) - don't leave a stale pid file behind.
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "[keepalive] daemon exited immediately (main service already running)"
        return 0
    fi
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
