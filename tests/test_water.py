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


@pytest.mark.parametrize(
    "raw", ["nan", "inf", "-inf", float("nan"), float("inf"), True, False, [], {}, 10**400]
)
def test_malformed_measurements_are_unknown_not_empty_or_running(raw):
    state = WaterSystemReader(lambda _service, _path: raw).read(force=True)
    assert state == {"water_level": None, "water_valve": None, "pump_switch": None}


@pytest.mark.parametrize("level", ["-0.5", "100.01", "-100", "1e20"])
def test_out_of_range_level_does_not_hide_valid_switches(level):
    state = WaterSystemReader(lambda _service, path: level if path == "/Level" else "0").read()
    assert state == {"water_level": None, "water_valve": False, "pump_switch": False}


@pytest.mark.parametrize("state", ["-1", "2", "0.5", "1e20"])
def test_invalid_switch_states_do_not_mean_running(state):
    measured = WaterSystemReader(lambda _service, path: "0" if path == "/Level" else state).read()
    assert measured == {"water_level": 0.0, "water_valve": None, "pump_switch": None}


@pytest.mark.parametrize("level", ["0", "100"])
def test_level_boundary_is_a_real_measurement(level):
    state = WaterSystemReader(lambda _service, path: level if path == "/Level" else "1").read()
    assert state == {"water_level": float(level), "water_valve": True, "pump_switch": True}


def test_cache_expires_despite_wall_clock_rollback(monkeypatch):
    clock = {"monotonic": 10.0, "wall": 1000.0}
    monkeypatch.setattr(water_mod.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(water_mod.time, "time", lambda: clock["wall"])
    dbus_get = MagicMock(return_value="1")
    reader = WaterSystemReader(dbus_get)
    assert reader.read()["water_valve"] is True
    dbus_get.return_value = "nan"
    clock.update(monotonic=13.0, wall=1.0)
    assert reader.read() == {"water_level": None, "water_valve": None, "pump_switch": None}
    assert dbus_get.call_count == 6
