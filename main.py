#!/usr/bin/env python3
"""
Inverter Control - Main Entry Point
Grid-zero feed-in control for Victron system with split-phase compensation
"""

import argparse
import atexit
import errno
import gc
import logging
import os
import signal
import sys
import threading
import time
import traceback

from inverter_control.console_server import (
    start_server as start_console_server,
)
from inverter_control.console_server import (
    stop_server as stop_console_server,
)

try:
    from inverter_control.mqtt_bridge import MQTT_AVAILABLE, get_mqtt_bridge
except ImportError:
    MQTT_AVAILABLE = False

    def get_mqtt_bridge(*_args, **_kwargs):
        return None


# Re-export from submodules for backward compatibility (tests, external callers)
from inverter_control.controller import (
    VERSION,
    InverterController,
    log_exception,
)

# =============================================================================
# LOGGING SETUP - All errors go to file, debug to stdout
# =============================================================================
LOG_FILE = os.environ.get("INVERTER_CONTROL_LOG_FILE", "/var/log/inverter-control.log")

logger = logging.getLogger("inverter-control")
logger.setLevel(logging.DEBUG)

try:
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.DEBUG)  # Changed to DEBUG to capture debug messages
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(fh)
except Exception as log_err:
    print(f"Warning: Could not create log file: {log_err}", file=sys.stderr)

# Also log to stdout (captured by daemontools/multilog)
sh = logging.StreamHandler(sys.stdout)
sh.setLevel(logging.DEBUG)
sh.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(sh)


class _BrokenPipeSafeStream:
    """Wrap stdout/stderr so a closed log pipe (EPIPE) never crashes the loop.

    Venus OS runs us under daemontools with stdout piped to multilog. If that
    pipe breaks (e.g. the log service is restarted), a plain print() raises
    BrokenPipeError which previously killed the control cycle mid-run.
    Also handles EAGAIN/EWOULDBLOCK when pipe buffer is full to prevent
    blocking the control loop.
    """

    def __init__(self, stream):
        self._stream = stream

    def write(self, data: str) -> int:
        try:
            return self._stream.write(data)
        except OSError as e:
            if isinstance(e, BrokenPipeError) or e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return len(data)
            # Re-raise if it's some other OSError
            raise

    def flush(self) -> None:
        try:
            self._stream.flush()
        except BrokenPipeError:
            pass  # Ignore broken pipe when log service restarts

    def __getattr__(self, name):
        return getattr(self._stream, name)


# Install the safe wrappers before any application code calls print().
if sys.stdout is not None:
    sys.stdout = _BrokenPipeSafeStream(sys.stdout)
if sys.stderr is not None:
    sys.stderr = _BrokenPipeSafeStream(sys.stderr)


# =============================================================================
# ENTRY POINT
# =============================================================================


def main():
    logger.info("=== Inverter Control starting ===")

    try:
        _main_inner()
    except Exception as e:
        log_exception(f"FATAL ERROR in main: {e}")
        raise


def _setup_mqtt_bridge(controller):
    """Set up MQTT bridge and register command callbacks. Returns bridge or None."""
    from inverter_control.config import (  # pylint: disable=import-outside-toplevel
        MQTT_BROKER,
        MQTT_PORT,
        MQTT_TOPIC_PREFIX,
    )

    if not MQTT_AVAILABLE or not MQTT_BROKER:
        return None

    bridge = get_mqtt_bridge(broker=MQTT_BROKER, port=MQTT_PORT, prefix=MQTT_TOPIC_PREFIX)
    if not bridge:
        return None

    bridge.connect()
    bridge.register_callback("toggle", lambda p: controller.ha.toggle_entity(p.get("entity", "")))
    bridge.register_callback("press", lambda p: controller.ha.press_button(p.get("entity", "")))

    def _safe_setpoint(p):
        try:
            val = int(p.get("value", 0))
        except (ValueError, TypeError) as e:
            logger.warning("MQTT setpoint rejected: %s", e)
            return
        controller.set_manual_setpoint(val)

    bridge.register_callback("setpoint", _safe_setpoint)
    bridge.register_callback("dry_run", lambda p: controller.toggle_dry_run())

    def _safe_limits(p):
        try:
            lo = int(p.get("min", -2300))
            hi = int(p.get("max", 2250))
        except (ValueError, TypeError) as e:
            logger.warning("MQTT limits rejected: %s", e)
            return
        controller.set_power_limits(lo, hi)

    bridge.register_callback("limits", _safe_limits)
    bridge.register_callback("ess_mode", lambda p: controller.toggle_ess_mode())

    def _safe_loop_interval(p):
        try:
            val = float(p.get("interval", 0.33))
        except (ValueError, TypeError) as e:
            logger.warning("MQTT loop_interval rejected: %s", e)
            return
        controller.set_loop_interval(val)

    bridge.register_callback("loop_interval", _safe_loop_interval)
    print(f"  MQTT bridge: {MQTT_BROKER}:{MQTT_PORT} (topic: {MQTT_TOPIC_PREFIX}/)")
    return bridge


def _write_heartbeats(heartbeat_file: str, watchdog_heartbeat_file: str) -> None:
    """Write heartbeat files for the internal and external watchdogs."""
    try:
        with open(heartbeat_file, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        with open(watchdog_heartbeat_file, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass  # Ignore if heartbeat fails


HEARTBEAT_INTERVAL = 5.0


def _heartbeat_loop(stop_event, heartbeat_file: str, watchdog_heartbeat_file: str) -> None:
    """Keep the external watchdog's heartbeat fresh from its own thread.

    The external watchdog measures "process is alive". Writing from a
    dedicated ticker (instead of the control cycle) prevents a slow-but-alive
    loop - or CPU starvation during D-Bus storms - from being misread as a
    dead service and triggering the restart kill-loop. True stalls are still
    caught by the in-process HardwareWatchdog, which forces a safe setpoint.
    """
    while not stop_event.wait(HEARTBEAT_INTERVAL):
        _write_heartbeats(heartbeat_file, watchdog_heartbeat_file)


def _next_slot(next_deadline: float, interval: float) -> tuple[float, float]:
    """Deadline-anchored pacing for the control loop.

    Returns (delay_to_sleep, new_deadline). A slow cycle shortens (or skips)
    the sleep instead of pushing every following cycle later; if we stalled
    past a whole slot, realign rather than firing catch-up cycles back to back.
    """
    now = time.monotonic()
    delay = next_deadline - now
    if delay < -interval:
        return 0.0, now + interval
    return max(0.0, delay), next_deadline + interval


def _publish_state(controller, mqtt_bridge) -> None:
    """Publish current state + latest console line over MQTT."""
    if not mqtt_bridge or not mqtt_bridge.connected:
        return
    mqtt_bridge.publish_state(controller.get_state_for_mqtt())
    # Mark MQTT telemetry as fresh for hardware watchdog
    controller._watchdog.mark_mqtt_update()
    # Publish console line if available
    if controller.last_console_line:
        mqtt_bridge.publish_console(controller.last_console_line)


def _maybe_run_gc(last_gc_time: float, gc_interval: float) -> float:
    """Run gc.collect() once the interval has elapsed; return the updated mark."""
    now = time.time()
    if now - last_gc_time <= gc_interval:
        return last_gc_time
    gc.collect()
    return now


def _shutdown_main_loop(controller, mqtt_bridge, hb_stop, hb_thread) -> None:
    """Orderly teardown: heartbeat, filters, watchdog, console, MQTT, HA."""
    hb_stop.set()
    hb_thread.join(timeout=HEARTBEAT_INTERVAL + 2.0)
    if controller.grid_filter:
        controller.grid_filter.stop()
    if controller.derived_grid_filter:
        controller.derived_grid_filter.stop()
    # Stop hardware watchdog
    try:
        controller._watchdog.stop()
    except Exception:
        pass  # Best effort - shutdown must proceed even if watchdog stop fails
    stop_console_server()
    if mqtt_bridge:
        mqtt_bridge.disconnect()
    controller.ha.stop()


def _run_main_loop(controller, mqtt_bridge):
    """Run the main control loop until exit or error."""
    gc_interval = 300
    last_gc_time = time.time()
    # Use /run for runtime files (cleared on reboot, secure)
    heartbeat_dir = "/run/inverter-control"
    heartbeat_file = f"{heartbeat_dir}/heartbeat"
    # Also mirror in the same directory for the external watchdog service
    # (services/watchdog/run) which checks {heartbeat_dir}/inverter-control.heartbeat
    watchdog_heartbeat_file = f"{heartbeat_dir}/inverter-control.heartbeat"

    # Start the hardware watchdog just before entering the loop, so slow
    # startup work above doesn't get mistaken for a stalled control loop.
    controller._watchdog.start()
    if controller.grid_filter:
        controller.grid_filter.start()
    if controller.derived_grid_filter:
        controller.derived_grid_filter.start()

    hb_stop = threading.Event()
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(hb_stop, heartbeat_file, watchdog_heartbeat_file),
        name="heartbeat-writer",
        daemon=True,
    )
    hb_thread.start()
    # Prime the heartbeat immediately so a freshly started service isn't
    # seen as stale by the external watchdog during first-loop warmup.
    _write_heartbeats(heartbeat_file, watchdog_heartbeat_file)

    try:
        os.makedirs(heartbeat_dir, mode=0o755, exist_ok=True)
        next_deadline = time.monotonic() + controller.loop_interval
        while True:
            if not controller.run_cycle():
                logger.info("run_cycle returned False - exiting main loop")
                break
            _publish_state(controller, mqtt_bridge)
            last_gc_time = _maybe_run_gc(last_gc_time, gc_interval)
            delay, next_deadline = _next_slot(next_deadline, controller.loop_interval)
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        logger.info("Shutdown requested (KeyboardInterrupt)")
        print("\nShutting down...")
    finally:
        logger.info("Inverter Control shutting down")
        _shutdown_main_loop(controller, mqtt_bridge, hb_stop, hb_thread)


def _main_inner():
    parser = argparse.ArgumentParser(description="Inverter Control for Victron System")
    parser.add_argument(
        "setpoint",
        type=int,
        nargs="?",
        default=None,
        help="Manual setpoint (one-shot mode)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't actually send commands")
    args = parser.parse_args()

    print(f"=== Inverter Control {VERSION} ===")

    dry_run_mode = args.dry_run or None
    controller = InverterController(dry_run=dry_run_mode)

    mode = "DRY-RUN (safe mode)" if controller.dry_run else "LIVE (sending commands)"
    print(f"Mode: {mode}")

    mqtt_bridge = _setup_mqtt_bridge(controller)

    if args.setpoint is not None:
        controller.manual_setpoint = args.setpoint
        controller.run_cycle()
        return

    start_console_server()
    from inverter_control.prom_metrics import start as start_prom_metrics

    start_prom_metrics()
    print("Starting control loop...")
    print("-" * 80)

    _run_main_loop(controller, mqtt_bridge)


def signal_handler(signum, frame):
    """Log signal and exit"""
    sig_names = {
        signal.SIGTERM: "SIGTERM",
        signal.SIGINT: "SIGINT",
        signal.SIGHUP: "SIGHUP",
    }
    sig_name = sig_names.get(signum, f"signal {signum}")
    logger.warning(f"Received {sig_name} - shutting down")
    # Force-exit watchdog: if graceful shutdown blocks (e.g., full log pipe
    # or stuck MQTT socket), make sure the supervisor can still restart us.
    threading.Timer(10.0, os._exit, args=(0,)).start()
    sys.exit(0)


def excepthook(exc_type, exc_value, exc_tb):
    """Log uncaught exceptions"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.error(
        f"Uncaught exception: {exc_type.__name__}: {exc_value}\n{''.join(traceback.format_tb(exc_tb))}"
    )


def exit_handler():
    """Log on normal exit"""
    logger.info("Process exiting")


if __name__ == "__main__":
    # Install handlers to track exit reasons
    sys.excepthook = excepthook
    atexit.register(exit_handler)

    # Install signal handlers to log shutdown reason
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    main()
