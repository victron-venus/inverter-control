"""Tests for the sustained ESS-not-external loud warning."""

import logging
import os
import sys
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_main import _make_controller  # reuse the mocked-controller factory (pytest path)

_MOD = "inverter_control.controller"


def _set_ess(controller, external: bool) -> None:
    controller.state["ess_mode"] = {"is_external": external, "hub4_mode": 3 if external else 1}


def _tick(controller, t: float, bridge=None):
    """Run one ESS check at fake wall-clock t."""
    stack = ExitStack()
    stack.enter_context(patch("inverter_control.mqtt_bridge.get_mqtt_bridge", return_value=bridge))
    stack.enter_context(patch(f"{_MOD}.time.time", return_value=t))
    with stack:
        controller._check_ess_external()


class TestEssExternalWarning:
    def test_no_warning_before_threshold(self):
        controller, _, _, _ = _make_controller()
        bridge = MagicMock()
        _set_ess(controller, external=False)
        for t in (1000.0, 1100.0, 1199.0):  # 3.3 min < 5 min threshold
            _tick(controller, t, bridge)
        assert not controller._ess_notification_active
        bridge.publish_notification.assert_not_called()

    def test_warning_after_sustained_mismatch(self, caplog):
        controller, _, _, _ = _make_controller()
        bridge = MagicMock()
        _set_ess(controller, external=False)
        with caplog.at_level(logging.WARNING, logger="inverter-control"):
            _tick(controller, 1000.0, bridge)  # arm timer
            _tick(controller, 1301.0, bridge)  # past 5 min
        assert controller._ess_notification_active
        bridge.publish_notification.assert_called_once()
        kwargs = bridge.publish_notification.call_args.kwargs
        assert kwargs["level"] == "warning"
        assert kwargs["notification_id"] == "ess-not-external"
        assert "no-ops" in caplog.text

    def test_single_notification_then_hourly_rewarn(self, caplog):
        controller, _, _, _ = _make_controller()
        bridge = MagicMock()
        _set_ess(controller, external=False)
        with caplog.at_level(logging.WARNING, logger="inverter-control"):
            _tick(controller, 1000.0, bridge)  # arm
            _tick(controller, 1301.0, bridge)  # first warn
            _tick(controller, 2000.0, bridge)  # ~16 min in: silent
            _tick(controller, 5000.0, bridge)  # >1h after first warn
        assert bridge.publish_notification.call_count == 1
        rewarns = [r for r in caplog.records if "remains a no-op" in r.getMessage()]
        assert len(rewarns) == 1

    def test_recovery_emits_info_once(self):
        controller, _, _, _ = _make_controller()
        bridge = MagicMock()
        _set_ess(controller, external=False)
        _tick(controller, 1000.0, bridge)
        _tick(controller, 1301.0, bridge)  # warning out
        _set_ess(controller, external=True)
        _tick(controller, 1400.0, bridge)  # recovery info
        _set_ess(controller, external=False)
        _tick(controller, 1500.0, bridge)  # re-arm, no duplicate
        assert bridge.publish_notification.call_count == 2
        assert bridge.publish_notification.call_args_list[1].kwargs["level"] == "info"
        assert not controller._ess_notification_active

    def test_dry_run_resets_tracker(self):
        controller, _, _, _ = _make_controller()
        controller.dry_run = True
        controller._ess_not_external_since = 900.0
        controller._ess_notification_active = True
        _tick(controller, 1000.0, MagicMock())
        assert controller._ess_not_external_since is None
        assert not controller._ess_notification_active

    def test_flap_does_not_spam(self):
        controller, _, _, _ = _make_controller()
        bridge = MagicMock()
        _set_ess(controller, external=False)
        _tick(controller, 1000.0, bridge)
        _set_ess(controller, external=True)
        _tick(controller, 1200.0, bridge)  # flip resets timer
        _set_ess(controller, external=False)
        _tick(controller, 1400.0, bridge)  # re-arm from scratch
        _set_ess(controller, external=True)
        _tick(controller, 1600.0, bridge)
        bridge.publish_notification.assert_not_called()
