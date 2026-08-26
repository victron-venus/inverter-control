"""Tests for the WebhookServer class and its endpoints."""

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

from inverter_control.webhook_server import WebhookServer


def _start_server(pre_charge_callback=None, forecast_callback=None) -> WebhookServer:
    server = WebhookServer(
        host="127.0.0.1",
        port=0,
        pre_charge_callback=pre_charge_callback,
        forecast_callback=forecast_callback,
    )
    server.start()
    return server


def _post(port: int, endpoint: str, body: dict | str) -> tuple[int, dict]:
    data = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port: int, endpoint: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{endpoint}",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestWebhookHandler:
    def test_pre_charge_valid_payload_triggers_callback(self):
        """Valid pre-charge payload should call the callback and return 200."""
        callback = MagicMock(return_value=True)
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 1000,
                    "threshold_wh": 2000,
                    "horizon_hours": 6,
                },
            )
            assert status == 200
            assert body["status"] == "pre-charge triggered"
            callback.assert_called_once()
        finally:
            server.stop()

    def test_pre_charge_unknown_trigger_returns_400(self):
        """Unknown trigger should return 400 error."""
        callback = MagicMock()
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/pre-charge",
                {
                    "trigger": "unknown_trigger",
                    "forecast_energy_wh": 1000,
                    "threshold_wh": 2000,
                },
            )
            assert status == 400
            assert body["error"] == "Unknown trigger: unknown_trigger"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_pre_charge_missing_fields_returns_400(self):
        """Missing forecast_energy_wh or threshold_wh should return 400."""
        callback = MagicMock()
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            # Missing threshold_wh
            status, body = _post(
                port,
                "api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 1000,
                },
            )
            assert status == 400
            assert "Missing forecast_energy_wh or threshold_wh" in body["error"]
            callback.assert_not_called()
        finally:
            server.stop()

    def test_pre_charge_non_dict_payload_returns_400(self):
        """Payload that is not a dict (after json.loads) should return 400."""
        callback = MagicMock()
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            # Send a JSON array as a string
            status, body = _post(
                port,
                "api/v1/pre-charge",
                '["not", "a", "dict"]',
            )
            assert status == 400
            assert body["error"] == "Invalid JSON payload"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_pre_charge_invalid_json_returns_400(self):
        """Invalid JSON should return 400 error."""
        callback = MagicMock()
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/pre-charge",
                "invalid json",
            )
            assert status == 400
            assert body["error"] == "Invalid JSON"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_pre_charge_callback_exception_returns_500(self):
        """Callback raising an exception should return 500 error (generic exception block)."""
        callback = MagicMock(side_effect=ValueError("something bad"))
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 1000,
                    "threshold_wh": 2000,
                },
            )
            assert status == 500
            assert body["error"] == "something bad"
            callback.assert_called_once()
        finally:
            server.stop()

    def test_pre_charge_callback_false_returns_500(self):
        """Callback returning False should return 500 error."""
        callback = MagicMock(return_value=False)
        server = _start_server(pre_charge_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 1000,
                    "threshold_wh": 2000,
                },
            )
            assert status == 500
            assert body["error"] == "Pre-charge callback failed"
            callback.assert_called_once()
        finally:
            server.stop()

    def test_pre_charge_no_callback_returns_503(self):
        """No callback configured should return 503 error."""
        server = _start_server(pre_charge_callback=None)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 1000,
                    "threshold_wh": 2000,
                },
            )
            assert status == 503
            assert body["error"] == "Pre-charge handler not configured"
        finally:
            server.stop()

    def test_forecast_valid_payload_triggers_callback(self):
        """Valid forecast payload should call the callback and return 200."""
        callback = MagicMock(return_value=True)
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                {
                    "today_kwh": 12.3,
                    "tomorrow_kwh": 9.4,
                    "date": "2026-08-22",
                },
            )
            assert status == 200
            assert body["status"] == "forecast stored"
            callback.assert_called_once()
        finally:
            server.stop()

    def test_forecast_non_numeric_kwh_returns_400(self):
        """Non-numeric today_kwh or tomorrow_kwh should return 400."""
        callback = MagicMock()
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                {
                    "today_kwh": "not_a_number",
                    "tomorrow_kwh": 9.4,
                },
            )
            assert status == 400
            assert body["error"] == "Missing numeric today_kwh/tomorrow_kwh"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_forecast_missing_kwh_returns_400(self):
        """Missing today_kwh or tomorrow_kwh should return 400."""
        callback = MagicMock()
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                {
                    "date": "2026-08-22",
                    "today_kwh": 12.3,
                },
            )
            assert status == 400
            assert body["error"] == "Missing numeric today_kwh/tomorrow_kwh"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_forecast_non_dict_payload_returns_400(self):
        """Payload that is not a dict (after json.loads) should return 400."""
        callback = MagicMock()
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            # Send a JSON array as a string
            status, body = _post(
                port,
                "api/v1/forecast",
                '["not", "a", "dict"]',
            )
            assert status == 400
            assert body["error"] == "Invalid JSON payload"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_forecast_invalid_json_returns_400(self):
        """Invalid JSON should return 400 error."""
        callback = MagicMock()
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                "invalid json",
            )
            assert status == 400
            assert body["error"] == "Invalid JSON"
            callback.assert_not_called()
        finally:
            server.stop()

    def test_forecast_callback_exception_returns_500(self):
        """Callback raising an exception should return 500 error (generic exception block)."""
        callback = MagicMock(side_effect=ValueError("forecast error"))
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                {
                    "today_kwh": 12.3,
                    "tomorrow_kwh": 9.4,
                },
            )
            assert status == 500
            assert body["error"] == "forecast error"
            callback.assert_called_once()
        finally:
            server.stop()

    def test_forecast_callback_false_returns_500(self):
        """Callback returning False should return 500 error."""
        callback = MagicMock(return_value=False)
        server = _start_server(forecast_callback=callback)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                {
                    "today_kwh": 12.3,
                    "tomorrow_kwh": 9.4,
                },
            )
            assert status == 500
            assert body["error"] == "Forecast callback failed"
            callback.assert_called_once()
        finally:
            server.stop()

    def test_forecast_no_callback_returns_503(self):
        """No callback configured should return 503 error."""
        server = _start_server(forecast_callback=None)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "api/v1/forecast",
                {
                    "today_kwh": 12.3,
                    "tomorrow_kwh": 9.4,
                },
            )
            assert status == 503
            assert body["error"] == "Forecast handler not configured"
        finally:
            server.stop()

    def test_health_endpoint_returns_200(self):
        """GET /health should return 200 OK."""
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _get(port, "health")
            assert status == 200
            assert body["status"] == "ok"
        finally:
            server.stop()

    def test_unknown_endpoint_returns_404(self):
        """GET to unknown endpoint should return 404."""
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _get(port, "unknown")
            assert status == 404
            assert body["error"] == "Not found"
        finally:
            server.stop()

    def test_post_to_unknown_endpoint_returns_404(self):
        """POST to unknown endpoint should return 404."""
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _post(port, "unknown", {})
            assert status == 404
            assert body["error"] == "Not found"
        finally:
            server.stop()

    def test_webhook_server_start_when_already_running(self):
        """Calling start() on an already running server should not create a new thread."""
        server = _start_server()
        try:
            # Store the original thread
            original_thread = server._thread
            assert original_thread is not None and original_thread.is_alive()

            # Try to start again
            server.start()

            # The thread should be the same (not a new one)
            assert server._thread is original_thread
        finally:
            server.stop()

    def test_webhook_server_stop(self):
        """Stopping the server sets _running to False and joins the thread."""
        server = _start_server()
        assert server._running
        server.stop()
        assert not server._running
