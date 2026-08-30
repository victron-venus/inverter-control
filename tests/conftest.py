"""Shared pytest fixtures for inverter-control tests.

Goals:
- Tests run on dev box (macOS, Linux) without Venus OS / D-Bus.
- Tests do NOT spawn a real control loop, MQTT broker, or web server.
- The `VictronDBus` singleton is reset around every test that touches it.
- The MQTT bridge is created with a stubbed `paho.mqtt.client` so the
  queue / publish path can be exercised without a broker.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Allow `from inverter_control import ...` from the test CWD
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def fresh_victron():
    """Yield a `FakeVictronDBus` that wraps a real `VictronDBus(test_mode=True)`.

    The underlying singleton is reset after the test so the next test starts
    clean.
    """
    from tests.stubs import FakeVictronDBus

    fake = FakeVictronDBus()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def reset_victron_singleton():
    """Reset the global `_victron` singleton around a test. Tests that build
    their own `VictronDBus(test_mode=True)` should use this to keep state out
    of neighbouring tests.
    """
    from inverter_control import victron

    victron.reset_victron_for_testing()
    yield
    victron.reset_victron_for_testing()


@pytest.fixture
def mqtt_bridge_stub():
    """Yield an `MQTTBridge` whose underlying `paho.mqtt.client.Client` is
    replaced with a `MagicMock`. Lets the publish-queue / handler code run
    without a real broker.
    """
    from inverter_control import mqtt_bridge

    mqtt_bridge._mqtt_bridge = None
    fake_client = MagicMock(name="paho.Client")
    with (
        __import__("unittest.mock", fromlist=["patch"]).patch(
            "inverter_control.mqtt_bridge.MQTT_AVAILABLE", True
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch(
            "inverter_control.mqtt_bridge.mqtt.Client", return_value=fake_client
        ),
    ):
        bridge = mqtt_bridge.MQTTBridge(broker="localhost", port=1883, prefix="inverter")
        bridge._client = fake_client
        bridge._connected = True
    try:
        yield bridge
    finally:
        mqtt_bridge._mqtt_bridge = None


@pytest.fixture(autouse=False)
def no_network(monkeypatch):
    """Block accidental real network calls during a test. Tests that
    intentionally need the network should opt out.
    """
    import socket

    def _block(_host, _port, *a, **kw):
        raise RuntimeError("network blocked: test must stub network access")

    monkeypatch.setattr(socket, "create_connection", _block, raising=False)
    yield
