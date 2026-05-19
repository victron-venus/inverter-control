"""
Unit tests for Inverter Control logic
"""

import unittest
from logic import SetpointCalculator, SystemState, ControlResult

class TestLogic(unittest.TestCase):
    def setUp(self):
        self.config = {
            'EMA_ALPHA': 1.0,  # No smoothing for tests
            'POWER_LIMIT_MIN': -2300,
            'POWER_LIMIT_MAX': 2250,
            'SETPOINT_DELTA_LIMIT': 2000,
            'DAMPING_FACTOR': 1.0,  # No damping for tests
            'GRID_ZERO_DEADBAND_LOW': -10,
            'GRID_ZERO_DEADBAND_HIGH': 10,
            'INVERTER_EFFICIENCY': 1.0,  # 100% efficiency for simple math
            'SOLAR_OUTPUT_OFFSET': 0
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
        # Should increase discharge by 500W to 1500W
        # vanew = inv_power - gt = -1000 - 500 = -1500
        self.assertEqual(result.setpoint, -1500)

    def test_normal_strategy_math(self):
        """Verify the grid-zero math: vanew = inv_power - smoothed_gt"""
        state = self.get_base_state()
        state.gt = 1000  # Importing 1000W
        state.inv_power = 0
        state.previous_setpoint = 0
        
        result = self.calculator.calculate(state)
        # correction = -1000 * 1.0 = -1000
        # vanew = 0 + (-1000) = -1000
        self.assertEqual(result.setpoint, -1000)

    def test_deadband(self):
        """Verify that small fluctuations are ignored"""
        state = self.get_base_state()
        state.gt = 5  # Within deadband (-10, 10)
        state.previous_setpoint = -500
        
        result = self.calculator.calculate(state)
        self.assertEqual(result.setpoint, -500)
        self.assertIn("[~]", result.flags)

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
        state.gt = 1500  # Normal setpoint would be -1500
        
        self.calculator.delta_limit = 500
        result = self.calculator.calculate(state)
        self.assertEqual(result.setpoint, -500)
        self.assertIn("[!Δ1500]", result.flags)

    def test_ev_exclusion(self):
        """Verify that EV power is subtracted from grid when do_not_supply_charger is ON"""
        state = self.get_base_state()
        state.do_not_supply_charger = True
        state.mppt_total = 1000 # Provide enough solar to not cap output at 0
        state.gt = 2000
        state.ev_power = 1500
        
        result = self.calculator.calculate(state)
        # Effective grid = 2000 - 1500 = 500
        # Setpoint should be -500
        self.assertEqual(result.setpoint, -500)
        self.assertIn("[EV:1500]", result.flags)

if __name__ == '__main__':
    unittest.main()
