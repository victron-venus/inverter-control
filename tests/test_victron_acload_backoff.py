"""The signal-backed acload cache avoids repeatedly polling unavailable sensors."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control import grid_backup, victron

HOME = "com.victronenergy.acload.home"
MISSING = "com.victronenergy.acload.missing"


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100.0, wall=1000.0)
    fake_time = SimpleNamespace(monotonic=lambda: value.now, time=lambda: value.wall)
    monkeypatch.setattr(victron, "time", fake_time)
    monkeypatch.setattr(grid_backup, "time", fake_time)
    return value


@pytest.fixture
def make_reader(monkeypatch):
    monkeypatch.setattr(victron.VictronDBus, "_discover_services", lambda _self: None)

    def create(services=(HOME, MISSING), *, backup=False):
        if backup:
            monkeypatch.setattr(victron, "GRID_BACKUP_SERVICE", MISSING)
            monkeypatch.setattr(victron, "USE_GRID_SUBMETER_AS_BACKUP", True)
        reader = victron.VictronDBus(test_mode=True)
        reader._acload_services = list(services)
        reader._reconcile_all_batteries = Mock()
        return reader

    return create


def test_failed_channel_backs_off_both_name_and_power_reads(clock, make_reader):
    reader = make_reader()
    calls = []

    def read(service, path):
        calls.append((clock.now, service, path))
        if path == victron.ACLOAD_NAME_PATH:
            return "Home" if service == HOME else "Missing"
        return "250" if service == HOME else None

    reader._dbus_get = read
    reader._reconcile_acload_power()
    for second in (101, 102, 104.999):
        clock.now = second
        reader._reconcile_acload_power()
    assert [path for _at, service, path in calls if service == MISSING] == [
        victron.ACLOAD_NAME_PATH,
        victron.AC_POWER_PATH,
    ]
    assert len([call for call in calls if call[1] == HOME]) == 8
    assert reader._compose_acload_powers() == {"Home": 250.0, "Missing": 0.0}

    for second in (105, 115, 135, 175, 235, 295):
        clock.now = second - 0.001
        reader._reconcile_acload_power()
        before = len([call for call in calls if call[1] == MISSING])
        clock.now = second
        reader._reconcile_acload_power()
        assert len([call for call in calls if call[1] == MISSING]) == before + 2
    assert [
        at for at, service, path in calls if service == MISSING and path == victron.AC_POWER_PATH
    ] == [100, 105, 115, 135, 175, 235, 295]


@pytest.mark.parametrize("raw", [None, "", "nan", "inf", "-inf", "invalid", True])
def test_invalid_power_keeps_legacy_zero_cache_but_backs_off(clock, make_reader, raw):
    reader = make_reader((MISSING,))
    reader._acload_names[MISSING] = "Missing"
    reader._acload_powers_by_service[MISSING] = 27.0
    reader._dbus_get = Mock(
        side_effect=lambda _service, path: "Missing" if path == victron.ACLOAD_NAME_PATH else raw
    )
    reader._reconcile_acload_power()
    assert reader._compose_acload_powers() == {"Missing": 0.0}
    reader._dbus_get.reset_mock()
    clock.now += 1
    reader._reconcile_acload_power()
    reader._dbus_get.assert_not_called()


@pytest.mark.parametrize("raw", ["0", "-42.5"])
def test_valid_power_recovers_normal_polling_and_resets_failure_delay(clock, make_reader, raw):
    reader = make_reader((HOME,))
    power = [None]
    reader._dbus_get = Mock(
        side_effect=lambda _service, path: "Home" if path == victron.ACLOAD_NAME_PATH else power[0]
    )
    for second in (100, 105, 115):
        clock.now = second
        reader._reconcile_acload_power()
    power[0] = raw
    clock.now = 135
    reader._reconcile_acload_power()
    assert reader._compose_acload_powers() == {"Home": float(raw)}
    reader._dbus_get.reset_mock()
    clock.now = 136
    reader._reconcile_acload_power()
    assert reader._dbus_get.call_count == 2

    power[0] = None
    clock.now = 137
    reader._reconcile_acload_power()
    reader._dbus_get.reset_mock()
    clock.now = 142
    reader._reconcile_acload_power()
    assert reader._dbus_get.call_count == 2  # Recovery reset delay to five seconds.


@pytest.mark.parametrize("power", ["0", "-14.5"])
def test_power_signal_recovers_immediately_but_name_signal_does_not(clock, make_reader, power):
    reader = make_reader((HOME,))
    reader._dbus_get = Mock(
        side_effect=lambda _service, path: "Home" if path == victron.ACLOAD_NAME_PATH else None
    )
    reader._reconcile_acload_power()
    reader._dbus_get.reset_mock()
    clock.now += 1
    assert reader._apply_group_value(HOME, victron.ACLOAD_NAME_PATH, "Renamed")
    reader._reconcile_acload_power()
    reader._dbus_get.assert_not_called()

    assert reader._apply_group_value(HOME, victron.AC_POWER_PATH, power)
    assert reader._compose_acload_powers() == {"Renamed": float(power)}
    reader._dbus_get.side_effect = lambda _service, path: (
        "Home" if path == victron.ACLOAD_NAME_PATH else power
    )
    reader._reconcile_acload_power()
    assert reader._dbus_get.call_count == 2


@pytest.mark.parametrize("raw", [None, "nan", "inf", "invalid", True])
def test_invalid_signal_does_not_end_retry_wait(clock, make_reader, raw):
    reader = make_reader((HOME,))
    reader._dbus_get = Mock(
        side_effect=lambda _service, path: "Home" if path == victron.ACLOAD_NAME_PATH else None
    )
    reader._reconcile_acload_power()
    reader._dbus_get.reset_mock()
    clock.now += 1
    reader._apply_group_value(HOME, victron.AC_POWER_PATH, raw)
    reader._reconcile_acload_power()
    reader._dbus_get.assert_not_called()
    assert reader._compose_acload_powers() == {"Home": 0.0}


def test_healthy_signals_do_not_starve_failed_sibling_retry(clock, make_reader):
    reader = make_reader()
    reader._dbus_get = Mock(
        side_effect=lambda service, path: (
            service if path == victron.ACLOAD_NAME_PATH else ("12" if service == HOME else None)
        )
    )
    reader._reconcile_acload_power()
    reader._dbus_get.reset_mock()
    clock.now += 5
    clock.wall += 5
    reader._apply_group_value(HOME, victron.AC_POWER_PATH, "15")
    reader._reconcile_groups_if_stale()
    reader._dbus_get.assert_any_call(MISSING, victron.AC_POWER_PATH)


def test_wall_clock_jump_does_not_expire_monotonic_retry_wait(clock, make_reader):
    reader = make_reader((HOME,))
    reader._dbus_get = Mock(return_value=None)
    reader._reconcile_acload_power()
    reader._dbus_get.reset_mock()
    clock.wall += 86400
    clock.now += 1
    reader._reconcile_groups_if_stale()
    reader._dbus_get.assert_not_called()


def test_critical_grid_backup_keeps_one_second_cadence_during_auxiliary_backoff(clock, make_reader):
    reader = make_reader((MISSING,), backup=True)
    reader._dbus_get = Mock(return_value=None)
    reader._reconcile_acload_power()
    reader._dbus_get.reset_mock()
    snapshot = {
        "/Connected": 1,
        "/Role": "acload",
        "/Ac/Power": 12.0,
        "/LastUpdate": clock.wall,
        "/DeviceInstance": 78,
        "/CustomName": "Main supply",
        "/ProductName": "Meter",
    }
    reader._native = SimpleNamespace(get_items_values=Mock(return_value=snapshot))
    for second in (100, 100.2, 101, 101.9, 102):
        clock.now = second
        reader._poll_grid_backup()
        reader._reconcile_acload_power()
    assert reader._native.get_items_values.call_count == 3
    reader._native.get_items_values.assert_called_with(MISSING, timeout=0.5)
    reader._dbus_get.assert_not_called()
    selected = reader._grid_backup.select({"_grid_valid": False})
    assert selected["_grid_valid"] and selected["_grid_backup"]
    assert selected["gt"] == 12


@pytest.mark.parametrize("late_reply", [None, "-900"])
def test_fresh_signal_wins_over_in_flight_probe_and_clears_long_backoff(
    clock, make_reader, late_reply
):
    reader = make_reader((HOME,))
    reader._dbus_get = Mock(
        side_effect=lambda _service, path: "Home" if path == victron.ACLOAD_NAME_PATH else None
    )
    # Reach the longest retry interval using real failed observations.
    for second in (100, 105, 115, 135, 175):
        clock.now = second
        reader._reconcile_acload_power()

    def read_with_new_signal(service, path):
        if path == victron.ACLOAD_NAME_PATH:
            return "Home"
        # Detect a broad I/O lock before the inline signal could deadlock.
        assert not reader._acload_lock.locked()
        assert reader._apply_group_value(service, path, "125")
        return late_reply

    clock.now = 235
    reader._dbus_get = Mock(side_effect=read_with_new_signal)
    reader._reconcile_acload_power()
    assert reader._compose_acload_powers() == {"Home": 125.0}

    clock.now += 0.1
    reader._dbus_get = Mock(
        side_effect=lambda _service, path: "Home" if path == victron.ACLOAD_NAME_PATH else "126"
    )
    reader._reconcile_acload_power()
    reader._dbus_get.assert_any_call(HOME, victron.AC_POWER_PATH)
    assert reader._compose_acload_powers() == {"Home": 126.0}
