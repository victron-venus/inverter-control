"""A diagnostic sink cannot preserve failed auxiliary data or kill its reader."""

import logging
import threading

from inverter_control import background_reader
from inverter_control.background_reader import BackgroundReader


def test_failed_snapshot_is_unknown_before_blocked_warning_finishes(monkeypatch):
    published, fail_refresh = threading.Event(), threading.Event()
    logging_entered, release_logging = threading.Event(), threading.Event()
    calls = []

    class HeldHandler(logging.Handler):
        def emit(self, _record):
            logging_entered.set()
            if not release_logging.wait(2):
                raise RuntimeError("fixture diagnostic was not released")

    def refresh():
        calls.append(True)
        if len(calls) == 1:
            return {"power": 4400}
        raise OSError("auxiliary input unavailable")

    reader = BackgroundReader(refresh, {"power": None}, name="failed-reader")

    def wait_after_snapshot(_interval):
        if len(calls) == 1:
            published.set()
            fail_refresh.wait(2)
        else:
            reader._stop.set()

    monkeypatch.setattr(background_reader, "monotonic", lambda: 100.0)
    monkeypatch.setattr(reader._stop, "wait", wait_after_snapshot)
    monkeypatch.setattr(background_reader.logger, "handlers", [HeldHandler()])
    monkeypatch.setattr(background_reader.logger, "propagate", False)
    monkeypatch.setattr(background_reader.logger, "level", logging.WARNING)
    reader.start()
    try:
        first_ready = published.wait(1)
        assert first_ready
        assert reader.read() == {"power": 4400}
        fail_refresh.set()
        handler_blocked = logging_entered.wait(1)
        assert handler_blocked
        # The sample is not merely expired: the read itself has now failed.
        # A fixed clock keeps the previous successful sample within its lease.
        assert reader.read() == {"power": None}
    finally:
        release_logging.set()
        fail_refresh.set()
        stopped = reader.stop()
    assert stopped


def test_warning_failure_does_not_terminate_auxiliary_refresh(monkeypatch):
    first_published, allow_failure, recovered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    release = threading.Event()
    calls, errors = [], []

    class RaisingHandler(logging.Handler):
        def emit(self, _record):
            raise OSError("logging sink unavailable")

    def refresh():
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("temporary bad input")
        return {"power": 100 * len(calls)}

    reader = BackgroundReader(refresh, {"power": None}, name="recovering-reader")

    def wait_after_snapshot(_interval):
        if len(calls) == 1:
            first_published.set()
            allow_failure.wait(2)
        elif len(calls) >= 3:
            recovered.set()
            release.wait(2)

    def run():
        try:
            reader._run()
        except Exception as error:
            errors.append(type(error).__name__)

    monkeypatch.setattr(reader._stop, "wait", wait_after_snapshot)
    monkeypatch.setattr(background_reader.logger, "handlers", [RaisingHandler()])
    monkeypatch.setattr(background_reader.logger, "propagate", False)
    monkeypatch.setattr(background_reader.logger, "level", logging.WARNING)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        first_ready = first_published.wait(1)
        assert first_ready
        allow_failure.set()
        refreshed = recovered.wait(0.3)
        assert refreshed, f"diagnostic exception terminated reader: {errors}"
        assert reader.read() == {"power": 300}
        assert not errors
    finally:
        reader.request_stop()
        allow_failure.set()
        release.set()
        thread.join(1)
    assert not thread.is_alive()
