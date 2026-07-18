"""
Unit tests for Inverter Control logic
"""

import unittest
from inverter_control.logic import SetpointCalculator, SystemState


class TestLogic(unittest.TestCase):
    def setUp(self):
        self.config = {
            "EMA_ALPHA": 1.0,  # No smoothing for tests
            "POWER_LIMIT_MIN": -2300,
            "POWER_LIMIT_MAX": 2250,
            "SETPOINT_DELTA_LIMIT": 2000,
            "DAMPING_FACTOR": 0.7,  # Import damping
            "GRID_ZERO_DEADBAND_LOW": -10,
            "GRID_ZERO_DEADBAND_HIGH": 10,
            "INVERTER_EFFICIENCY": 1.0,  # 100% efficiency for simple math
            "SOLAR_OUTPUT_OFFSET": 0,
            "EXPORT_DAMPING": 1.0,  # Full correction for export
            "CREEP_RATE": 0.5,
            "CREEP_MAX": 100.0,
            "D_BRAKE_ZONE": 100,
            "D_THRESHOLD": 50,
            "D_GAIN": 0.3,
        }
        self.calculator = SetpointCalculator(self.config)

    def get_base_state(self):
        return SystemState(
            g1=0,
            g2=0,
            gt=0,
            t1=0,
            t2=0,
            tt=0,
            inv_power=0,
            mppt_total=0,
            tasmota_total=0,
            pv_total=0,
            ev_power=0,
            garage_power=0,
            only_charging=False,
            no_feed=False,
            house_support=False,
            charge_battery=False,
            do_not_supply_charger=False,
            limit_to_ev=False,
            previous_setpoint=0,
            filtered_gt=None,
        )

    def test_grid_zero_import(self):
        """Test grid-zero targeting when importing power"""
        state = self.get_base_state()
        state.gt = 500  # Importing 500W
        state.inv_power = -1000  # Currently discharging 1000W (negative)
        state.previous_setpoint = -1000

        result = self.calculator.calculate(state)
        # Raw: -1000 + (-500 * 0.7) = -1350
        # Rate limited: -1000 + (-350) * 7/10 = -1245
        # Should move more negative
        self.assertLess(result.setpoint, -1000)

    def test_normal_strategy_math(self):
        """Verify the grid-zero math: setpoint moves toward grid zero"""
        state = self.get_base_state()
        state.gt = 1000  # Importing 1000W
        state.inv_power = 0
        state.previous_setpoint = 0

        result = self.calculator.calculate(state)
        # Raw correction = -1000 * 0.7 = -700
        # Rate limited: 0 + (-700) * 7/10 = -490
        # Should move negative (more discharge) to compensate for import
        self.assertLess(result.setpoint, 0)
        self.assertGreater(result.setpoint, -700)

    def test_deadband(self):
        """Verify that small fluctuations are ignored"""
        state = self.get_base_state()
        state.gt = 5  # Within deadband (-10, 10)
        state.previous_setpoint = -500

        result = self.calculator.calculate(state)
        self.assertEqual(result.setpoint, -500)
        self.assertIn("[~", result.flags)

    def test_creep_import(self):
        """Creep should increase discharge when consistently importing in deadband"""
        state = self.get_base_state()
        state.gt = 50  # Importing 50W, within deadband (-10, 10)? No — 50 > 10
        # Widen deadband for this test
        self.calculator.strategies[0].deadband_low = -100
        self.calculator.strategies[0].deadband_high = 100
        state.previous_setpoint = -500

        # Run multiple cycles to accumulate creep
        for _ in range(10):
            state.filtered_gt = None  # Reset EMA each time for consistent effective_gt
            result = self.calculator.calculate(state)
            state.filtered_gt = result.filtered_gt
            state.previous_setpoint = result.setpoint

        # Creep should have moved setpoint more negative (more discharge)
        self.assertLess(result.setpoint, -500)

    def test_creep_export(self):
        """Creep should decrease discharge when consistently exporting in deadband"""
        state = self.get_base_state()
        state.gt = -30  # Exporting 30W
        self.calculator.strategies[0].deadband_low = -100
        self.calculator.strategies[0].deadband_high = 100
        state.previous_setpoint = -500

        for _ in range(10):
            state.filtered_gt = None
            result = self.calculator.calculate(state)
            state.filtered_gt = result.filtered_gt
            state.previous_setpoint = result.setpoint

        # Creep should have moved setpoint less negative (less discharge)
        self.assertGreater(result.setpoint, -500)

    def test_creep_reset_outside_deadband(self):
        """Creep accumulator should reset when grid leaves deadband"""
        normal = self.calculator.strategies[0]
        normal.deadband_low = -100
        normal.deadband_high = 100

        state = self.get_base_state()
        state.gt = 50
        state.previous_setpoint = -500

        # Accumulate creep
        for _ in range(10):
            state.filtered_gt = None
            result = self.calculator.calculate(state)
            state.filtered_gt = result.filtered_gt
            state.previous_setpoint = result.setpoint

        self.assertNotEqual(normal.creep_accumulator, 0.0)

        # Now push outside deadband — creep should reset
        state.gt = 500
        state.filtered_gt = None
        self.calculator.calculate(state)

        self.assertEqual(normal.creep_accumulator, 0.0)
        self.assertEqual(normal.stable_count, 0)

    def test_export_damping_stronger(self):
        """Export correction uses export_damping (1.0), stronger than import (0.7)"""
        normal = self.calculator.strategies[0]
        normal.deadband_low = -5  # narrow deadband
        normal.deadband_high = 5

        # Export case: gt = -200 (exporting 200W)
        state = self.get_base_state()
        state.gt = -200
        state.inv_power = -500
        state.previous_setpoint = -500
        state.filtered_gt = None

        result = self.calculator.calculate(state)
        # Raw: -500 + (-(-200) * 1.0) = -300
        # Rate limited: -500 + (200 * 7/10) = -360
        # Should move toward less negative (reduce export)
        self.assertGreater(result.setpoint, -500)
        self.assertLess(result.setpoint, -300)

        # Import case: gt = 200 (importing 200W), damping=0.7
        state2 = self.get_base_state()
        state2.gt = 200
        state2.inv_power = -500
        state2.previous_setpoint = -500
        state2.filtered_gt = None

        result2 = self.calculator.calculate(state2)
        # Raw: -500 + (-200 * 0.7) = -640
        # Rate limited: -500 + (-140 * 7/10) = -602
        # Should move more negative
        self.assertLess(result2.setpoint, -500)
        self.assertGreater(result2.setpoint, -640)

    def test_export_creep_faster(self):
        """Creep should accumulate 2x faster for export than import"""
        normal = self.calculator.strategies[0]
        normal.deadband_low = -100
        normal.deadband_high = 100
        normal.creep_accumulator = 0.0

        state = self.get_base_state()
        state.previous_setpoint = -500

        # Export creep: gt = -10
        state.gt = -10
        for _ in range(10):
            state.filtered_gt = None
            result = self.calculator.calculate(state)
            state.filtered_gt = result.filtered_gt
            state.previous_setpoint = result.setpoint

        export_accumulator = abs(normal.creep_accumulator)

        # Reset and test import creep
        normal.creep_accumulator = 0.0
        state.gt = 10
        state.previous_setpoint = -500
        for _ in range(10):
            state.filtered_gt = None
            result = self.calculator.calculate(state)
            state.filtered_gt = result.filtered_gt
            state.previous_setpoint = result.setpoint

        import_accumulator = abs(normal.creep_accumulator)

        # Export creep should be ~2x import creep
        self.assertGreater(export_accumulator, import_accumulator * 1.5)

    def test_only_charging_mode(self):
        """Verify only_charging limits output to MPPT production"""
        state = self.get_base_state()
        state.only_charging = True
        state.mppt_total = 500
        state.gt = 1000
        state.inv_power = 0
        state.previous_setpoint = 0

        result = self.calculator.calculate(state)
        # only_charging limits output to MPPT production
        # Should be negative (discharge) but capped
        self.assertLess(result.setpoint, 0)
        self.assertIn("[OC", result.flags)

    def test_charge_battery_priority(self):
        """Verify charge_battery overrides other modes"""
        self.calculator.delta_limit = 3000  # Allow large jump for test
        state = self.get_base_state()
        state.charge_battery = True
        state.only_charging = True  # Lower priority
        state.gt = 1000

        result = self.calculator.calculate(state)
        # charge_battery forces positive setpoint (charging)
        self.assertGreater(result.setpoint, 0)
        self.assertIn("[CHG]", result.flags)

    def test_software_fuse_delta_limit(self):
        """Verify that setpoint changes are clamped by SETPOINT_DELTA_LIMIT"""
        state = self.get_base_state()
        state.previous_setpoint = 0
        state.gt = 1500  # Would be -1050 with damping 0.7

        self.calculator.delta_limit = 500
        result = self.calculator.calculate(state)
        # Large grid import should trigger rate-limited negative setpoint
        self.assertLess(result.setpoint, 0)
        # Should hit delta limit
        self.assertIn("[!Δ", result.flags)

    def test_ev_exclusion(self):
        """Verify that EV power is subtracted from grid when do_not_supply_charger is ON"""
        state = self.get_base_state()
        state.do_not_supply_charger = True
        state.mppt_total = 1000  # Provide enough solar to not cap output at 0
        state.gt = 2000
        state.ev_power = 1500

        result = self.calculator.calculate(state)
        # Effective grid = 2000 - 1500 = 500
        # Should move negative to compensate
        self.assertLess(result.setpoint, 0)
        self.assertIn("[EV:1500]", result.flags)

    def test_burst_correction_pump_startup(self):
        """Pump turns on: gt jumps to -400 while filtered_gt is ~0.
        Burst correction should fire and apply immediate correction."""
        self.config["EMA_ALPHA"] = 0.3  # Realistic EMA
        self.config["BURST_THRESHOLD"] = 150
        self.config["BURST_GAIN"] = 0.8
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 0
        state.filtered_gt = 0.0  # EMA hasn't caught up yet

        # Cycle 1: pump turns on, grid jumps to -400
        state.gt = -400
        result = calculator.calculate(state)

        # Spike = -400 - 0 = -400, abs(400) > 150 → burst fires
        # burst_correction = -(-400) * 0.8 = +320
        self.assertIn("[B:", result.flags)
        self.assertGreater(result.setpoint, 0)  # Should increase setpoint

    def test_burst_correction_no_spike(self):
        """Normal operation: gt matches filtered_gt. No burst correction."""
        self.config["BURST_THRESHOLD"] = 150
        self.config["BURST_GAIN"] = 0.8
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 0
        state.filtered_gt = 10.0  # EMA is close to gt

        state.gt = 15  # Small change, within threshold
        result = calculator.calculate(state)

        # Spike = 15 - 10 = 5, abs(5) < 150 → no burst
        self.assertNotIn("[B:", result.flags)

    def test_burst_correction_import_spike(self):
        """Sudden load increase: gt jumps positive (importing more).
        Burst correction should fire and increase discharge."""
        self.config["EMA_ALPHA"] = 0.3
        self.config["BURST_THRESHOLD"] = 150
        self.config["BURST_GAIN"] = 0.8
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = -500
        state.filtered_gt = 0.0

        # Spike: importing 500W suddenly
        state.gt = 500
        result = calculator.calculate(state)

        # Spike = 500 - 0 = 500, abs(500) > 150 → burst fires
        # burst_correction = -(500) * 0.8 = -400
        self.assertIn("[B:", result.flags)
        self.assertLess(result.setpoint, -500)  # Should increase discharge

    def test_burst_below_threshold(self):
        """Spike below threshold should not trigger burst correction."""
        self.config["BURST_THRESHOLD"] = 150
        self.config["BURST_GAIN"] = 0.8
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 0
        state.filtered_gt = 0.0

        # Spike of 100W — below threshold of 150
        state.gt = 100
        result = calculator.calculate(state)

        self.assertNotIn("[B:", result.flags)

    def test_burst_correction_magnitude(self):
        """Burst correction should apply gain fraction of the spike."""
        self.config["EMA_ALPHA"] = 0.3  # Realistic EMA
        self.config["BURST_THRESHOLD"] = 100
        self.config["BURST_GAIN"] = 0.8
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 0
        state.filtered_gt = 0.0

        # Spike of -400W (export), gain 0.8 → correction = +320
        state.gt = -400
        result = calculator.calculate(state)

        # old_filtered_gt=0.0, effective_gt=-400, new_filtered_gt=0.3*-400+0.7*0=-120
        # Strategies: smoothed_gt=-120, outside deadband, damping=1.0
        #   correction = -(-120)*1.0 = 120, raw_vanew = 0+120 = 120
        # Burst: spike = -400-0 = -400, burst_correction = 320
        #   raw_vanew = 120+320 = 440
        # Rate limit: 0+(440)*9/10 = 396
        self.assertGreater(result.setpoint, 300)
        self.assertLess(result.setpoint, 450)
        self.assertIn("[B:", result.flags)

    def test_d_term_brakes_when_approaching_zero_fast(self):
        """When gt is close to zero but dropping fast, D-term should brake
        to prevent overshoot into export territory."""
        self.config["EMA_ALPHA"] = 1.0
        self.config["D_BRAKE_ZONE"] = 100
        self.config["D_THRESHOLD"] = 50
        self.config["D_GAIN"] = 0.3
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 500

        # Cycle 1: gt = 80 (close to zero, moving toward zero from above)
        state.gt = 80
        state.filtered_gt = 80.0
        result1 = calculator.calculate(state)
        self.assertNotIn("[D:", result1.flags)

        # Cycle 2: gt drops to -30 (moved 110W in one cycle — fast approach to zero)
        state.gt = -30
        state.filtered_gt = -30.0
        result2 = calculator.calculate(state)

        # d_gt = -30 - 80 = -110, abs(-110) > 50 threshold
        # abs(-30) < 100 brake zone → D-term fires
        # brake = -(-110) * 0.3 = +33 (slow down the correction)
        self.assertIn("[D:", result2.flags)

    def test_d_term_no_brake_outside_zone(self):
        """When gt is far from zero, D-term should not fire even if moving fast."""
        self.config["EMA_ALPHA"] = 1.0
        self.config["D_BRAKE_ZONE"] = 100
        self.config["D_THRESHOLD"] = 50
        self.config["D_GAIN"] = 0.3
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 0

        # Cycle 1: gt = 500 (far from zero)
        state.gt = 500
        state.filtered_gt = 500.0
        calculator.calculate(state)

        # Cycle 2: gt = 300 (moving fast but still far from zero)
        state.gt = 300
        state.filtered_gt = 300.0
        result = calculator.calculate(state)

        # abs(300) > 100 brake zone → D-term does NOT fire
        self.assertNotIn("[D:", result.flags)

    def test_d_term_no_brake_slow_movement(self):
        """When gt is close to zero but moving slowly, D-term should not fire."""
        self.config["EMA_ALPHA"] = 1.0
        self.config["D_BRAKE_ZONE"] = 100
        self.config["D_THRESHOLD"] = 50
        self.config["D_GAIN"] = 0.3
        calculator = SetpointCalculator(self.config)

        state = self.get_base_state()
        state.previous_setpoint = 0

        # Cycle 1: gt = 80
        state.gt = 80
        state.filtered_gt = 80.0
        calculator.calculate(state)

        # Cycle 2: gt = 60 (only moved 20W — slow)
        state.gt = 60
        state.filtered_gt = 60.0
        result = calculator.calculate(state)

        # d_gt = 60 - 80 = -20, abs(-20) < 50 threshold → no D-term
        self.assertNotIn("[D:", result.flags)


if __name__ == "__main__":
    unittest.main()
