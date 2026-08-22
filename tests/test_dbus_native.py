"""
Tests for the native dbus_fast client and its integration in VictronDBus.
"""

import asyncio
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from dbus_fast import Message, MessageType, Variant

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron
from inverter_control.dbus_native import (
    BUSITEM_INTERFACE,
    NativeDbusClient,
    _format_value,
)


class FakeBus:
    """Stand-in for dbus_fast MessageBus: returns one canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.messages = []
        self.call_count = 0

    async def call(self, message):
        self.call_count += 1
        self.messages.append(message)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def disconnect(self):
        pass


@pytest.fixture
def client():
    """NativeDbusClient with a running loop and an injected FakeBus."""
    native = NativeDbusClient()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    native._loop = loop
    yield native
    native.close()


def _return(body, signature="v"):
    # Replies need serial/reply_serial to pass dbus_fast message validation
    kwargs = {"body": body, "signature": signature} if body is not None else {}
    return Message(
        message_type=MessageType.METHOD_RETURN,
        serial=1,
        reply_serial=1,
        **kwargs,
    )


def _error():
    return Message(
        message_type=MessageType.ERROR,
        error_name="org.freedesktop.DBus.Error.UnknownObject",
        serial=1,
        reply_serial=1,
    )


class TestFormatValue:
    """_format_value mirrors dbus-send literal output shapes."""

    def test_bool(self):
        assert _format_value(True) == "1"
        assert _format_value(False) == "0"

    def test_numbers(self):
        assert _format_value(3) == "3"
        assert _format_value(-615) == "-615"
        assert _format_value(85.0) == "85.0"

    def test_string(self):
        assert _format_value("Europe/Moscow") == "Europe/Moscow"

    def test_unsupported(self):
        assert _format_value([1, 2]) is None
        assert _format_value(None) is None


class TestSetValue:
    """set_value marshals an explicitly signed variant and checks reply 0."""

    def test_success(self, client):
        bus = FakeBus(_return([0], "u"))
        client._bus = bus

        assert client.set_value("com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint", -615)

        msg = bus.messages[0]
        assert msg.destination == "com.victronenergy.vebus.ttyUSB2"
        assert msg.path == "/Hub4/L1/AcPowerSetpoint"
        assert msg.interface == BUSITEM_INTERFACE
        assert msg.member == "SetValue"
        assert msg.signature == "v"  # required, else Venus drops the argument
        assert msg.body[0].value == -615
        assert msg.body[0].signature == "n"

    def test_rejected_value(self, client):
        client._bus = FakeBus(_return([1], "u"))
        assert not client.set_value("com.victronenergy.test", "/path", 5)

    def test_unsupported_type(self, client):
        bus = FakeBus(_return([0], "u"))
        client._bus = bus
        assert not client.set_value("svc", "/path", 5, value_type="struct")
        assert bus.call_count == 0

    def test_type_codes(self, client):
        bus = FakeBus(_return([0], "u"))
        client._bus = bus
        for value_type, code in (("int16", "n"), ("int32", "i"), ("double", "d")):
            assert client.set_value("com.victronenergy.test", "/p", 1, value_type=value_type)
        assert [m.body[0].signature for m in bus.messages] == ["n", "i", "d"]


class TestGetValue:
    """get_value unwraps the reply variant into a literal string."""

    def test_variants(self, client):
        for raw, expected in (
            (Variant("u", 3), "3"),
            (Variant("d", 85.5), "85.5"),
            (Variant("b", True), "1"),
            (Variant("s", "Europe/Moscow"), "Europe/Moscow"),
        ):
            bus = FakeBus(_return([raw], "v"))
            client._bus = bus
            assert client.get_value("com.victronenergy.test", "/State") == expected
            msg = bus.messages[0]
            assert msg.member == "GetValue"
            assert not msg.signature  # no-arg call

    def test_error_reply(self, client):
        client._bus = FakeBus(_return(None))  # METHOD_RETURN with empty body
        assert client.get_value("com.victronenergy.test", "/x") is None


class TestFailureHandling:
    """Failures enter a reconnect cooldown instead of hammering the bus."""

    def test_error_enters_cooldown(self, client):
        bus = FakeBus(TimeoutError("no reply"))
        client._bus = bus
        assert client.get_value("com.victronenergy.test", "/x") is None
        assert client._fail_until > 0
        # Next call is skipped entirely while cooling down (_mark_failure
        # dropped the broken connection, so the bus is never called again)
        assert client.get_value("com.victronenergy.test", "/x") is None
        assert bus.call_count == 1

    def test_error_reply_no_cooldown(self, client):
        # Service answered with an error: bus is healthy, retry next call
        client._bus = FakeBus(_error())
        assert client.get_value("com.victronenergy.test", "/x") is None
        assert client._fail_until == 0.0


class TestVictronDBusIntegration:
    """VictronDBus routes Get/Set through the native client with CLI fallback."""

    def setup_method(self):
        victron.reset_victron_for_testing()

    def teardown_method(self):
        victron.reset_victron_for_testing()

    def test_test_mode_has_no_native(self):
        v = victron.get_victron(test_mode=True)
        assert v._native is None

    @patch("inverter_control.victron.USE_NATIVE_DBUS", True)
    @patch.object(victron.VictronDBus, "_start_background_polling")
    @patch.object(victron.VictronDBus, "_discover_services")
    def test_native_enabled_when_not_test_mode(self, _mock_disc, _mock_poll):
        v = victron.VictronDBus(test_mode=False)
        assert v._native is not None
        v._native.close()

    @patch("inverter_control.victron.USE_NATIVE_DBUS", False)
    @patch.object(victron.VictronDBus, "_start_background_polling")
    @patch.object(victron.VictronDBus, "_discover_services")
    def test_native_disabled_by_config(self, _mock_disc, _mock_poll):
        v = victron.VictronDBus(test_mode=False)
        assert v._native is None

    @patch.object(victron.VictronDBus, "_discover_services")
    @patch("inverter_control.victron.subprocess.run")
    def test_set_uses_native_without_subprocess(self, mock_run, _mock_disc):
        v = victron.get_victron(test_mode=True)
        v._vebus_service = "com.victronenergy.vebus.ttyUSB2"
        fake = MagicMock(return_value=True)
        v._native = fake

        assert v.set_grid_setpoint(-615)
        fake.set_value.assert_called_once_with(
            "com.victronenergy.vebus.ttyUSB2", "/Hub4/L1/AcPowerSetpoint", -615, "int16"
        )
        mock_run.assert_not_called()
        assert v._consecutive_errors == 0

    @patch("inverter_control.victron.subprocess.run")
    def test_set_falls_back_to_cli_on_native_failure(self, mock_run):
        v = victron.get_victron(test_mode=True)
        v._vebus_service = "com.victronenergy.vebus.ttyUSB2"
        v._native = MagicMock(return_value=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="   0\n")

        assert v._dbus_set(v._vebus_service, "/Hub4/L1/AcPowerSetpoint", -615)
        assert mock_run.called

    @patch.object(victron.VictronDBus, "_discover_services")
    @patch("inverter_control.victron.subprocess.run")
    def test_get_uses_native_without_subprocess(self, mock_run, _mock_disc):
        v = victron.get_victron(test_mode=True)
        v._native = MagicMock()
        v._native.get_value.return_value = "3"

        assert v._dbus_get("com.victronenergy.vebus.ttyUSB2", "/State") == "3"
        mock_run.assert_not_called()

    @patch("inverter_control.victron.subprocess.run")
    def test_get_falls_back_to_cli_on_native_miss(self, mock_run):
        v = victron.get_victron(test_mode=True)
        v._native = MagicMock()
        v._native.get_value.return_value = None
        mock_run.return_value = MagicMock(returncode=0, stdout="variant int32 9\n")

        assert v._dbus_get("svc", "/x") == "9"
        assert mock_run.called
