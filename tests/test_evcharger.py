"""Tests for the dbus-evcharger / dbus-ev D-Bus EV reader (inverter_control.evcharger)."""

from unittest.mock import MagicMock, patch

import pytest

from inverter_control import evcharger as ev_mod
from inverter_control.evcharger import EvChargerReader
from inverter_control.logic import SetpointCalculator, SystemState


@pytest.fixture(name="reader_factory")
def _reader_factory():
    """Return a callable that builds a fresh EvChargerReader with a fresh mock."""

    def _make(side_effect=None, names=None):
        dbus_get = (
            MagicMock(side_effect=side_effect) if side_effect else MagicMock(return_value=None)
        )
        names = names or ("com.victronenergy.ev.ha", "com.victronenergy.evcharger.charger")
        return EvChargerReader(dbus_get, lambda: names), dbus_get

    return _make


def _vehicle_responses(soc, ac_power):
    """Build a side_effect for a vehicle service (dbus-ev)."""

    def _fn(svc, path):
        if svc == "com.victronenergy.ev.ha":
            mapping = {
                "/DeviceInstance": "22",
                "/Mgmt/Connection": "evcharger:40",
                "/Soc": str(soc) if soc is not None else None,
                "/VIN": "TESTVIN",
                "/Ac/Power": str(ac_power) if ac_power is not None else None,
            }
            return mapping.get(path)
        return None

    return _fn


def _wallbox_responses(ac_power, current=None, voltage=None, connected=1):
    """Build a side_effect for a wallbox service (dbus-evcharger)."""

    def _fn(svc, path):
        if svc == "com.victronenergy.evcharger.charger":
            mapping = {
                "/DeviceInstance": "40",
                "/Mgmt/Connection": "Home Assistant",  # not a vehicle
                "/Connected": str(connected) if connected is not None else None,
                "/Ac/Power": str(ac_power) if ac_power is not None else None,
                "/Current": str(current) if current is not None else None,
                "/Ac/L1/Voltage": str(voltage) if voltage is not None else None,
            }
            return mapping.get(path)
        return None

    return _fn


class TestEvChargerReader:
    def test_vehicle_soc_does_not_imply_site_charging_power(self, reader_factory):
        reader, _ = reader_factory(_vehicle_responses(soc=85, ac_power=7250))
        state = reader.read(force=True)
        assert state["car_soc"] == 85
        assert state["ev_power"] is None
        assert state["ev_charging_kw"] is None

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
        """A matching vehicle service with no /Soc and no /VIN must NOT be treated as vehicle."""

        def _fn(svc, path):
            if svc == "com.victronenergy.ev.ha":
                return {"/DeviceInstance": "22", "/Mgmt/Connection": "evcharger:40"}.get(path)
            return None

        reader, _ = reader_factory(_fn)
        state = reader.read(force=True)
        assert state == {"ev_power": None, "car_soc": None, "ev_charging_kw": None}

    def test_wallbox_actually_vehicle_reattributes(self, reader_factory):
        """If a matching charger advertises /Soc + /Mgmt/Connection, treat as vehicle."""

        def _fn(svc, path):
            if svc == "com.victronenergy.evcharger.charger":
                mapping = {
                    "/DeviceInstance": "40",
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
        assert state["ev_power"] is None

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

    def test_instances_select_metadata_not_service_suffixes(self, monkeypatch, reader_factory):
        monkeypatch.setattr(ev_mod, "EV_INSTANCE", 33)
        monkeypatch.setattr(ev_mod, "EVCHARGER_INSTANCE", 7)
        seen = []

        def _fn(svc, path):
            seen.append(svc)
            if svc == "com.victronenergy.ev.ha":
                return {
                    "/DeviceInstance": "33",
                    "/Mgmt/Connection": "evcharger:7",
                    "/Soc": "55",
                    "/Ac/Power": "100",
                }.get(path)
            return None

        reader, _ = reader_factory(_fn)
        reader.read(force=True)
        assert "com.victronenergy.ev.ha" in seen
        assert "com.victronenergy.evcharger.charger" in seen

    def test_singleton_reset(self):
        ev_mod.reset_evcharger_for_testing()
        first = ev_mod.get_evcharger(lambda s, p: None)
        ev_mod.reset_evcharger_for_testing()
        second = ev_mod.get_evcharger(lambda s, p: None)
        assert first is not second


def test_missing_names_never_generate_numeric_destinations():
    dbus_get = MagicMock()
    reader = EvChargerReader(dbus_get, lambda: ())
    assert reader.read() == {"ev_power": None, "car_soc": None, "ev_charging_kw": None}
    dbus_get.assert_not_called()


def test_service_suffix_changes_are_selected_from_new_discovery_snapshot():
    names = ["com.victronenergy.ev.first"]

    def get(_service, path):
        return {"/DeviceInstance": "22", "/Mgmt/Connection": "evcharger:40", "/Soc": "61"}.get(path)

    dbus_get = MagicMock(side_effect=get)
    reader = EvChargerReader(dbus_get, lambda: tuple(names))
    assert reader.read()["car_soc"] == 61
    names[:] = ["com.victronenergy.ev.restarted"]
    dbus_get.reset_mock()
    assert reader.read(force=True)["car_soc"] == 61
    assert {call.args[0] for call in dbus_get.call_args_list} == set(names)


def test_wrong_or_duplicate_device_instances_are_not_selected():
    get = MagicMock(side_effect=_vehicle_responses(85, 7250))
    reader = EvChargerReader(get, lambda: ("com.victronenergy.ev.unmatched",))
    assert reader.read()["ev_power"] is None
    get.side_effect = lambda _service, path: "22" if path == "/DeviceInstance" else "85"
    reader = EvChargerReader(get, lambda: ("com.victronenergy.ev.one", "com.victronenergy.ev.two"))
    assert reader.read()["ev_power"] is None


def test_unavailable_instance_metadata_is_retried_after_bounded_interval(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(ev_mod.time, "monotonic", lambda: now[0])
    dbus_get = MagicMock(return_value=None)
    reader = EvChargerReader(dbus_get, lambda: ("com.victronenergy.ev.ha",))
    assert reader.read(force=True)["car_soc"] is None
    dbus_get.side_effect = _vehicle_responses(71, 0)
    assert reader.read(force=True)["car_soc"] is None
    now[0] += ev_mod.DISCOVERY_TTL
    assert reader.read(force=True)["car_soc"] == 71


def test_victron_service_snapshot_uses_existing_discovery_without_extra_bus_queries():
    from inverter_control.victron import VictronDBus

    with (
        patch.object(VictronDBus, "_discover_services"),
        patch.object(VictronDBus, "_load_battery_daily_energy"),
    ):
        victron = VictronDBus(test_mode=True)
    names = ("com.victronenergy.ev.ha", "com.victronenergy.evcharger.charger")
    victron._apply_discovery("\n".join(names))
    with patch.object(victron, "_run_discovery_command") as query:
        assert victron.get_service_names() == names
        assert victron.get_service_names() == names
    query.assert_not_called()


def _site_and_vehicle(wallbox_power, vehicle_connected=0, **wallbox_options):
    vehicle = _vehicle_responses(soc=62, ac_power=4400)
    wallbox = _wallbox_responses(wallbox_power, **wallbox_options)

    def read(service, path):
        if service == "com.victronenergy.ev.ha" and path == "/Connected":
            return str(vehicle_connected)  # Remote charging is never a home-grid load.
        return (
            vehicle(service, path)
            if service.startswith("com.victronenergy.ev.")
            else wallbox(service, path)
        )

    return read


@pytest.mark.parametrize("wallbox_power", [0, 3200])
@pytest.mark.parametrize("vehicle_connected", [0, 1])
def test_site_meter_wins_over_remote_vehicle_power(
    reader_factory, wallbox_power, vehicle_connected
):
    reader, dbus_get = reader_factory(
        _site_and_vehicle(wallbox_power, vehicle_connected=vehicle_connected)
    )
    state = reader.read(force=True)
    assert state == {
        "ev_power": wallbox_power,
        "car_soc": 62,
        "ev_charging_kw": wallbox_power / 1000,
    }
    assert ("com.victronenergy.ev.ha", "/Ac/Power") not in [
        call.args for call in dbus_get.call_args_list
    ]


@pytest.mark.parametrize("connected", [0, None, "unavailable", "nan", "inf", -1, 1.5])
def test_unavailable_wallbox_never_falls_back_to_vehicle(reader_factory, connected):
    reader, _ = reader_factory(_site_and_vehicle(3200, connected=connected))
    assert reader.read(force=True)["ev_power"] is None


@pytest.mark.parametrize("power", [None, "unavailable", "nan", "inf", "-inf", -100])
def test_invalid_site_power_never_becomes_vehicle_power(reader_factory, power):
    reader, _ = reader_factory(_site_and_vehicle(power))
    assert reader.read(force=True)["ev_power"] is None


@pytest.mark.parametrize(
    ("current", "voltage"),
    [(None, 230), (16, None), (-16, 230), (16, -230), (16, 0), ("inf", 230), (1e308, 1e308)],
)
def test_invalid_derived_site_power_is_unknown(reader_factory, current, voltage):
    reader, _ = reader_factory(_site_and_vehicle(None, current=current, voltage=voltage))
    assert reader.read(force=True)["ev_power"] is None


def test_disconnected_wallbox_invalidates_previous_power_on_next_read(reader_factory):
    values = {"connected": 1}
    source = _site_and_vehicle(3200)

    def read(service, path):
        if service == "com.victronenergy.evcharger.charger" and path == "/Connected":
            return str(values["connected"])
        return source(service, path)

    reader, _ = reader_factory(read)
    assert reader.read(force=True)["ev_power"] == 3200
    values["connected"] = 0
    assert reader.read(force=True)["ev_power"] is None


@pytest.mark.parametrize("soc", ["nan", "inf", "unavailable"])
def test_invalid_vehicle_soc_does_not_hide_site_power(reader_factory, soc):
    source = _site_and_vehicle(3200)

    def read(service, path):
        if service == "com.victronenergy.ev.ha" and path == "/Soc":
            return soc
        return source(service, path)

    reader, _ = reader_factory(read)
    state = reader.read(force=True)
    assert state["ev_power"] == 3200
    assert state["car_soc"] is None


@pytest.mark.parametrize("site_power,grid_power", [(0, 2400), (3200, 5600)])
def test_site_power_drives_ev_exclusion_without_phantom_export(
    reader_factory, site_power, grid_power
):
    reader, _ = reader_factory(_site_and_vehicle(site_power))
    measured = reader.read(force=True)
    state = SystemState(
        g1=grid_power,
        g2=0,
        gt=grid_power,
        t1=800,
        t2=0,
        tt=800,
        inv_power=2255,
        mppt_total=1700,
        pv_inverter_total=684,
        pv_total=2384,
        ev_power=measured["ev_power"] or 0,
        garage_power=0,
        only_charging=True,
        no_feed=False,
        house_support=False,
        charge_battery=False,
        do_not_supply_charger=True,
        limit_to_ev=False,
        previous_setpoint=2250,
        prefiltered_gt=grid_power,
    )
    result = SetpointCalculator({}).calculate(state)
    assert result.filtered_gt == 2400
    assert result.setpoint == 743  # One bounded cycle away from maximum charging.
    assert "[CHG]" not in result.flags
    assert ("[EV:" in result.flags) is (site_power > 0)
