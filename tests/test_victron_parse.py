"""Tests for victron_parse: D-Bus tree output parsers and battery SOC math."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron_parse as vp


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
        '            string "Dc/Pv/Power"\n'
        "            variant                double 1043.0\n"
    )

    def test_full_tree(self):
        d = vp.parse_system_data_output(self.TREE)
        # powers truncated to int by design; bank V/I/P never come from here
        assert d["g1"] == 100
        assert d["g2"] == -50
        assert d["t1"] == 300
        assert d["t2"] == 200
        assert d["pv_total"] == 1043
        # Dc/Battery paths are NOT parsed by the system parser (shunt-only by design)
        assert "bv" not in d and "bc" not in d and "bp" not in d

    def test_missing_paths_default_zero(self):
        d = vp.parse_system_data_output('string "Dc/Pv/Power"\nvariant double 7.0')
        assert d["pv_total"] == 7.0
        assert d["g1"] == 0


class TestParseShuntDataOutput:
    SHUNT_TREE = (
        '            string "Dc/0/Voltage"\n'
        "            variant                double 52.9\n"
        '            string "Dc/0/Current"\n'
        "            variant                double -12.7\n"
        '            string "Dc/0/Power"\n'
        "            variant                double -672\n"
    )

    def test_full_shunt_tree(self):
        d = vp.parse_shunt_data_output(self.SHUNT_TREE)
        assert d["bv"] == 52.9
        assert d["bc"] == -12.7
        assert d["bp"] == -672

    def test_partial_tree_keeps_existing_values(self):
        """A partial tree must not wipe good values with zeros."""
        partial = 'string "Dc/0/Voltage"\nvariant double 52.9'
        d = vp.parse_shunt_data_output(partial)
        assert d == {"bv": 52.9}
        assert "bc" not in d and "bp" not in d

    def test_empty_output(self):
        assert vp.parse_shunt_data_output("") == {}


class TestBatterySocFromVoltage:
    """Parity with the HA "Battery %" template: linear 40-54.4V, clamp, round."""

    def test_endpoints(self):
        assert vp.calculate_battery_soc_from_voltage(vp.BATTERY_VOLTAGE_MIN) == 0.0
        assert vp.calculate_battery_soc_from_voltage(vp.BATTERY_VOLTAGE_MAX) == 100.0

    def test_clamps_outside_range(self):
        assert vp.calculate_battery_soc_from_voltage(30.0) == 0.0
        assert vp.calculate_battery_soc_from_voltage(60.0) == 100.0

    def test_midpoint_is_half(self):
        mid = (vp.BATTERY_VOLTAGE_MIN + vp.BATTERY_VOLTAGE_MAX) / 2
        assert vp.calculate_battery_soc_from_voltage(mid) == 50.0

    def test_monotonic_and_whole_numbers(self):
        prev = -1.0
        v = vp.BATTERY_VOLTAGE_MIN
        while v <= vp.BATTERY_VOLTAGE_MAX:
            soc = vp.calculate_battery_soc_from_voltage(v)
            assert prev <= soc <= 100
            assert float(soc).is_integer()
            prev = soc
            v += 0.1

    def test_garbage_voltage_returns_zero(self):
        assert vp.calculate_battery_soc_from_voltage("not-a-number") == 0.0  # type: ignore[arg-type]
