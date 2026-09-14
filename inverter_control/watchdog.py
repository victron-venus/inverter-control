"""Hardware watchdog for Victron ESS setpoint safety."""

import logging
import threading
import time

logger = logging.getLogger("inverter-control")

GRID_LOSS_FALLBACK_SETPOINT = -10
GRID_LOSS_REFRESH_INTERVAL = 2.0
GRID_LOSS_RETRY_INTERVAL = 1.0
SETPOINT_OVERRIDE_INTERVAL = 2.0


class WatchdogTimeoutError(Exception):
    """Raised when a watchdog timeout occurs"""


class HardwareWatchdog:
    """
    Hardware watchdog for Victron ESS setpoint safety.

    Monitors setpoint-write liveness (D-Bus + MQTT). If the control loop stops
    writing grid setpoints for timeout seconds, forces ESS setpoint to 0W
    if the control loop stalls or crashes. An explicitly detected grid-meter
    outage instead holds briefly, then maintains a -10W AC-input command so
    the inverter's external-control timeout does not stop solar charging.

    Both setpoint writes and telemetry must stop before the generic fallback
    triggers. Failed generic writes retry once per check; meter-loss writes
    retry at most once per second until the transport accepts one.

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
        self._grid_loss_fallback_applied = False
        self._grid_loss_refresh_pending = False
        self._last_grid_loss_write: float | None = None
        self._last_grid_loss_attempt: float | None = None
        self._enabled = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        # One lock owns normal, meter-loss and explicit manual writes. A
        # generation invalidates calculations begun before Start/Stop/Edit.
        self._control_generation = 0
        self._override_value: int | None = None
        self._override_error: str | None = None
        self._override_rejection_error: str | None = None
        self._override_request_id: str | None = None
        self._override_last_attempt: float | None = None
        self._override_last_write: float | None = None
        self._override_refresh_failed = False
        self._override_status_callback = None
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

    def get_setpoint_override(self) -> dict:
        """Process-lifetime desired override; never restored after restart."""
        with self._lock:
            return {
                "value": self._override_value,
                "last_error": self._override_rejection_error or self._override_error,
                "request_id": self._override_request_id,
            }

    def set_override_status_callback(self, callback) -> None:
        """Callback must only enqueue a status publication, never block on MQTT."""
        with self._lock:
            self._override_status_callback = callback
            self._publish_override_locked()

    def _publish_override_locked(self) -> None:
        if self._override_status_callback is not None:
            try:
                self._override_status_callback(self.get_setpoint_override())
            except Exception:
                logger.exception("Failed to publish manual setpoint override status")

    def publish_override_status(self) -> None:
        with self._lock:
            self._publish_override_locked()

    def reject_setpoint_override(self, error: str, request_id: str | None = None) -> dict:
        with self._lock:
            self._override_rejection_error = error or "Setpoint override command rejected"
            self._override_request_id = request_id
            self._publish_override_locked()
            return self.get_setpoint_override()

    def set_setpoint_override(self, value: int | None, request_id: str | None = None) -> dict:
        """Immediately accept a manual command, or stop without writing zero.

        This explicit command is independent of DRY and has priority over
        automatic regulation. Failed edits leave the prior override active.
        """
        if value is not None and (type(value) is not int or not -(2**31) <= value < 2**31):
            return self.reject_setpoint_override(
                "value must be an int32 integer or null", request_id
            )
        with self._lock:
            if value is None and self._override_value is None:
                self._override_error = None
                self._override_rejection_error = None
                self._override_request_id = request_id
                self._publish_override_locked()
                return self.get_setpoint_override()
            if value is not None:
                try:
                    if not self.victron.set_grid_setpoint(value):
                        raise RuntimeError("Inverter rejected the setpoint write")
                except Exception as error:
                    return self.reject_setpoint_override(str(error), request_id)
            self._control_generation += 1
            self._override_value = value
            self._override_error = None
            self._override_rejection_error = None
            self._override_request_id = request_id
            now = time.monotonic()
            self._override_last_attempt = now if value is not None else None
            self._override_last_write = now if value is not None else None
            self._override_refresh_failed = False
            # Resume only fresh regulation or the current meter-loss policy;
            # never restore a setpoint captured before this manual session.
            self._triggered = False
            self._hardware_forced = False
            self._pre_forced_setpoint = 0
            self._grid_loss_forced = False
            self._grid_loss_fallback_applied = False
            self._grid_loss_refresh_pending = False
            self._last_grid_loss_write = None
            self._last_grid_loss_attempt = None
            self._fail_count = 0
            self._success_count = 0
            if value is not None:
                self._last_setpoint_update = now
                self._has_valid_setpoint = True
            self._publish_override_locked()
            self._wake_event.set()
            logger.info(
                "Manual setpoint override %s", "stopped" if value is None else f"set to {value}W"
            )
            return self.get_setpoint_override()

    def _maintain_override_locked(self, now: float) -> None:
        if self._override_value is None:
            return
        last = self._override_last_write
        interval = SETPOINT_OVERRIDE_INTERVAL
        if self._override_refresh_failed:
            last = self._override_last_attempt
            interval = SETPOINT_OVERRIDE_INTERVAL
        if last is not None and now - last < interval:
            return
        self._override_last_attempt = now
        try:
            if not self.victron.set_grid_setpoint(self._override_value):
                raise RuntimeError("Inverter rejected the setpoint refresh")
        except Exception as error:
            message = str(error) or "Inverter setpoint refresh failed"
            self._override_refresh_failed = True
            if message != self._override_error:
                self._override_error = message
                self._publish_override_locked()
                logger.warning("Manual setpoint override refresh failed: %s", message)
            return
        had_error = self._override_error is not None
        self._override_refresh_failed = False
        self._override_error = None
        self._override_last_write = time.monotonic()
        self._last_setpoint_update = self._override_last_write
        if had_error:
            self._publish_override_locked()

    def control_generation(self) -> int:
        with self._lock:
            return self._control_generation

    def write_control_setpoint(
        self, value: int, generation: int, *, dry_run: bool = False, on_accept=None
    ) -> bool:
        """Reject obsolete calculations while serializing every physical write."""
        with self._lock:
            if (
                self._override_value is not None
                or generation != self._control_generation
                or self._triggered
            ):
                return False
            if dry_run or self.dry_run:
                if on_accept is not None:
                    on_accept()
                return True
            accepted = self.victron.set_grid_setpoint(value)
            if accepted:
                self.mark_setpoint_update()
                if on_accept is not None:
                    on_accept()
            return accepted

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
                self._grid_loss_fallback_applied = False

    def check_grid_loss(self):
        """Enforce the optional outage deadline at control-loop cadence."""
        with self._lock:
            self._check_grid_loss_locked(time.monotonic())

    def _check_grid_loss_locked(self, now: float) -> None:
        if self._override_value is not None:
            self._maintain_override_locked(now)
            return
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
                    logger.warning(
                        "Grid loss: hold expired; maintaining %dW until meter recovery",
                        GRID_LOSS_FALLBACK_SETPOINT,
                    )
                    # A prior generic watchdog zero is not the meter-loss
                    # fallback. Require an accepted -10W before recovery.
                    self._hardware_forced = False
                    self._grid_loss_refresh_pending = True
                    self._last_grid_loss_write = None
                    self._last_grid_loss_attempt = None
                self._grid_loss_forced = True
                self._triggered = True
                # Recovery must calculate a new command from fresh data,
                # including when an earlier generic watchdog already forced 0.
                self._pre_forced_setpoint = 0
        if self._grid_loss_forced:
            self._maintain_grid_loss_fallback_locked(now)

    def _maintain_grid_loss_fallback_locked(self, now: float) -> None:
        """Refresh only the fallback, using monotonic accepted/attempt times."""
        if self._last_grid_loss_attempt is not None:
            if self._grid_loss_refresh_pending:
                if now - self._last_grid_loss_attempt < GRID_LOSS_RETRY_INTERVAL:
                    return
            elif (
                self._last_grid_loss_write is not None
                and now - self._last_grid_loss_write < GRID_LOSS_REFRESH_INTERVAL
            ):
                return
        self._last_grid_loss_attempt = now
        self._grid_loss_refresh_pending = True
        try:
            if not self.victron.set_grid_setpoint(GRID_LOSS_FALLBACK_SETPOINT):
                logger.error("Grid loss: fallback write rejected; retrying in 1s")
                return
        except Exception:
            logger.exception("Grid loss: fallback write failed; retrying in 1s")
            return
        first_write = not self._hardware_forced
        self._hardware_forced = True
        self._grid_loss_fallback_applied = True
        self._grid_loss_refresh_pending = False
        self._last_grid_loss_write = time.monotonic()
        if first_write:
            logger.warning(
                "WATCHDOG: grid telemetry lost - applied %dW AC-input fallback; refreshing every %.0fs",
                GRID_LOSS_FALLBACK_SETPOINT,
                GRID_LOSS_REFRESH_INTERVAL,
            )

    def start(self):
        """Start the watchdog monitoring thread"""
        if self._enabled:
            return
        self._enabled = True
        self._stop_event.clear()
        self._wake_event.clear()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_setpoint = 0
        self._fail_count = 0
        self._success_count = 0
        self._grid_invalid_since = None
        self._grid_loss_forced = False
        self._has_valid_setpoint = False
        self._grid_loss_fallback_applied = False
        self._grid_loss_refresh_pending = False
        self._last_grid_loss_write = None
        self._last_grid_loss_attempt = None
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
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self):
        """Maintain fallback deadlines without accelerating heartbeat checks."""
        next_heartbeat = time.monotonic() + self.check_interval
        while True:
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            deadline = next_heartbeat
            with self._lock:
                if self._override_value is not None:
                    last_write = self._override_last_write
                    interval = SETPOINT_OVERRIDE_INTERVAL
                    if self._override_refresh_failed:
                        last_write = self._override_last_attempt
                        interval = SETPOINT_OVERRIDE_INTERVAL
                    deadline = min(
                        deadline,
                        time.monotonic() if last_write is None else last_write + interval,
                    )
                elif self.grid_loss_hold_seconds is not None and not self.dry_run:
                    now = time.monotonic()
                    grid_deadline = now + GRID_LOSS_REFRESH_INTERVAL
                    if self._grid_loss_forced:
                        if self._grid_loss_refresh_pending:
                            last_write = self._last_grid_loss_attempt
                            interval = GRID_LOSS_RETRY_INTERVAL
                        else:
                            last_write = self._last_grid_loss_write
                            interval = GRID_LOSS_REFRESH_INTERVAL
                        grid_deadline = now if last_write is None else last_write + interval
                    deadline = min(deadline, grid_deadline)
            wait_seconds = max(0.0, deadline - time.monotonic())
            self._wake_event.wait(wait_seconds)
            if self._stop_event.is_set() or not self._enabled:
                break
            self.check_grid_loss()
            if time.monotonic() >= next_heartbeat:
                self._check_heartbeat()
                next_heartbeat = time.monotonic() + self.check_interval

    def _check_heartbeat(self):
        """Check if the control loop is alive and trigger failsafe if not"""
        with self._lock:
            self._check_heartbeat_locked()

    def _check_heartbeat_locked(self):
        if self._override_value is not None:
            self._maintain_override_locked(time.monotonic())
            return
        # In dry-run no setpoints are written, so liveness cannot be judged
        if self.dry_run:
            return
        now = time.monotonic()
        self._check_grid_loss_locked(now)
        if self._grid_loss_forced:
            # A meter-loss command must never be replaced by the generic zero
            # when normal control has intentionally stopped writing setpoints.
            self._fail_count = 0
            if (
                not self._hardware_forced
                or self._grid_loss_refresh_pending
                or self._telemetry_invalid
                or now - self._last_dbus_update > self.timeout_seconds
            ):
                self._success_count = 0
                return
            self._success_count += 1
            if self._success_count >= self._success_threshold:
                self._recover_from_failsafe_locked()
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

        Deliberately does not touch the ESS assistant mode or BatteryLife.
        The distinct meter-loss policy owns its maintained -10W command.
        """
        if self._override_value is not None:
            self._maintain_override_locked(time.monotonic())
            return
        if self.dry_run:
            logger.warning("[DRY] watchdog would force 0W setpoint")
            return
        if self._grid_loss_forced:
            self._maintain_grid_loss_fallback_locked(time.monotonic())
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
            self._grid_loss_fallback_applied = False
            logger.warning("WATCHDOG: stalled loop detected - forced 0W grid setpoint")
        except Exception:
            logger.exception("WATCHDOG: failsafe write failed")

    def _recover_from_failsafe(self):
        """Telemetry recovered - re-arm watchdog and restore the prior setpoint"""
        with self._lock:
            self._recover_from_failsafe_locked()

    def _recover_from_failsafe_locked(self):
        if self._override_value is not None:
            return
        if self._telemetry_invalid:
            return
        if self._grid_loss_forced:
            if not self._hardware_forced or self._grid_loss_refresh_pending:
                return
            # Keep the accepted fallback. The controller will calculate a fresh
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
            if self._override_value is not None:
                loss_state = "overridden"
            elif self.grid_loss_hold_seconds is None:
                loss_state = "disabled"
            elif self._grid_loss_forced:
                pending = not self._hardware_forced or self._grid_loss_refresh_pending
                loss_state = "fallback_pending" if pending else "fallback"
                if not pending and not self._telemetry_invalid:
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
                "grid_loss_fallback_applied": self._grid_loss_fallback_applied,
                "grid_loss_fallback_setpoint": GRID_LOSS_FALLBACK_SETPOINT,
                "grid_loss_refresh_pending": self._grid_loss_refresh_pending,
                "grid_loss_fallback_write_age": (
                    None
                    if self._last_grid_loss_write is None
                    else max(0.0, now - self._last_grid_loss_write)
                ),
                # Compatibility for older consumers; -10W must never be
                # advertised as an accepted zero command.
                "grid_loss_zero_applied": False,
            }
