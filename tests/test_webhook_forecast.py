"""Tests for /api/v1/forecast webhook endpoint."""

import json
import urllib.error
import urllib.request

from inverter_control.webhook_server import WebhookServer


class _Recorder:
    def __init__(self):
        self.payloads = []
        self.result = True

    def __call__(self, payload: dict) -> bool:
        self.payloads.append(payload)
        return self.result


def _start_server(callback) -> WebhookServer:
    server = WebhookServer(host="127.0.0.1", port=0, forecast_callback=callback)
    server.start()
    return server


def _port(server: WebhookServer) -> int:
    return server._server.server_address[1]  # pylint: disable=protected-access


def _post(port: int, body: dict | str) -> tuple[int, dict]:
    data = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/forecast",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_forecast_webhook_roundtrip():
    recorder = _Recorder()
    server = _start_server(recorder)
    try:
        status, body = _post(
            _port(server),
            {
                "date": "2026-08-22",
                "today_kwh": 12.3,
                "tomorrow_kwh": 9.4,
                "generated_at": "2026-08-22T06:00:00+00:00",
            },
        )
        assert status == 200
        assert body["status"] == "forecast stored"
        assert recorder.payloads[0]["today_kwh"] == 12.3
    finally:
        server.stop()


def test_forecast_webhook_rejects_missing_kwh():
    recorder = _Recorder()
    server = _start_server(recorder)
    try:
        status, _body = _post(_port(server), {"date": "2026-08-22"})
        assert status == 400
        assert not recorder.payloads
    finally:
        server.stop()


def test_forecast_webhook_callback_failure_returns_500():
    recorder = _Recorder()
    recorder.result = False
    server = _start_server(recorder)
    try:
        status, _body = _post(_port(server), {"today_kwh": 1, "tomorrow_kwh": 2})
        assert status == 500
    finally:
        server.stop()
