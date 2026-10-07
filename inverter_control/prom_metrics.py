"""Optional Prometheus exposition of CycleMetrics on :9102/metrics.

Enabled when INVERTER_METRICS_PORT is set and prometheus_client is
installed. Binding and recovery run in a background supervisor so the
control loop never waits for the monitoring network to become available.
"""

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("inverter-control")

_RETRY_INITIAL_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0
_HEALTH_CHECK_SECONDS = 1.0
_STOP_TIMEOUT_SECONDS = 2.0

_gauges: dict[str, Any] | None = None
_server: "_MetricsServer | None" = None
_lifecycle_lock = threading.Lock()


def _make_gauges(gauge_type: Any, registry: Any) -> dict[str, Any]:
    def gauge(name, description, labels=()):
        return gauge_type(name, description, labels, registry=registry)

    return {
        "cycle": gauge("inverter_control_cycle_ms", "Control cycle duration ms", ["quantile"]),
        "missed_deadlines": gauge(
            "inverter_control_missed_deadlines_total", "Missed cycle deadlines"
        ),
        "write": gauge(
            "inverter_control_setvalue_ms", "Grid setpoint write duration ms", ["quantile"]
        ),
        "failed_writes": gauge("inverter_control_failed_writes_total", "Failed setpoint writes"),
        "age": gauge("inverter_control_snapshot_age_ms", "Telemetry snapshot age ms", ["quantile"]),
        "stage": gauge(
            "inverter_control_stage_ms",
            "Control cycle per-stage duration ms",
            ["stage", "quantile"],
        ),
        "signals": gauge(
            "inverter_control_signals_healthy",
            "D-Bus fast-signal path health (1=healthy)",
        ),
        "subprocess": gauge(
            "inverter_control_dbus_subprocess_calls_total",
            "dbus-send subprocess spawns (storm canary)",
        ),
        "cpu": gauge("inverter_control_cpu_percent", "Process CPU percent"),
        "rss": gauge("inverter_control_rss_mb", "Process RSS MB"),
    }


class _MetricsServer:
    """Own one listener and recover it without involving the control loop."""

    def __init__(self, host: str, port: int, registry: Any, starter: Any):
        self.host = host
        self.port = port
        self.registry = registry
        self.starter = starter
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="metrics-supervisor", daemon=True)

    def _serve(self) -> None:
        server, thread = self.starter(self.port, addr=self.host, registry=self.registry)
        try:
            logger.info("Prometheus metrics server started on %s:%s", self.host, self.port)
            while not self.stop_event.wait(_HEALTH_CHECK_SECONDS):
                if not thread.is_alive():
                    raise RuntimeError("metrics HTTP serving thread stopped")
        finally:
            # shutdown() must not be called from the serving thread itself.
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=_STOP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        delay = _RETRY_INITIAL_SECONDS
        while not self.stop_event.is_set():
            started_at = time.monotonic()
            try:
                self._serve()
            except Exception as exc:  # Optional telemetry must never stop inverter control.
                if self.stop_event.is_set():
                    break
                if time.monotonic() - started_at >= _RETRY_MAX_SECONDS:
                    delay = _RETRY_INITIAL_SECONDS
                logger.warning(
                    "Metrics server unavailable on %s:%s: %s; retrying in %gs",
                    self.host,
                    self.port,
                    exc,
                    delay,
                )
            if self.stop_event.wait(delay):
                break
            delay = min(delay * 2, _RETRY_MAX_SECONDS)


def start() -> bool:
    """Enable background metrics recovery; True does not imply a listener is ready.

    A LAN address may be unavailable during boot. Retry that same address instead
    of permanently disabling metrics or widening the listener to all interfaces.
    Repeated calls are idempotent while the supervisor is alive.
    """
    global _gauges, _server
    with _lifecycle_lock:
        if _server is not None and _server.thread.is_alive():
            return True
        port = os.environ.get("INVERTER_METRICS_PORT")
        if not port:
            return False
        try:
            port_number = int(port)
            if not 1 <= port_number <= 65535:
                raise ValueError("port must be between 1 and 65535")
        except ValueError as exc:
            logger.warning("Metrics server disabled: invalid INVERTER_METRICS_PORT: %s", exc)
            return False
        try:
            from prometheus_client import (
                CollectorRegistry,
                Gauge,
                GCCollector,
                PlatformCollector,
                ProcessCollector,
                start_http_server,
            )
        except ImportError:
            logger.info("prometheus_client not installed, /metrics disabled")
            return False
        try:
            registry = CollectorRegistry()
            # Preserve the default diagnostics, without global registrations that
            # would conflict when the service is stopped and started again.
            GCCollector(registry=registry)
            PlatformCollector(registry=registry)
            ProcessCollector(registry=registry)
            _gauges = _make_gauges(Gauge, registry)
            _server = _MetricsServer(
                os.environ.get("INVERTER_METRICS_HOST", "127.0.0.1"),
                port_number,
                registry,
                start_http_server,
            )
            _server.thread.start()
        except Exception as exc:
            _server = None
            _gauges = None
            logger.warning("Metrics supervisor could not start: %s", exc)
            return False
        return True


def stop() -> None:
    """Stop retries and release the listener, with bounded shutdown waiting."""
    global _server, _gauges
    with _lifecycle_lock:
        server = _server
        if server is None:
            return
        server.stop_event.set()
    server.thread.join(timeout=_STOP_TIMEOUT_SECONDS)
    with _lifecycle_lock:
        if _server is server and not server.thread.is_alive():
            _server = None
            _gauges = None
        elif server.thread.is_alive():
            logger.warning("Metrics server shutdown is still pending")


def _set_gauge(name: str, value: Any, *label_values: str) -> None:
    """Set one gauge; never raise (metrics export must not break the loop)."""
    if value is None:
        return
    try:
        gauge = _gauges[name]
        if label_values:
            gauge.labels(*label_values).set(float(value))
        else:
            gauge.set(float(value))
    except (KeyError, TypeError, ValueError):
        pass  # Metrics export must never break the control loop


def _set_stage_metrics(snapshot: dict[str, Any]) -> None:
    """Push the per-stage percentile gauges."""
    for stage, stats in snapshot.get("stage_ms", {}).items():
        for quantile in ("p50", "p95", "max"):
            value = stats.get(quantile)
            if value is None:
                continue
            _set_gauge("stage", value, stage, quantile)


def _publish(snapshot: dict[str, Any]) -> None:
    if _gauges is None or not snapshot:
        return

    cycles = snapshot.get("cycle_ms", {})
    _set_gauge("cycle", cycles.get("p50"), "p50")
    _set_gauge("cycle", cycles.get("p95"), "p95")
    _set_gauge("cycle", cycles.get("max"), "max")
    _set_gauge("missed_deadlines", cycles.get("missed_deadlines"))

    writes = snapshot.get("setvalue_ms", {})
    _set_gauge("write", writes.get("p50"), "p50")
    _set_gauge("write", writes.get("p95"), "p95")
    _set_gauge("write", writes.get("max"), "max")
    _set_gauge("failed_writes", writes.get("failed"))

    ages = snapshot.get("snapshot_age_ms", {})
    _set_gauge("age", ages.get("p50"), "p50")
    _set_gauge("age", ages.get("max"), "max")

    _set_gauge("signals", snapshot.get("signals_healthy"))
    _set_gauge("subprocess", snapshot.get("dbus_subprocess_calls"))

    _set_stage_metrics(snapshot)

    _set_gauge("cpu", snapshot.get("cpu_percent"))
    _set_gauge("rss", snapshot.get("rss_mb"))


def publish(snapshot: dict[str, Any]) -> None:
    """Push a CycleMetrics.snapshot() dict into the gauges (no-op if disabled)."""
    _publish(snapshot)
