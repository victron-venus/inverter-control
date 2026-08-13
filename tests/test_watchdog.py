"""
Unit tests for the HardwareWatchdog failsafe.
"""

import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from main import HardwareWatchdog


def wait_until(condition, timeout=2.0, interval=0.01):
    """Poll `condition()` until truthy or the deadline expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


class TestHardwareWatchdog(unittest.TestCase):
    def make_watchdog(self, timeout=30, dry_run=False):
        victron = MagicMock()
        victron.get_ess_mode.return_value = {
            "hub4_mode": 3,
            "mode_name": "External control",
            "is_external": True,
        }
        watchdog = HardwareWatchdog(
            victron=victron,
            timeout_seconds=timeout,
            check_interval=0.05,
            dry_run=dry_run,
            get_setpoint=lambda: 1234,
        )
        return watchdog, victron

    def test_active_loop_never_triggers_failsafe(self):
        """A loop that keeps writing setpoints must never be failed over,
        even if it is running slower than the loop interval."""
        watchdog, victron = self.make_watchdog(timeout=30)
        watchdog.start()

        # Loop stays alive: refresh setpoint + dbus telemetry every "cycle"
        for _ in range(10):
            watchdog.mark_dbus_update()
            watchdog.mark_setpoint_update()
            time.sleep(0.01)

        # Simulate a slow cycle (still well inside the 30s timeout)
        time.sleep(0.1)
        watchdog.mark_dbus_update()
        watchdog.mark_setpoint_update()

        time.sleep(0.15)
        assert not watchdog.is_triggered()
        assert not watchdog._hardware_forced
        victron.set_grid_setpoint.assert_not_called()
        watchdog.stop()

    def test_stalled_loop_triggers_failsafe(self):
        """If both setpoint writes and D-Bus telemetry stop, force 0W safe mode."""
        watchdog, victron = self.make_watchdog(timeout=0.1)
        watchdog.start()
        watchdog.mark_dbus_update()
        watchdog.mark_setpoint_update()

        # Let the loop go silent past the timeout
        assert wait_until(watchdog.is_triggered)

        assert watchdog._hardware_forced
        # Failsafe wrote a 0W setpoint and left external control
        victron.set_grid_setpoint.assert_any_call(0)
        victron.set_ess_mode.assert_any_call(external=False)
        watchdog.stop()

    def test_recovery_restores_external_mode_and_setpoint(self):
        """When telemetry resumes, the watchdog re-arms and restores control."""
        watchdog, victron = self.make_watchdog(timeout=0.1)
        watchdog.start()
        watchdog.mark_dbus_update()
        watchdog.mark_setpoint_update()

        # Stall -> failsafe
        assert wait_until(watchdog.is_triggered)

        # Loop recovers and resumes marking regularly
        for _ in range(10):
            watchdog.mark_dbus_update()
            watchdog.mark_setpoint_update()
            time.sleep(0.02)

        assert wait_until(lambda: not watchdog.is_triggered())
        victron.set_ess_mode.assert_any_call(external=True)
        victron.set_grid_setpoint.assert_any_call(1234)
        watchdog.stop()

    def test_dry_run_never_triggers(self):
        """Dry-run writes no setpoints, so liveness cannot be judged - no failsafe."""
        watchdog, victron = self.make_watchdog(timeout=0.1, dry_run=True)
        watchdog.start()
        time.sleep(0.3)

        assert not watchdog.is_triggered()
        victron.set_grid_setpoint.assert_not_called()
        victron.set_ess_mode.assert_not_called()
        watchdog.stop()


if __name__ == "__main__":
    unittest.main()
