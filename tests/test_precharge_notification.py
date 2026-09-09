"""Tests for pre-charge webhook notification body and day-scoped id."""

import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_main import _make_controller

_MOD = "inverter_control.controller"


def test_precharge_day_scoped_banner_for_today():
    controller, *_ = _make_controller()
    bridge = MagicMock()
    fixed = datetime(2026, 9, 9, 15, 30, tzinfo=UTC)

    with (
        patch("inverter_control.mqtt_bridge.get_mqtt_bridge", return_value=bridge),
        patch(f"{_MOD}.datetime") as mock_dt,
        patch.object(controller, "_in_expensive_window", return_value=False),
    ):
        mock_dt.now.return_value = fixed
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        # Keep UTC symbol used by controller
        mock_dt.UTC = UTC

        ok = controller._handle_pre_charge_webhook(
            {
                "forecast_energy_wh": 1500,
                "threshold_wh": 6000,
                "horizon_hours": 24,
                "day": "today",
                "horizon": "next_day",
            }
        )

    assert ok is True
    bridge.publish_notification.assert_called_once()
    kwargs = bridge.publish_notification.call_args.kwargs
    assert kwargs["notification_id"] == "precharge-20260909"
    assert kwargs["title"] == "Pre-charge triggered"
    assert "for today" in kwargs["body"]
    assert "in 24h" not in kwargs["body"]
    assert "1.5 kWh" in kwargs["body"]
    assert "6.0 kWh" in kwargs["body"]


def test_precharge_legacy_horizon_keeps_in_xh_body():
    controller, *_ = _make_controller()
    bridge = MagicMock()
    fixed = datetime(2026, 9, 9, 15, 30, tzinfo=UTC)

    with (
        patch("inverter_control.mqtt_bridge.get_mqtt_bridge", return_value=bridge),
        patch(f"{_MOD}.datetime") as mock_dt,
        patch.object(controller, "_in_expensive_window", return_value=False),
    ):
        mock_dt.now.return_value = fixed
        mock_dt.UTC = UTC

        ok = controller._handle_pre_charge_webhook(
            {
                "forecast_energy_wh": 300,
                "threshold_wh": 2000,
                "horizon_hours": 6,
            }
        )

    assert ok is True
    kwargs = bridge.publish_notification.call_args.kwargs
    assert kwargs["notification_id"] == "precharge-20260909"
    assert "in 6h" in kwargs["body"]
    assert "for today" not in kwargs["body"]
