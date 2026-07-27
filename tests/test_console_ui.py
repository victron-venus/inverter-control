"""
Unit tests for Console UI
"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import console_ui


class TestConsoleUI:
    """Test console UI formatting"""

    def setup_method(self):
        """Create mock HA and Victron clients"""
        self.mock_ha = MagicMock()
        self.mock_victron = MagicMock()

        # Setup HA mock returns
        self.mock_ha.get_sensor.side_effect = lambda key, default=0: {
            "net_usage": 100,
            "home_total": 500,
            "compensation_voltage": 48,
            "car_soc": 80,
            "washer_time": "01:30:00",
            "dryer_time": "00:45:00",
            "dishwasher_duration": "02:15:00",
            "produced_today": 20,
            "produced_dollars": 5.50,
            "grid_kwh_today": 10,
            "battery_in_today": 15,
            "battery_out_today": 12,
            "garage": 50,
            "fridge": 100,
            "furnace": 0,
            "stove": 0,
            "microwave": 0,
            "kitchen_fridge_side": 0,
            "dishwasher": 0,
            "lost": 0,
            "water_level": 150,
        }.get(key, default)

        self.mock_ha.get_boolean.side_effect = lambda key: False
        self.mock_ha.get_binary_sensor.side_effect = lambda key: False

        # Setup mock returns
        self.mock_ha.water_valve_on = False
        self.mock_ha.pump_switch_on = False
        self.mock_ha.home_recliner_on = False
        self.mock_ha.home_garage_on = False

        self.mock_victron.get_inverter_state.return_value = (9, "Inverting")

        self.ui = console_ui.ConsoleUI(self.mock_ha, self.mock_victron)

    def test_format_line_basic(self):
        """Test basic line formatting"""
        sys_data = {
            "g1": 100,
            "g2": 50,
            "gt": 150,
            "t1": 200,
            "t2": 100,
            "tt": 300,
            "bv": 54.50,
        }

        line = self.ui.format_line(sys_data, 500, 400, "[TEST]", 140.0)

        assert "TEST" in line
        assert "500" in line
        assert "400" in line
        assert "54.50" in line
        assert "150" in line
        assert "300" in line

    def test_format_line_grid_section(self):
        """Test grid section formatting"""
        sys_data = {
            "g1": 100,
            "g2": 50,
            "gt": 150,
            "t1": 200,
            "t2": 100,
            "tt": 300,
            "bv": 50.0,
        }

        # With significant smoothing difference
        line = self.ui.format_line(sys_data, 0, 0, "", 0.0)
        assert "g:150[0]" in line  # gt - filtered_gt > 10

        # Without significant smoothing difference
        line2 = self.ui.format_line(sys_data, 0, 0, "", 145.0)
        # Difference is 5, less than 10, no smooth_str
        assert "[0]" not in line2

    def test_format_battery_section(self):
        """Test battery section formatting"""
        sys_data = {
            "bp": 1000,
            "battery_socs": [85.5, 90.0],
        }

        battery_section = self.ui._fmt_battery_section(sys_data)

        assert "Inverting" in battery_section  # inverter state
        assert "1000W" in battery_section
        assert "48%" in battery_section  # compensation voltage
        assert "85%" in battery_section  # soc1 truncated (int(85.5) = 85)
        assert "90%" in battery_section  # soc2 truncated

    def test_format_solar_section(self):
        """Test solar section formatting"""
        # With solar data
        sys_data = {
            "mppt_data": {"mppt0": {"w": 500.0, "a": 10.5}, "mppt1": {"w": 300.0, "a": 6.2}},
            "tasmota_powers": [200.0, 150.0],
        }

        solar_section = self.ui._fmt_solar_section(sys_data)
        assert "1150" in solar_section  # 500+300+200+150
        assert "10.5A" in solar_section
        assert "6.2A" in solar_section
        assert "200" in solar_section
        assert "150" in solar_section

        # Without solar data
        sys_data2 = {"mppt_data": {}, "tasmota_powers": []}
        solar_section2 = self.ui._fmt_solar_section(sys_data2)
        assert "0" in solar_section2

    def test_format_loads_section(self):
        """Test loads section formatting"""
        # Enable HA loads
        with patch("inverter_control.console_ui.ENABLE_HA_LOADS", True):
            loads_section = self.ui._fmt_loads_section()
            # Check some expected loads appear with values > 19
            # garage=50, fridge=100 should appear
            assert "50g" in loads_section
            assert "100f" in loads_section

        # Disable HA loads
        with patch("inverter_control.console_ui.ENABLE_HA_LOADS", False):
            loads_section = self.ui._fmt_loads_section()
            assert loads_section == ""

    def test_format_extra_info(self):
        """Test extra info formatting"""
        # Note: ENABLE_WASHER and ENABLE_DRYER are not imported in console_ui.py
        # so washer_time and dryer_time are always included
        with patch("inverter_control.console_ui.ENABLE_WATER", True):
            with patch("inverter_control.console_ui.ENABLE_EV", True):
                with patch("inverter_control.console_ui.ENABLE_DISHWASHER", False):
                    extra = self.ui._fmt_extra_info()
                    # Water level
                    assert "cm" in extra
                    # Car SOC
                    assert "80%" in extra

    def test_fmt_appliance_time(self):
        """Test appliance time formatting"""
        assert console_ui.fmt_appliance_time("01:30:00") == "1:30"
        assert console_ui.fmt_appliance_time("00:45:00") == "45"
        assert console_ui.fmt_appliance_time("0") == ""
        assert console_ui.fmt_appliance_time(None) == ""
        assert console_ui.fmt_appliance_time("") == ""

    def test_update_terminal_title(self):
        """Test terminal title update"""
        # Should not print until 10th call
        for i in range(9):
            self.ui.update_terminal_title()

        # 10th call should print
        with patch("builtins.print") as mock_print:
            self.ui.update_terminal_title()
            mock_print.assert_called_once()
            # Check title format
            call_args = mock_print.call_args[0][0]
            assert "kW" in call_args
            assert "kWh" in call_args


class TestConsoleUIEdgeCases:
    """Test edge cases and error handling"""

    def setup_method(self):
        self.mock_ha = MagicMock()
        self.mock_victron = MagicMock()
        self.mock_ha.get_sensor.return_value = 0
        self.mock_ha.get_boolean.return_value = False
        self.mock_ha.get_binary_sensor.return_value = False
        self.mock_ha.water_valve_on = False
        self.mock_ha.pump_switch_on = False
        self.mock_victron.get_inverter_state.return_value = (0, "Of")

        self.ui = console_ui.ConsoleUI(self.mock_ha, self.mock_victron)

    def test_format_line_missing_keys(self):
        """Test format_line handles missing keys gracefully"""
        # Minimal sys_data
        sys_data = {
            "g1": 0,
            "g2": 0,
            "gt": 0,
            "t1": 0,
            "t2": 0,
            "tt": 0,
            "bv": 0,
        }
        line = self.ui.format_line(sys_data, 0, 0, "", 0.0)
        assert "g:0" in line

    def test_solar_section_zero_current(self):
        """Test solar formatting with zero current"""
        sys_data = {
            "mppt_data": {"mppt0": {"w": 100.0, "a": 0.01}},  # Very small current
            "tasmota_powers": [],
        }
        solar = self.ui._fmt_solar_section(sys_data)
        assert "0A" in solar  # Shows 0A when under 0.05A


if __name__ == "__main__":
    pytest.main([__file__, "-v"])