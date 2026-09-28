"""A stalled auxiliary device cannot enter or hold a control-thread read."""

import threading
import time

import pytest

from inverter_control.background_reader import BackgroundReader


def wait_for(predicate):
    deadline = time.monotonic() + 2.0
    while not predicate():
        assert time.monotonic() < deadline, "Background refresh did not complete"
        time.sleep(0.001)


def test_reads_do_no_io_before_start_or_while_refresh_is_blocked():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked():
        calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(2.0)
        return {"value": 7}

    reader = BackgroundReader(blocked, {"value": None}, name="test-reader")
    assert reader.read() == {"value": None}
    assert calls == []
    reader.start()
    try:
        assert entered.wait(1.0)
        reader.start()  # Must not launch a second refresh.
        for _ in range(100):
            assert reader.read() == {"value": None}
        assert calls == ["test-reader"]
        release.set()
        wait_for(lambda: reader.read() == {"value": 7})
        result = reader.read()
        result["value"] = 99
        assert reader.read() == {"value": 7}
    finally:
        release.set()
        reader.stop()
    assert reader.read() == {"value": None}


def test_expired_snapshot_never_falls_back_to_synchronous_refresh(monkeypatch):
    calls = []
    reader = BackgroundReader(
        lambda: calls.append(True) or {"value": 0}, {"value": None}, name="test-reader"
    )
    reader.start()
    try:
        wait_for(lambda: reader.read() == {"value": 0})
        started = reader._snapshot[0]
        monkeypatch.setattr("inverter_control.background_reader.monotonic", lambda: started + 4)
        assert reader.read() == {"value": None}
        assert calls == [True]
    finally:
        reader.stop()


def test_slow_result_is_aged_from_start_not_completion(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    clock = [100.0]
    monkeypatch.setattr("inverter_control.background_reader.monotonic", lambda: clock[0])

    def delayed():
        entered.set()
        release.wait(1.0)
        return {"value": 7}

    reader = BackgroundReader(delayed, {"value": None}, name="test-reader")
    reader.start()
    try:
        assert entered.wait(1.0)
        clock[0] += 10
        release.set()
        wait_for(lambda: reader._snapshot is not None)
        assert reader.read() == {"value": None}
        # Stopping clears even a completed, expired sample.
        reader.stop()
        assert reader._snapshot is None
    finally:
        release.set()
        reader.stop()


def test_failed_refresh_invalidates_previous_sample():
    calls = []

    def refresh():
        calls.append(True)
        if len(calls) == 1:
            return {"value": 8}
        raise RuntimeError("unavailable")

    reader = BackgroundReader(refresh, {"value": None}, name="test-reader", interval=0.05)
    reader.start()
    try:
        wait_for(lambda: reader.read() == {"value": 8})
        wait_for(lambda: len(calls) > 1 and reader.read() == {"value": None})
    finally:
        reader.stop()


@pytest.mark.parametrize("interval,max_age", [(0, 4), (-1, 4), (2, 1)])
def test_invalid_age_configuration(interval, max_age):
    with pytest.raises(ValueError):
        BackgroundReader(dict, {}, name="invalid", interval=interval, max_age=max_age)


def test_controller_state_build_never_waits_for_display_io():
    from test_main import _make_controller

    controller, victron, *_ = _make_controller()
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(2)
        return []

    victron.get_battery_chain_socs.side_effect = blocked
    controller.telemetry = BackgroundReader(
        controller._read_display_telemetry,
        {
            "battery_socs": [],
            "inv_state": "unknown",
            "batteries": [],
            "mppt_chargers": [],
            "loads": {},
            "ess_mode": {},
            "daily_stats": {},
        },
        name="blocked-display",
    )
    controller.telemetry.start()
    try:
        assert entered.wait(1)
        # The refresh is blocked. State assembly must complete before release.
        done = threading.Event()
        errors = []

        def compose():
            try:
                controller.update_state({"_grid_valid": True}, 0)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=compose)
        thread.start()
        assert done.wait(0.5), "display I/O blocked the control thread"
        assert not errors
        assert controller.state["inverter_state"] == "unknown"
        thread.join()
    finally:
        release.set()
        controller.telemetry.stop()
