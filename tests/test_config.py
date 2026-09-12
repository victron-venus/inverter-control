"""
Unit tests for Inverter Control Configuration
"""

import importlib.util
import os
import sys
from types import ModuleType
from unittest.mock import mock_open, patch

import pytest

# Ensure we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import config


class TestConfigValidation:
    """Test configuration validation"""

    def test_validate_config_success(self):
        """Test that validation passes with valid config"""
        # Should not raise if import succeeds
        assert config.LOOP_INTERVAL > 0
        assert config.POWER_LIMIT_MIN < 0
        assert config.POWER_LIMIT_MAX > 0

    def test_types(self):
        """Test type of exported config values"""
        assert isinstance(config.LOOP_INTERVAL, (int, float))
        assert isinstance(config.POWER_LIMIT_MAX, (int, float))
        assert isinstance(config.POWER_LIMIT_MIN, (int, float))
        assert isinstance(config.DAMPING_FACTOR, (int, float))
        assert isinstance(config.EMA_ALPHA, (int, float))
        assert isinstance(config.HA_TOKEN, str)
        assert isinstance(config.HA_URL, str)
        assert isinstance(config.PORTAL_ID, str)
        assert isinstance(config.HA_SENSORS, dict)
        assert isinstance(config.VUE_SENSORS, dict)
        assert isinstance(config.HA_DUMP_LOADS, (list, tuple))

    def test_ranges(self):
        """Test value ranges"""
        assert 0.0 <= config.DAMPING_FACTOR <= 1.0
        assert 0.0 <= config.EMA_ALPHA <= 1.0
        assert config.LOOP_INTERVAL > 0
        assert config.POWER_LIMIT_MIN < config.POWER_LIMIT_MAX
        assert config.EXPORT_DAMPING >= 0.0
        assert 0.0 <= config.CREEP_RATE <= 100.0
        assert 0.0 <= config.CREEP_MAX <= 100.0
        assert config.SOLAR_OUTPUT_OFFSET >= 0
        assert 0.0 <= config.INVERTER_EFFICIENCY <= 1.0


class TestCreepLocalConfig:
    """Exercise startup loading without mutating the shared config module."""

    @staticmethod
    def load_config(monkeypatch, **overrides):
        local_config = ModuleType("local_config")
        local_config.HA_URL = "http://localhost:8123"
        local_config.HA_TOKEN = ""
        local_config.HA_SENSORS = {}
        local_config.VUE_SENSORS = {}
        local_config.HA_DUMP_LOADS = []
        for name, value in overrides.items():
            setattr(local_config, name, value)
        monkeypatch.setitem(sys.modules, "local_config", local_config)
        spec = importlib.util.spec_from_file_location("_creep_config_test", config.__file__)
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded

    def test_defaults_preserved(self, monkeypatch):
        loaded = self.load_config(monkeypatch)
        assert loaded.CREEP_RATE == 0.5
        assert loaded.CREEP_MAX == 100.0

    def test_zero_rate_override_disables_accumulation(self, monkeypatch):
        loaded = self.load_config(monkeypatch, CREEP_RATE=0)
        calculator_config = {name: getattr(loaded, name) for name in loaded.EXPORTED_KEYS}
        assert calculator_config["CREEP_RATE"] == 0.0
        assert calculator_config["CREEP_MAX"] == 100.0

    @pytest.mark.parametrize("name", ["CREEP_RATE", "CREEP_MAX"])
    @pytest.mark.parametrize("value", [0.0, 12.5, 100.0])
    def test_valid_local_override(self, monkeypatch, name, value):
        loaded = self.load_config(monkeypatch, **{name: value})
        assert getattr(loaded, name) == value

    @pytest.mark.parametrize("name", ["CREEP_RATE", "CREEP_MAX"])
    @pytest.mark.parametrize("value", [-0.1, 100.1, float("nan"), float("inf"), -float("inf")])
    def test_invalid_local_override_fails_at_startup(self, monkeypatch, name, value):
        with pytest.raises(ValueError, match=rf"{name} must be 0\.0-100\.0"):
            self.load_config(monkeypatch, **{name: value})


class TestGridLossLocalConfig:
    """The optional hold is loaded and validated before any hardware control."""

    def test_default_preserves_watchdog_policy(self, monkeypatch):
        assert TestCreepLocalConfig.load_config(monkeypatch).GRID_LOSS_HOLD_SECONDS is None

    @pytest.mark.parametrize("value", [None, 0, 0.5, 3.0, 30])
    def test_valid_hold(self, monkeypatch, value):
        loaded = TestCreepLocalConfig.load_config(monkeypatch, GRID_LOSS_HOLD_SECONDS=value)
        assert loaded.GRID_LOSS_HOLD_SECONDS == value

    @pytest.mark.parametrize("value", [-1, 30.1, "3", True, False, float("nan"), float("inf")])
    def test_invalid_hold_fails_at_startup(self, monkeypatch, value):
        with pytest.raises(ValueError, match="GRID_LOSS_HOLD_SECONDS must be"):
            TestCreepLocalConfig.load_config(monkeypatch, GRID_LOSS_HOLD_SECONDS=value)


class TestColors:
    """Test ANSI Colors class"""

    def test_color_constants(self):
        """Test color codes are valid ANSI sequences"""
        assert config.Colors.RED.startswith("\033[31m")
        assert config.Colors.GREEN.startswith("\033[32m")
        assert config.Colors.YELLOW.startswith("\033[33m")
        assert config.Colors.BLUE.startswith("\033[34m")
        assert config.Colors.MAGENTA.startswith("\033[35m")
        assert config.Colors.CYAN.startswith("\033[36m")
        assert config.Colors.WHITE.startswith("\033[37m")
        assert config.Colors.RESET == "\033[0m"
        assert config.Colors.BOLD == "\033[1m"


class TestInverterStates:
    """Test inverter state mappings"""

    def test_inverter_states_dict(self):
        """Test INVERTER_STATES contains expected keys"""
        expected_states = {
            0: "Off",
            1: "Low Power",
            2: "Fault",
            3: "Bulk",
            4: "Absorption",
            5: "Float",
            6: "Storage",
            7: "Equalize",
            8: "Passthru",
            9: "Inverting",
            10: "Power assist",
            11: "Power supply",
            252: "External control",
        }
        for code, name in expected_states.items():
            assert config.INVERTER_STATES[code] == name


class TestMQTTConfig:
    """Test MQTT configuration"""

    def test_mqtt_settings(self):
        """Test MQTT-related settings"""
        assert isinstance(config.MQTT_BROKER, str)
        assert isinstance(config.MQTT_PORT, int)
        assert isinstance(config.MQTT_TOPIC_PREFIX, str)
        assert isinstance(config.MQTT_SLIM_STATE, bool)
        assert isinstance(config.MQTT_SLIM_EXCLUDE_KEYS, frozenset)

    def test_slim_exclude_keys_content(self):
        """Cerbo/dbus-mirrored live tiles must not be republished on inverter/state."""
        assert config.MQTT_SLIM_STATE is True
        required = {
            "g1",
            "g2",
            "gt",
            "t1",
            "t2",
            "tt",
            "loads",
            "batteries",
            "battery_soc",
            "battery_power",
            "battery_voltage",
            "battery_current",
            "solar_total",
            "mppt_total",
            "mppt_chargers",
            "pv_inverter_total",
            "setpoint",
            "inverter_state",
            "ev_power",
            "car_soc",
            "ev_charging_kw",
            "water_level",
            "water_valve",
            "pump_switch",
        }
        assert required <= config.MQTT_SLIM_EXCLUDE_KEYS
        # Daemon-owned extras must remain publishable
        for keep in (
            "daily_stats",
            "solar_forecast",
            "booleans",
            "ess_mode",
            "dry_run",
            "ui_config",
            "version",
            "uptime",
            "filtered_gt",
            "ha_connected",
        ):
            assert keep not in config.MQTT_SLIM_EXCLUDE_KEYS


class TestOptionalFeatures:
    """Test optional feature flags"""

    def test_feature_flags_are_booleans(self):
        """Test all ENABLE_* flags are booleans"""
        for name in dir(config):
            if name.startswith("ENABLE_"):
                val = getattr(config, name)
                assert isinstance(val, bool), f"{name} should be bool, got {type(val)}"


class TestPortalId:
    """Test runtime VRM Portal ID detection"""

    def test_portal_id_is_string(self):
        """Test PORTAL_ID is exported as a string"""
        assert isinstance(config.PORTAL_ID, str)

    def test_stub_when_no_venus_utilities(self):
        """Test placeholder stub is returned on non-Venus systems"""
        config._detect_portal_id.cache_clear()
        with (
            patch("subprocess.check_output", side_effect=OSError),
            patch("builtins.open", side_effect=OSError),
            patch.dict(os.environ, {}, clear=True),
        ):
            assert config._detect_portal_id() == "your_portal_id"

    def test_env_var_override(self):
        """Test PORTAL_ID environment variable override"""
        config._detect_portal_id.cache_clear()
        with (
            patch("subprocess.check_output", side_effect=OSError),
            patch("builtins.open", side_effect=OSError),
            patch.dict(os.environ, {"PORTAL_ID": "abcdef012345"}),
        ):
            assert config._detect_portal_id() == "abcdef012345"

    def test_eth0_mac_fallback(self):
        """Test eth0 MAC address is used when get-unique-id is missing"""
        config._detect_portal_id.cache_clear()
        with (
            patch("subprocess.check_output", side_effect=OSError),
            patch("builtins.open", mock_open(read_data="b8:27:eb:ea:1e:ce\n")),
        ):
            assert config._detect_portal_id() == "b827ebea1ece"

    def test_get_unique_id_primary(self):
        """Test /sbin/get-unique-id is used first"""
        config._detect_portal_id.cache_clear()
        with (
            patch("subprocess.check_output", return_value="cafebabe1234\n"),
            patch("builtins.open", mock_open(read_data="b8:27:eb:ea:1e:ce\n")),
        ):
            assert config._detect_portal_id() == "cafebabe1234"


class TestValidationErrors:
    """Test configuration validation error handling"""

    def test_invalid_loop_interval(self):
        """Test validation catches negative LOOP_INTERVAL"""
        with patch("inverter_control.config.LOOP_INTERVAL", -1):
            with pytest.raises(ValueError, match="LOOP_INTERVAL must be positive"):
                config._validate_config()

    def test_invalid_damping_factor(self):
        """Test validation catches out-of-range DAMPING_FACTOR"""
        with patch("inverter_control.config.DAMPING_FACTOR", 1.5):
            with pytest.raises(ValueError, match="DAMPING_FACTOR must be 0.0-1.0"):
                config._validate_config()

    def test_invalid_ema_alpha(self):
        """Test validation catches out-of-range EMA_ALPHA"""
        with patch("inverter_control.config.EMA_ALPHA", -0.1):
            with pytest.raises(ValueError, match="EMA_ALPHA must be 0.0-1.0"):
                config._validate_config()

    def test_invalid_ha_token_type(self):
        """Test validation catches non-string HA_TOKEN"""
        with patch("inverter_control.config.HA_TOKEN", 123):
            with pytest.raises(ValueError, match="HA_TOKEN must be.*str"):
                config._validate_config()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
