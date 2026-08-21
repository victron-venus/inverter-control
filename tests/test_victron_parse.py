"""Tests for victron_parse: D-Bus tree output parsers and battery SOC math."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron_parse as vp


class TestExtractPowerFromTree:
    def test_tree_format(self):
        out = '         string "Ac/Power"\n        variant       double 188.0'
        assert vp.extract_power_from_tree(out) == 188.0

    def test_literal_variant_format(self):
        assert vp.extract_power_from_tree("   variant       double 42.5") == 42.5

    def test_none_and_empty(self):
        assert vp.extract_power_from_tree(None) == 0.0
        assert vp.extract_power_from_tree("") == 0.0
        assert vp.extract_power_from_tree("garbage") == 0.0

    def test_negative_power(self):
        assert vp.extract_power_from_tree("variant double -125.5") == -125.5


class TestParseSystemDataOutput:
    TREE = (
        '            string "Ac/Grid/L1/Power"\n'
        "            variant                double 100.5\n"
        '            string "Ac/Grid/L2/Power"\n'
        "            variant                double -50.25\n"
        '            string "Ac/Consumption/L1/Power"\n'
        "            variant                double 300.0\n"
        '            string "Ac/Consumption/L2/Power"\n'
        "            variant                double 200.0\n"
        '            string "Dc/Battery/Voltage"\n'
        "            variant                double 53.35\n"
        '            string "Dc/Battery/Current"\n'
        "            variant                double 10.2\n"
        '            string "Dc/Battery/Power"\n'
        "            variant                double 544.17\n"
        '            string "Dc/Pv/Power"\n'
        "            variant                double 1043.0\n"
    )

    def test_full_tree(self):
        d = vp.parse_system_data_output(self.TREE)
        # powers truncated to int by design, voltages/currents stay float
        assert d["g1"] == 100
        assert d["g2"] == -50
        assert d["t1"] == 300
        assert d["t2"] == 200
        assert d["bv"] == 53.35
        assert d["bc"] == 10.2
        assert d["bp"] == 544
        assert d["pv_total"] == 1043

    def test_missing_paths_default_zero(self):
        d = vp.parse_system_data_output('string "Dc/Pv/Power"\nvariant double 7.0')
        assert d["pv_total"] == 7.0
        assert d["g1"] == 0
        assert d["bp"] == 0


class TestParseVariantAndMppt:
    def test_parse_variant_value(self):
        assert vp.parse_variant_value("   variant       int32 3") == 3.0
        assert vp.parse_variant_value(None) == 0.0
        assert vp.parse_variant_value("no value here") == 0.0

    def test_parse_mppt_output(self):
        out = (
            '            string "Yield/Power"\n'
            "            variant                double 454.54\n"
            '            string "Dc/0/Current"\n'
            "            variant                double 8.2\n"
        )
        assert vp.parse_mppt_output(out) == {"w": 454.54, "a": 8.2}

    def test_parse_mppt_empty(self):
        assert vp.parse_mppt_output("") == {"w": 0.0, "a": 0.0}


class TestBatterySocFromVoltage:
    def test_voltage_out_of_range(self):
        assert vp.calculate_battery_soc_from_voltage(30.0, 0) == 0.0
        assert vp.calculate_battery_soc_from_voltage(60.0, 0) == 0.0

    def test_monotonic_in_range(self):
        low = vp.calculate_battery_soc_from_voltage(51.0, 0)
        high = vp.calculate_battery_soc_from_voltage(55.0, 0)
        assert 0 <= low < high <= 100

    def test_discharge_correction_raises_soc(self):
        base = vp._voltage_to_soc(53.0)
        corrected = vp.calculate_battery_soc_from_voltage(53.0, -500)
        assert corrected >= base

    def test_charge_correction_lowers_soc(self):
        base = vp._voltage_to_soc(53.0)
        corrected = vp.calculate_battery_soc_from_voltage(53.0, 500)
        assert corrected <= base

    def test_zero_power_no_correction(self):
        assert vp.calculate_battery_soc_from_voltage(53.0, 0) == round(vp._voltage_to_soc(53.0), 2)
