"""Optional Prometheus exposition of CycleMetrics on :9102/metrics.

Enabled when INVERTER_METRICS_PORT is set and prometheus_client is
installed; silently no-ops otherwise so the control loop never depends
on it.
"""

import logging
import os
from typing import Any

logger = logging.getLogger("inverter-control")

_gauges: dict[str, Any] | None = None


def start() -> bool:
    """Start the metrics HTTP server. Returns True if enabled."""
    global _gauges
    port = os.environ.get("INVERTER_METRICS_PORT")
    if not port:
        return False
    try:
        from prometheus_client import Gauge, start_http_server
    except ImportError:
        logger.info("prometheus_client not installed, /metrics disabled")
        return False
    try:
        start_http_server(int(port), addr="0.0.0.0")
    except (OSError, ValueError) as e:
        logger.warning("Metrics server not started: %s", e)
        return False

    _gauges = {
        "cycle": Gauge("inverter_control_cycle_ms", "Control cycle duration ms", ["quantile"]),
        "missed_deadlines": Gauge(
            "inverter_control_missed_deadlines_total", "Missed cycle deadlines"
        ),
        "write": Gauge(
            "inverter_control_setvalue_ms", "Grid setpoint write duration ms", ["quantile"]
        ),
        "failed_writes": Gauge("inverter_control_failed_writes_total", "Failed setpoint writes"),
        "age": Gauge("inverter_control_snapshot_age_ms", "Telemetry snapshot age ms", ["quantile"]),
        "signals": Gauge(
            "inverter_control_signals_healthy",
            "D-Bus fast-signal path health (1=healthy)",
        ),
        "subprocess": Gauge(
            "inverter_control_dbus_subprocess_calls_total",
            "dbus-send subprocess spawns (storm canary)",
        ),
        "cpu": Gauge("inverter_control_cpu_percent", "Process CPU percent"),
        "rss": Gauge("inverter_control_rss_mb", "Process RSS MB"),
    }
    logger.info("Prometheus metrics server started on port %s", port)
    return True


def publish(snapshot: dict[str, Any]) -> None:
    """Push a CycleMetrics.snapshot() dict into the gauges (no-op if disabled)."""
    if _gauges is None or not snapshot:
        return

    def _set(name: str, value: Any, quantile: str | None = None) -> None:
        if value is None:
            return
        try:
            gauge = _gauges[name]
            if quantile is not None:
                gauge.labels(quantile).set(float(value))
            else:
                gauge.set(float(value))
        except (KeyError, TypeError, ValueError):
            pass

    cycles = snapshot.get("cycle_ms", {})
    _set("cycle", cycles.get("p50"), "p50")
    _set("cycle", cycles.get("p95"), "p95")
    _set("cycle", cycles.get("max"), "max")
    _set("missed_deadlines", cycles.get("missed_deadlines"))

    writes = snapshot.get("setvalue_ms", {})
    _set("write", writes.get("p50"), "p50")
    _set("write", writes.get("p95"), "p95")
    _set("write", writes.get("max"), "max")
    _set("failed_writes", writes.get("failed"))

    ages = snapshot.get("snapshot_age_ms", {})
    _set("age", ages.get("p50"), "p50")
    _set("age", ages.get("max"), "max")

    _set("signals", snapshot.get("signals_healthy"))
    _set("subprocess", snapshot.get("dbus_subprocess_calls"))

    _set("cpu", snapshot.get("cpu_percent"))
    _set("rss", snapshot.get("rss_mb"))
