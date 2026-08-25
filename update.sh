#!/bin/sh
#
# inverter-control self-update script.
#
# Ships inside the release tarball and runs ON the Venus OS device to install
# the release into INSTALL_DIR (default /data/inverter-control). It is invoked
# by the auto-deploy webhook (../inverter-monitoring) or manually:
#
#     sh update.sh [INSTALL_DIR]
#
# This script owns all layout knowledge (runtime files, daemontools services,
# /service symlinks, device-local file preservation, restart order) so that
# callers like the webhook never need to hardcode where files go. Adding a new
# module or a new daemontools service requires a change here only.

set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${1:-/data/inverter-control}"

# Device-local files that must never be overwritten by an update.
LOCAL_ONLY="local_config.py ui_config_local.py inverter-control.crt inverter-control.key"

# Runtime items shipped at the repo root and installed at INSTALL_DIR root.
RUNTIME_ITEMS="main.py inverter_control version gitHubInfo setup"

# Historical flat-file leftovers from older layouts that are now dead code
# (all of these live in the inverter_control/ package since 1.17).
STALE_TOP_LEVEL="config.py console_server.py console_ui.py homeassistant.py keepalive.py logic.py log-forwarder.py mqtt_bridge.py server.py ui_config.py victron.py"

sep() { echo "=== inverter-control update: $*"; }

# 1. Stop the services BEFORE touching files so a half-written tree is never
#    executed and the multilog log dir is not disturbed under a running logger.
for svc in /service/inverter-control/log /service/inverter-control /service/log-forwarder /service/watchdog; do
    [ -e "$svc" ] && svc -dk "$svc" 2>/dev/null || true
done
sleep 1

# 1a. Hold the grid setpoint while we install. The controller is now down;
#     without this the inverter drifts into passthrough mode within seconds,
#     which also kills MPPT generation. The daemon re-reads the current
#     setpoint and re-writes it every second, clears any stale heartbeat on
#     start, and exits by itself once the new instance writes a fresh
#     heartbeat (or after its TIMEOUT net).
#     Never fatal: a keepalive failure must not abort the update.
sh "$SRC_DIR/keepalive.sh" start || true

# 1c. Reap stale daemontools supervise processes left behind by earlier
#     updates. Every time a service dir under $INSTALL_DIR/service is replaced
#     the inode changes, so svscan spawns a NEW supervise and the old one is
#     never killed - they linger forever with "(deleted)" cwd. Several
#     supervisors on one service corrupt runit state (broken log pipes that
#     crash print() with EPIPE, and down services that svc -u cannot bring up).
#     The same inode churn also orphans the run processes themselves (main.py
#     / log_forwarder.py, cwd == $INSTALL_DIR): when a supervise dies, svc -dk
#     can no longer reach its child, so it keeps running the old code and
#     hammering D-Bus next to the new instance. Drop the /service symlinks
#     first so svscan does not respawn supervisors while we replace the dirs
#     below, then kill anything whose cwd lives under our install tree. Fresh
#     supervisors are spawned in step 6.
rm -f /service/inverter-control /service/log-forwarder /service/watchdog
sleep 2
for pid in /proc/[0-9]*; do
    cwd=$(readlink "$pid/cwd" 2>/dev/null) || continue
    case "$cwd" in
        "$INSTALL_DIR/service/"*)
            kill -9 "${pid##*/}" 2>/dev/null || true
            ;;
        "$INSTALL_DIR")
            kill -9 "${pid##*/}" 2>/dev/null || true
            ;;
        *)
            # Ignore processes outside our install tree
            ;;
    esac
done
sleep 1

mkdir -p "$INSTALL_DIR"
sep "installing from $SRC_DIR into $INSTALL_DIR"

# 1b. Migrate old secrets.py layout if present (legacy from < 1.16).
if [ -f "$INSTALL_DIR/secrets.py" ] && [ ! -f "$INSTALL_DIR/local_config.py" ]; then
    mv "$INSTALL_DIR/secrets.py" "$INSTALL_DIR/local_config.py"
    sep "migrated secrets.py -> local_config.py"
fi

# 2. Back up device-local files so the wholesale copy below can restore them.
TMP_BACKUP="/tmp/inverter-control-update-$$"
mkdir -p "$TMP_BACKUP"
for f in $LOCAL_ONLY; do
    [ -f "$INSTALL_DIR/$f" ] && cp -p "$INSTALL_DIR/$f" "$TMP_BACKUP/"
done

# 3. Install runtime items (replace wholesale to also drop stale files).
for item in $RUNTIME_ITEMS; do
    if [ -e "$SRC_DIR/$item" ]; then
        rm -rf "$INSTALL_DIR/$item"
        cp -a "$SRC_DIR/$item" "$INSTALL_DIR/$item"
    fi
done

# 4. Install daemontools services: every dir under service/ maps to
#    INSTALL_DIR/service/. New services are picked up automatically.
mkdir -p "$INSTALL_DIR/service"
for svc in "$SRC_DIR/service"/*; do
    [ -d "$svc" ] || continue
    name="$(basename "$svc")"
    rm -rf "$INSTALL_DIR/service/$name"
    cp -a "$svc" "$INSTALL_DIR/service/$name"
    find "$INSTALL_DIR/service/$name" -type f -name run -exec chmod +x {} \; 2>/dev/null || true
done

# 5. Restore device-local files and drop stale flat-file leftovers.
for f in $LOCAL_ONLY; do
    [ -f "$TMP_BACKUP/$f" ] && cp -p "$TMP_BACKUP/$f" "$INSTALL_DIR/$f"
done
rm -rf "$TMP_BACKUP"
for f in $STALE_TOP_LEVEL; do
    rm -f "$INSTALL_DIR/$f"
done

# 5b. Optional: push the developer's local_config.py instead of keeping the
#     device copy (used by deploy.sh, where the dev machine is authoritative).
if [ "${PUSH_LOCAL_CONFIG:-0}" = "1" ] && [ -f "$SRC_DIR/local_config.py" ]; then
    SETUP_OPTIONS_DIR="/data/setupOptions/inverter-control"
    mkdir -p "$SETUP_OPTIONS_DIR"
    cp -p "$SRC_DIR/local_config.py" "$INSTALL_DIR/local_config.py"
    cp -p "$SRC_DIR/local_config.py" "$SETUP_OPTIONS_DIR/local_config.py"
    sep "pushed local_config.py (PUSH_LOCAL_CONFIG=1)"
fi

# 6. Refresh /service symlinks.
ln -sf "$INSTALL_DIR/service/inverter-control" /service/
ln -sf "$INSTALL_DIR/service/log-forwarder" /service/
ln -sf "$INSTALL_DIR/service/watchdog" /service/

# 6b. Give svscan a moment to spawn fresh supervisors for the new symlinks
#     before we try to bring the services up, so svc -u lands on a live one.
sleep 3

# 7. Let PackageManager rediscover the package (version/gitHubInfo changed).
svc -t /service/PackageManager 2>/dev/null || true

# 8. Bring everything back up (svc -d only marks down; svc -u starts).
for svc in /service/inverter-control/log /service/inverter-control /service/log-forwarder /service/watchdog; do
    [ -e "$svc" ] && svc -u "$svc" 2>/dev/null || true
done

# 9. Stop the keepalive if it somehow survived (it normally exits by itself
#    once the new instance writes its first heartbeat).
sh "$SRC_DIR/keepalive.sh" stop || true

sep "installed version $(cat "$INSTALL_DIR/version" 2>/dev/null || echo unknown)"
