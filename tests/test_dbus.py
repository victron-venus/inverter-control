"""
Unit tests for VUESensorDBusClient
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control.dbus import VUESensorDBusClient


class TestVUESensorDBusClientInit(unittest.TestCase):
    """Test VUESensorDBusClient.__init__"""

    @patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus")
    def test_stores_mapping(self, mock_setup):
        mapping = {"garage": "Garage", "ev_charger": "EV Charger"}
        client = VUESensorDBusClient(mapping)
        assert client._vue_sensor_mapping == mapping

    @patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus")
    def test_initializes_empty_proxies(self, mock_setup):
        client = VUESensorDBusClient({})
        assert client._vue_proxies == {}
        assert client._vue_services == {}
        assert client._available is False


class TestKeyForCustomName(unittest.TestCase):
    """Test VUESensorDBusClient._key_for_custom_name()"""

    def setUp(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            self.mapping = {"garage": "Garage", "ev_charger": "EV Charger"}
            self.client = VUESensorDBusClient(self.mapping)

    def test_returns_matching_key(self):
        assert self.client._key_for_custom_name("Garage") == "garage"

    def test_returns_slug_for_unknown_name(self):
        result = self.client._key_for_custom_name("Kitchen Fridge Side")
        assert result == "kitchen_fridge_side"

    def test_slug_removes_special_characters(self):
        result = self.client._key_for_custom_name("Living Room (Main)")
        assert result == "living_room_main"

    def test_empty_name_returns_acload(self):
        result = self.client._key_for_custom_name("")
        assert result == "acload"

    def test_slug_lowercases(self):
        result = self.client._key_for_custom_name("FURNACE")
        assert result == "furnace"


class TestSetupDbusSend(unittest.TestCase):
    """Test _setup_dbus_send fallback discovery"""

    def test_discovers_services_from_dbus_send(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._connect_dbus"):
            client = VUESensorDBusClient({"garage": "Garage", "fridge": "Fridge"})

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            list_names_result = MagicMock()
            list_names_result.returncode = 0
            list_names_result.stdout = (
                'array of strings [\n'
                '  string "com.victronenergy.acload.ttyACM0"\n'
                '  string "com.victronenergy.acload.ttyACM1"\n'
                ']\n'
            )
            garage_name = MagicMock()
            garage_name.returncode = 0
            garage_name.stdout = '   string "Garage"\n'
            fridge_name = MagicMock()
            fridge_name.returncode = 0
            fridge_name.stdout = '   string "Fridge"\n'

            mock_run.side_effect = [list_names_result, garage_name, fridge_name]

            client._vue_proxies = {}
            client._setup_dbus_send()

        assert "garage" in client._vue_services
        assert "fridge" in client._vue_services
        assert client._vue_services["garage"] == "com.victronenergy.acload.ttyACM0"
        assert client._vue_services["fridge"] == "com.victronenergy.acload.ttyACM1"

    def test_no_services_when_dbus_send_fails(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._connect_dbus"):
            client = VUESensorDBusClient({"garage": "Garage"})

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("dbus-send", 3)

            client._vue_proxies = {}
            client._setup_dbus_send()

        assert client._vue_services == {}

    def test_no_services_when_returncode_nonzero(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._connect_dbus"):
            client = VUESensorDBusClient({"garage": "Garage"})

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            mock_run.return_value = result

            client._vue_proxies = {}
            client._setup_dbus_send()

        assert client._vue_services == {}


class TestUpdateAll(unittest.TestCase):
    """Test VUESensorDBusClient.update_all()"""

    def test_updates_from_proxies(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({"garage": "Garage"})
            client._available = True

        mock_props = MagicMock()
        mock_power = MagicMock()
        mock_power.value = 1500.0
        mock_props.Get.return_value = mock_power
        client._vue_proxies = {"garage": mock_props}

        vue_sensors = {"garage": 0}
        client.update_all(vue_sensors)

        assert vue_sensors["garage"] == 1500.0

    def test_updates_from_dbus_send_services(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({"garage": "Garage"})
            client._available = True

        client._vue_proxies = {}
        client._vue_services = {"garage": "com.victronenergy.acload.ttyACM0"}

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 0
            result.stdout = '   variant    double 2500.0\n'
            mock_run.return_value = result

            vue_sensors = {"garage": 0}
            client.update_all(vue_sensors)

        assert vue_sensors["garage"] == 2500.0

    def test_no_update_when_not_available(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({})
            client._available = False

        vue_sensors = {"garage": 0}
        client.update_all(vue_sensors)

        assert vue_sensors["garage"] == 0

    def test_handles_proxy_exception(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({"garage": "Garage"})
            client._available = True

        mock_props = MagicMock()
        mock_props.Get.side_effect = Exception("D-Bus error")
        client._vue_proxies = {"garage": mock_props}

        vue_sensors = {"garage": 0}
        client.update_all(vue_sensors)

        assert vue_sensors["garage"] == 0


class TestDbusSendFallback(unittest.TestCase):
    """Test _get_custom_name_dbus_send"""

    def test_returns_custom_name(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({})

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 0
            result.stdout = '   string "Garage"\n'
            mock_run.return_value = result

            name = client._get_custom_name_dbus_send("com.victronenergy.acload.ttyACM0")

        assert name == "Garage"

    def test_returns_none_on_failure(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({})

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            mock_run.return_value = result

            name = client._get_custom_name_dbus_send("com.victronenergy.acload.ttyACM0")

        assert name is None

    def test_returns_none_on_timeout(self):
        with patch("inverter_control.dbus.VUESensorDBusClient._setup_dbus"):
            client = VUESensorDBusClient({})

        with patch("inverter_control.dbus.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("dbus-send", 2)

            name = client._get_custom_name_dbus_send("com.victronenergy.acload.ttyACM0")

        assert name is None


if __name__ == "__main__":
    unittest.main()
