"""Tests for optional Prometheus exposition of CycleMetrics."""

# pylint: disable=protected-access

from unittest.mock import MagicMock

from inverter_control import prom_metrics


def setup_function(_):
    prom_metrics._gauges = None


def test_start_disabled_without_env(monkeypatch):
    monkeypatch.delenv("INVERTER_METRICS_PORT", raising=False)
    assert prom_metrics.start() is False
    assert prom_metrics._gauges is None


def test_publish_noop_when_disabled():
    # Must not raise when never started
    prom_metrics.publish({"cycle_ms": {"p50": 10.0}})


def _fake_gauges():
    gauges = {}

    def make(name, doc, labels=None):
        g = MagicMock()
        g.labels.return_value = g
        gauges[name] = g
        return g

    return gauges, make


def test_publish_sets_all_gauges(monkeypatch):
    _gauges_unused, make = _fake_gauges()
    fake_pc = MagicMock()
    fake_pc.Gauge.side_effect = make

    # Inject a prometheus_client stub so start() takes the enabled path
    import sys
    import types

    monkeypatch.setenv("INVERTER_METRICS_PORT", "19102")
    stub = types.ModuleType("prometheus_client")
    stub.Gauge = fake_pc.Gauge
    stub.start_http_server = MagicMock()
    sys.modules["prometheus_client"] = stub
    try:
        assert prom_metrics.start() is True
    finally:
        del sys.modules["prometheus_client"]
    prom_metrics._gauges = {
        "cycle": MagicMock(),
        "missed_deadlines": MagicMock(),
        "write": MagicMock(),
        "failed_writes": MagicMock(),
        "age": MagicMock(),
        "cpu": MagicMock(),
        "rss": MagicMock(),
    }

    snapshot = {
        "cycle_ms": {"p50": 10.0, "p95": 20.0, "max": 30.0, "missed_deadlines": 2},
        "setvalue_ms": {"p50": 5.0, "failed": 1},
        "snapshot_age_ms": {"p50": 100.0, "max": 400.0},
        "cpu_percent": 12.5,
        "rss_mb": 42.0,
    }
    prom_metrics.publish(snapshot)
    prom_metrics._gauges["missed_deadlines"].set.assert_called_with(2.0)
    prom_metrics._gauges["rss"].set.assert_called_with(42.0)
    prom_metrics._gauges["cycle"].labels.assert_any_call("p50")
