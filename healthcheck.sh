#!/bin/sh
#
# Health check watchdog for inverter-control
# Runs as separate daemontools service, restarts main service if unresponsive
#

SERVICE="inverter-control"
CHECK_URL="https://127.0.0.1:8080/api/state"
CHECK_INTERVAL=30
FAIL_THRESHOLD=3
LOG_FILE="/var/log/inverter-control.log"
fail_count=0

log() {
    msg="$(date '+%Y-%m-%d %H:%M:%S') [HEALTHCHECK] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

while true; do
    # Check if web server responds
    if wget -q -O /dev/null --timeout=5 "$CHECK_URL" 2>/dev/null; then
        if [ $fail_count -gt 0 ]; then
            log "OK: Web server recovered after $fail_count failures"
        fi
        fail_count=0
    else
        fail_count=$((fail_count + 1))
        log "FAIL: Web server not responding ($fail_count/$FAIL_THRESHOLD)"
        
        if [ $fail_count -ge $FAIL_THRESHOLD ]; then
            log "RESTART: Restarting $SERVICE after $fail_count consecutive failures"
            
            # Kill any zombie processes holding port 8080
            pkill -9 -f "python3 main.py" 2>/dev/null || true
            sleep 2
            
            # Restart the service
            svc -t /service/$SERVICE
            
            fail_count=0
            sleep 10  # Wait for service to start
        fi
    fi
    
    sleep $CHECK_INTERVAL
done
