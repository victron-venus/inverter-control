"""One-shot mode uses the same writer-quiescence gate as the normal loop."""

import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from inverter_control import config, mqtt_bridge
from inverter_control.watchdog import HardwareWatchdog


def one_shot_context(monkeypatch):
    closed = threading.Event()

    def close(**_kwargs):
        closed.set()
        return True

    victron = SimpleNamespace(
        request_stop=Mock(),
        stop_polling=Mock(return_value=True),
        close=Mock(side_effect=close),
        set_grid_setpoint=Mock(return_value=True),
    )
    controller = SimpleNamespace(
        dry_run=False,
        victron=victron,
        _watchdog=HardwareWatchdog(victron),
        grid_filter=None,
        derived_grid_filter=None,
        ha=SimpleNamespace(request_stop=Mock(), stop=Mock(return_value=True)),
        _handle_forecast_webhook=Mock(),
        _handle_pre_charge_webhook=Mock(),
        start_auxiliary_readers=Mock(),
        request_stop_auxiliary_readers=Mock(),
        stop_auxiliary_readers=Mock(return_value=True),
        run_cycle=Mock(return_value=True),
    )
    controller.set_setpoint_override = controller._watchdog.set_setpoint_override
    monkeypatch.setattr(sys, "argv", ["main.py", "100"])
    monkeypatch.setattr(main, "InverterController", Mock(return_value=controller))
    monkeypatch.setattr(main, "MQTT_AVAILABLE", False)
    monkeypatch.setattr(main, "request_stop_console_server", Mock())
    monkeypatch.setattr(main, "stop_console_server", Mock(return_value=True))
    return controller, closed


@pytest.mark.parametrize("release_before_deadline", [True, False])
def test_one_shot_waits_for_actual_mqtt_write_or_refuses_close(
    monkeypatch, release_before_deadline
):
    controller, closed = one_shot_context(monkeypatch)
    entered, release, callback_done = threading.Event(), threading.Event(), threading.Event()
    disconnect_entered, main_done = threading.Event(), threading.Event()
    writes, errors, outcomes = [], [], []

    def write(value):
        writes.append(value)
        entered.set()
        released = release.wait(2)
        if not released:
            raise AssertionError("fixture writer was not released")
        return True

    controller.victron.set_grid_setpoint = write
    monkeypatch.setattr(mqtt_bridge, "MQTT_AVAILABLE", False)
    bridge = mqtt_bridge.MQTTBridge()
    bridge.connect = Mock(return_value=True)
    bridge.publish_setpoint_override = Mock()
    monkeypatch.setattr(main, "MQTT_AVAILABLE", True)
    monkeypatch.setattr(main, "get_mqtt_bridge", Mock(return_value=bridge))
    monkeypatch.setattr(config, "MQTT_BROKER", "localhost")

    def callback():
        try:
            bridge._on_message(
                None,
                None,
                SimpleNamespace(
                    topic=bridge.prefix + "/cmd/setpoint_override",
                    payload=b'{"value":200}',
                    retain=False,
                ),
            )
        finally:
            callback_done.set()

    caller = threading.Thread(target=callback)

    class Client:
        def loop_stop(self):
            disconnect_entered.set()
            caller.join(timeout=2)
            if caller.is_alive():
                raise AssertionError("callback still active after fixture cleanup")

        def disconnect(self):
            assert callback_done.is_set()

    bridge._client = Client()

    def cycle():
        caller.start()
        started = entered.wait(1)
        if not started:
            raise AssertionError("MQTT writer did not start")
        return True

    controller.run_cycle = cycle
    original_shutdown = main._shutdown_main_loop

    def shutdown(*args):
        outcome = original_shutdown(*args, timeout=1.0 if release_before_deadline else 0.05)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(main, "_shutdown_main_loop", shutdown)

    def run():
        try:
            main._main_inner()
        except Exception as error:
            errors.append(error)
        finally:
            main_done.set()

    runner = threading.Thread(target=run)
    runner.start()
    try:
        disconnect_started = disconnect_entered.wait(1)
        assert disconnect_started
        assert not closed.is_set() and not callback_done.is_set()
        assert bridge._disconnect_requested is True
        if release_before_deadline:
            release.set()
        finished = main_done.wait(1)
        assert finished
        assert outcomes == [release_before_deadline]
        assert closed.is_set() is release_before_deadline
        assert writes == [200]
        assert controller.manual_setpoint == 100
        assert errors == []
        if not release_before_deadline:
            controller.victron.close.assert_not_called()
    finally:
        release.set()
        if caller.ident is not None:
            caller.join(timeout=2)
        runner.join(timeout=2)
    assert not caller.is_alive() and not runner.is_alive()


def test_one_shot_success_cleans_up_without_a_heartbeat_worker(monkeypatch):
    controller, closed = one_shot_context(monkeypatch)
    main._main_inner()
    assert controller.manual_setpoint == 100
    controller.run_cycle.assert_called_once_with()
    controller.start_auxiliary_readers.assert_called_once_with()
    controller.request_stop_auxiliary_readers.assert_called_once_with()
    controller.stop_auxiliary_readers.assert_called_once()
    controller.ha.stop.assert_called_once()
    assert controller._watchdog._stop_event.is_set()
    assert closed.is_set()


def test_one_shot_partial_reader_start_failure_still_cleans_up(monkeypatch):
    controller, closed = one_shot_context(monkeypatch)
    failure = RuntimeError("second reader failed to start")
    controller.start_auxiliary_readers.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        main._main_inner()
    assert raised.value is failure
    controller.run_cycle.assert_not_called()
    controller.request_stop_auxiliary_readers.assert_called_once_with()
    controller.stop_auxiliary_readers.assert_called_once()
    controller.ha.stop.assert_called_once()
    assert closed.is_set()
