"""Tests for the dbus-pump D-Bus water reader (inverter_control.water)."""

from unittest.mock import MagicMock

import pytest

from inverter_control import water as water_mod
from inverter_control.water import WaterSystemReader


@pytest.fixture(name="reader")
def _reader():
    dbus_get = MagicMock(return_value=None)
    return WaterSystemReader(dbus_get), dbus_get


class TestWaterSystemReader:
    def test_reads_level_and_states(self):
        dbus_get = MagicMock(
            side_effect=lambda svc, path: {
                ("com.victronenergy.tank.ha_tank21", "/Level"): "66.5",
                ("com.victronenergy.pump.startstop2", "/State"): "1",
                ("com.victronenergy.pump.startstop1", "/State"): "0",
            }.get((svc, path))
        )
        state = WaterSystemReader(dbus_get).read(force=True)

        assert state["water_level"] == 66.5
        assert state["water_valve"] is True
        assert state["pump_switch"] is False

    def test_missing_service_yields_none_not_zero(self):
        dbus_get = MagicMock(return_value=None)
        state = WaterSystemReader(dbus_get).read(force=True)

        assert state == {"water_level": None, "water_valve": None, "pump_switch": None}
        assert dbus_get.call_count == 3

    def test_invalid_values_yield_none(self):
        dbus_get = MagicMock(side_effect=lambda svc, path: "unavailable")
        state = WaterSystemReader(dbus_get).read(force=True)

        assert state == {"water_level": None, "water_valve": None, "pump_switch": None}

    def test_cache_within_ttl(self):
        dbus_get = MagicMock(return_value="50")
        reader = WaterSystemReader(dbus_get)

        first = reader.read()
        second = reader.read()

        assert first == second
        assert dbus_get.call_count == 3  # no extra reads within TTL

    def test_force_bypasses_cache(self):
        dbus_get = MagicMock(return_value="50")
        reader = WaterSystemReader(dbus_get)

        reader.read()
        reader.read(force=True)

        assert dbus_get.call_count == 6

    def test_service_names_follow_instances(self, monkeypatch):
        monkeypatch.setattr(water_mod, "WATER_TANK_INSTANCE", 33)
        monkeypatch.setattr(water_mod, "WATER_PUMP_INSTANCE", 7)
        monkeypatch.setattr(water_mod, "WATER_VALVE_INSTANCE", 9)
        seen = []

        def dbus_get(service, path):
            seen.append(service)
            return "1"

        WaterSystemReader(dbus_get).read(force=True)

        assert "com.victronenergy.tank.ha_tank33" in seen
        assert "com.victronenergy.pump.startstop7" in seen
        assert "com.victronenergy.pump.startstop9" in seen

    def test_singleton_reset(self):
        water_mod.reset_water_for_testing()
        first = water_mod.get_water(lambda s, p: None)
        water_mod.reset_water_for_testing()
        second = water_mod.get_water(lambda s, p: None)

        assert first is not second
