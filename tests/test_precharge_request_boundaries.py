"""HTTP framing and expiry regressions using actual handlers without sockets."""

import io
import json
import threading
from http.client import HTTPMessage
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control.mqtt_bridge import MQTTBridge
from inverter_control.precharge import PrechargeInbox
from inverter_control.webhook_server import MAX_WEBHOOK_BODY_BYTES, WebhookHandler


def request(request_id="boundary"):
    return {
        "version": 1,
        "request_id": request_id,
        "trigger": "low_solar_forecast",
        "forecast_energy_wh": 100,
        "threshold_wh": 6000,
        "issued_at": 1000.0,
        "expires_at": 1001.0,
    }


def invoke(route, raw, callback, *, content_length=None):
    replies = []
    reader = Mock(wraps=io.BytesIO(raw))
    headers = HTTPMessage()
    headers["Content-Length"] = str(len(raw)) if content_length is None else content_length
    handler = SimpleNamespace(
        headers=headers,
        rfile=reader,
        close_connection=False,
        pre_charge_callback=callback,
        forecast_callback=callback,
        _send_response=lambda code, body: replies.append((code, body)),
    )
    getattr(WebhookHandler, "_handle_" + route)(handler)
    return replies, reader, handler


@pytest.mark.parametrize("route", ["pre_charge", "forecast"])
@pytest.mark.parametrize(
    "length,code", [("-1", 400), ("invalid", 400), ("1.5", 400), ("4097", 413)]
)
def test_invalid_length_rejects_before_read_and_closes(route, length, code):
    callback = Mock()
    replies, reader, handler = invoke(route, b"{}", callback, content_length=length)
    assert replies[0][0] == code
    reader.read.assert_not_called()
    callback.assert_not_called()
    assert handler.close_connection is True


@pytest.mark.parametrize("route", ["pre_charge", "forecast"])
def test_short_read_never_reaches_callback(route):
    raw = json.dumps({**request(), "today_kwh": 1, "tomorrow_kwh": 2}).encode()
    callback = Mock()
    replies, reader, handler = invoke(route, raw, callback, content_length=str(len(raw) + 1))
    assert replies == [(400, {"error": "Incomplete request body"})]
    reader.read.assert_called_once_with(len(raw) + 1)
    callback.assert_not_called()
    assert handler.close_connection is True


@pytest.mark.parametrize("route", ["pre_charge", "forecast"])
def test_invalid_utf8_is_bad_request(route):
    callback = Mock()
    replies, _, _ = invoke(route, b"\xff", callback)
    assert replies == [(400, {"error": "Invalid JSON"})]
    callback.assert_not_called()


@pytest.mark.parametrize("size,expected", [(4096, 202), (4097, 413)])
def test_http_mqtt_size_boundary_parity(size, expected):
    payload = request()
    payload["padding"] = ""
    payload["padding"] = "x" * (size - len(json.dumps(payload).encode()))
    raw = json.dumps(payload).encode()
    assert len(raw) == size
    assert MAX_WEBHOOK_BODY_BYTES == 4096
    callback = Mock(
        return_value={"request_id": "boundary", "status": "accepted", "http_status": 202}
    )
    replies, reader, _ = invoke("pre_charge", raw, callback)
    assert replies[0][0] == expected
    assert callback.call_count == int(size == 4096)
    assert reader.read.call_count == int(size == 4096)
    callback.reset_mock()
    client = Mock()
    MQTTBridge._on_message(
        SimpleNamespace(forecast_prefix="synthetic/site", _callbacks={"pre_charge": callback}),
        client,
        None,
        SimpleNamespace(topic="synthetic/site/pre_charge_request", payload=raw, retain=False),
    )
    assert callback.call_count == int(size == 4096)
    assert client.publish.call_count == int(size == 4096)


def test_daily_summary_payload_and_callback_are_unchanged():
    payload = {
        "site_id": "synthetic-site",
        "generated_at": "2026-09-29T12:00:00+00:00",
        "date": "2026-09-29",
        "today_kwh": 12.3,
        "tomorrow_kwh": 14.8,
    }
    callback = Mock(return_value=True)
    replies, _, handler = invoke("forecast", json.dumps(payload).encode(), callback)
    assert replies == [(200, {"status": "forecast stored"})]
    callback.assert_called_once_with(payload)
    assert handler.close_connection is False


@pytest.mark.parametrize("route", ["pre-charge", "forecast"])
@pytest.mark.parametrize("length", ["-1", "4097", "invalid"])
def test_unread_rejected_body_cannot_be_parsed_as_next_keepalive_request(route, length):
    callback = Mock(return_value=True)

    class KeepAliveHandler(WebhookHandler):
        protocol_version = "HTTP/1.1"
        pre_charge_callback = staticmethod(callback)
        forecast_callback = staticmethod(callback)

    headers = f"POST /api/v1/{route} HTTP/1.1\r\nHost: fixture\r\nContent-Length: {length}\r\n\r\n".encode()
    # Deliberately request-shaped unread body: accepting it as the next message
    # would return a second /health response on the same connection.
    raw = headers + b"GET /health HTTP/1.1\r\nHost: fixture\r\n\r\n"
    handler = KeepAliveHandler.__new__(KeepAliveHandler)
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 1)
    handler.server = SimpleNamespace(server_name="fixture", server_port=0)
    handler.handle()
    assert handler.wfile.getvalue().count(b"HTTP/1.1 ") == 1
    assert handler.rfile.tell() == len(headers)
    assert handler.close_connection is True
    callback.assert_not_called()


def test_request_expiring_behind_another_delivery_never_enqueues(tmp_path, monkeypatch):
    inbox = PrechargeInbox(tmp_path / "journal.json")
    entered = threading.Event()
    release = threading.Event()
    second_timed = threading.Event()
    now = [1000.0]
    outcomes = {}
    errors = []
    accept_second = Mock()

    def clock():
        value = now[0]
        if threading.current_thread().name == "waiting-delivery":
            second_timed.set()
        return value

    def accept_first():
        entered.set()
        if not release.wait(2):
            raise RuntimeError("fixture timeout")

    def deliver(name, accept):
        try:
            outcomes[name] = inbox.handle(request(name), lambda: False, accept)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr("inverter_control.precharge.time.time", clock)
    first = threading.Thread(target=deliver, args=("first", accept_first))
    second = threading.Thread(
        name="waiting-delivery", target=deliver, args=("second", accept_second)
    )
    try:
        first.start()
        assert entered.wait(2)
        second.start()
        assert second_timed.wait(2)
        now[0] = 1002.0
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert outcomes["second"]["http_status"] == 410
    accept_second.assert_not_called()
    assert "second" not in inbox.records
    assert "second" not in json.loads(inbox.path.read_text())


@pytest.mark.parametrize(
    "record,status,reason",
    [
        ({"status": "accepted", "queued": True}, "duplicate", "already_decided"),
        ({"status": "suppressed"}, "duplicate", "already_decided"),
        ({"status": "uncertain"}, "unavailable", "decision_uncertain"),
        ({"status": "accepted"}, "unavailable", "legacy_decision_unverified"),
    ],
)
def test_persisted_response_survives_expiry_while_waiting(
    tmp_path, monkeypatch, record, status, reason
):
    path = tmp_path / "journal.json"
    original = json.dumps({"boundary": {**record, "until": 1000.0 + 172800}})
    path.write_text(original)
    inbox = PrechargeInbox(path)
    timed = threading.Event()
    now = [1000.0]
    outcomes = []
    accept = Mock()

    def clock():
        value = now[0]
        timed.set()
        return value

    monkeypatch.setattr("inverter_control.precharge.time.time", clock)
    inbox.lock.acquire()
    worker = threading.Thread(
        target=lambda: outcomes.append(inbox.handle(request(), lambda: False, accept))
    )
    try:
        worker.start()
        assert timed.wait(2)
        now[0] = 1002.0
    finally:
        inbox.lock.release()
        worker.join(2)
    assert not worker.is_alive()
    assert (outcomes[0]["status"], outcomes[0]["reason"]) == (status, reason)
    accept.assert_not_called()
    assert path.read_text() == original


def parse_memory_request(route, raw, framing):
    """Use the actual HTTP parser/keep-alive loop, without a socket."""
    callback = Mock(return_value=True)

    class KeepAliveHandler(WebhookHandler):
        protocol_version = "HTTP/1.1"
        pre_charge_callback = staticmethod(callback)
        forecast_callback = staticmethod(callback)

    headers = f"POST /api/v1/{route} HTTP/1.1\r\nHost: fixture\r\n{framing}\r\n".encode()
    following = b"GET /health HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n"
    handler = KeepAliveHandler.__new__(KeepAliveHandler)
    handler.rfile = io.BytesIO(headers + raw + following)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 1)
    handler.server = SimpleNamespace(server_name="fixture", server_port=0)
    handler.handle()
    return handler, callback, len(headers)


@pytest.mark.parametrize("route", ["pre-charge", "forecast"])
@pytest.mark.parametrize(
    "framing",
    [
        "Content-Length: {length}\r\ncontent-length: 999999\r\n",
        "Content-Length: {length}\r\nContent-Length: {length}\r\n",
        "Transfer-Encoding: chunked\r\nContent-Length: {length}\r\n",
        "transfer-encoding: identity\r\nContent-Length: {length}\r\n",
        "Transfer-Encoding:\r\nContent-Length: {length}\r\n",
        "Transfer-Encoding: chunked\r\n",
        "Content-Length: +{length}\r\n",
        "Content-Length: {length}, {length}\r\n",
    ],
)
def test_ambiguous_framing_rejected_before_body_and_following_request(route, framing):
    raw = json.dumps({**request(), "today_kwh": 1, "tomorrow_kwh": 2}).encode()
    handler, callback, header_size = parse_memory_request(
        route, raw, framing.format(length=len(raw))
    )
    response = handler.wfile.getvalue()
    assert response.startswith(b"HTTP/1.1 400 ")
    assert response.count(b"HTTP/1.1 ") == 1
    assert handler.rfile.tell() == header_size
    assert handler.close_connection is True
    callback.assert_not_called()


@pytest.mark.parametrize("route", ["pre_charge", "forecast"])
@pytest.mark.parametrize("length", ["\u0661", "\uff11", "1_0", "1\n", "+1"])
def test_length_requires_ascii_decimal_digits(route, length):
    callback = Mock()
    replies, reader, handler = invoke(route, b"{}", callback, content_length=length)
    assert replies[0][0] == 400
    reader.read.assert_not_called()
    callback.assert_not_called()
    assert handler.close_connection is True


@pytest.mark.parametrize("route", ["pre-charge", "forecast"])
def test_ascii_decimal_with_ows_and_leading_zeroes_is_valid(route):
    payload = {**request(), "today_kwh": 1, "tomorrow_kwh": 2}
    raw = json.dumps(payload).encode()
    handler, callback, _ = parse_memory_request(
        route, raw, f"Content-Length: \t000{len(raw)} \t\r\n"
    )
    expected = 202 if route == "pre-charge" else 200
    response = handler.wfile.getvalue()
    assert response.startswith(f"HTTP/1.1 {expected} ".encode())
    assert response.count(b"HTTP/1.1 ") == 2
    callback.assert_called_once_with(payload)


@pytest.mark.parametrize("route", ["pre_charge", "forecast"])
def test_huge_decimal_length_is_413_without_integer_parsing_or_body_read(route):
    callback = Mock()
    replies, reader, handler = invoke(route, b"{}", callback, content_length="9" * 5000)
    assert replies[0][0] == 413
    reader.read.assert_not_called()
    callback.assert_not_called()
    assert handler.close_connection is True
