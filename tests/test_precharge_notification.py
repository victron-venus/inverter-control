"""Pre-charge is explicit, durable and at most once across transports."""

import concurrent.futures
import time
from unittest.mock import Mock, patch

import pytest

from inverter_control.precharge import PrechargeInbox


def request(**changes):
    now = time.time()
    return {
        "version": 1,
        "request_id": "test-day",
        "trigger": "low_solar_forecast",
        "forecast_energy_wh": 0,
        "threshold_wh": 6000,
        "issued_at": now,
        "expires_at": now + 300,
        **changes,
    }


def test_concurrent_deliveries_and_restart_never_repeat(tmp_path):
    path = tmp_path / "requests.json"
    inbox = PrechargeInbox(path)
    accept = Mock()
    payload = request()
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda _: inbox.handle(payload, lambda: False, accept), range(16)))
    assert [o["status"] for o in outcomes].count("accepted") == 1
    assert [o["status"] for o in outcomes].count("duplicate") == 15
    accept.assert_called_once()
    assert PrechargeInbox(path).handle(payload, lambda: False, accept)["status"] == "duplicate"
    accept.assert_called_once()


def test_suppression_is_explicit_and_persistent(tmp_path):
    path = tmp_path / "requests.json"
    accept = Mock()
    payload = request()
    result = PrechargeInbox(path).handle(payload, lambda: True, accept)
    assert (result["status"], result["http_status"]) == ("suppressed", 409)
    result = PrechargeInbox(path).handle(payload, lambda: False, accept)
    assert result["original_status"] == "suppressed"
    accept.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"forecast_energy_wh": float("nan")},
        {"forecast_energy_wh": True},
        {"forecast_energy_wh": -1},
        {"threshold_wh": "6000"},
        {"request_id": "x/+"},
        {"version": 2},
        {"threshold_wh": 0},
        {"expires_at": 1},
        {"issued_at": time.time() + 3600},
    ],
)
def test_invalid_or_expired_never_actuates(changes):
    accept = Mock()
    assert (
        PrechargeInbox().handle(request(**changes), lambda: False, accept)["status"] == "rejected"
    )
    accept.assert_not_called()


def test_corrupt_or_failed_persistence_is_fail_closed(tmp_path):
    path = tmp_path / "requests.json"
    path.write_text("broken")
    accept = Mock()
    assert PrechargeInbox(path).handle(request(), lambda: False, accept)["status"] == "unavailable"
    with patch("inverter_control.precharge.os.replace", side_effect=OSError):
        assert (
            PrechargeInbox(tmp_path / "new.json").handle(request(), lambda: False, accept)["status"]
            == "unavailable"
        )
    accept.assert_not_called()


def test_http_exposes_suppression_without_claiming_charge():
    from test_webhook_server import _post, _start_server

    server = _start_server(
        pre_charge_callback=lambda p: PrechargeInbox().handle(p, lambda: True, Mock())
    )
    try:
        code, body = _post(server._server.server_port, "api/v1/pre-charge", request())
        assert code == 409
        assert body["status"] == "suppressed"
    finally:
        server.stop()


@pytest.mark.parametrize(
    "expired,expensive,expected", [(False, False, True), (True, False, False), (False, True, False)]
)
def test_queued_intent_rechecks_expiry_and_tariff(expired, expensive, expected):
    from test_main import _make_controller

    controller, victron, _, calc = _make_controller()
    controller._pre_charge_requested = True
    controller._pre_charge_expires_at = time.time() + (-1 if expired else 100)
    victron.get_mppt_data.return_value = {}
    victron.get_pv_power.return_value = []
    with patch.object(controller, "_in_expensive_window", return_value=expensive):
        controller.calculate_setpoint(
            {"_grid_valid": True, **dict.fromkeys(("g1", "g2", "gt", "t1", "t2", "tt"), 0)}
        )
    assert calc.calculate.call_args.args[0].charge_battery is expected
    assert controller._pre_charge_requested is False
