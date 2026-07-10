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
    "$SCRIPT_DIR/config.py" \
    "$SCRIPT_DIR/victron.py" \
    "$SCRIPT_DIR/homeassistant.py" \
    "$SCRIPT_DIR/mqtt_bridge.py" \
    "$SCRIPT_DIR/ui_config.py" \
    "$SCRIPT_DIR/keepalive.py" \
    "$SCRIPT_DIR/console_server.py" \
    "$SCRIPT_DIR/log-forwarder.py"
echo "    Syntax OK"

# Create directories on remote
echo ">>> Creating directories..."
ssh "$SSH_HOST" "mkdir -p $INSTALL_DIR $SETUP_OPTIONS_DIR"

# Copy Python files
echo ">>> Copying files..."
scp -q "$SCRIPT_DIR/main.py" \
       "$SCRIPT_DIR/config.py" \
       "$SCRIPT_DIR/victron.py" \
       "$SCRIPT_DIR/homeassistant.py" \
       "$SCRIPT_DIR/mqtt_bridge.py" \
       "$SCRIPT_DIR/ui_config.py" \
       "$SCRIPT_DIR/keepalive.py" \
       "$SCRIPT_DIR/console_server.py" \
       "$SCRIPT_DIR/log-forwarder.py" \
       "$SSH_HOST:$INSTALL_DIR/"

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
