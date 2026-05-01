#!/bin/bash
#
# Post-installation script for inverter-control
# Run this AFTER SetupHelper installation to copy secrets.py
#
# Usage: ./postinstall.sh
#

set -e

SSH_HOST="Cerbo"
INSTALL_DIR="/data/inverter-control"
SETUP_OPTIONS_DIR="/data/setupOptions/inverter-control"
LOCAL_SECRETS="secrets.py"

echo "=== inverter-control post-install ==="

if [ ! -f "$LOCAL_SECRETS" ]; then
    echo "Error: $LOCAL_SECRETS not found"
    echo "Create it from secrets.example.py first"
    exit 1
fi

echo "Creating directories..."
ssh "$SSH_HOST" "mkdir -p $SETUP_OPTIONS_DIR"

echo "Copying secrets.py to Cerbo..."
# Copy to setupOptions (survives reinstall)
scp "$LOCAL_SECRETS" "$SSH_HOST:$SETUP_OPTIONS_DIR/secrets.py"
# Copy to install dir (used by running service)
scp "$LOCAL_SECRETS" "$SSH_HOST:$INSTALL_DIR/secrets.py"

echo "Restarting inverter-control service..."
ssh "$SSH_HOST" "svc -t /service/inverter-control 2>/dev/null || true"

echo ""
echo "=== Done! ==="
echo "secrets.py copied to:"
echo "  - $SSH_HOST:$SETUP_OPTIONS_DIR/ (survives reinstall)"
echo "  - $SSH_HOST:$INSTALL_DIR/ (active)"
