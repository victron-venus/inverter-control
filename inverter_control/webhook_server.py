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


class WebhookHandler(BaseHTTPRequestHandler):
    """HTTP request handler for webhook endpoints."""

    # Class-level callback storage (set by server instance)
    pre_charge_callback: Callable[[dict], bool] | None = None

    def _send_response(self, status: int, data: dict):
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_POST(self):
        """Handle POST requests."""
        if self.path == "/api/v1/pre-charge":
            self._handle_pre_charge()
        else:
            self._send_response(404, {"error": "Not found"})

    def _handle_pre_charge(self):
        """Handle pre-charge webhook."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_data = self.rfile.read(content_length).decode("utf-8")
            payload = json.loads(raw_data) if raw_data else {}

            # Validate payload
            if not isinstance(payload, dict):
                self._send_response(400, {"error": "Invalid JSON payload"})
                return

            trigger = payload.get("trigger")
            forecast_energy_wh = payload.get("forecast_energy_wh")
            threshold_wh = payload.get("threshold_wh")
            horizon_hours = payload.get("horizon_hours")

            if trigger != "low_solar_forecast":
                self._send_response(400, {"error": f"Unknown trigger: {trigger}"})
                return

            if forecast_energy_wh is None or threshold_wh is None:
                self._send_response(400, {"error": "Missing forecast_energy_wh or threshold_wh"})
                return

            logger.info(
                f"Pre-charge webhook received: forecast={forecast_energy_wh:.0f}Wh "
                f"threshold={threshold_wh:.0f}Wh horizon={horizon_hours}h"
            )

            # Call the registered callback
            if self.pre_charge_callback:
                success = self.pre_charge_callback(payload)
                if success:
                    self._send_response(200, {"status": "pre-charge triggered"})
                else:
                    self._send_response(500, {"error": "Pre-charge callback failed"})
            else:
                self._send_response(503, {"error": "Pre-charge handler not configured"})

        except json.JSONDecodeError:
            self._send_response(400, {"error": "Invalid JSON"})
        except Exception as e:
            logger.exception("Pre-charge webhook error")
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
        host: str = "0.0.0.0",
        port: int = 8081,
        pre_charge_callback: Callable[[dict], bool] | None = None,
    ):
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        # Store callback in handler class
        WebhookHandler.pre_charge_callback = pre_charge_callback

    def start(self):
        """Start the server in a background thread."""
        if self._running:
            return

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), WebhookHandler)
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="WebhookServer")
            self._thread.start()
            logger.info(f"Webhook server started on {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start webhook server: {e}")
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
    host: str = "0.0.0.0",
    port: int = 8081,
    pre_charge_callback: Callable[[dict], bool] | None = None,
) -> WebhookServer:
    """Get or create webhook server singleton."""
    global _webhook_server
    if _webhook_server is None:
        _webhook_server = WebhookServer(host, port, pre_charge_callback)
    return _webhook_server
