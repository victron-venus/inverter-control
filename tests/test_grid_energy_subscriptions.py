"""Real subscription bookkeeping: timezone repair cannot affect control health."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control.dbus_native import NativeDbusClient
from inverter_control.victron import SETTINGS_SERVICE, SYSTEM_SERVICE, TIME_ZONE_PATH


def client():
    native = NativeDbusClient()
    native._bus = SimpleNamespace(connected=True)
    native._send_add_match = Mock()
    return native


def timezone_rule(rule):
    return SETTINGS_SERVICE in rule


def reject_timezone(rule):
    if timezone_rule(rule):
        raise OSError("optional AddMatch failed")


def test_optional_replay_failure_preserves_required_control_health():
    native = client()
    native.subscribe_service_items(SYSTEM_SERVICE)
    native.subscribe_service_items(SETTINGS_SERVICE, required=False)
    native.subscribe_busitem(SETTINGS_SERVICE, TIME_ZONE_PATH, required=False)
    assert native.subscriptions_healthy()
    assert native.optional_subscriptions_healthy()
    native._send_add_match.reset_mock()
    native._send_add_match.side_effect = reject_timezone
    native._replay_subscriptions()
    assert all(not timezone_rule(call.args[0]) for call in native._send_add_match.call_args_list)
    assert native.subscriptions_healthy()
    assert not native.optional_subscriptions_healthy()
    assert all(timezone_rule(rule) for rule in native._subscriptions - native._armed_subscriptions)
    native._send_add_match.side_effect = None
    assert native.subscribe_service_items(SETTINGS_SERVICE, required=False)
    assert native.subscribe_busitem(SETTINGS_SERVICE, TIME_ZONE_PATH, required=False)
    assert native.optional_subscriptions_healthy()


def test_failed_initial_optional_match_is_remembered_for_reconnect():
    native = client()
    native.subscribe_service_items(SYSTEM_SERVICE)
    native._send_add_match.side_effect = reject_timezone
    assert not native.subscribe_service_items(SETTINGS_SERVICE, required=False)
    assert native.subscriptions_healthy()
    assert not native.optional_subscriptions_healthy()
    native._send_add_match.side_effect = None
    native._replay_subscriptions()
    assert not native.optional_subscriptions_healthy()
    assert native.subscribe_service_items(SETTINGS_SERVICE, required=False)
    assert native.optional_subscriptions_healthy()


@pytest.mark.parametrize("optional_first", [False, True])
def test_optional_caller_cannot_downgrade_existing_required_rule(optional_first):
    native = client()
    if optional_first:
        native.subscribe_service_items(SYSTEM_SERVICE, required=False)
    native.subscribe_service_items(SYSTEM_SERVICE)
    native.subscribe_service_items(SYSTEM_SERVICE, required=False)
    native._armed_subscriptions.clear()
    assert not native.subscriptions_healthy()
