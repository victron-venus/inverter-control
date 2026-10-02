"""Explicit ESS selections use fake D-Bus only; never contact an inverter."""

from unittest.mock import MagicMock, call, patch

import pytest
from test_main import _make_controller

from inverter_control import victron
from inverter_control.ess_modes import (
    BATTERY_LIFE_PATH,
    MODES,
    EssSelection,
    selected_mode,
    validate_selection,
)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"mode": "off"},
        {"mode": "off", "request_id": ""},
        {"mode": "off", "request_id": "a", "extra": 1},
        {"mode": "toggle", "request_id": "a"},
        {"mode": False, "request_id": "a"},
        {"mode": "off", "request_id": "../x"},
        {"mode": "off", "request_id": "a" * 129},
    ],
)
def test_malformed_selection_never_calls_writer(payload):
    writer = MagicMock()
    with pytest.raises(ValueError):
        EssSelection().apply(payload, writer)
    writer.assert_not_called()


@pytest.mark.parametrize("mode", MODES)
def test_valid_selection(mode):
    assert validate_selection({"mode": mode, "request_id": "test-1"}) == (mode, "test-1")


@pytest.mark.parametrize(
    "hub,bl,switch,expected",
    [
        (3, 9, 3, "external_control"),
        (3, 9, 4, "off"),
        (1, 1, 3, "optimized_with_battery_life"),
        (2, 8, 3, "optimized_with_battery_life"),
        (1, 9, 3, "keep_batteries_charged"),
        (2, 10, 3, "optimized_without_battery_life"),
        (1, 11, 3, "optimized_without_battery_life"),
        (1, 12, 3, "optimized_without_battery_life"),
        (1, 0, 3, "optimized_without_battery_life"),
        (None, None, 3, "on"),
        (3, 9, None, None),
        (3, 9, 1, None),
        (3, 9, 2, None),
    ],
)
def test_observed_mode(hub, bl, switch, expected):
    assert selected_mode(hub, bl, switch) == expected


@pytest.fixture
def device():
    with patch("inverter_control.victron.subprocess.run"):
        v = victron.VictronDBus(test_mode=True)
    v._vebus_service = "com.victronenergy.vebus.test"
    v._dbus_set = MagicMock(return_value=True)
    v._dbus_get = MagicMock(
        side_effect=lambda service, path: "1" if path == "/ModeIsAdjustable" else "3"
    )
    return v


@pytest.mark.parametrize("mode,switch", [("off", 4), ("on", 3)])
def test_power_preserves_profile(device, mode, switch):
    device.select_ess_mode(mode)
    device._dbus_set.assert_called_once_with(device._vebus_service, "/Mode", switch, "int32")


@pytest.mark.parametrize(
    "mode,value",
    [
        ("optimized_with_battery_life", 1),
        ("optimized_without_battery_life", 10),
        ("keep_batteries_charged", 9),
    ],
)
@pytest.mark.parametrize("old_hub,new_hub", [("1", 1), ("2", 2), ("3", 1)])
def test_profile_preserves_power_and_phase_compensation(device, mode, value, old_hub, new_hub):
    device._dbus_get.return_value = old_hub
    device._dbus_get.side_effect = None
    device.select_ess_mode(mode)
    assert device._dbus_set.call_args_list == [
        call(victron.SETTINGS_SERVICE, BATTERY_LIFE_PATH, value, "int32"),
        call(victron.SETTINGS_SERVICE, victron.HUB4_MODE_PATH, new_hub, "int32"),
    ]


def test_external_preserves_profile_and_power(device):
    device.select_ess_mode("external_control")
    device._dbus_set.assert_called_once_with(
        victron.SETTINGS_SERVICE, victron.HUB4_MODE_PATH, 3, "int32"
    )


@pytest.mark.parametrize("mode", MODES)
def test_missing_settings_or_switch_rejects_without_writes(device, mode):
    device._dbus_get.side_effect = None
    device._dbus_get.return_value = None
    with pytest.raises(RuntimeError):
        device.select_ess_mode(mode)
    device._dbus_set.assert_not_called()


def test_profile_failure_stops_before_hub_write_and_invalidates_cache(device):
    device._dbus_set.return_value = False
    device._ess_mode_cache = {"selected": "external_control"}
    with pytest.raises(RuntimeError, match="unconfirmed"):
        device.select_ess_mode("optimized_with_battery_life")
    assert device._dbus_set.call_count == 1
    assert device._ess_mode_cache is None


def test_redelivery_cannot_overwrite_later_choice():
    selection = EssSelection()
    writer = MagicMock()
    a = {"mode": "off", "request_id": "a"}
    b = {"mode": "on", "request_id": "b"}
    selection.apply(a, writer)
    selection.apply(b, writer)
    selection.apply(a, writer)
    assert writer.call_args_list == [call("off"), call("on")]
    assert selection.snapshot()["request_id"] == "b"


def test_failed_selection_is_correlated_and_not_retried():
    selection = EssSelection()
    writer = MagicMock(side_effect=RuntimeError("unconfirmed"))
    request = {"mode": "off", "request_id": "a"}
    selection.apply(request, writer)
    selection.apply(request, writer)
    assert writer.call_count == 1
    assert selection.snapshot() == {
        "selection_supported": True,
        "request_id": "a",
        "error": "unconfirmed",
    }


def test_controller_dry_run_never_writes():
    controller, _, _, _ = _make_controller()
    controller.dry_run = True
    controller.select_ess_mode({"mode": "off", "request_id": "a"})
    controller.victron.select_ess_mode.assert_not_called()
    assert "DRY" in controller.ess_selection.snapshot()["error"]


def test_controller_receipt_keeps_observed_mode_not_requested_mode():
    controller, _, _, _ = _make_controller()
    controller.dry_run = False
    controller.victron.get_ess_mode.return_value = {"selected": "external_control"}
    controller.select_ess_mode({"mode": "off", "request_id": "a"})
    controller.victron.select_ess_mode.assert_called_once_with("off")
    assert controller.get_state_for_mqtt()["ess_mode"] == {
        "selected": "external_control",
        "selection_supported": True,
        "request_id": "a",
        "error": None,
    }


def test_retained_selection_is_never_dispatched(mqtt_bridge_stub):
    import json

    callback = MagicMock()
    bridge = mqtt_bridge_stub
    bridge.register_callback("set_ess_mode", callback)
    msg = MagicMock(topic="inverter/cmd/set_ess_mode", retain=True)
    msg.payload = json.dumps({"mode": "off", "request_id": "a"}).encode()
    bridge._on_message(None, None, msg)
    callback.assert_not_called()


def test_cached_mode_read_does_not_wait_for_settings_reconciliation(device):
    import threading

    device._ess_mode_cache = {"selected": "external_control"}
    device._ess_mode_cache_time = 1.0
    completed = threading.Event()
    with device._ess_selection_lock:
        reader = threading.Thread(target=lambda: (device.get_ess_mode(), completed.set()))
        reader.start()
        finished_while_locked = completed.wait(1)
    reader.join(1)
    assert finished_while_locked


def test_observation_cannot_pair_old_state_with_new_receipt():
    import threading

    selection = EssSelection()
    entered = threading.Event()
    release = threading.Event()
    result = []
    observed = {"selected": "external_control"}

    def write(mode):
        entered.set()
        assert release.wait(2)
        observed["selected"] = mode

    writer = threading.Thread(
        target=lambda: selection.apply({"mode": "off", "request_id": "a"}, write)
    )
    writer.start()
    assert entered.wait(1)
    reader = threading.Thread(
        target=lambda: result.append(selection.observe(lambda: dict(observed)))
    )
    reader.start()
    release.set()
    writer.join(2)
    reader.join(2)
    assert result == [
        {"selected": "off", "selection_supported": True, "request_id": "a", "error": None}
    ]
