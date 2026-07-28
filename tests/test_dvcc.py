"""
Unit tests for DVCC (Dynamic Voltage and Current Control) calculator
"""

import unittest

from inverter_control.dvcc import DvccCalculator, DvccConfig, create_dvcc_from_config


class TestDvccCellVoltage(unittest.TestCase):
    def setUp(self):
        self.config = DvccConfig(
            max_charge_current=100.0,
            max_discharge_current=120.0,
            min_charge_current=2.0,
        )
        self.calc = DvccCalculator(self.config)

    def test_no_cell_data_returns_max_charge_current(self):
        ccl, reason = self.calc.calculate_ccl_from_cell_voltage(None)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "no_cell_data")

    def test_normal_voltage_full_current(self):
        ccl, reason = self.calc.calculate_ccl_from_cell_voltage(3.30)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "normal")

    def test_cell_cutoff_stops_charging(self):
        ccl, reason = self.calc.calculate_ccl_from_cell_voltage(3.60)
        self.assertEqual(ccl, 0.0)
        self.assertIn("cell_overvoltage", reason)

    def test_above_cutoff_stops_charging(self):
        ccl, _ = self.calc.calculate_ccl_from_cell_voltage(3.70)
        self.assertEqual(ccl, 0.0)

    def test_near_full_reduces_to_tail_charge(self):
        ccl, reason = self.calc.calculate_ccl_from_cell_voltage(3.55)
        self.assertLessEqual(ccl, self.config.min_charge_current)
        self.assertIn("tail_charge", reason)


class TestDvccImbalance(unittest.TestCase):
    def setUp(self):
        self.calc = DvccCalculator(DvccConfig(max_charge_current=100.0, min_charge_current=2.0))

    def test_no_delta_returns_max(self):
        ccl, reason = self.calc.calculate_ccl_from_imbalance(None)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "no_delta")

    def test_negative_delta_returns_max(self):
        ccl, reason = self.calc.calculate_ccl_from_imbalance(-0.01)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "no_delta")

    def test_balanced_full_current(self):
        ccl, reason = self.calc.calculate_ccl_from_imbalance(0.02)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "balanced")

    def test_critical_imbalance_minimal_current(self):
        ccl, reason = self.calc.calculate_ccl_from_imbalance(0.25)
        self.assertEqual(ccl, 2.0)
        self.assertIn("critical_imbalance", reason)


class TestDvccTemperature(unittest.TestCase):
    def setUp(self):
        self.calc = DvccCalculator(DvccConfig(max_charge_current=100.0, min_charge_current=2.0))

    def test_no_temp_data_returns_max(self):
        ccl, reason = self.calc.calculate_ccl_from_temperature(None, None)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "no_temp_data")

    def test_too_cold_stops_charging(self):
        ccl, reason = self.calc.calculate_ccl_from_temperature(-5.0, -5.0)
        self.assertEqual(ccl, 0.0)
        self.assertIn("too_cold", reason)

    def test_too_hot_stops_charging(self):
        ccl, reason = self.calc.calculate_ccl_from_temperature(55.0, 55.0)
        self.assertEqual(ccl, 0.0)
        self.assertIn("too_hot", reason)

    def test_optimal_temp_full_current(self):
        ccl, reason = self.calc.calculate_ccl_from_temperature(25.0, 25.0)
        self.assertEqual(ccl, 100.0)
        self.assertIn("temp_ok", reason)

    def test_discharge_stops_when_too_cold(self):
        dcl, reason = self.calc.calculate_dcl_from_temperature(-25.0, -25.0)
        self.assertEqual(dcl, 0.0)
        self.assertIn("too_cold_discharge", reason)


class TestDvccSoc(unittest.TestCase):
    def setUp(self):
        self.calc = DvccCalculator(
            DvccConfig(
                max_charge_current=100.0,
                max_discharge_current=120.0,
                soc_reduce_start=95.0,
                soc_reduce_factor=0.5,
                soc_discharge_stop=5.0,
                soc_discharge_reduced=10.0,
            )
        )

    def test_soc_below_threshold_full_current(self):
        ccl, reason = self.calc.calculate_ccl_from_soc(50.0)
        self.assertEqual(ccl, 100.0)
        self.assertEqual(reason, "soc_ok")

    def test_soc_100_reduces_to_factor(self):
        ccl, reason = self.calc.calculate_ccl_from_soc(100.0)
        self.assertEqual(ccl, 50.0)
        self.assertEqual(reason, "soc_100")

    def test_deep_discharge_stops_discharging(self):
        dcl, reason = self.calc.calculate_dcl_from_soc(3.0)
        self.assertEqual(dcl, 0.0)
        self.assertIn("deep_discharge", reason)

    def test_discharge_soc_ok_above_threshold(self):
        dcl, reason = self.calc.calculate_dcl_from_soc(50.0)
        self.assertEqual(dcl, 120.0)
        self.assertEqual(reason, "soc_discharge_ok")


class TestDvccCalculate(unittest.TestCase):
    def setUp(self):
        self.calc = DvccCalculator(
            DvccConfig(max_charge_current=100.0, max_discharge_current=120.0)
        )

    def test_bms_block_charge_forces_zero_ccl(self):
        result = self.calc.calculate({"allow_charge": False})
        self.assertEqual(result.ccl, 0.0)
        self.assertEqual(result.ccl_reason, "bms_blocked")

    def test_bms_block_discharge_forces_zero_dcl(self):
        result = self.calc.calculate({"allow_discharge": False})
        self.assertEqual(result.dcl, 0.0)
        self.assertEqual(result.dcl_reason, "bms_blocked")

    def test_bms_block_is_immediate_not_rate_limited(self):
        # Prime the calculator with a full-current baseline first.
        self.calc.calculate({})
        result = self.calc.calculate({"allow_charge": False, "allow_discharge": False})
        # Hard safety cutoffs must apply immediately regardless of change rate.
        self.assertEqual(result.ccl, 0.0)
        self.assertEqual(result.dcl, 0.0)

    def test_cell_overvoltage_cutoff_is_immediate_not_rate_limited(self):
        self.calc.calculate({"max_cell": 3.30})
        result = self.calc.calculate({"max_cell": 3.60})
        # Hard cutoff (0A) must not be rate limited even though ccl_change_rate
        # would otherwise only allow a small step down.
        self.assertEqual(result.ccl, 0.0)

    def test_first_call_does_not_apply_inflated_dt(self):
        # On the very first calculate() call there's no prior timestamp, so
        # rate limiting must not use an inflated/stale dt based on __init__ time.
        result = self.calc.calculate({"max_cell": 3.30})
        self.assertEqual(result.ccl, 100.0)

    def test_cvl_uses_cell_count_and_max_voltage(self):
        calc = DvccCalculator(DvccConfig(cell_count=16, cell_max_voltage=3.65))
        result = calc.calculate({})
        self.assertAlmostEqual(result.cvl, 16 * 3.65, places=2)

    def test_returns_diagnostic_fields(self):
        result = self.calc.calculate(
            {
                "max_cell": 3.30,
                "max_cell_id": 1,
                "min_cell": 3.28,
                "min_cell_id": 2,
                "max_temp": 25.0,
                "min_temp": 20.0,
                "soc": 50.0,
            }
        )
        self.assertEqual(result.max_cell_voltage, 3.30)
        self.assertEqual(result.max_cell_id, 1)
        self.assertEqual(result.min_cell_voltage, 3.28)
        self.assertEqual(result.min_cell_id, 2)
        self.assertAlmostEqual(result.cell_delta, 0.02, places=5)
        self.assertEqual(result.min_temp, 20.0)
        self.assertEqual(result.max_temp, 25.0)
        self.assertEqual(result.soc, 50.0)


class TestCreateDvccFromConfig(unittest.TestCase):
    def test_maps_config_dict_to_dvcc_config(self):
        config = {
            "DVCC_CELL_COUNT": 8,
            "DVCC_MAX_CHARGE_CURRENT": 50.0,
            "DVCC_MAX_DISCHARGE_CURRENT": 60.0,
            "DVCC_CELL_MAX_VOLTAGE": 3.65,
            "DVCC_CELL_START_LIMIT": 3.45,
            "DVCC_CELL_BALANCE_VOLTAGE": 3.50,
            "DVCC_CCL_CHANGE_RATE": 5.0,
            "DVCC_DCL_CHANGE_RATE": 7.5,
            "DVCC_CELL_FULL_CURRENT": 3.40,
            "DVCC_CELL_NEAR_FULL": 3.55,
            "DVCC_CELL_CUTOFF": 3.60,
            "DVCC_MIN_CHARGE_CURRENT": 1.0,
            "DVCC_IMBALANCE_START_LIMIT": 0.05,
            "DVCC_IMBALANCE_AGGRESSIVE": 0.10,
            "DVCC_IMBALANCE_CRITICAL": 0.20,
            "DVCC_TEMP_STOP_CHARGE": 0.0,
            "DVCC_TEMP_FULL_CURRENT_MIN": 10.0,
            "DVCC_TEMP_FULL_CURRENT_MAX": 40.0,
            "DVCC_TEMP_STOP_CHARGE_HIGH": 50.0,
            "DVCC_TEMP_DISCHARGE_MIN": -20.0,
            "DVCC_TEMP_DISCHARGE_REDUCED": -10.0,
            "DVCC_SOC_REDUCE_START": 90.0,
            "DVCC_SOC_REDUCE_FACTOR": 0.4,
            "DVCC_SOC_DISCHARGE_STOP": 4.0,
            "DVCC_SOC_DISCHARGE_REDUCED": 9.0,
        }
        calc = create_dvcc_from_config(config)
        self.assertEqual(calc.config.cell_count, 8)
        self.assertEqual(calc.config.max_charge_current, 50.0)
        self.assertEqual(calc.config.max_discharge_current, 60.0)
        self.assertEqual(calc.config.cell_full_current, 3.40)
        self.assertEqual(calc.config.cell_near_full, 3.55)
        self.assertEqual(calc.config.cell_cutoff, 3.60)
        self.assertEqual(calc.config.min_charge_current, 1.0)
        self.assertEqual(calc.config.imbalance_start, 0.05)
        self.assertEqual(calc.config.imbalance_aggressive, 0.10)
        self.assertEqual(calc.config.imbalance_critical, 0.20)
        self.assertEqual(calc.config.temp_charge_min, 0.0)
        self.assertEqual(calc.config.temp_charge_optimal, 10.0)
        self.assertEqual(calc.config.temp_charge_limit, 40.0)
        self.assertEqual(calc.config.temp_charge_stop, 50.0)
        self.assertEqual(calc.config.temp_discharge_min, -20.0)
        self.assertEqual(calc.config.temp_discharge_reduced, -10.0)
        self.assertEqual(calc.config.soc_reduce_start, 90.0)
        self.assertEqual(calc.config.soc_reduce_factor, 0.4)
        self.assertEqual(calc.config.soc_discharge_stop, 4.0)
        self.assertEqual(calc.config.soc_discharge_reduced, 9.0)

    def test_defaults_when_config_dict_empty(self):
        calc = create_dvcc_from_config({})
        self.assertEqual(calc.config.cell_count, 16)
        self.assertEqual(calc.config.max_charge_current, 100.0)
        self.assertEqual(calc.config.max_discharge_current, 120.0)


if __name__ == "__main__":
    unittest.main()
