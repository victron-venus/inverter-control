"""Tests for fast-signal path health truth and unhealthy-poll throttling."""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron as victron_mod
from inverter_control.victron import (
    SIGNAL_SILENCE_TIMEOUT,
    UNHEALTHY_POLL_INTERVAL,
    VictronDBus,
    reset_victron_for_testing,
)


def _make_victron() -> VictronDBus:
    """Test-mode instance with a fake native client and healthy flags."""
    reset_victron_for_testing()
    v = VictronDBus(test_mode=True)
    v._native = MagicMock()
    v._signal_paths_subscribed = True
    return v


class TestUnhealthyPollThrottle:
    def test_throttled_to_one_per_interval(self):
        v = _make_victron()
        v._signal_paths_subscribed = False
        with (
            patch.object(v, "_poll_system_data") as poll,
            patch("inverter_control.victron.time.monotonic", side_effect=[0.0, 0.2, 0.4, 0.6, 0.8]),
            patch(
                "inverter_control.victron.time.time",
                side_effect=[1000.0, 1000.2, 1000.4, 1000.6, 1000.8],
            ),
            patch.object(v, "_setup_fast_signals"),
            patch.object(v, "_reconcile_mppt_data"),
            patch.object(v, "_reconcile_pv_power"),
            patch.object(v, "_reconcile_acload_power"),
            patch.object(v, "_poll_battery_chain_socs"),
            patch.object(v, "_poll_inverter_state"),
            patch.object(v, "_poll_battery_cell_data_tree"),
            patch.object(v, "_poll_daily_yields"),
            patch.object(v, "_poll_battery_daily_energy"),
        ):
            for _ in range(5):  # 1 second of 5Hz passes
                v._poll_all()
        # 5Hz pass -> exactly one tree poll per UNHEALTHY_POLL_INTERVAL
        assert poll.call_count == 1 / UNHEALTHY_POLL_INTERVAL

    def test_next_poll_allowed_after_interval(self):
        v = _make_victron()
        v._signal_paths_subscribed = False
        mono = iter([0.0, 1.5])
        with (
            patch.object(v, "_poll_system_data") as poll,
            patch("inverter_control.victron.time.monotonic", side_effect=mono),
            patch("inverter_control.victron.time.time", side_effect=[1000.0, 1001.5]),
            patch.object(v, "_setup_fast_signals"),
            patch.object(v, "_reconcile_mppt_data"),
            patch.object(v, "_reconcile_pv_power"),
            patch.object(v, "_reconcile_acload_power"),
            patch.object(v, "_poll_battery_chain_socs"),
            patch.object(v, "_poll_inverter_state"),
            patch.object(v, "_poll_battery_cell_data_tree"),
            patch.object(v, "_poll_daily_yields"),
            patch.object(v, "_poll_battery_daily_energy"),
        ):
            v._poll_all()
            v._poll_all()
        assert poll.call_count == 2


class TestSilenceInvalidation:
    def test_silent_healthy_path_flips_unhealthy(self, caplog):
        v = _make_victron()
        v._last_signal_ok_monotonic = -SIGNAL_SILENCE_TIMEOUT - 1.0
        # Fresh setup attempt timestamp: keep the 10s retry from repairing the
        # path inside this same pass (a MagicMock native always resubscribes).
        v._last_signal_setup_try = 1000.0
        with (
            patch("inverter_control.victron.time.monotonic", return_value=0.0),
            patch("inverter_control.victron.time.time", return_value=1000.0),
            patch.object(v, "_poll_system_data"),
            patch.object(v, "_reconcile_mppt_data"),
            patch.object(v, "_reconcile_pv_power"),
            patch.object(v, "_reconcile_acload_power"),
            patch.object(v, "_poll_battery_chain_socs"),
            patch.object(v, "_poll_battery_cell_data_tree"),
            patch.object(v, "_poll_daily_yields"),
            patch.object(v, "_poll_battery_daily_energy"),
            caplog.at_level(logging.WARNING, logger="inverter-control"),
        ):
            assert v.is_signals_healthy()
            v._poll_all()
        assert not v._signal_paths_subscribed
        assert "resubscribing" in caplog.text
        assert "unhealthy" in caplog.text

    def test_fresh_traffic_stays_healthy(self):
        v = _make_victron()
        v._last_signal_ok_monotonic = -1.0
        with (
            patch("inverter_control.victron.time.monotonic", return_value=0.0),
            patch("inverter_control.victron.time.time", return_value=1000.0),
            patch.object(v, "_poll_inverter_state"),
            patch.object(v, "_poll_inverter_power"),
            patch.object(v, "_reconcile_mppt_data"),
            patch.object(v, "_reconcile_pv_power"),
            patch.object(v, "_reconcile_acload_power"),
            patch.object(v, "_poll_battery_chain_socs"),
            patch.object(v, "_poll_battery_cell_data_tree"),
            patch.object(v, "_poll_daily_yields"),
            patch.object(v, "_poll_battery_daily_energy"),
        ):
            v._poll_all()
        assert v._signal_paths_subscribed


class TestHealthTransitions:
    def test_flip_logs_once(self, caplog):
        v = _make_victron()
        with caplog.at_level(logging.INFO, logger="inverter-control"):
            v._set_signals_healthy(False)
            v._set_signals_healthy(False)  # no second warning
            v._set_signals_healthy(True)
            v._set_signals_healthy(True)  # no second info
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        infos = ["healthy" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 1
        assert sum(infos) == 1

    def test_apply_fast_value_marks_alive(self):
        v = _make_victron()
        v._last_signal_ok_monotonic = None
        with patch("inverter_control.victron.time.monotonic", return_value=42.0):
            v._apply_fast_value(victron_mod.SYSTEM_SERVICE, "/Ac/Grid/L1/Power", "-13")
        assert v._last_signal_ok_monotonic == 42.0

    def test_apply_fast_value_unknown_service_not_alive(self):
        """Signals from senders outside the fast-input set prove nothing."""
        v = _make_victron()
        v._last_signal_ok_monotonic = None
        with patch("inverter_control.victron.time.monotonic", return_value=42.0):
            v._apply_fast_value("com.victronenergy.unknown", "/Dc/0/Power", "-13")
        assert v._last_signal_ok_monotonic is None
