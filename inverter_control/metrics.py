#!/usr/bin/env python3
"""
Lightweight control-loop latency metrics.

Rolling-window counters so a long hardware run produces p50/p95/p99 numbers
for the user's plan (cycle_duration_ms, setvalue_duration_ms, snapshot age,
missed deadlines) plus process CPU/RSS on Linux (/proc, Venus OS). No
dependencies, no I/O - everything is computed from in-memory deques.
"""

import os
import time
from collections import deque


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an ascending-sorted list."""
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(pct / 100.0 * (len(sorted_values) - 1))))
    return round(sorted_values[idx], 1)


class CycleMetrics:
    """Rolling performance counters for one control-loop instance."""

    WINDOW = 600  # samples (~3 min at 3 Hz)

    def __init__(self):
        self._cycle_ms = deque(maxlen=self.WINDOW)
        self._write_ms = deque(maxlen=self.WINDOW)
        self._age_ms = deque(maxlen=self.WINDOW)
        self._stage_ms: dict[str, deque] = {}
        self.missed_deadlines = 0
        self.failed_writes = 0
        self._cpu_last: tuple[float, float] | None = None  # (cpu_seconds, monotonic)
        self.cpu_percent: float | None = None

    def record_cycle(self, started_monotonic: float, interval: float) -> None:
        """Record one control-cycle duration and deadline misses."""
        dur_ms = (time.monotonic() - started_monotonic) * 1000.0
        self._cycle_ms.append(dur_ms)
        if dur_ms > interval * 1000.0:
            self.missed_deadlines += 1

    def record_write(self, duration_ms: float, ok: bool) -> None:
        """Record one setpoint write (native or CLI fallback)."""
        if not ok:
            self.failed_writes += 1
        self._write_ms.append(duration_ms)

    def record_age(self, age_ms: float | None) -> None:
        """Record telemetry snapshot age at calculation time."""
        if age_ms is not None and age_ms >= 0:
            self._age_ms.append(age_ms)

    def record_stage(self, name: str, duration_ms: float) -> None:
        """Record one named control-cycle stage duration (windowed)."""
        stage = self._stage_ms.get(name)
        if stage is None:
            stage = self._stage_ms[name] = deque(maxlen=self.WINDOW)
        stage.append(duration_ms)

    def sample_process(self) -> None:
        """Update CPU% and RSS from /proc (no-op off Linux, e.g. macOS dev)."""
        try:
            with open("/proc/self/stat", encoding="utf-8") as f:
                parts = f.read().split()
            cpu_ticks = float(parts[13]) + float(parts[14])  # utime + stime
            hz = os.sysconf("SC_CLK_TCK")
            with open("/proc/self/status", encoding="utf-8") as f:
                rss_kb = next(int(line.split()[1]) for line in f if line.startswith("VmRSS:"))
            now = time.monotonic()
            if self._cpu_last is not None:
                d_cpu = cpu_ticks / hz - self._cpu_last[0]
                d_wall = now - self._cpu_last[1]
                if d_wall > 0:
                    self.cpu_percent = round(100.0 * d_cpu / d_wall, 1)
            self._cpu_last = (cpu_ticks / hz, now)
            self.rss_mb = round(rss_kb / 1024.0, 1)
        except (OSError, ValueError, IndexError, StopIteration):
            pass  # Non-Linux or restricted /proc: leave CPU/RSS unset

    def snapshot(self) -> dict:
        """Current stats as an MQTT/console-friendly dict."""
        cycles = sorted(self._cycle_ms)
        writes = sorted(self._write_ms)
        ages = sorted(self._age_ms)
        return {
            "cycle_ms": {
                "p50": _percentile(cycles, 50),
                "p95": _percentile(cycles, 95),
                "p99": _percentile(cycles, 99),
                "max": _percentile(cycles, 100),
                "missed_deadlines": self.missed_deadlines,
            },
            "setvalue_ms": {
                "p50": _percentile(writes, 50),
                "p95": _percentile(writes, 95),
                "max": _percentile(writes, 100),
                "failed": self.failed_writes,
            },
            "snapshot_age_ms": {
                "p50": _percentile(ages, 50),
                "max": _percentile(ages, 100),
            },
            "stage_ms": {
                name: {
                    "p50": _percentile(sorted(samples), 50),
                    "p95": _percentile(sorted(samples), 95),
                    "max": _percentile(sorted(samples), 100),
                }
                for name, samples in sorted(self._stage_ms.items())
            },
            "cpu_percent": self.cpu_percent,
            "rss_mb": getattr(self, "rss_mb", None),
        }
