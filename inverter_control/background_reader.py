"""Bounded-age snapshots for auxiliary readers; device I/O never runs in read()."""

import logging
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

logger = logging.getLogger("inverter-control")


class BackgroundReader:
    """Refresh independently of the control loop and discard expired snapshots.

    The callback must never control devices. No lock is held across it, and each
    reader owns one worker so a slow water service cannot delay the EV reader.
    Before the first result, after an error, or after max_age, read returns the
    supplied unknown values. Stopping never falls back to synchronous I/O.
    """

    def __init__(
        self,
        refresh: Callable[[], dict[str, Any]],
        unknown: dict[str, Any],
        *,
        name: str,
        interval: float = 2.0,
        max_age: float = 4.0,
    ):
        if interval <= 0 or max_age < interval:
            raise ValueError("Require 0 < interval <= max_age")
        self._refresh = refresh
        self._unknown = dict(unknown)
        self._name = name
        self._interval = interval
        self._max_age = max_age
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: tuple[float, dict[str, Any]] | None = None

    def read(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or monotonic() - snapshot[0] >= self._max_age:
            return dict(self._unknown)
        return dict(snapshot[1])

    def start(self) -> None:
        # Lifecycle is owned by the main thread. Do not create another worker
        # if a bounded stop left the original callback finishing in the daemon.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Signal cancellation without waiting for an in-flight read."""
        self._stop.set()
        with self._lock:
            self._snapshot = None

    def stop(self, timeout: float = 1.0) -> bool:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
        return self._thread is None or not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = monotonic()
            try:
                values = {**self._unknown, **self._refresh()}
                # Age from the beginning of the read pass: a delayed result is
                # not relabelled fresh just because its last call returned now.
                snapshot = (started, values)
            except Exception as error:
                logger.warning("Auxiliary reader %s failed: %s", self._name, type(error).__name__)
                snapshot = None
            with self._lock:
                if not self._stop.is_set():
                    self._snapshot = snapshot
            # Keep a slow/erroring service from spinning or queuing catch-up work.
            self._stop.wait(self._interval)
