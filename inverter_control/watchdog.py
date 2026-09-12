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

    Both setpoint writes and telemetry must stop before triggering. Failed
    fallback writes are retried once per check until the transport accepts one.

    Runs as a daemon thread checking heartbeats every check_interval seconds
    (WATCHDOG_CHECK_INTERVAL, default 5s).
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
        self._telemetry_invalid = False
        self._enabled = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_setpoint: int = 0
        self._lock = threading.Lock()
        # Hysteresis counters to prevent flapping
        self._fail_count = 0
        self._success_count = 0
        self._fail_threshold = 3  # consecutive failed checks to trigger
        self._success_threshold = 2  # consecutive successful checks to recover

    def mark_dbus_update(self):
        """Call when D-Bus telemetry is successfully read"""
        with self._lock:
            self._last_dbus_update = time.monotonic()
            self._telemetry_invalid = False

    def mark_dbus_invalid(self):
        """Pause recovery when control telemetry is explicitly unavailable.

        Keep the existing timeout and failure hysteresis: invalidation stops
        heartbeat renewal; it does not introduce an immediate hardware action.
        """
        with self._lock:
            self._telemetry_invalid = True
            self._success_count = 0

    def mark_mqtt_update(self):
        """Call when MQTT state is successfully published"""
        with self._lock:
            self._last_mqtt_update = time.monotonic()

    def mark_setpoint_update(self):
        """Call every time a grid setpoint is written to the inverter"""
        with self._lock:
            self._last_setpoint_update = time.monotonic()

    def start(self):
        """Start the watchdog monitoring thread"""
        if self._enabled:
            return
        self._enabled = True
        self._stop_event.clear()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_setpoint = 0
        self._fail_count = 0
        self._success_count = 0
        now = time.monotonic()
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
        now = time.monotonic()
        with self._lock:
            setpoint_age = now - self._last_setpoint_update
            dbus_age = now - self._last_dbus_update
            telemetry_invalid = self._telemetry_invalid

        # The loop is healthy as long as it keeps writing setpoints (even if
        # slowly). Only force the failsafe when BOTH the setpoint writes and
        # D-Bus telemetry have been silent for the full timeout, i.e. the
        # control loop has genuinely stalled or crashed.
        stale = setpoint_age > self.timeout_seconds and dbus_age > self.timeout_seconds

        if stale:
            self._fail_count += 1
            self._success_count = 0
        elif telemetry_invalid:
            # A brief good sample must not re-arm after an explicit new loss.
            self._success_count = 0
            self._fail_count = 0
        else:
            self._success_count += 1
            self._fail_count = 0

        if self._fail_count >= self._fail_threshold:
            self._triggered = True
            if not self._hardware_forced:
                self._apply_failsafe()
        elif self._success_count >= self._success_threshold and self._triggered:
            self._recover_from_failsafe()

    def _apply_failsafe(self):
        """Force a safe 0W setpoint, remembering the prior value for recovery.

        Deliberately does NOT touch the ESS assistant mode: with Hub4 in
        External control (mode 3) the GX keeps honoring AcPowerSetpoint=0,
        which is a complete failsafe. Flipping Hub4Mode 3->1->3 on recovery
        made vebus dip into passthru each time - and set_ess_mode(False) also
        resets BatteryLife State to 0 - so every transient stall caused its
        own grid disturbance.
        """
        if self.dry_run:
            logger.warning("[DRY] watchdog would force 0W setpoint")
            return
        if not self._hardware_forced:
            try:
                self._pre_forced_setpoint = self._get_setpoint() if self._get_setpoint else 0
            except Exception:
                # Reading the recovery value must never prevent the safety write.
                self._pre_forced_setpoint = 0
                logger.exception(
                    "WATCHDOG: failed to capture prior setpoint; recovery defaults to 0W"
                )
        try:
            if not self.victron.set_grid_setpoint(0):
                logger.error("WATCHDOG: failsafe write rejected; retrying on the next check")
                return
            self._hardware_forced = True
            logger.warning("WATCHDOG: stalled loop detected - forced 0W grid setpoint")
        except Exception:
            logger.exception("WATCHDOG: failsafe write failed")

    def _recover_from_failsafe(self):
        """Telemetry recovered - re-arm watchdog and restore the prior setpoint"""
        with self._lock:
            if self._telemetry_invalid:
                return
        if self._hardware_forced:
            try:
                if not self.victron.set_grid_setpoint(self._pre_forced_setpoint):
                    logger.error("WATCHDOG: setpoint restore rejected; watchdog remains armed")
                    return
            except Exception:
                logger.exception("WATCHDOG: setpoint restore failed")
                return
            self._hardware_forced = False
        self._triggered = False
        self._pre_forced_setpoint = 0
        logger.info("hardware watchdog re-armed after telemetry recovery")

    def is_triggered(self) -> bool:
        """Return True if watchdog has triggered failsafe"""
        return self._triggered

    def get_status(self) -> dict:
        """Return watchdog status for UI/debugging"""
        now = time.monotonic()
        with self._lock:
            setpoint_age = now - self._last_setpoint_update
            dbus_age = now - self._last_dbus_update
            mqtt_age = now - self._last_mqtt_update
        return {
            "enabled": self._enabled,
            "triggered": self._triggered,
            "hardware_forced": self._hardware_forced,
            "setpoint_age": round(setpoint_age, 1),
            "dbus_age": round(dbus_age, 1),
            "mqtt_age": round(mqtt_age, 1),
            "timeout_seconds": self.timeout_seconds,
        }
