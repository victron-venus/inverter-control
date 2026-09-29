"""Periodic telemetry reads keep coherent snapshots without avoidable CLI processes."""

import time
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from inverter_control.victron import (
    BATTERY_CELL_SERVICES,
    SYSTEM_SERVICE,
    VEBUS_INV_POWER_PATH,
    VictronDBus,
)

METER = "com.victronenergy.grid.site"
SHUNT = "com.victronenergy.battery.shunt"
VEBUS = "com.victronenergy.vebus.ttyUSB0"
SYSTEM = {
    "/Ac/Grid/NumberOfPhases": "2",
    "/Ac/Grid/L1/Power": "100.9",
    "/Ac/Grid/L2/Power": "-30.2",
    "/Ac/In/0/Source": "1",
    "/Ac/In/0/ServiceName": METER,
    "/Ac/In/0/DeviceInstance": "40",
    "/Ac/Consumption/L1/Power": "150.9",
    "/Ac/Consumption/L2/Power": "40.9",
    "/Dc/Pv/Power": "420.9",
    # System aggregate must never replace SmartShunt bank readings.
    "/Dc/Battery/Power": "9999",
}
CHAIN = {
    "/Cell/1/Voltage": "3.41",
    "/Cell/2/Voltage": "3.42",
    # A gap ends the voltage series; temperatures can legitimately be sparse.
    "/Cell/4/Voltage": "3.44",
    "/Cell/1/Temperature": "-2.5",
    "/Cell/4/Temperature": "26.1",
    "/Soc": "85.5",
    "/Info/AllowCharge": "0",
    "/Info/AllowDischarge": "1",
}
SNAPSHOTS = {
    SYSTEM_SERVICE: SYSTEM,
    METER: {"/Connected": "1", "/NrOfPhases": "2"},
    SHUNT: {"/Dc/0/Voltage": "52.12", "/Dc/0/Current": "-4.3", "/Dc/0/Power": "-224.9"},
    **dict.fromkeys(BATTERY_CELL_SERVICES, CHAIN),
}


def tree(fields):
    """The existing CLI wire representation, for transport-parity comparisons."""
    lines = []
    for path, value in fields.items():
        lines.append(f'string "{path.lstrip("/")}"')
        if value is None:
            lines.append("variant array [\n]")
        elif value.startswith("com."):
            lines.append(f'variant string "{value}"')
        else:
            lines.append(f"variant double {value}")
    return "\n".join(lines)


@pytest.fixture
def reader():
    with (
        patch.object(VictronDBus, "_discover_services"),
        patch.object(VictronDBus, "_load_battery_daily_energy"),
    ):
        v = VictronDBus(test_mode=True)
    v._shunt_service = SHUNT
    v._vebus_service = VEBUS
    v._native = MagicMock()
    v._native.get_values_connected.side_effect = lambda service, **_kwargs: deepcopy(
        SNAPSHOTS[service]
    )
    v._native.get_value_connected.return_value = "-600.9"
    # The offline baseline replay still uses the original unbounded tree API.
    v._native.get_values.side_effect = lambda service: deepcopy(SNAPSHOTS[service])

    def cli(cmd, **kwargs):
        service = next(
            value.removeprefix("--dest=") for value in cmd if value.startswith("--dest=")
        )
        return "variant double -600.9" if service == VEBUS else tree(SNAPSHOTS[service])

    v._safe_subprocess = MagicMock(side_effect=cli)
    return v


def reconcile(v):
    v._poll_system_data()
    v._poll_shunt_data()
    v._poll_inverter_power()
    v._poll_battery_cell_data_tree()


def comparable_state(v):
    return (
        {key: value for key, value in v._system_data.items() if key != "_last_update"},
        v.get_grid_status()["gt"],
        v.get_grid_status()["_grid_valid"],
        deepcopy(v._cached_battery_cell_data),
        dict(v._chain_cell_counts),
    )


def test_successful_native_reconciliation_matches_cli_without_five_processes(reader):
    reconcile(reader)
    native_state = comparable_state(reader)
    reader._safe_subprocess.assert_not_called()
    assert (
        reader._native.get_values_connected.call_count == 5
    )  # system + meter + shunt + two chains
    reader._native.get_value_connected.assert_called_once_with(
        VEBUS, VEBUS_INV_POWER_PATH, timeout=0.25
    )
    assert native_state[0]["bp"] == -224
    assert native_state[0]["bv"] == 52.12
    assert native_state[0]["inv_power"] == -600
    assert native_state[2] is True
    assert native_state[3][BATTERY_CELL_SERVICES[0]] == {
        "voltages": [3.41, 3.42],
        "temps": [-2.5, 26.1],
        "soc": 85.5,
        "allow_charge": False,
        "allow_discharge": True,
    }
    # Failed native reads take exactly the existing bounded CLI routes.
    reader._native.get_values_connected.side_effect = None
    reader._native.get_values_connected.return_value = None
    reader._native.get_value_connected.return_value = None
    reader._last_battery_cell_data_time = 0
    reconcile(reader)
    assert comparable_state(reader) == native_state
    assert reader._safe_subprocess.call_count == 6  # includes external-meter revalidation
    assert all(0 < call.kwargs["timeout"] <= 0.5 for call in reader._safe_subprocess.call_args_list)


def test_cli_disabled_native_client_still_reads_all_trees(reader):
    reader._native = None
    reconcile(reader)
    assert reader.get_grid_status()["_grid_valid"]
    assert reader._system_data["bp"] == -224
    assert reader._safe_subprocess.call_count == 6
    assert all(call.kwargs["timeout"] == 0.5 for call in reader._safe_subprocess.call_args_list)


@pytest.mark.parametrize("missing", ["/Ac/Grid/L2/Power", "/Ac/In/0/ServiceName"])
def test_partial_native_system_snapshot_invalidates_grid_without_cli_revival(reader, missing):
    reader._poll_system_data()
    assert reader.get_grid_status()["_grid_valid"]
    partial = dict(SYSTEM)
    partial.pop(missing)
    reader._native.get_values_connected.side_effect = lambda service, **_kwargs: (
        partial if service == SYSTEM_SERVICE else SNAPSHOTS[service]
    )
    reader._poll_system_data()
    assert not reader.get_grid_status()["_grid_valid"]
    reader._safe_subprocess.assert_not_called()


def test_native_reply_from_previous_owner_cannot_revive_grid(reader):
    def old_reply(service, **_kwargs):
        assert service == SYSTEM_SERVICE
        reader._on_name_owner_changed(SYSTEM_SERVICE, ":1.1", "")
        return SYSTEM

    reader._native.get_values_connected.side_effect = old_reply
    reader._poll_system_data()
    assert not reader.get_grid_status()["_grid_valid"]
    reader._native.get_values_connected.assert_called_once_with(SYSTEM_SERVICE, timeout=0.25)
    reader._safe_subprocess.assert_not_called()


def test_empty_snapshot_is_successful_transport_but_not_valid_grid(reader):
    reader._native.get_values_connected.side_effect = None
    reader._native.get_values_connected.return_value = {}
    reader._poll_system_data()
    assert not reader.get_grid_status()["_grid_valid"]
    reader._safe_subprocess.assert_not_called()


@pytest.mark.parametrize("invalid", [None, "nan", "inf", "-inf", "invalid", []])
def test_invalid_shunt_reading_preserves_last_good_without_system_aggregate(reader, invalid):
    reader._system_data.update(bv=51.5, bc=-3.2, bp=-165)
    reader._native.get_values_connected.side_effect = None
    reader._native.get_values_connected.return_value = {
        "/Dc/0/Voltage": invalid,
        "/Dc/0/Power": "0",
    }
    reader._poll_shunt_data()
    assert reader._system_data["bv"] == 51.5
    assert reader._system_data["bc"] == -3.2
    assert reader._system_data["bp"] == 0
    reader._safe_subprocess.assert_not_called()


def test_cell_growth_invalid_data_and_existing_throttle(reader):
    reader._chain_cell_counts[BATTERY_CELL_SERVICES[0]] = 1
    reader._native.get_values_connected.side_effect = None
    reader._native.get_values_connected.return_value = {
        **CHAIN,
        "/Cell/3/Voltage": "3.43",
        "/Cell/4/Temperature": "inf",
        "/Soc": "nan",
        "/Info/AllowDischarge": None,
    }
    reader._poll_battery_cell_data_tree()
    entry = reader._cached_battery_cell_data[BATTERY_CELL_SERVICES[0]]
    assert entry["voltages"] == [3.41, 3.42]  # one-cell growth per poll
    assert entry["temps"] == [-2.5]
    assert entry["soc"] is None
    assert entry["allow_discharge"] is None
    reader._native.get_values_connected.reset_mock()
    reader._poll_battery_cell_data_tree()
    reader._native.get_values_connected.assert_not_called()
    reader._last_battery_cell_data_time = 0
    reader._poll_battery_cell_data_tree()
    assert entry["voltages"] == [3.41, 3.42, 3.43]


def test_failed_and_backed_off_reads_keep_cells_and_skip_io(reader):
    reader._poll_battery_cell_data_tree()
    old = deepcopy(reader._cached_battery_cell_data)
    reader._native.get_values_connected.side_effect = None
    reader._native.get_values_connected.return_value = None
    reader._safe_subprocess.side_effect = None
    reader._safe_subprocess.return_value = None
    for _ in range(reader.SERVICE_FAIL_THRESHOLD):
        reader._last_battery_cell_data_time = 0
        reader._poll_battery_cell_data_tree()
    assert reader._cached_battery_cell_data == old
    assert all(
        reader._service_backoff_until[service] > time.time() for service in BATTERY_CELL_SERVICES
    )
    reader._native.get_values_connected.reset_mock()
    reader._safe_subprocess.reset_mock()
    reader._last_battery_cell_data_time = 0
    reader._poll_battery_cell_data_tree()
    reader._native.get_values_connected.assert_not_called()
    reader._safe_subprocess.assert_not_called()


@pytest.mark.parametrize("value", ["0", "nan", "inf", "-inf", "invalid", None])
def test_inverter_power_zero_invalid_and_failed_reads(reader, value):
    reader._native.get_value_connected.return_value = value
    reader._safe_subprocess.side_effect = None
    reader._safe_subprocess.return_value = None
    reader._system_data["inv_power"] = 123
    reader._poll_inverter_power()
    assert reader._system_data["inv_power"] == (0 if value == "0" else 123)
    assert reader._consecutive_errors == (0 if value == "0" else 1)


@pytest.mark.parametrize("elapsed", [0.125, 0.25, 0.5, 0.75])
def test_reconciliation_native_and_cli_share_one_monotonic_budget(reader, elapsed):
    reconcile(reader)
    old_cells = deepcopy(reader._cached_battery_cell_data)
    reader._last_battery_cell_data_time = 0
    reader._safe_subprocess.reset_mock()
    clock = [100.0]

    def failed_native(*_args, **_kwargs):
        assert _kwargs["timeout"] == 0.25
        clock[0] += elapsed

    reader._native.get_values_connected.side_effect = failed_native
    reader._native.get_value_connected.side_effect = failed_native
    with patch("inverter_control.victron.time.monotonic", side_effect=lambda: clock[0]):
        reconcile(reader)
        grid_valid = reader.get_grid_status()["_grid_valid"]

    if elapsed < 0.5:
        assert reader._safe_subprocess.call_count == 6
        for call in reader._safe_subprocess.call_args_list:
            expected = 0.5 - elapsed
            assert call.kwargs["timeout"] == expected
        assert grid_valid
    else:
        reader._safe_subprocess.assert_not_called()
        assert not grid_valid
        assert reader._cached_battery_cell_data == old_cells
        assert reader._system_data["bp"] == -224
        assert reader._system_data["inv_power"] == -600
        assert reader._service_consecutive_fails == {
            **{service: 1 for service in (SYSTEM_SERVICE, SHUNT, VEBUS, *BATTERY_CELL_SERVICES)},
            METER: 0,
        }


def test_healthy_signals_retry_failed_grid_snapshot_early_without_repolling_every_group(reader):
    clock = [100.0]
    with patch("inverter_control.victron.time.monotonic", side_effect=lambda: clock[0]):
        reader._poll_system_data()
        assert reader.get_grid_status()["_grid_valid"]
        reader._native.get_values_connected.side_effect = None
        reader._native.get_values_connected.return_value = None
        reader._safe_subprocess.side_effect = None
        reader._safe_subprocess.return_value = None
        reader._poll_system_data()
        assert not reader.get_grid_status()["_grid_valid"]
        assert reader._next_grid_retry == 101.0
        # Ordinary signals cannot remove GridTelemetry's failed-snapshot latch.
        for path, value in SYSTEM.items():
            reader._apply_fast_value(SYSTEM_SERVICE, path, value)
        assert not reader.get_grid_status()["_grid_valid"]
        reader._signal_paths_subscribed = True
        reader._last_signal_reconcile = 100.0
        reader._native.get_values_connected.side_effect = lambda service, **_kwargs: SNAPSHOTS[
            service
        ]
        reader._native.get_values_connected.reset_mock()
        reader._safe_subprocess.reset_mock()
        with ExitStack() as stack:
            polls = {
                name: stack.enter_context(patch.object(reader, name))
                for name in (
                    "_poll_grid_backup",
                    "_poll_shunt_data",
                    "_poll_inverter_power",
                    "_reconcile_mppt_data",
                    "_reconcile_pv_power",
                    "_reconcile_acload_power",
                    "_poll_battery_chain_socs",
                    "_poll_inverter_state",
                    "_reconcile_groups_if_stale",
                    "_poll_battery_cell_data_tree",
                    "_poll_daily_yields",
                    "_poll_battery_daily_energy",
                )
            }
            clock[0] = 100.9
            reader._poll_all()
            reader._native.get_values_connected.assert_not_called()
            clock[0] = 101.0
            reader._poll_all()
            assert reader.get_grid_status()["_grid_valid"]
            assert reader._next_grid_retry is None
            assert reader._last_signal_reconcile == 100.0
            polls["_poll_shunt_data"].assert_not_called()
            polls["_poll_inverter_power"].assert_not_called()
            polls["_reconcile_acload_power"].assert_not_called()
        reader._safe_subprocess.assert_not_called()


def test_early_grid_retry_respects_service_backoff(reader):
    reader._service_backoff_until[SYSTEM_SERVICE] = time.time() + 10
    reader._grid_telemetry.unavailable("failed snapshot")
    reader._native.get_values_connected.reset_mock()
    reader._safe_subprocess.reset_mock()
    reader._poll_system_data()
    reader._native.get_values_connected.assert_not_called()
    reader._safe_subprocess.assert_not_called()
    assert not reader.get_grid_status()["_grid_valid"]
    assert reader._next_grid_retry is not None


@pytest.mark.parametrize("initial_read", ["seed", "sync"])
@pytest.mark.parametrize("failed_service", [SYSTEM_SERVICE, METER])
def test_initial_grid_read_failure_retries_while_signals_stay_healthy(
    reader, initial_read, failed_service
):
    clock = [100.0]
    reader._signal_paths_subscribed = True
    reader._last_signal_ok_monotonic = clock[0]
    reader._last_signal_reconcile = clock[0]

    def cli(cmd, **_kwargs):
        service = next(
            value.removeprefix("--dest=") for value in cmd if value.startswith("--dest=")
        )
        return None if service == failed_service else tree(SNAPSHOTS[service])

    reader._safe_subprocess.side_effect = cli
    reader._native.get_values.side_effect = lambda service: (
        None if service == failed_service else SNAPSHOTS[service]
    )
    reader._native.get_values_connected.side_effect = lambda service, **_kwargs: (
        None if service == failed_service else SNAPSHOTS[service]
    )
    reader._native.get_value.return_value = None
    with (
        patch("inverter_control.victron.time.monotonic", side_effect=lambda: clock[0]),
        patch.object(reader, "_fast_targets", return_value=[]),
    ):
        if initial_read == "seed":
            reader._seed_fast_values()
        else:
            reader.get_system_data()  # Empty cache takes the synchronous path.
        assert not reader.get_grid_status()["_grid_valid"]
        assert reader._next_grid_retry == 101.0
        assert reader._last_signal_reconcile == 100.0
        assert reader.is_signals_healthy()

        reader._native.get_values_connected.side_effect = lambda service, **_kwargs: SNAPSHOTS[
            service
        ]
        reader._native.get_values_connected.reset_mock()
        reader._safe_subprocess.reset_mock()
        with ExitStack() as stack:
            for method in (
                "_poll_grid_backup",
                "_poll_battery_chain_socs",
                "_poll_inverter_state",
                "_reconcile_groups_if_stale",
                "_poll_battery_cell_data_tree",
                "_poll_daily_yields",
                "_poll_battery_daily_energy",
            ):
                stack.enter_context(patch.object(reader, method))
            clock[0] = 100.9
            reader._poll_all()
            reader._native.get_values_connected.assert_not_called()
            clock[0] = 101.1
            reader._poll_all()
        assert reader.get_grid_status()["_grid_valid"]
        assert reader._next_grid_retry is None
        assert reader._last_signal_reconcile == 100.0
        assert [call.args[0] for call in reader._native.get_values_connected.call_args_list] == [
            SYSTEM_SERVICE,
            METER,
        ]
        reader._safe_subprocess.assert_not_called()
