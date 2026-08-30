"""Tests for the dbus-evcharger / dbus-ev D-Bus EV reader (inverter_control.evcharger)."""

from unittest.mock import MagicMock

import pytest

from inverter_control import evcharger as ev_mod
from inverter_control.evcharger import EvChargerReader


@pytest.fixture(name="reader_factory")
def _reader_factory():
    """Return a callable that builds a fresh EvChargerReader with a fresh mock."""

    def _make(side_effect=None):
        dbus_get = (
            MagicMock(side_effect=side_effect) if side_effect else MagicMock(return_value=None)
        )
        return EvChargerReader(dbus_get), dbus_get

    return _make


def _vehicle_responses(soc, ac_power):
    """Build a side_effect for a vehicle service (dbus-ev)."""

    def _fn(svc, path):
        if svc == "com.victronenergy.ev.22":
            mapping = {
                "/Mgmt/Connection": "evcharger:40",
                "/Soc": str(soc) if soc is not None else None,
                "/VIN": "TESTVIN",
                "/Ac/Power": str(ac_power) if ac_power is not None else None,
            }
            return mapping.get(path)
        return None

    return _fn


def _wallbox_responses(ac_power, current=None, voltage=None):
    """Build a side_effect for a wallbox service (dbus-evcharger)."""

    def _fn(svc, path):
        if svc == "com.victronenergy.evcharger.40":
            mapping = {
                "/Mgmt/Connection": "evcharger:40",  # not a vehicle
                "/Ac/Power": str(ac_power) if ac_power is not None else None,
                "/Current": str(current) if current is not None else None,
                "/Ac/L1/Voltage": str(voltage) if voltage is not None else None,
            }
            return mapping.get(path)
        return None

    return _fn


class TestEvChargerReader:
    def test_reads_vehicle_soc_and_power(self, reader_factory):
        reader, _ = reader_factory(_vehicle_responses(soc=85, ac_power=7250))
        state = reader.read(force=True)
        assert state["car_soc"] == 85
        assert state["ev_power"] == 7250.0
        assert state["ev_charging_kw"] == 7.25

    def test_reads_wallbox_power(self, reader_factory):
        reader, _ = reader_factory(_wallbox_responses(ac_power=3300))
        state = reader.read(force=True)
        assert state["ev_power"] == 3300.0
        assert state["ev_charging_kw"] == 3.3
        assert state["car_soc"] is None  # wallbox has no /Soc

    def test_wallbox_via_current_and_voltage(self, reader_factory):
        reader, _ = reader_factory(_wallbox_responses(ac_power=None, current=16.0, voltage=230.0))
        state = reader.read(force=True)
        assert state["ev_power"] == 16.0 * 230.0
        assert state["ev_charging_kw"] == round(16.0 * 230.0 / 1000.0, 6)

    def test_missing_services_yield_none(self, reader_factory):
        reader, _ = reader_factory()  # all return None
        state = reader.read(force=True)
        assert state == {"ev_power": None, "car_soc": None, "ev_charging_kw": None}

    def test_invalid_values_yield_none(self, reader_factory):
        reader, _ = reader_factory(side_effect=lambda svc, path: "unavailable")
        state = reader.read(force=True)
        assert state == {"ev_power": None, "car_soc": None, "ev_charging_kw": None}

    def test_vehicle_service_rejected_if_no_vehicle_paths(self, reader_factory):
        """A service at .ev.22 with no /Soc and no /VIN must NOT be treated as vehicle."""

        def _fn(svc, path):
            if svc == "com.victronenergy.ev.22":
                return {"/Mgmt/Connection": "evcharger:40"}.get(path)
            return None

        reader, _ = reader_factory(_fn)
        state = reader.read(force=True)
        assert state == {"ev_power": None, "car_soc": None, "ev_charging_kw": None}

    def test_wallbox_actually_vehicle_reattributes(self, reader_factory):
        """If evcharger.40 advertises /Soc + /Mgmt/Connection, treat as vehicle."""

        def _fn(svc, path):
            if svc == "com.victronenergy.evcharger.40":
                mapping = {
                    "/Mgmt/Connection": "evcharger:40",
                    "/Soc": "42",
                    "/Ac/Power": "1100",
                }
                return mapping.get(path)
            return None

        reader, _ = reader_factory(_fn)
        state = reader.read(force=True)
        # /Soc=42 makes it a vehicle
        assert state["car_soc"] == 42
        assert state["ev_power"] == 1100.0

    def test_cache_within_ttl(self, reader_factory):
        reader, dbus_get = reader_factory(_vehicle_responses(soc=70, ac_power=2200))
        first = reader.read()
        second = reader.read()
        assert first == second
        # Discovery reads + read passes total = ~7 (1 conn + soc + vin + power) per pass
        assert dbus_get.call_count < 15

    def test_force_bypasses_cache(self, reader_factory):
        reader, dbus_get = reader_factory(_vehicle_responses(soc=50, ac_power=1500))
        reader.read()
        reader.read(force=True)
        # At least the read path should run twice
        assert dbus_get.call_count > 5

    def test_service_names_follow_instances(self, monkeypatch, reader_factory):
        monkeypatch.setattr(ev_mod, "EV_INSTANCE", 33)
        monkeypatch.setattr(ev_mod, "EVCHARGER_INSTANCE", 7)
        seen = []

        def _fn(svc, path):
            seen.append(svc)
            if svc == "com.victronenergy.ev.33":
                return {
                    "/Mgmt/Connection": "evcharger:7",
                    "/Soc": "55",
                    "/Ac/Power": "100",
                }.get(path)
            return None

        reader, _ = reader_factory(_fn)
        reader.read(force=True)
        assert "com.victronenergy.ev.33" in seen
        assert "com.victronenergy.evcharger.7" in seen

    def test_singleton_reset(self):
        ev_mod.reset_evcharger_for_testing()
        first = ev_mod.get_evcharger(lambda s, p: None)
        ev_mod.reset_evcharger_for_testing()
        second = ev_mod.get_evcharger(lambda s, p: None)
        assert first is not second
