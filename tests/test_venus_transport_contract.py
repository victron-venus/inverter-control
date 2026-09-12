"""Regressions for transport failures observed during the Venus OS audit."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control.dbus_native import NAME_OWNER_RULE, NativeDbusClient
from inverter_control.victron import VictronDBus


def test_owner_change_uses_dbus_argument_order_and_updates_sender():
    client = NativeDbusClient()
    callback = Mock()
    client.add_name_owner_handler(callback)
    assert NAME_OWNER_RULE in client._subscriptions
    service = "com.victronenergy.vebus.ttyUSB2"
    client._subscription_services.add(service)
    client._sender_service[":1.10"] = service
    client._handle_name_owner_changed(SimpleNamespace(body=[service, ":1.10", ":1.11"]))
    callback.assert_called_once_with(service, ":1.10", ":1.11")
    assert client._sender_service == {":1.11": service}


def test_remembered_subscription_is_not_healthy_during_disconnect():
    client = NativeDbusClient()
    rule = client._build_rule("com.victronenergy.system", "ItemsChanged", "/")
    client._subscriptions.add(rule)
    client._armed_subscriptions.add(rule)
    client._fail_until = time.time() + 60
    assert not client.subscribe_service_items("com.victronenergy.system")
    assert not client.is_connected()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(VictronDBus, "_discover_services", lambda _: None)
        victron = VictronDBus(test_mode=True)
    victron._native = client
    victron._signal_paths_subscribed = True
    assert not victron.is_signals_healthy()


def test_failed_match_replay_is_retried(monkeypatch):
    client = NativeDbusClient()
    rule = client._build_rule("com.victronenergy.system", "ItemsChanged", "/")
    client._subscriptions.add(rule)
    client._armed_subscriptions.add(rule)
    send = Mock(side_effect=ConnectionError("replay rejected"))
    monkeypatch.setattr(client, "_send_add_match", send)
    client._replay_subscriptions()
    assert rule not in client._armed_subscriptions
    monkeypatch.setattr(client, "_get_bus", object)
    send.side_effect = None
    assert client.subscribe_service_items("com.victronenergy.system")
    assert send.call_count == 2


def test_discovery_runs_on_poll_thread_not_signal_callback(monkeypatch):
    discover = Mock()
    monkeypatch.setattr(VictronDBus, "_discover_services", discover)
    victron = VictronDBus(test_mode=True)
    discover.reset_mock()
    victron._on_name_owner_changed("com.victronenergy.ev.ha", "", ":1.2")
    assert not victron._discovery_requested.is_set()
    victron._on_name_owner_changed("com.victronenergy.vebus.ttyUSB2", "", ":1.3")
    discover.assert_not_called()
    assert victron._check_rescan_needed()
    discover.assert_called_once()


@pytest.mark.parametrize(
    "reply,accepted",
    [
        ("method return reply_serial=1\n   int32 0\n", True),
        ("method return reply_serial=1\n   uint32 0\n", True),
        ("method return reply_serial=1\n   int32 1\n", False),
        ("", False),
        (None, False),
        ('   string "0"\n', False),
    ],
)
def test_cli_write_checks_busitem_acceptance(monkeypatch, reply, accepted):
    monkeypatch.setattr(VictronDBus, "_discover_services", lambda _: None)
    victron = VictronDBus(test_mode=True)
    run = Mock(return_value=reply)
    monkeypatch.setattr(victron, "_safe_subprocess", run)
    assert victron._dbus_set("com.victronenergy.test", "/Setpoint", 0) is accepted
    assert "--print-reply" in run.call_args.args[0]


def test_fallback_refreshes_inverter_power(monkeypatch):
    monkeypatch.setattr(VictronDBus, "_discover_services", lambda _: None)
    victron = VictronDBus(test_mode=True)
    calls = {}
    for name in (
        "_poll_system_data",
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
    ):
        calls[name] = Mock()
        monkeypatch.setattr(victron, name, calls[name])
    victron._poll_all()
    calls["_poll_inverter_power"].assert_called_once()


@pytest.mark.parametrize("body", [[], [1], [False], ["0"], [0, 0]])
def test_native_write_requires_explicit_zero_reply(monkeypatch, body):
    client = NativeDbusClient()
    monkeypatch.setattr(client, "call_busitem", Mock(return_value=SimpleNamespace(body=body)))
    assert not client.set_value("com.victronenergy.test", "/Setpoint", 0)


def test_empty_connection_result_enters_cooldown(monkeypatch):
    client = NativeDbusClient()
    connect = Mock(return_value=None)
    replay = Mock()
    monkeypatch.setattr(client, "_call_on_loop", connect)
    monkeypatch.setattr(client, "_ensure_loop", lambda: None)
    monkeypatch.setattr(client, "_replay_subscriptions", replay)
    for _ in range(3):
        assert client._get_bus() is None
    assert connect.call_count == 1
    assert client._fail_until > time.time()
    replay.assert_not_called()
