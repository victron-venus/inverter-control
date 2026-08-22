"""Tests for the control-loop latency metrics module."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control.metrics import CycleMetrics, _percentile


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 50) is None

    def test_single(self):
        assert _percentile([5.0], 99) == 5.0

    def test_ranks(self):
        vals = list(range(1, 101))  # 1..100
        assert _percentile(vals, 50) == 51  # nearest-rank on 100 items
        assert _percentile(vals, 0) == 1
        assert _percentile(vals, 100) == 100


class TestCycleMetrics:
    def test_cycle_recording_and_deadlines(self):
        m = CycleMetrics()
        t = time.monotonic()
        time.sleep(0.001)
        m.record_cycle(t, 0.33)  # fast cycle: no miss
        m.record_cycle(t - 1.0, 0.33)  # fake 1s duration: miss
        assert m.missed_deadlines == 1
        snap = m.snapshot()
        assert snap["cycle_ms"]["max"] >= 1.0
        assert snap["cycle_ms"]["missed_deadlines"] == 1

    def test_write_recording(self):
        m = CycleMetrics()
        m.record_write(12.0, True)
        m.record_write(50.0, False)
        m.record_write(20.0, True)
        snap = m.snapshot()
        assert snap["setvalue_ms"]["p50"] == 20.0
        assert snap["setvalue_ms"]["max"] == 50.0
        assert snap["setvalue_ms"]["failed"] == 1

    def test_age_recording_rejects_negative(self):
        m = CycleMetrics()
        m.record_age(120.0)
        m.record_age(-5.0)
        m.record_age(None)
        snap = m.snapshot()
        assert snap["snapshot_age_ms"]["p50"] == 120.0
        assert snap["snapshot_age_ms"]["max"] == 120.0

    def test_window_bounded(self):
        m = CycleMetrics()
        for _ in range(m.WINDOW + 50):
            m.record_write(1.0, True)
        assert len(m._write_ms) == m.WINDOW

    def test_sample_process_noop_off_linux(self):
        # Must never raise on macOS / restricted environments
        m = CycleMetrics()
        m.sample_process()
        snap = m.snapshot()
        assert snap["cpu_percent"] in (None, m.cpu_percent)

    def test_snapshot_empty(self):
        snap = CycleMetrics().snapshot()
        assert snap["cycle_ms"]["p50"] is None
        assert snap["cycle_ms"]["missed_deadlines"] == 0
        assert snap["setvalue_ms"]["failed"] == 0
