#!/usr/bin/env python3
"""
HTTP Webhook Server for Inverter Control
Receives pre-charge triggers from solar-forecast-langgraph
Uses Python standard library only (no extra dependencies).
"""

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("inverter-control")

# Match the existing MQTT pre-charge request bound; forecast summaries are small JSON objects.
MAX_WEBHOOK_BODY_BYTES = 4096


def _read_json_payload(handler) -> dict | None:
    """Read one bounded object, or respond without invoking a callback."""
    lengths = handler.headers.get_all("Content-Length", ["0"])
    raw_length = lengths[0].strip(" \t") if len(lengths) == 1 else ""
    content_length = -1
    if (
        handler.headers.get_all("Transfer-Encoding") is None
        and raw_length
        and raw_length.isascii()
        and raw_length.isdecimal()
    ):
        digits = raw_length.lstrip("0") or "0"
        # Avoid parsing an unbounded integer, while allowing leading zeroes.
        content_length = (
            MAX_WEBHOOK_BODY_BYTES + 1
            if len(digits) > len(str(MAX_WEBHOOK_BODY_BYTES))
            else int(digits)
        )
    if content_length < 0 or content_length > MAX_WEBHOOK_BODY_BYTES:
        # The body remains unread. Never interpret it as another keep-alive request.
        handler.close_connection = True
        status = 400 if content_length < 0 else 413
        error = "Invalid request framing" if status == 400 else "Request body too large"
        handler._send_response(status, {"error": error})
        return None
    raw_data = handler.rfile.read(content_length)
    if len(raw_data) != content_length:
        handler.close_connection = True
        handler._send_response(400, {"error": "Incomplete request body"})
        return None
    payload = json.loads(raw_data.decode("utf-8")) if raw_data else {}
    if not isinstance(payload, dict):
        handler._send_response(400, {"error": "Invalid JSON payload"})
        return None
    return payload


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for webhook endpoints."""

    # Class-level callback storage (set by server instance)
    pre_charge_callback: Callable[[dict], bool | dict] | None = None
    forecast_callback: Callable[[dict], bool | dict] | None = None

    def _send_response(self, status: int, data: dict):
        """Send JSON response."""
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        """Handle POST requests."""
        if self.path == "/api/v1/pre-charge":
            self._handle_pre_charge()
        elif self.path == "/api/v1/forecast":
            self._handle_forecast()
        else:
            self._send_response(404, {"error": "Not found"})

    def _handle_pre_charge(self):
        """Handle pre-charge webhook."""
        try:
            payload = _read_json_payload(self)
            if payload is None:
                return

            trigger = payload.get("trigger")
            forecast_energy_wh = payload.get("forecast_energy_wh")
            threshold_wh = payload.get("threshold_wh")

            if trigger != "low_solar_forecast":
                self._send_response(400, {"error": f"Unknown trigger: {trigger}"})
                return

            if forecast_energy_wh is None or threshold_wh is None:
                self._send_response(400, {"error": "Missing forecast_energy_wh or threshold_wh"})
                return

            logger.info("Pre-charge webhook received")

            # Call the registered callback
            if self.pre_charge_callback:
                success = self.pre_charge_callback(payload)
                if isinstance(success, dict):
                    self._send_response(
                        success["http_status"],
                        {k: v for k, v in success.items() if k != "http_status"},
                    )
                elif success:
                    self._send_response(202, {"status": "accepted"})
                else:
                    self._send_response(500, {"error": "Pre-charge callback failed"})
            else:
                self._send_response(503, {"error": "Pre-charge handler not configured"})

        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(400, {"error": "Invalid JSON"})
        except Exception as e:
            logger.exception("Pre-charge webhook error")
            self._send_response(500, {"error": str(e)})

    def _handle_forecast(self):
        """Handle daily forecast summary from solar-forecast-langgraph."""
        try:
            payload = _read_json_payload(self)
            if payload is None:
                return

            today_kwh = payload.get("today_kwh")
            tomorrow_kwh = payload.get("tomorrow_kwh")

            if not isinstance(today_kwh, int | float) or not isinstance(tomorrow_kwh, int | float):
                self._send_response(400, {"error": "Missing numeric today_kwh/tomorrow_kwh"})
                return

            logger.info(
                f"Forecast webhook received: today={today_kwh:.1f}kWh "
                f"tomorrow={tomorrow_kwh:.1f}kWh date={payload.get('date')}"
            )

            if self.forecast_callback:
                success = self.forecast_callback(payload)
                if success:
                    self._send_response(200, {"status": "forecast stored"})
                else:
                    self._send_response(500, {"error": "Forecast callback failed"})
            else:
                self._send_response(503, {"error": "Forecast handler not configured"})

        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(400, {"error": "Invalid JSON"})
        except Exception as e:
            logger.exception("Forecast webhook error")
            self._send_response(500, {"error": str(e)})

    def do_GET(self):
        """Handle GET requests (health check)."""
        if self.path == "/health":
            self._send_response(200, {"status": "ok"})
        else:
            self._send_response(404, {"error": "Not found"})

    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.debug("%s - %s", self.address_string(), format % args)


class WebhookServer:
    """Threaded HTTP server for webhook endpoints."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8081,
        pre_charge_callback: Callable[[dict], bool | dict] | None = None,
        forecast_callback: Callable[[dict], bool | dict] | None = None,
    ):
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        # Store callbacks in handler class
        self._handler = type(
            "BoundWebhookHandler",
            (WebhookHandler,),
            {
                "pre_charge_callback": staticmethod(pre_charge_callback)
                if pre_charge_callback
                else None,
                "forecast_callback": staticmethod(forecast_callback) if forecast_callback else None,
            },
        )

    def start(self):
        """Start the server in a background thread."""
        if self._running:
            return

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), self._handler)
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="WebhookServer")
            self._thread.start()
            logger.info(f"Webhook server started on {self.host}:{self.port}")
        except Exception:
            logger.exception("Failed to start webhook server")
            self._running = False

    def _run(self):
        """Server run loop."""
        if self._server:
            self._server.serve_forever()

    def stop(self):
        """Stop the server."""
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("Webhook server stopped")


# Global instance
_webhook_server: WebhookServer | None = None


def get_webhook_server(
    host: str = "127.0.0.1",
    port: int = 8081,
    pre_charge_callback: Callable[[dict], bool | dict] | None = None,
    forecast_callback: Callable[[dict], bool | dict] | None = None,
) -> WebhookServer:
    """Get or create webhook server singleton."""
    global _webhook_server
    if _webhook_server is None:
        _webhook_server = WebhookServer(host, port, pre_charge_callback, forecast_callback)
    return _webhook_server
