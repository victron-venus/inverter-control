"""Lossy write-path diagnostics, consumed only by the background performance worker."""

import logging
import math
import threading
from collections import deque

CAPACITY = 32
MAX_TEXT = 256
EVENT_LEVELS = {
    "native_self_call_refused": logging.DEBUG,
    "native_connect_failed": logging.DEBUG,
    "native_disconnect_failed": logging.DEBUG,
    "native_request_invalid": logging.DEBUG,
    "native_request_failed": logging.DEBUG,
    "native_reply_rejected": logging.DEBUG,
    "native_type_unsupported": logging.DEBUG,
    "cli_exception": logging.DEBUG,
    "native_fallback": logging.WARNING,
    "fallback_rejected": logging.WARNING,
    "override_status_failed": logging.ERROR,
    "override_set": logging.INFO,
    "override_stopped": logging.INFO,
    "override_refresh_failed": logging.WARNING,
    "grid_hold_started": logging.WARNING,
    "grid_hold_expired": logging.WARNING,
    "grid_fallback_rejected": logging.ERROR,
    "grid_fallback_failed": logging.ERROR,
    "grid_fallback_applied": logging.WARNING,
    "dry_watchdog_zero": logging.WARNING,
    "dry_run_changed": logging.INFO,
    "prior_setpoint_unavailable": logging.ERROR,
    "watchdog_zero_rejected": logging.ERROR,
    "watchdog_zero_applied": logging.WARNING,
    "watchdog_zero_failed": logging.ERROR,
    "watchdog_restore_rejected": logging.ERROR,
    "watchdog_restore_failed": logging.ERROR,
    "watchdog_rearmed": logging.INFO,
}


class WriteDiagnostics:
    """Never call a sink, stringify arbitrary objects, or wait for the consumer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = deque(maxlen=CAPACITY)

    def record(
        self,
        event,
        *,
        service=None,
        path=None,
        error_type=None,
        value_type=None,
        accepted=None,
        dry_run=None,
        value=None,
        seconds=None,
    ):
        if type(event) is not str or event not in EVENT_LEVELS:
            return
        record = {"event": event}
        for key, text in (
            ("service", service),
            ("path", path),
            ("error_type", error_type),
            ("value_type", value_type),
        ):
            if type(text) is str:
                record[key] = text[:MAX_TEXT]
        if type(accepted) is bool:
            record["accepted"] = accepted
        if type(dry_run) is bool:
            record["dry_run"] = dry_run
        if type(value) is int and -(2**31) <= value < 2**31:
            record["value"] = value
        if (type(seconds) is float and math.isfinite(seconds)) or (
            type(seconds) is int and 0 <= seconds <= 2**31
        ):
            record["seconds"] = seconds
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._pending.append(record)
        finally:
            self._lock.release()

    def drain(self) -> list[dict]:
        with self._lock:
            pending, self._pending = self._pending, deque(maxlen=CAPACITY)
        return list(pending)
