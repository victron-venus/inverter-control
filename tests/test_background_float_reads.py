"""Background telemetry reads tolerate a disappearing optional D-Bus service."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control.victron import (
    TASMOTA_ENERGY_DAILY_PATH,
    TASMOTA_ENERGY_YESTERDAY_PATH,
    VictronDBus,
)


@pytest.fixture(name="reader")
def reader_fixture(monkeypatch):
    """Avoid discovery, live D-Bus connections and background threads."""
    monkeypatch.setattr(VictronDBus, "_discover_services", lambda _: None)
    return VictronDBus(test_mode=True)


@pytest.mark.parametrize("native_enabled", [False, True])
@pytest.mark.parametrize("reply", [None, ""])
def test_missing_cli_float_reply_keeps_existing_zero_fallback(
    reader, monkeypatch, native_enabled, reply
):
    native_read = Mock(return_value=None)
    reader._native = SimpleNamespace(get_value=native_read) if native_enabled else None
    cli = Mock(return_value=reply)
    monkeypatch.setattr(reader, "_safe_subprocess", cli)

    assert reader._get_float_nolock("com.victronenergy.pvinverter.test", "/Ac/Power") == 0.0
    cli.assert_called_once()
    assert native_read.call_count == int(native_enabled)


def test_float_read_honors_service_backoff_without_an_exception(reader, monkeypatch):
    service = "com.victronenergy.pvinverter.test"
    reader._service_backoff_until[service] = time.time() + 60
    native_read = Mock(return_value="1.5")
    reader._native = SimpleNamespace(get_value=native_read)
    cli = Mock()
    monkeypatch.setattr(reader, "_safe_subprocess", cli)

    assert reader._get_float_nolock(service, "/Ac/Power") == 0.0
    native_read.assert_not_called()
    cli.assert_not_called()


def test_pv_cli_failure_does_not_abort_remaining_poll_and_recovers(reader, monkeypatch):
    """A missing PV yield cannot skip later services or battery-energy polling."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("inverter_control.victron.time.time", lambda: clock["now"])
    reader._native = SimpleNamespace(get_value=Mock(return_value=None))
    reader._pv_inverter_services = [
        "com.victronenergy.pvinverter.disappearing",
        "com.victronenergy.pvinverter.healthy",
    ]
    reader._mppt_services = []
    reader._last_signal_ok_monotonic = None
    reader._last_signal_reconcile = time.monotonic()
    monkeypatch.setattr(reader, "_signals_healthy", lambda: True)
    for method in (
        "_poll_battery_chain_socs",
        "_poll_inverter_state",
        "_reconcile_groups_if_stale",
        "_poll_battery_cell_data_tree",
    ):
        monkeypatch.setattr(reader, method, Mock())
    battery_energy = Mock()
    monkeypatch.setattr(reader, "_poll_battery_daily_energy", battery_energy)

    def cli_reply(command, timeout):
        disappearing = "disappearing" in command[3]
        if disappearing and clock["now"] == 1000.0:
            return None
        values = {
            TASMOTA_ENERGY_DAILY_PATH: 2.25 if disappearing else 4.25,
            TASMOTA_ENERGY_YESTERDAY_PATH: 1.5 if disappearing else 3.0,
        }
        return f"variant double {values[command[4]]}"

    monkeypatch.setattr(reader, "_safe_subprocess", cli_reply)
    reader._poll_all()
    assert reader._cached_pv_inverter_daily_yields == [0.0, 4.25]
    assert reader._cached_pv_inverter_yesterday_yields == [0.0, 3.0]
    assert reader._last_daily_yields_time == 1000.0
    battery_energy.assert_called_once()

    clock["now"] = 1006.0
    reader._poll_all()
    assert reader._cached_pv_inverter_daily_yields == [2.25, 4.25]
    assert reader._cached_pv_inverter_yesterday_yields == [1.5, 3.0]
    assert reader._last_daily_yields_time == 1006.0
    assert battery_energy.call_count == 2
