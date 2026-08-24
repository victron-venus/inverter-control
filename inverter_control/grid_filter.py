"""Background grid-power EMA filter.

Owns the smoothed grid measurement (filtered_gt) so the control loop never
blocks on telemetry acquisition and the filter keeps converging even when a
control cycle stalls. The time constant (tau) replaces the per-cycle
EMA_ALPHA so smoothing is independent of loop-rate jitter.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class GridFilter(threading.Thread):
    """Continuously EMA-smooths raw grid power in a daemon thread.

    Args:
        getter: callable returning the latest raw grid power (Watts) from a
            cache (must not block for long - it runs on the filter tick).
        tau: EMA time constant in seconds. alpha is derived per-tick as
            ``1 - exp(-dt / tau)``, keeping behavior identical across tick
            rates and loop jitter.
        tick: seconds between filter updates.
    """

    def __init__(
        self,
        getter: Callable[[], float],
        tau: float = 2.0,
        tick: float = 0.25,
    ):
        super().__init__(name="grid-filter", daemon=True)
        if tau <= 0:
            raise ValueError(f"tau must be positive, got {tau}")
        self._getter = getter
        self.tau = tau
        self.tick = tick
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._value: float | None = None
        self._last_t: float | None = None
        self._errors = 0

    def value(self) -> float | None:
        """Latest smoothed grid power, or None before the first sample."""
        with self._lock:
            return self._value

    def run(self) -> None:
        while not self.stop_event.wait(self.tick):
            try:
                gt = float(self._getter())
            except (TypeError, ValueError) as e:
                self._errors += 1
                logger.debug("GridFilter getter failed: %s", e)
                continue
            now = time.monotonic()
            with self._lock:
                if self._last_t is None or self._value is None:
                    self._value = gt
                else:
                    dt = min(now - self._last_t, 30.0)  # clamp long stalls
                    alpha = 1.0 - math.exp(-dt / self.tau)
                    self._value += alpha * (gt - self._value)
                self._last_t = now

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=self.tick * 10 + 2.0)
