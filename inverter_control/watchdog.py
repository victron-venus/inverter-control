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
        grid_loss_hold_seconds: float | None = None,
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
        self.grid_loss_hold_seconds = grid_loss_hold_seconds
        self._grid_invalid_since: float | None = None
        self._grid_loss_forced = False
        self._has_valid_setpoint = False
        self._grid_loss_zero_applied = False
        self._enabled = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_setpoint: int = 0
        # Serialize safety writes, recovery and validity transitions. A new
        # invalidation must not race a restoration from the watchdog thread.
        self._lock = threading.RLock()
        # Hysteresis counters to prevent flapping
        self._fail_count = 0
        self._success_count = 0
        self._fail_threshold = 3  # consecutive failed checks to trigger
        self._success_threshold = 2  # consecutive successful checks to recover

    def mark_dbus_update(self):
        """Call when D-Bus telemetry is successfully read"""
        with self._lock:
            now = time.monotonic()
            # A read that returns after the deadline cannot retrospectively
            # cancel an expired hold, even if no periodic check ran in time.
            self._check_grid_loss_locked(now)
            self._last_dbus_update = now
            self._telemetry_invalid = False
            if not self._grid_loss_forced:
                self._grid_invalid_since = None

    def mark_dbus_invalid(self):
        """Pause recovery when control telemetry is explicitly unavailable.

        Start a single outage deadline without renewing it on repeated bad
        reads. Hardware action is handled by check_grid_loss/the watchdog.
        """
        with self._lock:
            if self._grid_invalid_since is None:
                self._grid_invalid_since = time.monotonic()
                if self.grid_loss_hold_seconds is not None:
                    logger.warning(
                        "Grid loss: holding last accepted command for at most %.1fs",
                        self.grid_loss_hold_seconds if self._has_valid_setpoint else 0.0,
                    )
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
            if not self._telemetry_invalid:
                self._has_valid_setpoint = True
                self._grid_loss_zero_applied = False

    def check_grid_loss(self):
        """Enforce the optional outage deadline at control-loop cadence."""
        with self._lock:
            self._check_grid_loss_locked(time.monotonic())

    def _check_grid_loss_locked(self, now: float) -> None:
        if self.dry_run or self.grid_loss_hold_seconds is None:
            return
        if self._telemetry_invalid and self._grid_invalid_since is not None:
            elapsed = now - self._grid_invalid_since
            if (
                not self._has_valid_setpoint
                or self._triggered
                or elapsed >= self.grid_loss_hold_seconds
            ):
                if not self._grid_loss_forced:
                    logger.warning("Grid loss: hold expired; requesting 0W until meter recovery")
                self._grid_loss_forced = True
                self._triggered = True
                # Recovery must calculate a new command from fresh data,
                # including when an earlier generic watchdog already forced 0.
                self._pre_forced_setpoint = 0
                if self._hardware_forced:
                    self._grid_loss_zero_applied = True
        if self._grid_loss_forced and not self._hardware_forced:
            # A rejected zero remains pending even if the meter returns before
            # the retry; recovery cannot silently skip the required write.
            self._apply_failsafe()

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
        self._grid_invalid_since = None
        self._grid_loss_forced = False
        self._has_valid_setpoint = False
        self._grid_loss_zero_applied = False
        self._telemetry_invalid = False
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
        with self._lock:
            self._check_heartbeat_locked()

    def _check_heartbeat_locked(self):
        # In dry-run no setpoints are written, so liveness cannot be judged
        if self.dry_run:
            return
        now = time.monotonic()
        self._check_grid_loss_locked(now)
        if self._grid_loss_forced and not self._hardware_forced:
            self._success_count = 0
            return
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
                self._pre_forced_setpoint = (
                    self._get_setpoint() if self._get_setpoint and not self._grid_loss_forced else 0
                )
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
            if self._grid_loss_forced:
                self._grid_loss_zero_applied = True
            logger.warning(
                "WATCHDOG: %s - forced 0W grid setpoint",
                "grid telemetry lost" if self._grid_loss_forced else "stalled loop detected",
            )
        except Exception:
            logger.exception("WATCHDOG: failsafe write failed")

    def _recover_from_failsafe(self):
        """Telemetry recovered - re-arm watchdog and restore the prior setpoint"""
        with self._lock:
            self._recover_from_failsafe_locked()

    def _recover_from_failsafe_locked(self):
        if self._telemetry_invalid:
            return
        if self._grid_loss_forced:
            if not self._hardware_forced:
                return
            # Keep the accepted zero. The controller will calculate a fresh
            # command after two valid checks, never replay a pre-outage value.
            self._grid_loss_forced = False
            self._grid_invalid_since = None
            self._hardware_forced = False
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
            elapsed = (
                None
                if self._grid_invalid_since is None
                else max(0.0, now - self._grid_invalid_since)
            )
            remaining = None
            if self.grid_loss_hold_seconds is None:
                loss_state = "disabled"
            elif self._grid_loss_forced:
                loss_state = "zero" if self._hardware_forced else "zero_pending"
                if self._hardware_forced and not self._telemetry_invalid:
                    loss_state = "recovering"
                remaining = 0.0
            elif self._telemetry_invalid:
                loss_state = "holding"
                remaining = max(
                    0.0,
                    (self.grid_loss_hold_seconds if self._has_valid_setpoint else 0.0)
                    - (elapsed or 0.0),
                )
            else:
                loss_state = "normal"
            return {
                "enabled": self._enabled,
                "triggered": self._triggered,
                "hardware_forced": self._hardware_forced,
                "setpoint_age": round(setpoint_age, 1),
                "dbus_age": round(dbus_age, 1),
                "mqtt_age": round(mqtt_age, 1),
                "timeout_seconds": self.timeout_seconds,
                "grid_loss_state": loss_state,
                "grid_loss_hold_seconds": self.grid_loss_hold_seconds,
                "grid_loss_elapsed": elapsed,
                "grid_loss_remaining": remaining,
                "grid_loss_zero_applied": self._grid_loss_zero_applied,
            }
