"""Hardware watchdog for Victron ESS setpoint safety."""

import logging
import threading
import time

logger = logging.getLogger("inverter-control")


class WatchdogTimeoutError(Exception):
    """Raised when a watchdog timeout occurs"""


class HardwareWatchdog:
    """
    Hardware watchdog for Victron ESS setpoint safety.

    Monitors setpoint-write liveness (D-Bus + MQTT). If the control loop stops
    writing grid setpoints for timeout seconds, forces ESS setpoint to 0W
    (pass-through/fallback mode) to prevent uncontrolled grid export/import if
    the control loop stalls or crashes.

    Trigger is based on the actual setpoint writes, not telemetry reads, so a
    slow-but-alive loop is never mistaken for a crash.

    Runs as a daemon thread checking heartbeat every second.
    """

    def __init__(
        self,
        victron,
        timeout_seconds: int = 30,
        check_interval: float = 1.0,
        dry_run: bool = False,
        get_setpoint=None,
    ):
        self.victron = victron
        self.timeout_seconds = timeout_seconds
        self.check_interval = check_interval
        self.dry_run = dry_run
        self._get_setpoint = get_setpoint
        self._last_dbus_update = 0.0
        self._last_mqtt_update = 0.0
        self._last_setpoint_update = 0.0
        self._enabled = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_external: bool | None = None
        self._pre_forced_setpoint: int = 0
        self._lock = threading.Lock()

    def mark_dbus_update(self):
        """Call when D-Bus telemetry is successfully read"""
        with self._lock:
            self._last_dbus_update = time.time()

    def mark_mqtt_update(self):
        """Call when MQTT state is successfully published"""
        with self._lock:
            self._last_mqtt_update = time.time()

    def mark_setpoint_update(self):
        """Call every time a grid setpoint is written to the inverter"""
        with self._lock:
            self._last_setpoint_update = time.time()

    def start(self):
        """Start the watchdog monitoring thread"""
        if self._enabled:
            return
        self._enabled = True
        self._stop_event.clear()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_external = None
        self._pre_forced_setpoint = 0
        now = time.time()
        self._last_dbus_update = now
        self._last_mqtt_update = now
        self._last_setpoint_update = now
        self._thread = threading.Thread(target=self._run, name="hardware-watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the watchdog thread"""
        self._enabled = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self):
        """Main watchdog loop - checks heartbeat every interval"""
        while not self._stop_event.wait(self.check_interval):
            if not self._enabled:
                break
            self._check_heartbeat()

    def _check_heartbeat(self):
        """Check if the control loop is alive and trigger failsafe if not"""
        # In dry-run no setpoints are written, so liveness cannot be judged
        if self.dry_run:
            return
        now = time.time()
        with self._lock:
            setpoint_age = now - self._last_setpoint_update
            dbus_age = now - self._last_dbus_update

        # The loop is healthy as long as it keeps writing setpoints (even if
        # slowly). Only force the failsafe when BOTH the setpoint writes and
        # D-Bus telemetry have been silent for the full timeout, i.e. the
        # control loop has genuinely stalled or crashed.
        stale = setpoint_age > self.timeout_seconds and dbus_age > self.timeout_seconds

        if stale and not self._triggered:
            self._triggered = True
            self._apply_failsafe()
        elif stale and self._triggered and not self._hardware_forced:
            self._apply_failsafe()
        elif not stale and self._triggered:
            self._recover_from_failsafe()

    def _apply_failsafe(self):
        """Force ESS into safe pass-through mode, remembering prior state for recovery"""
        if self.dry_run:
            logger.warning("[DRY] watchdog would force 0W setpoint / ESS pass-through")
            return
        try:
            if self._pre_forced_external is None:
                self._pre_forced_external = self.victron.get_ess_mode().get("is_external")
                self._pre_forced_setpoint = self._get_setpoint() if self._get_setpoint else 0
            self.victron.set_grid_setpoint(0)
            self.victron.set_ess_mode(external=False)
            self._hardware_forced = True
        except Exception:
            pass  # Best effort - don't crash watchdog

    def _recover_from_failsafe(self):
        """Telemetry recovered - re-arm watchdog and restore prior ESS mode/setpoint"""
        self._triggered = False
        if self._hardware_forced:
            try:
                if self._pre_forced_external:
                    self.victron.set_ess_mode(external=True)
                    self.victron.set_grid_setpoint(self._pre_forced_setpoint)
            except Exception:
                pass  # Best effort - don't crash watchdog
            self._hardware_forced = False
        self._pre_forced_external = None
        self._pre_forced_setpoint = 0
        logger.info("hardware watchdog re-armed after telemetry recovery")

    def is_triggered(self) -> bool:
        """Return True if watchdog has triggered failsafe"""
        return self._triggered

    def get_status(self) -> dict:
        """Return watchdog status for UI/debugging"""
        return {
            "enabled": self._enabled,
            "triggered": self._triggered,
            "hardware_forced": self._hardware_forced,
            "setpoint_age": round(time.time() - self._last_setpoint_update, 1),
            "dbus_age": round(time.time() - self._last_dbus_update, 1),
            "mqtt_age": round(time.time() - self._last_mqtt_update, 1),
            "timeout_seconds": self.timeout_seconds,
        }
