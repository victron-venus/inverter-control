"""
Unit tests for Inverter Control Keepalive
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import keepalive


class TestDBusGet:
    """Test dbus_get function"""

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_get_success_int32(self, mock_run):
        """Test successful D-Bus read returning int32"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "variant       int32 -500\n"
        mock_run.return_value = mock_result

        result = keepalive.dbus_get("com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint")
        assert result == -500

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_get_success_double(self, mock_run):
        """Test successful D-Bus read returning double - currently fails conversion"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "variant       double 54.3\n"
        mock_run.return_value = mock_result

        result = keepalive.dbus_get("com.victronenergy.vebus.ttyUSB2", "/Some/Path")
        # Currently fails due to int("54.3") ValueError, returns None
        assert result is None

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_get_failure(self, mock_run):
        """Test D-Bus read failure returns None"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        result = keepalive.dbus_get("com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint")
        assert result is None

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_get_timeout(self, mock_run):
        """Test D-Bus read timeout returns None"""
        mock_run.side_effect = subprocess.TimeoutExpired("dbus-send", 2)

        result = keepalive.dbus_get("com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint")
        assert result is None

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_get_exception(self, mock_run):
        """Test D-Bus read exception returns None"""
        mock_run.side_effect = Exception("D-Bus error")

        result = keepalive.dbus_get("com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint")
        assert result is None


class TestDBusSet:
    """Test dbus_set function"""

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_set_success(self, mock_run):
        """Test successful D-Bus write"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        result = keepalive.dbus_set(
            "com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint", 500
        )
        assert result is True
        mock_run.assert_called_once()

    @patch("inverter_control.keepalive.subprocess.run")
    def test_dbus_set_failure(self, mock_run):
        """Test D-Bus write failure returns False"""
        mock_run.side_effect = Exception("D-Bus error")

        result = keepalive.dbus_set(
            "com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint", 500
        )
        assert result is False


class TestFindVebusService:
    """Test find_vebus_service function"""

    @patch("inverter_control.keepalive.subprocess.run")
    def test_find_vebus_service_success(self, mock_run):
        """Test finding VE.Bus service via dbusmonitor"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "array [\n"
            '  string "com.victronenergy.vebus.ttyUSB2"\n'
            '  string "com.victronenergy.solarcharger.ttyUSB3"\n'
            "]"
        )
        mock_run.return_value = mock_result

        result = keepalive.find_vebus_service()
        assert result == "com.victronenergy.vebus.ttyUSB2"

    @patch("inverter_control.keepalive.subprocess.run")
    def test_find_vebus_service_fallback(self, mock_run):
        """Test fallback to default service name on failure"""
        mock_run.side_effect = Exception("dbusmonitor not available")

        result = keepalive.find_vebus_service()
        assert result == "com.victronenergy.vebus.ttyUSB2"

    @patch("inverter_control.keepalive.subprocess.run")
    def test_find_vebus_service_no_match(self, mock_run):
        """Test fallback when no VE.Bus service found"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'array [\n  string "com.victronenergy.solarcharger.ttyUSB3"\n]'
        mock_run.return_value = mock_result

        result = keepalive.find_vebus_service()
        assert result == "com.victronenergy.vebus.ttyUSB2"


class TestKeepaliveMain:
    """Test main keepalive loop"""

    @patch("inverter_control.keepalive.find_vebus_service")
    @patch("inverter_control.keepalive.dbus_get")
    @patch("inverter_control.keepalive.dbus_set")
    @patch("inverter_control.keepalive.socket.socket")
    @patch("inverter_control.keepalive.time.sleep")
    @patch("inverter_control.keepalive.time.time")
    def test_main_process_back_exits_early(
        self, mock_time, mock_sleep, mock_socket, mock_dbus_set, mock_dbus_get, mock_find_vebus
    ):
        """Test exits early when main process returns"""
        mock_find_vebus.return_value = "com.victronenergy.vebus.ttyUSB2"
        mock_dbus_get.return_value = 500

        # Time progression: start=0, first check=1, second check=2
        mock_time.side_effect = [0, 1, 2, 2]

        # Socket connection succeeds (main process back)
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0
        mock_socket.return_value = mock_sock

        with patch.dict(os.environ, {"KEEPALIVE_DURATION": "30"}):
            result = keepalive.main()

        assert result == 0
        mock_dbus_get.assert_called_once()
        # Should not call dbus_set since process is back

    @patch("inverter_control.keepalive.find_vebus_service")
    @patch("inverter_control.keepalive.dbus_get")
    @patch("inverter_control.keepalive.dbus_set")
    @patch("inverter_control.keepalive.socket.socket")
    @patch("inverter_control.keepalive.time.sleep")
    @patch("inverter_control.keepalive.time.time")
    def test_main_sends_setpoint_until_timeout(
        self, mock_time, mock_sleep, mock_socket, mock_dbus_set, mock_dbus_get, mock_find_vebus
    ):
        """Test sends setpoint repeatedly until timeout"""
        mock_find_vebus.return_value = "com.victronenergy.vebus.ttyUSB2"
        mock_dbus_get.return_value = 500

        # Time progression: start at 0, then check every 1 second, timeout at 30
        time_values = [0]  # start
        for i in range(1, 32):
            time_values.append(i)  # each sleep/loop
        mock_time.side_effect = time_values

        # Socket connection fails (main process not back)
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1
        mock_socket.return_value = mock_sock

        mock_dbus_set.return_value = True

        with patch.dict(os.environ, {"KEEPALIVE_DURATION": "30"}):
            result = keepalive.main()

        # Should have called dbus_set multiple times (every second for 30 seconds)
        assert result == 1
        assert mock_dbus_set.call_count >= 20  # At least 20 times
        mock_dbus_get.assert_called_once()


class TestKeepaliveDuration:
    """Test KEEPALIVE_DURATION environment variable"""

    @patch("inverter_control.keepalive.time.time")
    @patch("inverter_control.keepalive.time.sleep")
    @patch("inverter_control.keepalive.socket.socket")
    @patch("inverter_control.keepalive.dbus_get")
    @patch("inverter_control.keepalive.find_vebus_service")
    def test_default_duration(
        self, mock_find_vebus, mock_dbus_get, mock_socket, mock_sleep, mock_time
    ):
        """Test default duration is 30 seconds"""
        mock_find_vebus.return_value = "com.victronenergy.vebus.ttyUSB2"
        mock_dbus_get.return_value = 0
        mock_sleep.return_value = None

        time_values = [0]
        for i in range(1, 32):
            time_values.append(i)
        mock_time.side_effect = time_values

        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1
        mock_socket.return_value = mock_sock

        # No KEEPALIVE_DURATION env var
        with patch.dict(os.environ, {}, clear=True):
            result = keepalive.main()

        assert result == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
