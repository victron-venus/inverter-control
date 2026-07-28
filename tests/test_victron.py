"""
Unit tests for Victron D-Bus Interface
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron


class TestVictronDBus:
    """Test VictronDBus class"""

    def setup_method(self):
        """Reset singleton"""
        victron._victron = None

    def teardown_method(self):
        """Reset singleton"""
        victron._victron = None

    @patch("inverter_control.victron.subprocess.run")
    def test_init_discover_services(self, mock_run):
        """Test service discovery on init"""
        mock_result = MagicMock()
        mock_result.stdout = (
            "com.victronenergy.vebus.ttyUSB2\n"
            "com.victronenergy.solarcharger.ttyUSB1\n"
            "com.victronenergy.solarcharger.ttyUSB3\n"
        )
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        v = victron.VictronDBus()

        assert v.vebus_service == "com.victronenergy.vebus.ttyUSB2"
        assert len(v.mppt_services) == 2
        assert "com.victronenergy.solarcharger.ttyUSB1" in v.mppt_services

    @patch("inverter_control.victron.subprocess.run")
    def test_init_no_vebus(self, mock_run):
        """Test when no VE.Bus service found"""
        mock_result = MagicMock()
        mock_result.stdout = "com.victronenergy.solarcharger.ttyUSB1\n"
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        v = victron.VictronDBus()
        assert v.vebus_service is None
        assert len(v.mppt_services) == 1

    @patch("inverter_control.victron.subprocess.run")
    def test_init_discovery_error(self, mock_run):
        """Test discovery handles errors"""
        mock_run.side_effect = Exception("D-Bus error")

        v = victron.VictronDBus()
        assert v.vebus_service is None
        assert v.mppt_services == []

    def test_dbus_get_success(self):
        """Test successful D-Bus get"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "variant       int32 -500\n"
            mock_run.return_value = mock_result

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            result = v._dbus_get("test.service", "/Some/Path")

        assert result == "-500"

    def test_dbus_get_timeout(self):
        """Test D-Bus get timeout"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("dbus-send", 0.3)

            victron._victron = None
            v = victron.VictronDBus()
            result = v._dbus_get("test.service", "/Some/Path")

        assert result is None

    def test_dbus_get_failure(self):
        """Test D-Bus get failure"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_run.return_value = mock_result

            victron._victron = None
            v = victron.VictronDBus()
            result = v._dbus_get("test.service", "/Some/Path")

        assert result is None

    def test_dbus_set_success(self):
        """Test successful D-Bus set"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_run.return_value = mock_result

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            result = v._dbus_set("test.service", "/Path", 100)

        assert result is True

    def test_dbus_set_failure(self):
        """Test D-Bus set failure"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_run.side_effect = Exception("D-Bus error")

            victron._victron = None
            v = victron.VictronDBus()
            result = v._dbus_set("test.service", "/Path", 100)

        assert result is False

    def test_get_system_data(self):
        """Test getting system data"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = (
                "Ac/Grid/L1/Power\nvariant       int32 500\n"
                "Ac/Grid/L2/Power\nvariant       int32 -300\n"
                "Ac/Consumption/L1/Power\nvariant       int32 200\n"
                "Ac/Consumption/L2/Power\nvariant       int32 100\n"
                "Dc/Battery/Voltage\nvariant       double 52.4\n"
                "Dc/Battery/Current\nvariant       double -5.2\n"
                "Dc/Battery/Power\nvariant       int32 -270\n"
                "Dc/Pv/Power\nvariant       int32 1500\n"
            )
            mock_run.return_value = mock_result

            victron._victron = None
            v = victron.VictronDBus()
            data = v.get_system_data()

        assert data["g1"] == 500
        assert data["g2"] == -300
        assert data["gt"] == 200
        assert data["t1"] == 200
        assert data["t2"] == 100
        assert data["tt"] == 300
        assert data["bv"] == 52.4
        assert data["bc"] == -5.2
        assert data["bp"] == -270
        assert data["pv_total"] == 1500

    def test_get_system_data_empty(self):
        """Test getting system data with empty result"""
        with patch("inverter_control.victron.subprocess.run") as mock_run:
            mock_run.return_value = None

            victron._victron = None
            v = victron.VictronDBus()
            data = v.get_system_data()

        assert data["gt"] == 0
        assert data["tt"] == 0

    def test_get_inverter_state(self):
        """Test getting inverter state"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "9"

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            code, name = v.get_inverter_state()

        assert code == 9
        assert name == "Inverting"

    def test_get_inverter_state_unknown(self):
        """Test getting unknown inverter state"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "999"

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            code, name = v.get_inverter_state()

        assert code == 999
        assert "? (999)" in name

    def test_get_inverter_state_no_service(self):
        """Test getting inverter state with no service"""
        victron._victron = None
        v = victron.VictronDBus()
        v._vebus_service = None
        code, name = v.get_inverter_state()

        assert code == 0
        assert name == "Unknown"

    def test_get_inverter_power(self):
        """Test getting inverter power"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "1234"

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            power = v.get_inverter_power()

        assert power == 1234

    def test_get_inverter_power_failure(self):
        """Test getting inverter power failure"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = None

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            power = v.get_inverter_power()

        assert power == 0

    def test_get_ac_in_power(self):
        """Test getting AC input power"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "500"

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            power = v.get_ac_in_power()

        assert power == 500

    def test_set_grid_setpoint(self):
        """Test setting grid setpoint"""
        with patch("inverter_control.victron.VictronDBus._dbus_set") as mock_set:
            mock_set.return_value = True

            victron._victron = None
            v = victron.VictronDBus()
            v._vebus_service = "test.service"
            result = v.set_grid_setpoint(500)

        assert result is True
        mock_set.assert_called_once_with("test.service", "/Hub4/L1/AcPowerSetpoint", 500, "int16")

    def test_get_mppt_data(self):
        """Test getting MPPT data"""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            m.returncode = 0
            if "/Yield/Power" in args[0]:
                m.stdout = "variant       double 500.0\n"
            elif "/Dc/0/Current" in args[0]:
                m.stdout = "variant       double 10.5\n"
            else:
                m.stdout = ""
            return m

        with patch("inverter_control.victron.subprocess.run", side_effect=side_effect):
            victron._victron = None
            v = victron.VictronDBus()
            v._mppt_services = ["service1", "service2"]
            data = v.get_mppt_data()

        assert "mppt0" in data
        assert "mppt1" in data
        assert data["mppt0"]["w"] == 500.0
        assert data["mppt0"]["a"] == 10.5

    def test_get_tasmota_pv_power(self):
        """Test getting Tasmota PV power"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["1200", "800"]

            victron._victron = None
            v = victron.VictronDBus()
            powers = v.get_tasmota_pv_power()

        assert powers == [1200.0, 800.0]

    def test_get_battery_soc(self):
        """Test getting battery SoC"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "85.5"

            victron._victron = None
            v = victron.VictronDBus()
            soc = v.get_battery_soc()

        assert soc == 85.5

    def test_get_battery_chain_socs(self):
        """Test getting battery chain SoCs"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["75.0", "80.0"]

            victron._victron = None
            v = victron.VictronDBus()
            socs = v.get_battery_chain_socs()

        assert socs == [75.0, 80.0]

    def test_get_ess_mode_external(self):
        """Test getting ESS mode - external control"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["3", "0"]

            victron._victron = None
            v = victron.VictronDBus()
            mode = v.get_ess_mode()

        assert mode["hub4_mode"] == 3
        assert mode["mode_name"] == "External control"
        assert mode["is_external"] is True

    def test_get_ess_mode_optimized(self):
        """Test getting ESS mode - optimized without BatteryLife"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["1", "0"]

            victron._victron = None
            v = victron.VictronDBus()
            mode = v.get_ess_mode()

        assert mode["hub4_mode"] == 1
        assert mode["mode_name"] == "Optimized without BatteryLife"
        assert mode["is_external"] is False

    def test_get_ess_mode_keep_charged(self):
        """Test getting ESS mode - keep batteries charged"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["1", "9"]

            victron._victron = None
            v = victron.VictronDBus()
            mode = v.get_ess_mode()

        assert mode["mode_name"] == "Keep batteries charged"

    def test_set_ess_mode_external(self):
        """Test setting ESS mode to external"""
        with patch("inverter_control.victron.VictronDBus._dbus_set") as mock_set:
            mock_set.return_value = True

            victron._victron = None
            v = victron.VictronDBus()
            result = v.set_ess_mode(external=True)

        assert result is True
        mock_set.assert_called_once_with(
            "com.victronenergy.settings",
            "/Settings/CGwacs/Hub4Mode",
            3,
            "int32",
        )

    def test_set_ess_mode_optimized(self):
        """Test setting ESS mode to optimized"""
        with patch("inverter_control.victron.VictronDBus._dbus_set") as mock_set:
            mock_set.return_value = True

            victron._victron = None
            v = victron.VictronDBus()
            result = v.set_ess_mode(external=False)

        assert result is True
        assert mock_set.call_count == 2

    def test_get_all_batteries(self):
        """Test getting all batteries"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:

            def side_effect(service, path):
                values = {
                    "com.victronenergy.battery.dbus-mqtt-chain1": {
                        "/Dc/0/Current": "5.0",
                        "/Dc/0/Voltage": "52.0",
                        "/Dc/0/Power": "260.0",
                        "/Soc": "80.0",
                        "/TimeToGo": "3600",
                    },
                    "com.victronenergy.battery.dbus-mqtt-chain2": {
                        "/Dc/0/Current": "-3.0",
                        "/Dc/0/Voltage": "51.0",
                        "/Dc/0/Power": "-153.0",
                        "/Soc": "75.0",
                        "/TimeToGo": "7200",
                    },
                }
                return values.get(service, {}).get(path, "")

            mock_get.side_effect = side_effect
            with patch("inverter_control.victron.VictronDBus._dbus_get", side_effect=side_effect):
                victron._victron = None
                v = victron.VictronDBus()
                batteries = v.get_all_batteries()

        assert len(batteries) == 3
        assert batteries[0]["name"] == "JBD Chain 1"
        assert batteries[0]["current"] == 5.0
        assert batteries[0]["state"] == "Charging"
        assert batteries[1]["name"] == "JBD Chain 2"
        assert batteries[1]["state"] == "Discharging"

    def test_get_mppt_chargers(self):
        """Test getting MPPT chargers"""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            m.returncode = 0
            if "/Pv/V" in args[0]:
                m.stdout = "variant       double 100.0\n"
            elif "/Dc/0/Current" in args[0]:
                m.stdout = "variant       double 5.0\n"
            elif "/Yield/Power" in args[0]:
                m.stdout = "variant       double 500.0\n"
            else:
                m.stdout = ""
            return m

        with patch("inverter_control.victron.subprocess.run", side_effect=side_effect):
            victron._victron = None
            v = victron.VictronDBus()
            v._mppt_services = ["service1", "service2"]
            chargers = v.get_mppt_chargers()

        assert len(chargers) == 2
        assert chargers[0]["pv_voltage"] == 100.0
        assert chargers[0]["current"] == 5.0
        assert chargers[0]["power"] == 500.0

    def test_read_chain_cell_voltages(self):
        """Test reading chain cell voltages"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["3.45", "3.46", "3.44", "3.47", None]

            victron._victron = None
            v = victron.VictronDBus()
            voltages = v._read_chain_cell_voltages("test.service", 0)

        assert len(voltages) == 4
        assert voltages[0][0] == 3.45
        assert voltages[1][0] == 3.46

    def test_read_chain_cell_voltages_invalid_value(self):
        """Test reading chain cell voltages with invalid value"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["3.45", "invalid", "3.44", None]

            victron._victron = None
            v = victron.VictronDBus()
            voltages = v._read_chain_cell_voltages("test.service", 0)

        assert len(voltages) == 2
        assert voltages[0][0] == 3.45
        assert voltages[1][0] == 3.44

    def test_read_chain_cell_voltages_zero_volt(self):
        """Test reading chain cell voltages with zero volt (skipped)"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.side_effect = ["3.45", "0", "3.44", None]

            victron._victron = None
            v = victron.VictronDBus()
            voltages = v._read_chain_cell_voltages("test.service", 0)

        assert len(voltages) == 2
        assert voltages[0][0] == 3.45
        assert voltages[1][0] == 3.44

    def test_read_chain_cell_temps(self):
        """Test reading chain cell temperatures"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            # Return temps for cells 1, 3, 5 (sparse)
            def side_effect(service, path):
                if "/Cell/1/Temperature" in path:
                    return "25.5"
                elif "/Cell/3/Temperature" in path:
                    return "26.0"
                elif "/Cell/5/Temperature" in path:
                    return "24.8"
                return None

            mock_get.side_effect = side_effect

            victron._victron = None
            v = victron.VictronDBus()
            temps = v._read_chain_cell_temps("test.service")

        assert len(temps) == 3
        assert temps == [25.5, 26.0, 24.8]

    def test_read_chain_cell_temps_invalid(self):
        """Test reading chain cell temperatures with invalid values"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            # 16 values: invalid (out of range), valid, invalid format, rest None
            side_effects = ["150", "25.5", "invalid"] + [None] * 13
            mock_get.side_effect = side_effects

            victron._victron = None
            v = victron.VictronDBus()
            temps = v._read_chain_cell_temps("test.service")

            # Only the valid temp (25.5) should be included
            assert temps == [25.5]

    def test_read_chain_soc(self):
        """Test reading chain SoC"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "85.5"

            victron._victron = None
            v = victron.VictronDBus()
            soc = v._read_chain_soc("test.service")

        assert soc == 85.5

    def test_read_chain_soc_none(self):
        """Test reading chain SoC when None"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = None

            victron._victron = None
            v = victron.VictronDBus()
            soc = v._read_chain_soc("test.service")

        assert soc is None

    def test_read_chain_soc_invalid(self):
        """Test reading chain SoC with invalid value"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "invalid"

            victron._victron = None
            v = victron.VictronDBus()
            soc = v._read_chain_soc("test.service")

        assert soc is None

    def test_read_chain_allow_flag_true(self):
        """Test reading chain allow flag - true"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "1"

            victron._victron = None
            v = victron.VictronDBus()
            result = v._read_chain_allow_flag("test.service", "/Info/AllowCharge")

        assert result is True

    def test_read_chain_allow_flag_false(self):
        """Test reading chain allow flag - false"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "0"

            victron._victron = None
            v = victron.VictronDBus()
            result = v._read_chain_allow_flag("test.service", "/Info/AllowDischarge")

        assert result is False

    def test_read_chain_allow_flag_none(self):
        """Test reading chain allow flag - None"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = None

            victron._victron = None
            v = victron.VictronDBus()
            result = v._read_chain_allow_flag("test.service", "/Info/AllowCharge")

        assert result is None

    def test_read_chain_allow_flag_invalid(self):
        """Test reading chain allow flag - invalid value"""
        with patch("inverter_control.victron.VictronDBus._dbus_get") as mock_get:
            mock_get.return_value = "invalid"

            victron._victron = None
            v = victron.VictronDBus()
            result = v._read_chain_allow_flag("test.service", "/Info/AllowCharge")

        assert result is None

    def test_get_battery_cell_data(self):
        """Test getting battery cell data for DVCC"""
        with patch("inverter_control.victron.VictronDBus._read_chain_cell_voltages") as mock_voltages:
            with patch("inverter_control.victron.VictronDBus._read_chain_cell_temps") as mock_temps:
                with patch("inverter_control.victron.VictronDBus._read_chain_soc") as mock_soc:
                    with patch("inverter_control.victron.VictronDBus._read_chain_allow_flag") as mock_allow:
                        # Chain 1: 4 cells, temps, soc=80, allow_charge=1, allow_discharge=1
                        mock_voltages.side_effect = [
                            [(3.45, 0), (3.46, 1), (3.44, 2), (3.47, 3)],
                            [(3.50, 4), (3.48, 5)],  # Chain 2: 2 cells
                        ]
                        mock_temps.side_effect = [[25.5, 26.0], [24.8]]
                        mock_soc.side_effect = [80.0, 75.0]
                        mock_allow.side_effect = [True, True, True, True]  # charge1, discharge1, charge2, discharge2

                        victron._victron = None
                        v = victron.VictronDBus()
                        result = v.get_battery_cell_data()

        assert result["max_cell"] == 3.50
        assert result["max_cell_id"] == 4
        assert result["min_cell"] == 3.44
        assert result["min_cell_id"] == 2
        assert result["max_temp"] == 26.0
        assert result["min_temp"] == 24.8
        assert result["soc"] == 77.5
        assert result["allow_charge"] is True
        assert result["allow_discharge"] is True

    def test_get_battery_cell_data_allow_false(self):
        """Test getting battery cell data with allow flags false"""
        with patch("inverter_control.victron.VictronDBus._read_chain_cell_voltages") as mock_voltages:
            with patch("inverter_control.victron.VictronDBus._read_chain_cell_temps") as mock_temps:
                with patch("inverter_control.victron.VictronDBus._read_chain_soc") as mock_soc:
                    with patch("inverter_control.victron.VictronDBus._read_chain_allow_flag") as mock_allow:
                        mock_voltages.side_effect = [
                            [(3.45, 0), (3.46, 1)],
                            [(3.50, 2), (3.48, 3)],
                        ]
                        mock_temps.side_effect = [[25.5], [24.8]]
                        mock_soc.side_effect = [80.0, 75.0]
                        mock_allow.side_effect = [False, True, True, False]  # charge1=F, discharge1=T, charge2=T, discharge2=F

                        victron._victron = None
                        v = victron.VictronDBus()
                        result = v.get_battery_cell_data()

        assert result["allow_charge"] is False
        assert result["allow_discharge"] is False

    def test_get_battery_cell_data_no_cells(self):
        """Test getting battery cell data when no cells found"""
        with patch("inverter_control.victron.VictronDBus._read_chain_cell_voltages") as mock_voltages:
            with patch("inverter_control.victron.VictronDBus._read_chain_cell_temps") as mock_temps:
                with patch("inverter_control.victron.VictronDBus._read_chain_soc") as mock_soc:
                    with patch("inverter_control.victron.VictronDBus._read_chain_allow_flag") as mock_allow:
                        mock_voltages.side_effect = [[], []]
                        mock_temps.side_effect = [[], []]
                        mock_soc.side_effect = [None, None]
                        mock_allow.side_effect = [None, None, None, None]

                        victron._victron = None
                        v = victron.VictronDBus()
                        result = v.get_battery_cell_data()

        assert result["max_cell"] is None
        assert result["min_cell"] is None
        assert result["max_temp"] is None
        assert result["min_temp"] is None
        assert result["soc"] is None
        assert result["allow_charge"] is True
        assert result["allow_discharge"] is True


class TestGetVictron:
    """Test get_victron singleton"""

    def setup_method(self):
        victron._victron = None

    def teardown_method(self):
        victron._victron = None

    @patch("inverter_control.victron.subprocess.run")
    def test_get_victron_singleton(self, mock_run):
        """Test get_victron returns same instance"""
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        v1 = victron.get_victron()
        v2 = victron.get_victron()

        assert v1 is v2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
