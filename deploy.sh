#!/bin/bash
#
# Deploy Inverter Control to Venus OS
#
# Prerequisites:
#   - SSH config with host 'Cerbo' pointing to Venus OS device
#   - SSH key authentication configured
#
# Usage: ./deploy.sh [SSH_HOST]
#

set -e

SSH_HOST="${1:-Cerbo}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/data/inverter-control"
SETUP_OPTIONS_DIR="/data/setupOptions/inverter-control"
SEPARATOR="=============================================="

echo "$SEPARATOR"
echo "  Deploying Inverter Control to Venus OS"
echo "$SEPARATOR"
echo "SSH Host: $SSH_HOST"
echo ""

# Check local syntax before copying
echo ">>> Checking Python syntax..."
python3 -m py_compile \
    "$SCRIPT_DIR/main.py" \
    "$SCRIPT_DIR/inverter_control/__init__.py" \
    "$SCRIPT_DIR/inverter_control/config.py" \
    "$SCRIPT_DIR/inverter_control/victron.py" \
    "$SCRIPT_DIR/inverter_control/homeassistant.py" \
    "$SCRIPT_DIR/inverter_control/mqtt_bridge.py" \
    "$SCRIPT_DIR/inverter_control/ui_config.py" \
    "$SCRIPT_DIR/inverter_control/keepalive.py" \
    "$SCRIPT_DIR/inverter_control/console_server.py" \
    "$SCRIPT_DIR/inverter_control/console_ui.py" \
    "$SCRIPT_DIR/inverter_control/logic.py" \
    "$SCRIPT_DIR/inverter_control/log-forwarder.py"
echo "    Syntax OK"

# Create directories on remote
echo ">>> Creating directories..."
ssh "$SSH_HOST" "mkdir -p $INSTALL_DIR/inverter_control $SETUP_OPTIONS_DIR"

# Copy Python files
echo ">>> Copying files..."
scp -q "$SCRIPT_DIR/main.py" "$SSH_HOST:$INSTALL_DIR/"
scp -qr "$SCRIPT_DIR/inverter_control/"* "$SSH_HOST:$INSTALL_DIR/inverter_control/"

# Copy setup and gitHubInfo for PackageManager discovery
echo ">>> Copying setup files..."
scp -q "$SCRIPT_DIR/setup" "$SSH_HOST:$INSTALL_DIR/"
ssh "$SSH_HOST" "chmod +x $INSTALL_DIR/setup"
scp -q "$SCRIPT_DIR/gitHubInfo" "$SSH_HOST:$INSTALL_DIR/"

# Copy log-forwarder service
echo ">>> Setting up log-forwarder service..."
ssh "$SSH_HOST" "mkdir -p $INSTALL_DIR/service/log-forwarder"
scp -q "$SCRIPT_DIR/service/log-forwarder/run" "$SSH_HOST:$INSTALL_DIR/service/log-forwarder/"
ssh "$SSH_HOST" "chmod +x $INSTALL_DIR/service/log-forwarder/run"
ssh "$SSH_HOST" "ln -sf $INSTALL_DIR/service/log-forwarder /service/ 2>/dev/null || true"

# Copy secrets.py if exists
if [[ -f "$SCRIPT_DIR/secrets.py" ]]; then
    echo ">>> Copying secrets.py..."
    scp -q "$SCRIPT_DIR/secrets.py" "$SSH_HOST:$INSTALL_DIR/"
    scp -q "$SCRIPT_DIR/secrets.py" "$SSH_HOST:$SETUP_OPTIONS_DIR/"
fi

# Copy version file
if [[ -f "$SCRIPT_DIR/version" ]]; then
    scp -q "$SCRIPT_DIR/version" "$SSH_HOST:$INSTALL_DIR/"
fi

# Restart service
echo ">>> Restarting service..."
ssh "$SSH_HOST" "svc -t /service/inverter-control 2>/dev/null || true"

# Wait and check status
sleep 2
echo ">>> Service status:"
ssh "$SSH_HOST" "svstat /service/inverter-control"

echo ""
echo "$SEPARATOR"
echo "  Deployment Complete!"
echo "$SEPARATOR"
