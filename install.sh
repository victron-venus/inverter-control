#!/bin/bash
#
# Inverter Control Installer for Venus OS
# Creates systemd service that runs in screen
#
# Usage: ./install.sh
#

set -e

INSTALL_DIR="/data/inverter_control"
SERVICE_NAME="inverter-control"
SCREEN_NAME="inverter"

echo "=============================================="
echo "  Inverter Control Installer for Venus OS"
echo "=============================================="
echo ""

# Create install directory
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/web"

# Copy files if running from source
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    echo ">>> Copying files to $INSTALL_DIR..."
    cp "$SCRIPT_DIR/config.py" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/main.py" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/victron.py" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/homeassistant.py" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/web/__init__.py" "$INSTALL_DIR/web/" 2>/dev/null || true
    cp "$SCRIPT_DIR/web/server.py" "$INSTALL_DIR/web/" 2>/dev/null || true
fi

chmod +x "$INSTALL_DIR/main.py"
chmod +x "$INSTALL_DIR/healthcheck.sh" 2>/dev/null || true

# Install required Python packages
echo ">>> Installing Python dependencies..."
pip3 install requests 2>/dev/null || opkg install python3-requests 2>/dev/null || true

# Create wrapper script for screen
echo ">>> Creating screen wrapper..."
cat > "$INSTALL_DIR/run-in-screen.sh" << 'EOF'
#!/bin/bash
SCREEN_NAME="inverter"
INSTALL_DIR="/data/inverter_control"

# Check if screen session exists
if screen -list | grep -q "$SCREEN_NAME"; then
    # Create new window in existing session
    screen -S "$SCREEN_NAME" -X screen -t inverter_ctrl
    screen -S "$SCREEN_NAME" -p inverter_ctrl -X stuff "cd $INSTALL_DIR && python3 main.py\n"
else
    # Create new screen session
    screen -dmS "$SCREEN_NAME" -t inverter_ctrl bash -c "cd $INSTALL_DIR && python3 main.py; exec bash"
fi
EOF
chmod +x "$INSTALL_DIR/run-in-screen.sh"

# Create daemontools service
echo ">>> Setting up service..."
mkdir -p /service/$SERVICE_NAME
mkdir -p /var/log

# Create log file
touch /var/log/$SERVICE_NAME.log

cat > /service/$SERVICE_NAME/run << EOF
#!/bin/sh
cd $INSTALL_DIR

# Free port 8080 if occupied by orphan process (but not our own pid)
PORT_PID=\$(fuser 8080/tcp 2>/dev/null)
if [ -n "\$PORT_PID" ]; then
    echo "Port 8080 in use by PID \$PORT_PID, killing..."
    kill -9 \$PORT_PID 2>/dev/null || true
    sleep 1
fi

# Run Python - logging is handled internally to /var/log/$SERVICE_NAME.log
exec python3 -u main.py 2>/dev/null >/dev/null
EOF
chmod +x /service/$SERVICE_NAME/run

# Remove old log service if exists (we use simple file logging now)
rm -rf /service/$SERVICE_NAME/log 2>/dev/null || true

# Create healthcheck service (watchdog)
echo ">>> Setting up healthcheck watchdog..."
mkdir -p /service/inverter-healthcheck

cat > /service/inverter-healthcheck/run << EOF
#!/bin/sh
exec 2>&1
sleep 60  # Wait for main service to start
exec $INSTALL_DIR/healthcheck.sh
EOF
chmod +x /service/inverter-healthcheck/run

# Create helper script to view live output
cat > "$INSTALL_DIR/live.sh" << 'EOF'
#!/bin/sh
# View live output from inverter-control
# Press Ctrl+C to exit (service keeps running)

echo "=== Inverter Control Live Output ==="
echo "Press Ctrl+C to exit (service continues running)"
echo ""

# Stop service, run manually with output visible
svc -d /service/inverter-control
sleep 1
cd /data/inverter_control
python3 -u main.py

# When user presses Ctrl+C, restart service
echo ""
echo "Restarting service..."
svc -u /service/inverter-control
EOF
chmod +x "$INSTALL_DIR/live.sh"

echo ""
echo "=============================================="
echo "  Installation Complete!"
echo "=============================================="
echo ""
echo "Service is starting automatically."
echo ""
echo "Commands:"
echo "  Status:      svstat /service/$SERVICE_NAME"
echo "  Restart:     svc -t /service/$SERVICE_NAME"
echo "  Stop:        svc -d /service/$SERVICE_NAME"
echo "  Error log:   tail -f /var/log/$SERVICE_NAME.log"
echo "  Live view: nc <cerbo-ip> 9999"
echo ""
echo "Web interface: https://<cerbo-ip>:8080"
echo ""

# Show service status
sleep 2
svstat /service/$SERVICE_NAME 2>/dev/null || echo "Service starting..."
