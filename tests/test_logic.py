"""
Unit tests for Inverter Control logic
"""

import unittest
from logic import SetpointCalculator, SystemState

class TestLogic(unittest.TestCase):
    def setUp(self):
        self.config = {
            'EMA_ALPHA': 1.0,  # No smoothing for tests
            'POWER_LIMIT_MIN': -2300,
            'POWER_LIMIT_MAX': 2250,
            'SETPOINT_DELTA_LIMIT': 2000,
            'DAMPING_FACTOR': 0.7,  # Import damping
            'GRID_ZERO_DEADBAND_LOW': -10,
            'GRID_ZERO_DEADBAND_HIGH': 10,
            'INVERTER_EFFICIENCY': 1.0,  # 100% efficiency for simple math
            'SOLAR_OUTPUT_OFFSET': 0,
            'EXPORT_DAMPING': 1.0,  # Full correction for export
            'CREEP_RATE': 0.5,
            'CREEP_MAX': 100.0,
        }
        self.calculator = SetpointCalculator(self.config)

    def get_base_state(self):
        return SystemState(
            g1=0, g2=0, gt=0,
            t1=0, t2=0, tt=0,
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
            filtered_gt=None
        )

    def test_grid_zero_import(self):
        """Test grid-zero targeting when importing power"""
        state = self.get_base_state()
        state.gt = 500  # Importing 500W
        state.inv_power = -1000  # Currently discharging 1000W (negative)
        state.previous_setpoint = -1000

        result = self.calculator.calculate(state)
        # Outside deadband, import: correction = -500 * 0.7 = -350
        # vanew = -1000 + (-350) = -1350
        self.assertEqual(result.setpoint, -1350)

    def test_normal_strategy_math(self):
        """Verify the grid-zero math: vanew = inv_power + correction"""
        state = self.get_base_state()
        state.gt = 1000  # Importing 1000W
        state.inv_power = 0
        state.previous_setpoint = 0

        result = self.calculator.calculate(state)
        # correction = -1000 * 0.7 (DAMPING_FACTOR) = -700
        # vanew = 0 + (-700) = -700
        self.assertEqual(result.setpoint, -700)

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

        # Now push outside deadband
        state.gt = 500
        state.filtered_gt = None
        result = self.calculator.calculate(state)

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
        # Outside deadband (gt=-200 < deadband_low=-5), export path
        # correction = -(-200) * EXPORT_DAMPING(1.0) = 200
        # vanew = -500 + 200 = -300
        self.assertEqual(result.setpoint, -300)

        # Import case: gt = 200 (importing 200W), damping=0.7
        state2 = self.get_base_state()
        state2.gt = 200
        state2.inv_power = -500
        state2.previous_setpoint = -500
        state2.filtered_gt = None

        result2 = self.calculator.calculate(state2)
        # Outside deadband, import path
        # correction = -200 * DAMPING_FACTOR(0.7) = -140
        # vanew = -500 + (-140) = -640
        self.assertEqual(result2.setpoint, -640)

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
        # Normal calc would be -1000, but limited to -500 (MPPT)
        self.assertEqual(result.setpoint, -500)
        self.assertIn("[OC:500]", result.flags)

    def test_charge_battery_priority(self):
        """Verify charge_battery overrides other modes"""
        self.calculator.delta_limit = 3000 # Allow large jump for test
        state = self.get_base_state()
        state.charge_battery = True
        state.only_charging = True # Lower priority
        state.gt = 1000
        
        result = self.calculator.calculate(state)
        self.assertEqual(result.setpoint, 2200)
        self.assertIn("[CHG]", result.flags)

    def test_software_fuse_delta_limit(self):
        """Verify that setpoint changes are clamped by SETPOINT_DELTA_LIMIT"""
        state = self.get_base_state()
        state.previous_setpoint = 0
        state.gt = 1500  # Would be -1050 with damping 0.7

        self.calculator.delta_limit = 500
        result = self.calculator.calculate(state)
        # correction = -1500 * 0.7 = -1050, clamped to -500 by delta_limit
        self.assertEqual(result.setpoint, -500)
        self.assertIn("[!Δ1050]", result.flags)

    def test_ev_exclusion(self):
        """Verify that EV power is subtracted from grid when do_not_supply_charger is ON"""
        state = self.get_base_state()
        state.do_not_supply_charger = True
        state.mppt_total = 1000  # Provide enough solar to not cap output at 0
        state.gt = 2000
        state.ev_power = 1500

        result = self.calculator.calculate(state)
        # Effective grid = 2000 - 1500 = 500
        # Outside deadband, import: correction = -500 * 0.7 = -350
        # vanew = 0 + (-350) = -350
        self.assertEqual(result.setpoint, -350)
        self.assertIn("[EV:1500]", result.flags)

if __name__ == '__main__':
    unittest.main()
