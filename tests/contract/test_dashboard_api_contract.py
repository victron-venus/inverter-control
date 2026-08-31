"""Dashboard contract tests: HTTP API shapes.

The webhooks are:
- POST /api/v1/pre-charge   — solar-forecast-langgraph → inverter-control
- POST /api/v1/forecast     — daily summary
- GET  /health              — health check

The dashboard does not consume these directly, but the desktop + Grafana
provisioning does, and the integration-tests repo starts the control loop
and asserts health. Pin the request/response shapes.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from inverter_control.webhook_server import WebhookHandler, WebhookServer

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _start_server(*, pre_charge=None, forecast=None) -> WebhookServer:
    s = WebhookServer(
        host="127.0.0.1",
        port=0,
        pre_charge_callback=pre_charge,
        forecast_callback=forecast,
    )
    s.start()
    return s


def _post(port: int, path: str, body: dict | str) -> tuple[int, dict]:
    data = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(port: int, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


class TestHealthContract:
    def test_health_returns_200_with_status_ok(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _get(port, "/health")
            assert status == 200
            assert body == {
                "status": "ok"
            }, f"dashboards/Grafana probe this body literally; got {body!r}"
        finally:
            server.stop()

    def test_unknown_get_path_returns_404(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _get(port, "/")
            assert status == 404
            assert "error" in body
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# POST /api/v1/pre-charge
# ---------------------------------------------------------------------------


class TestPreChargeContract:
    def test_valid_payload_returns_200_status(self):
        cb = MagicMock(return_value=True)
        server = _start_server(pre_charge=cb)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "/api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 500,
                    "threshold_wh": 1000,
                    "horizon_hours": 4,
                },
            )
            assert status == 200
            assert (
                body["status"] == "pre-charge triggered"
            ), f"integration-tests asserts this exact string; got {body!r}"
            cb.assert_called_once()
        finally:
            server.stop()

    @pytest.mark.parametrize(
        "missing_key,payload",
        [
            ("trigger", {"forecast_energy_wh": 500, "threshold_wh": 1000}),
            ("forecast_energy_wh", {"trigger": "low_solar_forecast", "threshold_wh": 1000}),
            ("threshold_wh", {"trigger": "low_solar_forecast", "forecast_energy_wh": 500}),
        ],
    )
    def test_missing_required_field_returns_400(self, missing_key, payload):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _post(port, "/api/v1/pre-charge", payload)
            assert status == 400
            assert "error" in body
        finally:
            server.stop()

    def test_unknown_trigger_returns_400(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "/api/v1/pre-charge",
                {
                    "trigger": "garbage",
                    "forecast_energy_wh": 500,
                    "threshold_wh": 1000,
                },
            )
            assert status == 400
            assert "error" in body
            assert "Unknown trigger" in body["error"]
        finally:
            server.stop()

    def test_invalid_json_returns_400(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _post(port, "/api/v1/pre-charge", "{not valid json")
            assert status == 400
            assert "error" in body
        finally:
            server.stop()

    def test_callback_returns_false_responds_500(self):
        """Callback signalling failure should bubble up to the caller so the
        forecast service can retry — the 500 status is part of the contract.
        """
        cb = MagicMock(return_value=False)
        server = _start_server(pre_charge=cb)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "/api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 500,
                    "threshold_wh": 1000,
                },
            )
            assert status == 500
            assert "error" in body
        finally:
            server.stop()

    def test_unconfigured_callback_returns_503(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "/api/v1/pre-charge",
                {
                    "trigger": "low_solar_forecast",
                    "forecast_energy_wh": 500,
                    "threshold_wh": 1000,
                },
            )
            assert status == 503
            assert "error" in body
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# POST /api/v1/forecast
# ---------------------------------------------------------------------------


class TestForecastContract:
    def test_valid_payload_returns_200_status(self):
        cb = MagicMock(return_value=True)
        server = _start_server(forecast=cb)
        try:
            port = server._server.server_address[1]
            status, body = _post(
                port,
                "/api/v1/forecast",
                {"today_kwh": 25.0, "tomorrow_kwh": 18.0, "date": "2026-08-30"},
            )
            assert status == 200
            assert body["status"] == "forecast stored"
            cb.assert_called_once()
        finally:
            server.stop()

    def test_missing_kwh_returns_400(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, _body = _post(port, "/api/v1/forecast", {"today_kwh": 1.0})
            assert status == 400
        finally:
            server.stop()

    def test_non_numeric_kwh_returns_400(self):
        server = _start_server()
        try:
            port = server._server.server_address[1]
            status, _body = _post(
                port,
                "/api/v1/forecast",
                {"today_kwh": "yes", "tomorrow_kwh": 1.0},
            )
            assert status == 400
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# Handler attribute assertions — pin the WebhookHandler class shape
# ---------------------------------------------------------------------------


class TestHandlerShape:
    """Static shape of `WebhookHandler` — used by integration-tests via
    `WebhookHandler` directly. Pin the public attributes.
    """

    def test_handler_has_post_paths(self):
        assert hasattr(WebhookHandler, "do_POST")
        assert hasattr(WebhookHandler, "do_GET")
        assert hasattr(WebhookHandler, "_send_response")

    def test_handler_class_callback_attrs_default_none(self):
        # Class-level callback slots (overridden by WebhookServer.start)
        assert WebhookHandler.pre_charge_callback is None or callable(
            WebhookHandler.pre_charge_callback
        )
        assert WebhookHandler.forecast_callback is None or callable(
            WebhookHandler.forecast_callback
        )
