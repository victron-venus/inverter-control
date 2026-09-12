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

# Runtime items shipped at the repo root and installed at INSTALL_DIR root.
RUNTIME_ITEMS="main.py inverter_control version gitHubInfo setup update.sh keepalive.sh local_config.example.py"

# Historical flat-file leftovers from older layouts that are now dead code
# (all of these live in the inverter_control/ package since 1.17).
STALE_TOP_LEVEL="config.py console_server.py console_ui.py homeassistant.py keepalive.py logic.py log-forwarder.py mqtt_bridge.py server.py ui_config.py victron.py"

sep() { echo "=== inverter-control update: $*"; }

# The service launchers use the standard persistent package path.
if [ "$INSTALL_DIR" != /data/inverter-control ]; then
    echo "Unsupported install directory: $INSTALL_DIR (expected /data/inverter-control)" >&2
    exit 1
fi

# Validate the release and dependencies before interrupting the controller.
# compile() avoids generating __pycache__ in a package manager's source tree.
python3 - "$SRC_DIR" <<'CHECK'
import pathlib
import sys
import requests
import paho.mqtt.client
root = pathlib.Path(sys.argv[1])
for source in [root / "main.py", *(root / "inverter_control").glob("*.py")]:
    compile(source.read_bytes(), str(source), "exec")
CHECK
for name in inverter-control log-forwarder watchdog; do
    test -f "$SRC_DIR/service/$name/run"
done

# Record freshness before any downtime; an old heartbeat cannot prove recovery.
STARTED_AT=$(date +%s)

# Stop the watchdog first so it cannot restart the controller during update.
# Keep loggers and service directory inodes alive; only replace shipped scripts.
for name in watchdog log-forwarder inverter-control; do
    [ ! -e "/service/$name" ] || svc -d "/service/$name"
done
sleep 2
for name in watchdog log-forwarder inverter-control; do
    [ ! -e "/service/$name" ] || svc -k "/service/$name" 2>/dev/null || true
done

# 1a. Hold the grid setpoint while we install. The controller is now down;
#     without this the inverter drifts into passthrough mode within seconds,
#     which also kills MPPT generation. The daemon re-reads the current
#     setpoint and re-writes it every second, clears any stale heartbeat on
#     start, and exits by itself once the new instance writes a fresh
#     heartbeat (or after its TIMEOUT net).
#     Never fatal: a keepalive failure must not abort the update.
sh "$SRC_DIR/keepalive.sh" start || true

# Never kill processes based on cwd: an installer or SSH shell can share the
# package directory. Supervisors are kept in place across an ordinary update.

mkdir -p "$INSTALL_DIR"
sep "installing from $SRC_DIR into $INSTALL_DIR"

# 1b. Migrate old secrets.py layout if present (legacy from < 1.16).
if [ -f "$INSTALL_DIR/secrets.py" ] && [ ! -f "$INSTALL_DIR/local_config.py" ]; then
    mv "$INSTALL_DIR/secrets.py" "$INSTALL_DIR/local_config.py"
    sep "migrated secrets.py -> local_config.py"
fi

# 2. Copy release files only when staging differs from installation. Never
# remove files from a source tree while installing that same tree in place.
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
    for item in $RUNTIME_ITEMS; do
        if [ -e "$SRC_DIR/$item" ]; then
            rm -rf "${INSTALL_DIR:?}/$item"
            cp -a "$SRC_DIR/$item" "$INSTALL_DIR/$item"
        fi
    done
fi

# 3. Refresh run scripts without replacing live supervise/ directory inodes.
for name in inverter-control log-forwarder watchdog; do
    mkdir -p "$INSTALL_DIR/service/$name/log" "/var/log/$name"
    for item in run log/run; do
        [ -f "$SRC_DIR/service/$name/$item" ] || continue
        if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
            cp "$SRC_DIR/service/$name/$item" "$INSTALL_DIR/service/$name/$item.new"
            chmod +x "$INSTALL_DIR/service/$name/$item.new"
            mv "$INSTALL_DIR/service/$name/$item.new" "$INSTALL_DIR/service/$name/$item"
        else
            chmod +x "$INSTALL_DIR/service/$name/$item"
        fi
    done
    rm -f "$INSTALL_DIR/service/$name/down" "$INSTALL_DIR/service/$name/log/down"
done

# 4. Preserve device-local configuration; bootstrap it only on first install.
# Local configuration/certificates are absent from RUNTIME_ITEMS and never removed.
if [ ! -f "$INSTALL_DIR/local_config.py" ]; then
    cp "$SRC_DIR/local_config.example.py" "$INSTALL_DIR/local_config.py"
    sep "created local_config.py from example; configure it before enabling control"
fi
for f in $STALE_TOP_LEVEL; do
    rm -f "$INSTALL_DIR/$f"
done

# 5b. Optional: push the developer's local_config.py instead of keeping the
#     device copy (used by deploy.sh, where the dev machine is authoritative).
if [ "${PUSH_LOCAL_CONFIG:-0}" = "1" ] && [ -f "$SRC_DIR/local_config.py" ]; then
    SETUP_OPTIONS_DIR="/data/setupOptions/inverter-control"
    mkdir -p "$SETUP_OPTIONS_DIR"
    if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
        cp -p "$SRC_DIR/local_config.py" "$INSTALL_DIR/local_config.py"
    fi
    cp -p "$SRC_DIR/local_config.py" "$SETUP_OPTIONS_DIR/local_config.py"
    sep "pushed local_config.py (PUSH_LOCAL_CONFIG=1)"
fi

# 6. Refresh links, retiring old supervisors only when the target changes.
for name in inverter-control log-forwarder watchdog; do
    target="$INSTALL_DIR/service/$name"
    if [ -e "/service/$name" ] && { [ ! -L "/service/$name" ] || \
        [ "$(readlink "/service/$name")" != "$target" ]; }; then
        svc -dx "/service/$name" "/service/$name/log" 2>/dev/null || true
        sleep 2
        rm -rf "/service/$name"
    fi
    ln -snf "$target" "/service/$name"
done

# Refresh both historical marker variants and insert before a final exit 0.
RC_LOCAL=/data/rc.local
[ -f "$RC_LOCAL" ] || printf '#!/bin/sh\n' > "$RC_LOCAL"
sed -i '/# === inverter-control.*persistence ===/,/# === end inverter-control ===/d' "$RC_LOCAL"
HOOK=$(mktemp /data/.inverter-control-boot.XXXXXX)
cat > "$HOOK" <<'RCEOF'
# === inverter-control service persistence ===
ln -snf /data/inverter-control/service/inverter-control /service/inverter-control
ln -snf /data/inverter-control/service/log-forwarder /service/log-forwarder
ln -snf /data/inverter-control/service/watchdog /service/watchdog
# === end inverter-control ===
RCEOF
awk -v hook="$HOOK" '
    function insert_hook() { while ((getline line < hook) > 0) print line; close(hook) }
    !inserted && /^[[:space:]]*exit[[:space:]]+0[[:space:]]*$/ { insert_hook(); inserted=1 }
    { print }
    END { if (!inserted) insert_hook() }
' "$RC_LOCAL" > "$RC_LOCAL.inverter-control"
chmod +x "$RC_LOCAL.inverter-control"
mv "$RC_LOCAL.inverter-control" "$RC_LOCAL"
rm -f "$HOOK"

# 6b. Give svscan a moment to spawn fresh supervisors for the new symlinks
#     before we try to bring the services up, so svc -u lands on a live one.
sleep 3

# 8. Bring everything back up (svc -d only marks down; svc -u starts).
for svc in /service/inverter-control/log /service/inverter-control /service/log-forwarder /service/watchdog; do
    [ -e "$svc" ] && svc -u "$svc" 2>/dev/null || true
done

# A successful svc command is not proof that the new process started. Wait
# for a fresh heartbeat before stopping the bounded keepalive helper.
ready=0
attempt=0
while [ "$attempt" -lt 30 ]; do
    heartbeat=$(cat /run/inverter-control/inverter-control.heartbeat 2>/dev/null || true)
    case "$heartbeat" in
        ''|*[!0-9]*) ;;
        *)
            if [ "$heartbeat" -gt "$STARTED_AT" ] &&
                svstat /service/inverter-control 2>/dev/null | grep -q ': up (pid '; then
                ready=1
                break
            fi
            ;;
    esac
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$ready" != 1 ]; then
    echo "Controller did not produce a fresh heartbeat; inspect /var/log/inverter-control/current" >&2
    # The helper has its own bounded timeout; do not stop it on failed startup.
    exit 1
fi
sh "$SRC_DIR/keepalive.sh" stop || true

sep "installed version $(cat "$INSTALL_DIR/version" 2>/dev/null || echo unknown)"
