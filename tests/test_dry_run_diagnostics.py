"""Controller mode changes must not wait for a shared logging sink."""

import logging
import threading
from unittest.mock import Mock

import pytest
import test_write_isolation

from inverter_control.controller import InverterController
from inverter_control.watchdog import HardwareWatchdog
from inverter_control.write_diagnostics import CAPACITY

facade = test_write_isolation.facade


@pytest.mark.parametrize("action", ["set", "toggle"])
@pytest.mark.parametrize("queue_state", ["normal", "full", "busy", "broken"])
def test_mode_transition_while_shared_handler_is_blocked(facade, action, queue_state, monkeypatch):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    errors, results = [], []

    class BlockedHandler(logging.Handler):
        def emit(self, _record):
            entered.set()
            released = release.wait(3)
            assert released

    handler = BlockedHandler()
    logger = logging.getLogger("isolated-mode-transition-fixture")
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    monkeypatch.setattr("inverter_control.controller.logger", logger)
    controller = InverterController.__new__(InverterController)
    controller.victron = facade
    controller.dry_run = False
    controller._trim_mode_generation = 0
    controller._watchdog = HardwareWatchdog(
        facade, dry_run=False, diagnostics=facade.write_diagnostics
    )
    buffer = facade.write_diagnostics

    def act():
        try:
            result = (
                controller.set_dry_run(True) if action == "set" else controller.toggle_dry_run()
            )
            results.append(result)
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    consumer = threading.Thread(target=lambda: logger.warning("blocked sink"))
    actor = threading.Thread(target=act)
    consumer.start()
    try:
        sink_entered = entered.wait(1)
        assert sink_entered
        if queue_state == "full":
            for _ in range(CAPACITY):
                buffer.record("native_fallback")
        elif queue_state == "busy":
            buffer._lock.acquire()
        elif queue_state == "broken":
            monkeypatch.setattr(buffer, "record", Mock(side_effect=RuntimeError("buffer")))
        actor.start()
        completed = done.wait(1)
        assert completed, "Mode transition waited for logging while owning the hardware lock"
        assert results == [True] and errors == []
        assert controller.dry_run is controller._watchdog.dry_run is True
        assert controller._trim_mode_generation == 1
        acquired = controller._watchdog._lock.acquire(blocking=False)
        assert acquired
        controller._watchdog._lock.release()
        assert not release.is_set()
        assert not facade._native_write._bus.messages
    finally:
        if queue_state == "busy" and buffer._lock.locked():
            buffer._lock.release()
        release.set()
        actor.join(3)
        consumer.join(3)
        logger.removeHandler(handler)
        handler.close()
    assert not actor.is_alive() and not consumer.is_alive()
    if queue_state in ("normal", "full"):
        assert buffer.drain()[-1] == {"event": "dry_run_changed", "dry_run": True}


def test_noop_and_toggle_preserve_mode_generation_and_diagnostic_values(facade):
    controller = InverterController.__new__(InverterController)
    controller.dry_run = False
    controller._trim_mode_generation = 0
    controller._watchdog = HardwareWatchdog(
        facade, dry_run=False, diagnostics=facade.write_diagnostics
    )
    unchanged = controller.set_dry_run(False)
    assert unchanged is False and controller._trim_mode_generation == 0
    enabled = controller.toggle_dry_run()
    disabled = controller.toggle_dry_run()
    assert enabled is True and disabled is False and controller._trim_mode_generation == 2
    assert controller.dry_run is controller._watchdog.dry_run is False
    assert facade.write_diagnostics.drain() == [
        {"event": "dry_run_changed", "dry_run": False},
        {"event": "dry_run_changed", "dry_run": True},
        {"event": "dry_run_changed", "dry_run": False},
    ]
    assert not facade._native_write._bus.messages
