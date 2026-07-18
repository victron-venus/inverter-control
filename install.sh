#!/bin/bash
#
# Inverter Control Installer for Venus OS
# Creates systemd service that runs in screen
#
# Usage: ./install.sh
#

set -e

INSTALL_DIR="/data/inverter-control"
SERVICE_NAME="inverter-control"
SCREEN_NAME="inverter"
SEPARATOR="=============================================="

echo "$SEPARATOR"
echo "  Inverter Control Installer for Venus OS"
echo "$SEPARATOR"
echo ""

# Create install directory
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/web"

# Copy files if running from source
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
    echo ">>> Copying files to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR/inverter_control"
    cp "$SCRIPT_DIR/main.py" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/inverter_control/"*.py "$INSTALL_DIR/inverter_control/" 2>/dev/null || true
fi

chmod +x "$INSTALL_DIR/main.py"
chmod +x "$INSTALL_DIR/healthcheck.sh" 2>/dev/null || true

# Install required Python packages (pinned for Venus OS stability)
echo ">>> Installing Python dependencies..."
pip3 install "requests>=2.28,<3" "fastapi>=0.100,<1" "uvicorn>=0.20,<1" "msgpack>=1.0,<2" 2>/dev/null || {
    opkg update 2>/dev/null || true
    opkg install python3-requests 2>/dev/null || true
    pip3 install "requests>=2.28,<3" "fastapi>=0.100,<1" "uvicorn>=0.20,<1" "msgpack>=1.0,<2" 2>/dev/null || true
}

# Create wrapper script for screen
echo ">>> Creating screen wrapper..."
cat > "$INSTALL_DIR/run-in-screen.sh" << 'EOF'
#!/bin/bash
SCREEN_NAME="inverter"
INSTALL_DIR="/data/inverter-control"

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
#mkdir -p /service/inverter-healthcheck

#cat > /service/inverter-healthcheck/run << EOF
##!/bin/sh
#exec 2>&1
#sleep 60  # Wait for main service to start
#exec $INSTALL_DIR/healthcheck.sh
#EOF
#chmod +x /service/inverter-healthcheck/run

echo ""
echo "$SEPARATOR"
echo "  Installation Complete!"
echo "$SEPARATOR"
echo ""
echo "Service is starting automatically."
echo ""
echo "Commands:"
echo "  Status:      svstat /service/$SERVICE_NAME"
echo "  Restart:     svc -t /service/$SERVICE_NAME"
echo "  Stop:        svc -d /service/$SERVICE_NAME"
echo "  Error log:   tail -f /var/log/$SERVICE_NAME.log" >&2
echo "  Live view: nc <cerbo-ip> 9999"
echo ""
echo "Web interface: https://<cerbo-ip>:8080"
echo ""

# Show service status
sleep 2
svstat /service/$SERVICE_NAME 2>/dev/null || echo "Service starting..."
