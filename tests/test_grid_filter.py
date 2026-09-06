"""Tests for the background GridFilter thread."""

import threading
import time

import pytest

from inverter_control.grid_filter import GridFilter


class TestGridFilter:
    def make_filter(self, values, tau=0.05, tick=0.01):
        it = iter(values)
        lock = threading.Lock()

        def getter():
            with lock:
                return next(it, values[-1])

        return GridFilter(getter=getter, tau=tau, tick=tick)

    def test_value_none_before_start(self):
        f = self.make_filter([100])
        assert f.value() is None

    def test_seeds_first_sample_then_converges(self):
        f = self.make_filter([0, 100, 100, 100, 100, 100, 100, 100, 100])
        f.start()
        time.sleep(0.08)
        f.stop()
        v = f.value()
        assert v is not None
        # Seeded at 0 then pulled toward 100 - must land strictly between
        assert 0.0 < v <= 100.0
        # And be well on its way after several tau worth of ticks
        assert v > 60.0

    def test_tau_slower_than_instant(self):
        # One tick after a step should move only a fraction of the gap
        f = GridFilter(getter=lambda: 100.0, tau=10.0, tick=0.02)
        with f._lock:
            f._value = 0.0
            f._last_t = time.monotonic()
        f.start()
        time.sleep(0.05)
        f.stop()
        v = f.value()
        assert v is not None
        # dt/tau ~ 0.002-0.005 -> alpha tiny; value stays near 0... but the
        # seed above set _last_t before start, first tick dt~0.07 -> alpha~0.007
        assert v < 5.0

    def test_stop_joins_thread(self):
        f = self.make_filter([42])
        f.start()
        f.stop()
        assert not f.is_alive()

    def test_getter_errors_do_not_kill_thread(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise TypeError("boom")

        f = GridFilter(getter=bad, tau=0.05, tick=0.01)
        f.start()
        time.sleep(0.05)
        f.stop()
        assert calls["n"] > 1  # kept ticking through errors
        assert f.value() is None

    def test_invalid_tau_rejected(self):
        with pytest.raises(ValueError):
            GridFilter(getter=lambda: 0.0, tau=0)

    def test_concurrent_reads_safe(self):
        f = self.make_filter([50] * 100)
        f.start()
        errs = []

        def reader():
            try:
                for _ in range(50):
                    assert f.value() in (None, 50.0)
            except Exception as e:  # pylint: disable=broad-exception-caught
                errs.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        f.stop()
        assert not errs


class TestControllerWiring:
    """GridFilter integration points on the controller."""

    def test_controller_creates_filter_when_tau_positive(self):
        from unittest.mock import MagicMock, patch

        _MOD = "inverter_control.controller"

        with (
            patch(f"{_MOD}.GRID_FILTER_TAU", 2.0),
            patch(f"{_MOD}.get_victron") as mock_get_victron,
            patch(f"{_MOD}.get_ha"),
            patch(f"{_MOD}.ConsoleUI"),
            patch("inverter_control.config.get_ui_config", return_value={}),
            patch(f"{_MOD}.DRY_RUN", False),
            patch(f"{_MOD}.DVCC_ENABLED", False),
            patch(f"{_MOD}.ENABLE_GRID_SMOOTHING_WITH_HOME", False),
            patch(f"{_MOD}.ENABLE_EV", False),
            patch(f"{_MOD}.ENABLE_WATER", False),
            patch(f"{_MOD}.ENABLE_HA", True),
        ):
            mock_victron = MagicMock()
            mock_get_victron.return_value = mock_victron
            mock_victron.get_cell_counts.return_value = {}
            from inverter_control.controller import InverterController

            controller = InverterController(dry_run=True)

        assert controller.grid_filter is not None
        # Per-cycle EMA must be identity when the background filter owns smoothing
        assert controller.calculator.ema_alpha == 1.0
        # Filter not started at construction (unit tests stay single-threaded)
        assert not controller.grid_filter.is_alive()

    def test_calculate_setpoint_uses_filter_value(self):
        from unittest.mock import MagicMock, patch

        _MOD = "inverter_control.controller"

        with (
            patch(f"{_MOD}.GRID_FILTER_TAU", 0.0),  # disabled: legacy path
            patch(f"{_MOD}.get_victron") as mock_get_victron,
            patch(f"{_MOD}.get_ha"),
            patch(f"{_MOD}.ConsoleUI"),
            patch(f"{_MOD}.SetpointCalculator") as mock_calc_cls,
            patch("inverter_control.config.get_ui_config", return_value={}),
            patch(f"{_MOD}.DRY_RUN", False),
            patch(f"{_MOD}.DVCC_ENABLED", False),
            patch(f"{_MOD}.ENABLE_GRID_SMOOTHING_WITH_HOME", False),
            patch(f"{_MOD}.ENABLE_EV", False),
            patch(f"{_MOD}.ENABLE_WATER", False),
            patch(f"{_MOD}.ENABLE_HA", True),
        ):
            mock_victron = MagicMock()
            mock_get_victron.return_value = mock_victron
            mock_victron.get_cell_counts.return_value = {}
            from inverter_control.controller import InverterController

            controller = InverterController(dry_run=True)

        # GRID_FILTER_TAU=0 -> no thread, legacy self.filtered_gt passthrough
        assert controller.grid_filter is None
        assert controller.calculator.ema_alpha != 1.0

        controller.filtered_gt = 123.0
        sys_data = {
            "g1": 0,
            "g2": 0,
            "gt": 100,
            "t1": 0,
            "t2": 0,
            "tt": 0,
        }
        controller.victron = MagicMock()
        controller.victron.get_system_data.return_value = sys_data
        controller.ha.get_vue_sensor.return_value = 0
        controller.ha.get_boolean.return_value = False
        controller.calculator.calculate.return_value = MagicMock(
            setpoint=0, flags="", filtered_gt=100.0
        )
        setpoint, _flags = controller.calculate_setpoint(sys_data)
        state_arg = controller.calculator.calculate.call_args[0][0]
        assert state_arg.filtered_gt == 123.0  # legacy value passed through
        assert setpoint == 0

    def test_derived_filter_created_and_fed(self):
        """ENABLE_GRID_SMOOTHING_WITH_HOME + TAU>0: second filter created, fed
        the raw derived value, and the state receives its smoothed output."""
        from unittest.mock import MagicMock, patch

        _MOD = "inverter_control.controller"

        with (
            patch(f"{_MOD}.GRID_FILTER_TAU", 0.0),
            patch(f"{_MOD}.GRID_SMOOTHING_DERIVED_TAU", 3.2),
            patch(f"{_MOD}.get_victron") as mock_get_victron,
            patch(f"{_MOD}.get_ha"),
            patch(f"{_MOD}.ConsoleUI"),
            patch(f"{_MOD}.SetpointCalculator"),
            patch("inverter_control.config.get_ui_config", return_value={}),
            patch(f"{_MOD}.DRY_RUN", False),
            patch(f"{_MOD}.DVCC_ENABLED", False),
            patch(f"{_MOD}.ENABLE_GRID_SMOOTHING_WITH_HOME", True),
            patch(f"{_MOD}.ENABLE_EV", False),
            patch(f"{_MOD}.ENABLE_WATER", False),
            patch(f"{_MOD}.ENABLE_HA", True),
        ):
            mock_victron = MagicMock()
            mock_get_victron.return_value = mock_victron
            mock_victron.get_cell_counts.return_value = {}
            from inverter_control.controller import InverterController

            controller = InverterController(dry_run=True)

            assert controller.grid_filter is None
            assert controller.derived_grid_filter is not None
            assert not controller.derived_grid_filter.is_alive()

            sys_data = {"g1": 0, "g2": 0, "gt": 100, "t1": 0, "t2": 0, "tt": 0}
            controller.victron = MagicMock()
            controller.victron.get_system_data.return_value = sys_data
            controller.ha.get_vue_sensor.return_value = 500  # home_total
            controller.ha.get_boolean.return_value = False
            controller.victron.get_mppt_data.return_value = {}
            controller.victron.get_pv_power.return_value = []
            controller.victron.get_inverter_power.return_value = 0
            controller.calculator.calculate.return_value = MagicMock(
                setpoint=0, flags="", filtered_gt=100.0
            )
            # Flag must still be patched here: calculate_setpoint gates the
            # derived path on it at call time.
            controller.calculate_setpoint(sys_data)
            # Raw derived (home - pv) landed on the filter's getter...
            assert controller._raw_derived_gt == 500.0
            # ...but no tick has run yet, so state got None this cycle.
            state_arg = controller.calculator.calculate.call_args[0][0]
            assert state_arg.derived_gt is None
