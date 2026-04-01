#!/bin/bash
#
# Deploy Inverter Control to Venus OS
#
# Prerequisites:
#   - SSH config with host 'Cerbo' pointing to Venus OS device
#   - SSH key authentication configured
#
# Usage: ./deploy.sh [SSH_HOST] [--full]
#   --full: Run full install (create services). Default is quick update.
#

set -e

SSH_HOST="${1:-Cerbo}"
FULL_INSTALL=false
[[ "$2" == "--full" ]] && FULL_INSTALL=true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/data/inverter_control"

echo "=============================================="
echo "  Deploying Inverter Control to Venus OS"
echo "=============================================="
echo "SSH Host: $SSH_HOST"
echo "Mode: $([ "$FULL_INSTALL" = true ] && echo "Full install" || echo "Quick update")"
echo ""

# Check local syntax before copying
echo ">>> Checking Python syntax..."
python3 -m py_compile "$SCRIPT_DIR/main.py" "$SCRIPT_DIR/config.py" "$SCRIPT_DIR/victron.py" "$SCRIPT_DIR/homeassistant.py" "$SCRIPT_DIR/web/server.py"
echo "    Syntax OK"

# Create directories on remote
ssh "$SSH_HOST" "mkdir -p $REMOTE_DIR/web"

# Copy all files in parallel using tar (faster than multiple scp)
echo ">>> Copying files..."
tar -cf - -C "$SCRIPT_DIR" \
    config.py main.py victron.py homeassistant.py install.sh healthcheck.sh keepalive.py \
    web/__init__.py web/server.py \
    $([ -f "$SCRIPT_DIR/secrets.py" ] && echo "secrets.py") \
    2>/dev/null | ssh "$SSH_HOST" "tar -xf - -C $REMOTE_DIR"

if [ "$FULL_INSTALL" = true ]; then
    # Full install: create/update services
    echo ">>> Running full install..."
    ssh "$SSH_HOST" "chmod +x $REMOTE_DIR/main.py $REMOTE_DIR/install.sh && cd $REMOTE_DIR && ./install.sh"
else
    # Quick update: just restart the service
    # Note: keepalive removed - it was causing issues and the restart is fast enough
    echo ">>> Restarting service..."
    ssh "$SSH_HOST" "svc -t /service/inverter-control 2>/dev/null || true"
    ssh "$SSH_HOST" "svc -t /service/inverter-healthcheck 2>/dev/null || true"
fi

# Wait for service to come up
echo ">>> Waiting for service..."
sleep 2

# Verify service is running and responding
if ssh "$SSH_HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/state" | grep -q "200"; then
    echo "    Service OK"
else
    echo "    Warning: Service may still be starting..."
fi

ssh "$SSH_HOST" "svstat /service/inverter-control"

echo ""
echo "=============================================="
echo "  Deployment Complete!"
echo "=============================================="
echo ""
echo "Quick deploy (code only):  ./deploy.sh"
echo "Full install (services):   ./deploy.sh Cerbo --full"
echo ""
