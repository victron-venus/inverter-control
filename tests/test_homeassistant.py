"""
Unit tests for Home Assistant Client
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import homeassistant


class TestHomeAssistantClient:
    """Test HomeAssistantClient class"""

    def setup_method(self):
        """Setup mock config"""
        self.patches = [
            patch("inverter_control.homeassistant.HA_URL", "http://test:8123"),
            patch("inverter_control.homeassistant.HA_TOKEN", "test_token"),
            patch("inverter_control.homeassistant.HA_TIMEOUT", 2.0),
            patch("inverter_control.homeassistant.HA_POLL_INTERVAL", 1.5),
            patch(
                "inverter_control.homeassistant.HA_SENSORS",
                {"sensor1": "entity1", "sensor2": "entity2"},
            ),
            patch("inverter_control.homeassistant.HA_BOOLEANS", {"bool1": "entity_bool1"}),
            patch(
                "inverter_control.homeassistant.HA_BINARY_SENSORS", {"binary1": "entity_binary1"}
            ),
            patch("inverter_control.homeassistant.HA_DUMP_LOADS", ["load1", "load2"]),
            patch("inverter_control.homeassistant.HA_WATER_VALVE", "valve1"),
            patch("inverter_control.homeassistant.HA_PUMP_SWITCH", "pump1"),
            patch("inverter_control.homeassistant.VUE_SENSORS", {"vue1": "entity_vue1"}),
            patch("inverter_control.homeassistant.ENABLE_DISHWASHER", True),
            patch("inverter_control.homeassistant.ENABLE_WASHER", True),
            patch("inverter_control.homeassistant.ENABLE_DRYER", True),
            patch("inverter_control.homeassistant.ENABLE_WATER", True),
            patch("inverter_control.homeassistant.HA_WASHER_POWER", "washer_power"),
            patch("inverter_control.homeassistant.HA_DRYER_POWER", "dryer_power"),
            patch("inverter_control.homeassistant.HA_LAUNDRY_OUTLET", "laundry_outlet"),
        ]
        for p in self.patches:
            p.start()

        # Create client
        self.client = homeassistant.HomeAssistantClient()

    def teardown_method(self):
        """Cleanup"""
        for p in self.patches:
            p.stop()
        if hasattr(self, "client") and self.client._running:
            self.client.stop()

    def test_init(self):
        """Test initialization"""
        assert self.client._session is not None
        assert "Authorization" in self.client._session.headers
        assert self.client._session.headers["Authorization"] == "Bearer test_token"
        assert self.client._session.headers["Content-Type"] == "application/json"
        assert self.client._connected is False
        assert self.client._consecutive_failures == 0
        assert self.client._circuit_open is False

    def test_init_disabled_ha_token(self):
        """Test initialization with disabled HA token"""
        with patch("inverter_control.homeassistant.HA_TOKEN", "your_token_here"):
            client = homeassistant.HomeAssistantClient()
            # Should still be able to create client but ENABLE_HA will be False

    def test_parse_numeric(self):
        """Test numeric parsing"""
        assert self.client._parse_numeric("100") == 100
        assert self.client._parse_numeric("100.5") == 100.5
        assert self.client._parse_numeric("-50") == -50
        assert self.client._parse_numeric("unavailable") == 0
        assert self.client._parse_numeric("unknown") == 0
        assert self.client._parse_numeric(None) == 0
        assert self.client._parse_numeric("") == 0

    def test_parse_duration(self):
        """Test duration parsing"""
        assert self.client._parse_duration("01:30:00") == 90  # 1h 30m = 90m
        assert self.client._parse_duration("45:30") == 46  # 45m 30s = 46m (rounded)
        assert self.client._parse_duration("120") == 120  # numeric
        assert self.client._parse_duration("unavailable") == 0
        assert self.client._parse_duration(None) == 0

    @patch("inverter_control.homeassistant.requests.Session.get")
    def test_get_state_success(self, mock_get):
        """Test getting entity state"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"state": "on"}
        mock_get.return_value = mock_resp

        result = self.client._get_state("switch.test")
        assert result == "on"

    @patch("inverter_control.homeassistant.requests.Session.get")
    def test_get_state_not_found(self, mock_get):
        """Test getting entity state - not found"""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = self.client._get_state("switch.test")
        assert result is None

    @patch("inverter_control.homeassistant.requests.Session.get")
    def test_get_state_exception(self, mock_get):
        """Test getting entity state - exception"""
        import requests

        mock_get.side_effect = requests.exceptions.RequestException("Connection error")

        result = self.client._get_state("switch.test")
        assert result is None

    def test_build_template(self):
        """Test template building"""
        template = self.client._build_template()
        assert '"sensor1": "{{ states("entity1") }}"' in template
        assert '"sensor2": "{{ states("entity2") }}"' in template
        assert '"bool1": "{{ states("entity_bool1") }}"' in template
        assert '"binary1": "{{ states("entity_binary1") }}"' in template
        assert '"washer_power": "{{ states("washer_power") }}"' in template
        assert '"dryer_power": "{{ states("dryer_power") }}"' in template
        assert '"laundry_outlet": "{{ states("laundry_outlet") }}"' in template
        assert '"home_recliner": "{{ states(\'switch.recliner_recliner\') }}"' in template
        assert '"home_garage": "{{ states(\'switch.garage_opener_l\') }}"' in template

    def test_build_template_disabled_features(self):
        """Test template building with disabled features"""
        with patch("inverter_control.homeassistant.ENABLE_DISHWASHER", False):
            with patch("inverter_control.homeassistant.ENABLE_WASHER", False):
                with patch("inverter_control.homeassistant.ENABLE_DRYER", False):
                    with patch("inverter_control.homeassistant.ENABLE_WATER", False):
                        client = homeassistant.HomeAssistantClient()
                        template = client._build_template()
                        # Should not include disabled sensors
                        assert "dishwasher_duration" not in template
                        assert "dishwasher_running" not in template
                        assert "washer_time" not in template
                        assert "dryer_time" not in template
                        assert "water_level" not in template
                        assert "washer_power" not in template
                        assert "dryer_power" not in template
                        assert "laundry_outlet" not in template

    @patch("inverter_control.homeassistant.requests.Session.post")
    def test_fetch_template_data_success(self, mock_post):
        """Test fetching template data"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"sensor1": "100", "bool1": "on"}
        mock_post.return_value = mock_resp

        data = self.client._fetch_template_data()

        assert data == {"sensor1": "100", "bool1": "on"}
        mock_post.assert_called_once()

    @patch("inverter_control.homeassistant.requests.Session.post")
    def test_fetch_template_data_timeout(self, mock_post):
        """Test fetching template data - timeout"""
        import requests

        mock_post.side_effect = requests.exceptions.Timeout()

        with pytest.raises(homeassistant.HomeAssistantTimeoutError):
            self.client._fetch_template_data()

    @patch("inverter_control.homeassistant.requests.Session.post")
    def test_fetch_template_data_connection_error(self, mock_post):
        """Test fetching template data - connection error"""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError()

        with pytest.raises(homeassistant.HomeAssistantConnectionError):
            self.client._fetch_template_data()

    @patch("inverter_control.homeassistant.requests.Session.post")
    def test_fetch_template_data_api_error(self, mock_post):
        """Test fetching template data - API error"""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_post.return_value = mock_resp

        with pytest.raises(homeassistant.HomeAssistantAPIError):
            self.client._fetch_template_data()

    @patch("inverter_control.homeassistant.requests.Session.post")
    def test_fetch_template_data_invalid_response(self, mock_post):
        """Test fetching template data - invalid response"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = "not a dict"
        mock_post.return_value = mock_resp

        with pytest.raises(homeassistant.HomeAssistantResponseError):
            self.client._fetch_template_data()

    def test_parse_sensors(self):
        """Test sensor parsing"""
        data = {
            "sensor1": "100",
            "sensor2": "unavailable",
        }
        self.client._parse_sensors(data)
        assert self.client._sensors["sensor1"] == 100
        assert self.client._sensors["sensor2"] == 0  # default for unavailable

    def test_parse_boolean_sensors(self):
        """Test boolean sensor parsing"""
        data = {
            "bool1": "on",
            "binary1": "off",
        }
        self.client._parse_boolean_sensors(data)
        assert self.client._booleans["bool1"] is True
        assert self.client._binary_sensors["binary1"] is False

    def test_parse_switches(self):
        """Test switch parsing"""
        data = {
            "water_valve": "on",
            "pump_switch": "off",
            "washer_power": "on",
            "dryer_power": "off",
            "laundry_outlet": "on",
            "home_recliner": "on",
            "home_garage": "off",
        }
        self.client._parse_switches(data)
        assert self.client._water_valve is True
        assert self.client._pump_switch is False
        assert self.client._washer_power is True
        assert self.client._dryer_power is False
        assert self.client._laundry_outlet is True
        assert self.client._home_recliner is True
        assert self.client._home_garage is False

    @patch("inverter_control.homeassistant.HomeAssistantClient._fetch_template_data")
    def test_poll_all_success(self, mock_fetch):
        """Test successful poll_all"""
        mock_fetch.return_value = {
            "sensor1": "150",
            "bool1": "on",
            "binary1": "off",
            "water_valve": "on",
            "vue1": "200",
        }
        self.client._poll_all()
        # _connected is set by _poll_loop, not _poll_all
        # Check that data was parsed correctly
        assert self.client._sensors["sensor1"] == 150
        assert self.client._booleans["bool1"] is True
        assert self.client._binary_sensors["binary1"] is False
        assert self.client._water_valve is True
        assert self.client._vue_sensors["vue1"] == 200

    @patch("inverter_control.homeassistant.HomeAssistantClient._fetch_template_data")
    def test_poll_all_failure(self, mock_fetch):
        """Test poll_all failure"""
        import requests

        mock_fetch.side_effect = requests.exceptions.ConnectionError()

        try:
            self.client._poll_all()
        except requests.exceptions.ConnectionError:
            pass
        # _poll_all doesn't set _connected=False - that happens in _poll_loop
        assert self.client._consecutive_failures == 0  # not incremented by _poll_all

    def test_get_sensor(self):
        """Test getting sensor value"""
        self.client._sensors["test_sensor"] = 42
        assert self.client.get_sensor("test_sensor") == 42
        assert self.client.get_sensor("missing") == 0

    def test_get_vue_sensor(self):
        """Test getting VUE sensor value"""
        self.client._vue_sensors["vue_sensor"] = 100
        assert self.client.get_vue_sensor("vue_sensor") == 100

    def test_get_boolean(self):
        """Test getting boolean value"""
        self.client._booleans["test_bool"] = True
        assert self.client.get_boolean("test_bool") is True
        assert self.client.get_boolean("missing") is False

    def test_get_binary_sensor(self):
        """Test getting binary sensor value"""
        self.client._binary_sensors["test_binary"] = True
        assert self.client.get_binary_sensor("test_binary") is True

    def test_switch_properties(self):
        """Test switch properties"""
        self.client._water_valve = True
        self.client._pump_switch = False
        assert self.client.water_valve_on is True
        assert self.client.pump_switch_on is False

    def test_control_dump_loads(self):
        """Test control dump loads"""
        with patch.object(self.client, "turn_on", return_value=True) as mock_on:
            with patch.object(self.client, "turn_off", return_value=False) as mock_off:
                changed = self.client.control_dump_loads(turn_on=True)
                assert changed == 2
                assert mock_on.call_count == 2

    def test_toggle_entity(self):
        """Test toggle entity"""
        with patch.object(self.client, "_call_service", return_value=True) as mock_call:
            result = self.client.toggle_entity("switch.test")
            assert result is True
            mock_call.assert_called_once_with("switch", "toggle", "switch.test")

    def test_press_button(self):
        """Test press button"""
        with patch.object(self.client, "_call_service", return_value=True) as mock_call:
            result = self.client.press_button("button.test")
            assert result is True
            mock_call.assert_called_once_with("button", "press", "button.test")

    def test_turn_on_off(self):
        """Test turn on/off"""
        with patch.object(self.client, "_call_service", return_value=True) as mock_call:
            assert self.client.turn_on("light.test") is True
            mock_call.assert_called_with("light", "turn_on", "light.test")

            assert self.client.turn_off("light.test") is True
            mock_call.assert_called_with("light", "turn_off", "light.test")

    def test_call_service(self):
        """Test call service"""
        with patch.object(self.client._session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            result = self.client._call_service("light", "turn_on", "light.test")
            assert result is True
            mock_post.assert_called_once()

    def test_call_service_error(self):
        """Test call service error"""
        with patch.object(self.client._session, "post") as mock_post:
            mock_post.side_effect = Exception("Error")

            result = self.client._call_service("light", "turn_on", "light.test")
            assert result is False

    def test_start_stop(self):
        """Test start and stop"""
        assert self.client._running is False
        self.client.start()
        assert self.client._running is True
        assert self.client._thread is not None
        self.client.stop()
        assert self.client._running is False

    def test_uptime(self):
        """Test uptime property"""
        import time

        self.client._start_time = time.time() - 10
        assert self.client.uptime >= 10

    def test_connected_property(self):
        """Test connected property"""
        assert self.client.connected is False
        self.client._connected = True
        assert self.client.connected is True


class TestGetHA:
    """Test get_ha singleton"""

    def setup_method(self):
        homeassistant._ha_client = None

    def teardown_method(self):
        homeassistant._ha_client = None

    @patch("inverter_control.homeassistant.HA_TOKEN", "your_token_here")
    @patch("inverter_control.homeassistant.HomeAssistantClient")
    def test_get_ha_disabled(self, mock_client_class):
        """Test get_ha when HA disabled - still creates client but ENABLE_HA flag is False"""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        client = homeassistant.get_ha()
        # get_ha still creates client even when token is invalid
        assert client is not None
        mock_client_class.assert_called_once()

    @patch("inverter_control.homeassistant.HA_TOKEN", "valid_token")
    @patch("inverter_control.homeassistant.HomeAssistantClient")
    def test_get_ha_enabled(self, mock_client_class):
        """Test get_ha when HA enabled"""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        client = homeassistant.get_ha()
        assert client == mock_client
        mock_client.start.assert_called_once()

    @patch("inverter_control.homeassistant.HA_TOKEN", "valid_token")
    @patch("inverter_control.homeassistant.HomeAssistantClient")
    def test_get_ha_singleton(self, mock_client_class):
        """Test get_ha returns singleton"""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        client1 = homeassistant.get_ha()
        client2 = homeassistant.get_ha()
        assert client1 is client2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
