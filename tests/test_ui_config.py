"""Tests for ui_config module."""

import pytest
from inverter_control.ui_config import get_ui_config


class TestUIConfig:
    """Tests for UI configuration."""

    def test_get_ui_config_returns_dict(self):
        """Test that get_ui_config returns a dictionary."""
        config = get_ui_config()
        assert isinstance(config, dict)

    def test_header_toggles_present(self):
        """Test header toggles configuration."""
        config = get_ui_config()
        assert "header_toggles" in config
        toggles = config["header_toggles"]
        assert isinstance(toggles, list)
        assert len(toggles) >= 7

        toggle_ids = [t["id"] for t in toggles]
        assert "only_charging" in toggle_ids
        assert "no_feed" in toggle_ids
        assert "house_support" in toggle_ids
        assert "charge_battery" in toggle_ids
        assert "do_not_supply_charger" in toggle_ids
        assert "set_limit_to_ev_charger" in toggle_ids
        assert "minimize_charging" in toggle_ids

        for toggle in toggles:
            assert "id" in toggle
            assert "label" in toggle
            assert "entity" in toggle

    def test_home_buttons_present(self):
        """Test home buttons configuration."""
        config = get_ui_config()
        assert "home_buttons" in config
        buttons = config["home_buttons"]
        assert isinstance(buttons, list)
        assert len(buttons) >= 3

        button_ids = [b["id"] for b in buttons]
        assert "recliner" in button_ids
        assert "garage" in button_ids
        assert "laundry" in button_ids

        for button in buttons:
            assert "id" in button
            assert "label" in button
            assert "entity" in button
            assert "state_key" in button

    def test_batteries_configuration(self):
        """Test batteries configuration."""
        config = get_ui_config()
        assert "batteries" in config
        batteries = config["batteries"]
        assert isinstance(batteries, list)
        assert len(batteries) >= 3

        battery_ids = [b["id"] for b in batteries]
        assert "chain1" in battery_ids
        assert "chain2" in battery_ids
        assert "virtual" in battery_ids

        for battery in batteries:
            assert "id" in battery
            assert "name" in battery
            assert "show_current" in battery
            assert "show_power" in battery
            assert isinstance(battery["show_current"], bool)
            assert isinstance(battery["show_power"], bool)

    def test_solar_sources_configuration(self):
        """Test solar sources configuration."""
        config = get_ui_config()
        assert "solar_sources" in config
        solar = config["solar_sources"]

        assert "mppt_names" in solar
        assert isinstance(solar["mppt_names"], dict)

        assert "pv_inverters" in solar
        pvs = solar["pv_inverters"]
        assert isinstance(pvs, list)
        assert len(pvs) >= 2

        for pv in pvs:
            assert "id" in pv
            assert "name" in pv
            assert "index" in pv

    def test_loads_configuration(self):
        """Test loads configuration."""
        config = get_ui_config()
        assert "loads" in config
        loads = config["loads"]

        assert "hidden" in loads
        assert isinstance(loads["hidden"], list)
        assert "solar_shed" in loads["hidden"]

        assert "min_watts" in loads
        assert isinstance(loads["min_watts"], int)
        assert loads["min_watts"] == 10

    def test_water_configuration(self):
        """Test water configuration."""
        config = get_ui_config()
        assert "water" in config
        water = config["water"]

        assert "valve_entity" in water
        assert "pump_entity" in water
        assert water["valve_entity"] == "switch.shutoff_valve"
        assert water["pump_entity"] == "switch.pump_switch"

    def test_ev_configuration(self):
        """Test EV configuration."""
        config = get_ui_config()
        assert "ev" in config
        ev = config["ev"]

        assert "charging_sensor" in ev
        assert "power_sensor" in ev
        assert "soc_sensor" in ev
        assert ev["charging_sensor"] == "ev_charging_power"
        assert ev["power_sensor"] == "ev_charger"
        assert ev["soc_sensor"] == "car_soc"

    def test_config_structure_immutability(self):
        """Test that config returned is a new dict each time."""
        config1 = get_ui_config()
        config2 = get_ui_config()

        assert config1 == config2
        assert config1 is not config2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
