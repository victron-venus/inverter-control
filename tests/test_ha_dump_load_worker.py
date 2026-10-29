"""HA dump-load latency cannot hold up grid regulation or replay stale intent."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_main import _make_controller

from inverter_control import homeassistant


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("inverter_control.controller.ENABLE_HA", True)
    monkeypatch.setattr(homeassistant, "HA_DUMP_LOADS", ["switch.first", "switch.second"])
    value = homeassistant.HomeAssistantClient()
    monkeypatch.setattr(value, "_poll_loop", value._stop_event.wait)
    monkeypatch.setattr(value._session, "post", Mock())
    value.start()
    yield value
    assert value.stop(timeout=2)


def test_blocked_http_keeps_grid_cycle_running_and_coalesces_latest_intent(client):
    entered, release, finished, cycle_done = (threading.Event() for _ in range(4))
    calls, results = [], []

    def post(url, *, json, timeout):
        calls.append((url.rsplit("/", 1)[-1], json["entity_id"], threading.current_thread().name))
        assert timeout == (3, homeassistant.HA_TIMEOUT)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        if len(calls) == 3:
            finished.set()
        return SimpleNamespace(status_code=200)

    client._session.post.side_effect = post
    client._sensors["net_usage"] = 100
    controller, victron, _, _ = _make_controller()
    controller.ha = client
    controller._control_flags["minimize_charging"] = True
    controller.delay = 0
    victron.get_system_data.return_value = {"_grid_valid": True, "gt": 0, "bp": 800}
    victron.get_inverter_state.return_value = (9, "Inverting")
    victron.set_grid_setpoint.return_value = True

    def cycle():
        results.append(controller.run_cycle())
        cycle_done.set()

    with (
        patch.object(controller, "_grid_ready_for_control", return_value=True),
        patch.object(controller, "_update_dvcc_limits"),
        patch.object(controller, "calculate_setpoint", return_value=(-100, "")),
        patch.object(controller, "update_state"),
        patch("inverter_control.controller.broadcast_line"),
    ):
        worker = threading.Thread(target=cycle)
        try:
            worker.start()
            assert entered.wait(1)
            assert cycle_done.wait(0.3), "HA service call blocked the grid control cycle"
            victron.set_grid_setpoint.assert_called_once_with(-100)
            for _ in range(1000):
                assert client.request_dump_loads(True) == 2
            assert client.request_dump_loads(False) == 2
            assert len(calls) == 1
            release.set()
            assert finished.wait(1)
        finally:
            release.set()
            worker.join(2)
    assert not worker.is_alive()
    assert results == [True]
    assert calls == [
        ("turn_on", "switch.first", "ha-dump-loads"),
        ("turn_off", "switch.first", "ha-dump-loads"),
        ("turn_off", "switch.second", "ha-dump-loads"),
    ]


def test_expired_pending_intent_is_not_replayed_when_http_recovers(client, monkeypatch):
    entered, release, expired_checked = (threading.Event() for _ in range(3))
    now = [1000.0]

    def monotonic():
        if now[0] > 1005:
            expired_checked.set()
        return now[0]

    monkeypatch.setattr(homeassistant, "time", SimpleNamespace(time=time.time, monotonic=monotonic))

    def post(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status_code=200)

    client._session.post.side_effect = post
    try:
        assert client.request_dump_loads(True) == 2
        assert entered.wait(1)
        assert client.request_dump_loads(False) == 2
        now[0] = 1006.0
        release.set()
        assert expired_checked.wait(1)
        assert client._session.post.call_count == 1
    finally:
        release.set()


def test_stop_drops_pending_intent_and_keeps_active_http_session_open(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    closed = Mock()
    monkeypatch.setattr(client._session, "close", closed)

    def post(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status_code=200)

    client._session.post.side_effect = post
    try:
        assert client.request_dump_loads(True) == 2
        assert entered.wait(1)
        assert client.request_dump_loads(False) == 2
        assert client.stop(timeout=0.01) is False
        closed.assert_not_called()
        assert client.request_dump_loads(True) == 0
        old_worker = client._dump_load_thread
        client.start()
        assert client._dump_load_thread is old_worker
        release.set()
        assert client.stop(timeout=1) is True
        closed.assert_called_once()
        assert client._session.post.call_count == 1
        assert client._dump_load_pending is None
    finally:
        release.set()


def test_dump_load_request_requires_started_worker_and_valid_entities(monkeypatch):
    monkeypatch.setattr(homeassistant, "HA_DUMP_LOADS", ["invalid", "switch.valid"])
    client = homeassistant.HomeAssistantClient()
    try:
        assert client.request_dump_loads(True) == 0
        monkeypatch.setattr(client, "_poll_loop", client._stop_event.wait)
        monkeypatch.setattr(client, "_dump_load_loop", client._stop_event.wait)
        client.start()
        assert client.request_dump_loads("true") == 0
        assert client.request_dump_loads(True) == 1
        assert client.stop()
        assert client.request_dump_loads(False) == 0
    finally:
        client.stop()


@pytest.mark.parametrize(
    "transition", ["disable", "grid_invalid", "source", "recovery", "override"]
)
def test_controller_transitions_cancel_queued_and_remaining_loads(client, monkeypatch, transition):
    entered, release, idle = (threading.Event() for _ in range(3))
    controller, victron, _, _ = _make_controller()
    controller.ha = client
    controller._control_flags["minimize_charging"] = True
    monkeypatch.setattr("inverter_control.mqtt_bridge.get_mqtt_bridge", lambda: None)

    def post(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status_code=200)

    client._session.post.side_effect = post
    try:
        assert controller._request_dump_loads(turn_on=True) == 2
        assert entered.wait(1)
        assert controller._request_dump_loads(turn_on=True) == 2
        if transition == "disable":
            controller.set_control_flag("minimize_charging", False)
            assert controller._request_dump_loads(turn_on=True) == 0
        elif transition == "grid_invalid":
            assert controller._grid_ready_for_control({"_grid_valid": False}) is False
        elif transition == "source":
            assert (
                controller._grid_ready_for_control(
                    {"_grid_valid": True, "_grid_selection_generation": 1}
                )
                is False
            )
        elif transition == "recovery":
            monkeypatch.setattr(controller._watchdog, "is_triggered", lambda: True)
            assert controller._grid_ready_for_control({"_grid_valid": True}) is False
        else:
            monkeypatch.setattr(controller, "get_setpoint_override", lambda: {"value": 50})
            monkeypatch.setattr(controller, "_grid_ready_for_control", lambda _data: True)
            monkeypatch.setattr(controller, "_update_grid_loss_state", lambda: None)
            victron.get_system_data.return_value = {"_grid_valid": True}
            assert controller.run_cycle() is True
        assert client._dump_load_pending is None
        wait = client._dump_load_wakeup.wait

        def wait_until_requested():
            idle.set()
            return wait()

        monkeypatch.setattr(client._dump_load_wakeup, "wait", wait_until_requested)
        release.set()
        assert idle.wait(1)
        assert client._session.post.call_count == 1
    finally:
        release.set()


def test_repeated_same_intent_does_not_starve_remaining_loads(client):
    entered, release, finished = (threading.Event() for _ in range(3))
    calls = []

    def post(_url, *, json, timeout):
        calls.append(json["entity_id"])
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        if len(calls) == 4:
            finished.set()
        return SimpleNamespace(status_code=200)

    client._session.post.side_effect = post
    try:
        assert client.request_dump_loads(True) == 2
        assert entered.wait(1)
        assert client.request_dump_loads(True) == 2
        release.set()
        assert finished.wait(1)
        assert calls == ["switch.first", "switch.second"] * 2
    finally:
        release.set()
