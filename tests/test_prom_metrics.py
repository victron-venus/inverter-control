"""Exporter recovery tests, including real loopback HTTP scraping."""

# pylint: disable=protected-access

import errno
import http.client
import socket
import sys
import threading
import time
from unittest.mock import MagicMock

import prometheus_client
import pytest

from inverter_control import prom_metrics


@pytest.fixture(autouse=True)
def reset_exporter(monkeypatch):
    prom_metrics.stop()
    prom_metrics._gauges = None
    monkeypatch.delenv("INVERTER_METRICS_PORT", raising=False)
    monkeypatch.delenv("INVERTER_METRICS_HOST", raising=False)
    monkeypatch.setattr(prom_metrics, "_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(prom_metrics, "_RETRY_MAX_SECONDS", 0.04)
    monkeypatch.setattr(prom_metrics, "_HEALTH_CHECK_SECONDS", 0.01)
    yield
    prom_metrics.stop()


def _wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "timed out waiting for exporter state"


def _reserve_port():
    reservation = socket.socket()
    reservation.bind(("127.0.0.1", 0))
    return reservation


def _scrape(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/metrics")
        response = connection.getresponse()
        assert response.status == 200
        return response.read().decode()
    finally:
        connection.close()


def test_start_disabled_without_env():
    assert prom_metrics.start() is False
    assert prom_metrics._gauges is None
    assert prom_metrics._server is None


def test_start_disabled_without_optional_dependency(monkeypatch):
    monkeypatch.setenv("INVERTER_METRICS_PORT", "9102")
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    assert prom_metrics.start() is False
    assert prom_metrics._server is None


def test_worker_start_failure_does_not_break_control_startup(monkeypatch):
    monkeypatch.setenv("INVERTER_METRICS_PORT", "9102")

    def no_threads_available(_thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads_available)
    assert prom_metrics.start() is False
    assert prom_metrics._server is None
    assert prom_metrics._gauges is None
    prom_metrics.stop()


@pytest.mark.parametrize("port", ["nope", "0", "-1", "65536"])
def test_invalid_port_disables_exporter_without_retries(monkeypatch, port):
    monkeypatch.setenv("INVERTER_METRICS_PORT", port)
    assert prom_metrics.start() is False
    assert prom_metrics._server is None


def test_publish_noop_when_disabled():
    prom_metrics.publish({"cycle_ms": {"p50": 10.0}})


def test_publish_sets_all_gauges():
    prom_metrics._gauges = {
        name: MagicMock()
        for name in (
            "cycle",
            "missed_deadlines",
            "write",
            "failed_writes",
            "age",
            "cpu",
            "rss",
            "signals",
            "subprocess",
            "stage",
        )
    }
    snapshot = {
        "cycle_ms": {"p50": 10.0, "p95": 20.0, "max": 30.0, "missed_deadlines": 2},
        "setvalue_ms": {"p50": 5.0, "failed": 1},
        "snapshot_age_ms": {"p50": 100.0, "max": 400.0},
        "stage_ms": {"calculate_setpoint": {"p50": 1.0, "p95": 2.0, "max": 3.0}},
        "signals_healthy": True,
        "dbus_subprocess_calls": 4,
        "cpu_percent": 12.5,
        "rss_mb": 42.0,
    }
    prom_metrics.publish(snapshot)
    prom_metrics._gauges["missed_deadlines"].set.assert_called_with(2.0)
    prom_metrics._gauges["failed_writes"].set.assert_called_with(1.0)
    prom_metrics._gauges["rss"].set.assert_called_with(42.0)
    prom_metrics._gauges["signals"].set.assert_called_with(1.0)
    prom_metrics._gauges["subprocess"].set.assert_called_with(4.0)
    prom_metrics._gauges["cycle"].labels.assert_any_call("p50")
    prom_metrics._gauges["stage"].labels.assert_any_call("calculate_setpoint", "p95")
    # Malformed optional measurements must not escape into the control loop.
    prom_metrics.publish({"cycle_ms": {"p50": "invalid"}, "rss_mb": None})
    prom_metrics._gauges.clear()
    prom_metrics.publish(snapshot)


@pytest.mark.parametrize("configured_host", [None, "127.0.0.1"])
def test_transient_missing_address_recovers_and_preserves_metrics(monkeypatch, configured_host):
    with _reserve_port() as reservation:
        port = reservation.getsockname()[1]
    monkeypatch.setenv("INVERTER_METRICS_PORT", str(port))
    if configured_host:
        monkeypatch.setenv("INVERTER_METRICS_HOST", configured_host)
    real_start = prometheus_client.start_http_server
    attempts = []
    listeners = []
    ready = threading.Event()

    def temporarily_missing_address(requested_port, **kwargs):
        attempts.append((requested_port, kwargs))
        if len(attempts) <= 2:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        listener = real_start(requested_port, **kwargs)
        listeners.append(listener)
        ready.set()
        return listener

    monkeypatch.setattr(prometheus_client, "start_http_server", temporarily_missing_address)
    assert prom_metrics.start() is True
    supervisor = prom_metrics._server
    gauges = prom_metrics._gauges
    # Publish before the address appears: recovery must expose these same gauges.
    prom_metrics.publish({"cycle_ms": {"p50": 12.5}, "signals_healthy": True})
    for _ in range(5):
        assert prom_metrics.start() is True
    assert prom_metrics._server is supervisor
    assert ready.wait(3)
    assert len(attempts) == 3
    assert len(listeners) == 1
    assert all(p == port and k["addr"] == "127.0.0.1" for p, k in attempts)
    assert all(k["registry"] is supervisor.registry for _, k in attempts)
    assert prom_metrics._gauges is gauges
    body = _scrape(port)
    assert 'inverter_control_cycle_ms{quantile="p50"} 12.5' in body
    assert "inverter_control_signals_healthy 1.0" in body
    assert "python_gc_objects_collected_total" in body
    assert "python_info" in body
    if sys.platform.startswith("linux"):
        assert "process_virtual_memory_bytes" in body
    prom_metrics.stop()
    assert not supervisor.thread.is_alive()
    assert not listeners[0][1].is_alive()
    assert listeners[0][0].socket.fileno() == -1
    # The old socket and registry must both be reusable after a clean shutdown.
    ready.clear()
    assert prom_metrics.start() is True
    assert ready.wait(3)
    assert len(listeners) == 2
    assert prom_metrics._gauges is not gauges
    assert "inverter_control_failed_writes_total" in _scrape(port)


def test_busy_port_retries_real_bind_then_recovers(monkeypatch):
    reservation = _reserve_port()
    port = reservation.getsockname()[1]
    reservation.listen()
    monkeypatch.setenv("INVERTER_METRICS_PORT", str(port))
    real_start = prometheus_client.start_http_server
    attempts = []
    ready = threading.Event()

    def recording_start(*args, **kwargs):
        attempts.append(True)
        listener = real_start(*args, **kwargs)
        ready.set()
        return listener

    monkeypatch.setattr(prometheus_client, "start_http_server", recording_start)
    try:
        assert prom_metrics.start() is True
        _wait_for(lambda: len(attempts) >= 2)
        assert not ready.is_set()
    finally:
        reservation.close()
    assert ready.wait(3)
    assert "inverter_control_failed_writes_total" in _scrape(port)


def test_listener_thread_exit_is_recovered_without_resetting_gauges(monkeypatch):
    with _reserve_port() as reservation:
        port = reservation.getsockname()[1]
    monkeypatch.setenv("INVERTER_METRICS_PORT", str(port))
    real_start = prometheus_client.start_http_server
    listeners = []

    def recording_start(*args, **kwargs):
        listener = real_start(*args, **kwargs)
        listeners.append(listener)
        return listener

    monkeypatch.setattr(prometheus_client, "start_http_server", recording_start)
    assert prom_metrics.start() is True
    _wait_for(lambda: len(listeners) == 1)
    prom_metrics.publish({"setvalue_ms": {"failed": 7}})
    gauges = prom_metrics._gauges
    first_server, first_thread = listeners[0]
    first_server.shutdown()
    first_thread.join(timeout=1)
    _wait_for(lambda: len(listeners) == 2)
    assert first_server.socket.fileno() == -1
    assert prom_metrics._gauges is gauges
    assert "inverter_control_failed_writes_total 7.0" in _scrape(port)


def test_retry_backoff_is_capped_and_cancellable(monkeypatch, caplog):
    monkeypatch.setattr(prom_metrics, "_RETRY_INITIAL_SECONDS", 1)
    monkeypatch.setattr(prom_metrics, "_RETRY_MAX_SECONDS", 30)
    starter = MagicMock(side_effect=OSError("network not ready"))
    server = prom_metrics._MetricsServer("192.0.2.10", 9102, object(), starter)
    server.stop_event = MagicMock()
    server.stop_event.is_set.return_value = False
    server.stop_event.wait.side_effect = [False] * 6 + [True]
    server._run()
    delays = [call.args[0] for call in server.stop_event.wait.call_args_list]
    assert delays == [1, 2, 4, 8, 16, 30, 30]
    assert starter.call_count == 7
    assert len(caplog.records) == 7
    assert all("192.0.2.10:9102" in record.message for record in caplog.records)


def test_stop_interrupts_long_backoff_and_restart_is_safe(monkeypatch):
    monkeypatch.setenv("INVERTER_METRICS_HOST", "192.0.2.10")
    monkeypatch.setenv("INVERTER_METRICS_PORT", "9102")
    monkeypatch.setattr(prom_metrics, "_RETRY_INITIAL_SECONDS", 30)
    attempted = threading.Event()

    def fail_to_bind(*args, **kwargs):
        assert kwargs["addr"] == "192.0.2.10"
        attempted.set()
        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

    monkeypatch.setattr(prometheus_client, "start_http_server", fail_to_bind)
    for _ in range(2):
        attempted.clear()
        assert prom_metrics.start() is True
        server = prom_metrics._server
        assert attempted.wait(1)
        prom_metrics.stop()
        assert not server.thread.is_alive()
        assert prom_metrics._server is None
        assert prom_metrics._gauges is None
    prom_metrics.stop()


def test_slow_start_does_not_block_publish_or_create_duplicate_workers(monkeypatch):
    monkeypatch.setenv("INVERTER_METRICS_PORT", "9102")
    entered = threading.Event()
    release = threading.Event()

    def slow_start(*args, **kwargs):
        entered.set()
        release.wait(3)
        raise OSError("network not ready")

    monkeypatch.setattr(prometheus_client, "start_http_server", slow_start)
    try:
        assert prom_metrics.start() is True
        assert entered.wait(1)
        server = prom_metrics._server
        prom_metrics.publish({"rss_mb": 42})
        assert prom_metrics.start() is True
        assert prom_metrics._server is server
        monkeypatch.setattr(prom_metrics, "_STOP_TIMEOUT_SECONDS", 0.01)
        prom_metrics.stop()
        # Retain an in-flight worker until it exits, even after shutdown times out.
        assert prom_metrics._server is server
        assert prom_metrics.start() is True
        assert prom_metrics._server is server
    finally:
        release.set()
        server.thread.join(timeout=1)
    prom_metrics.stop()
    assert prom_metrics._server is None
