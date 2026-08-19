#!/bin/bash
#
# Deploy Inverter Control to Venus OS
#
# Packs the local repository (minus VCS/CI/cache cruft), streams it to the
# device and runs the repo's own self-update script (update.sh) there, so all
# install logic lives in exactly one place - the same path the auto-deploy
# webhook uses for release tarballs.
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
DEPLOY_DIR="/data/.inverter-control-deploy"
SEPARATOR="=============================================="

echo "$SEPARATOR"
echo "  Deploying Inverter Control to Venus OS"
echo "$SEPARATOR"
echo "SSH Host: $SSH_HOST"
echo ""

# Check local syntax before shipping (fail fast on the dev machine)
echo ">>> Checking Python syntax..."
python3 -m py_compile "$SCRIPT_DIR/main.py" "$SCRIPT_DIR"/inverter_control/*.py
echo "    Syntax OK"

# Package the repo and run update.sh on the device. `set -e` on the remote
# aborts the whole chain if update.sh fails, so the deploy is atomic-ish.
#
# Before the update we start a keepalive daemon that re-writes the last
# grid setpoint every second.  This prevents the inverter from dropping
# into passthrough mode (which also kills MPPT generation) during the
# window when the main service is stopped.
echo ">>> Streaming repository to $SSH_HOST and running update.sh..."
tar \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='.coverage' \
    --exclude='logs' \
    --exclude='*.egg-info' \
    --exclude='.venv' \
    --exclude='.mcp.json' \
    -czf - -C "$SCRIPT_DIR" . \
    | ssh "$SSH_HOST" "set -e; rm -rf $DEPLOY_DIR; mkdir -p $DEPLOY_DIR; \
        tar -xz -C $DEPLOY_DIR --strip-components=1; \
        sh $DEPLOY_DIR/keepalive.sh start; \
        PUSH_LOCAL_CONFIG=1 sh $DEPLOY_DIR/update.sh; \
        waited=0; while [ \$waited -lt 15 ] && ! [ -f /run/inverter-control/heartbeat ]; do sleep 1; waited=\$((waited + 1)); done; \
        sh $DEPLOY_DIR/keepalive.sh stop; \
        rm -rf $DEPLOY_DIR"

# Wait for supervise to bring the service back up (svc -u is async)
echo ">>> Service status:"
for i in $(seq 1 15); do
    sleep 1
    STATUS="$(ssh "$SSH_HOST" "svstat /service/inverter-control 2>&1")" \
        && printf '%s\n' "$STATUS" && break
    [ "$i" = "15" ] && echo "$STATUS" && exit 1
done

echo ""
echo "$SEPARATOR"
echo "  Deployment Complete!"
echo "$SEPARATOR"
