"""Dashboard contract tests: MQTT topic + payload shapes.

The dashboards (inverter-dashboard, inverter-dashboard-go, inverter-dashboard-vue)
and inverter-desktop subscribe to the MQTT topics inverter-control publishes on.
A field rename or a topic suffix change in `mqtt_bridge` silently breaks every
dashboard; this file pins the contract so we catch that at PR time.

Tests run with a stubbed `paho.mqtt.client` — no broker, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from inverter_control import mqtt_bridge

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _new_bridge(prefix: str = "inverter") -> mqtt_bridge.MQTTBridge:
    """Build a connected MQTTBridge whose underlying client is a MagicMock.

    Returns the bridge with its internal `_publish_queue` already drained once
    so the first call into `publish_state` is observable.
    """
    mqtt_bridge._mqtt_bridge = None
    fake_client = MagicMock(name="paho.Client")
    with (
        patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True),
        patch("inverter_control.mqtt_bridge.mqtt.Client", return_value=fake_client),
    ):
        bridge = mqtt_bridge.MQTTBridge(broker="localhost", port=1883, prefix=prefix)
        bridge._client = fake_client
        bridge._connected = True
    return bridge


def _drain_publish_queue(bridge: mqtt_bridge.MQTTBridge) -> list[tuple[str, str, int, bool]]:
    """Pull every queued (topic, payload, qos, retain) tuple out of the bridge
    without spawning the publish thread, so tests can assert on what was queued.
    """
    out: list[tuple[str, str, int, bool]] = []
    while not bridge._publish_queue.empty():
        out.append(bridge._publish_queue.get_nowait())
    return out


# ---------------------------------------------------------------------------
# topic shape
# ---------------------------------------------------------------------------


class TestMQTTTopicShape:
    """`inverter-control` publishes on these topics. Dashboards subscribe to
    them by string literal. Renames break dashboards silently — pin them here.
    """

    def test_publish_state_topic(self):
        bridge = _new_bridge(prefix="inverter")
        bridge.publish_state({"g1": 1})
        queued = _drain_publish_queue(bridge)
        assert len(queued) == 1
        topic, _, qos, retain = queued[0]
        assert (
            topic == "inverter/state"
        ), f"dashboards subscribe to 'inverter/state' literally; got {topic!r}"
        assert retain is True, "state must be retained so late subscribers see latest"
        assert qos == 0

    def test_publish_console_topic(self):
        bridge = _new_bridge()
        bridge.publish_console("hello")
        queued = _drain_publish_queue(bridge)
        assert len(queued) == 1
        topic, _, _, retain = queued[0]
        assert topic == "inverter/console"
        assert retain is False, "console stream is firehose; do not retain"

    def test_publish_notifications_topic(self):
        bridge = _new_bridge()
        bridge.publish_notification(
            notification_id="abc",
            level="info",
            title="t",
            body="m",
        )
        queued = _drain_publish_queue(bridge)
        assert len(queued) == 1
        topic, _, _, retain = queued[0]
        assert topic == "inverter/notifications"
        assert retain is False

    def test_subscribe_topics(self):
        """`_on_connect` (triggered on broker connect) subscribes to cmd/# and alert/ack.
        The dashboard and desktop send commands on cmd/#; the inverter side acks on alert/ack.
        """
        bridge = _new_bridge()
        # _on_connect is called by paho-mqtt when the client connects to the broker.
        # Trigger it directly so we can observe what subscriptions were registered.
        bridge._on_connect(bridge._client, None, {}, 0)
        subs = [c.args[0] for c in bridge._client.subscribe.call_args_list]
        assert "inverter/cmd/#" in subs
        assert "inverter/alert/ack" in subs
        assert "solar/forecast" in subs

    def test_state_topic_payload_is_json(self):
        bridge = _new_bridge()
        bridge.publish_state({"g1": 1, "battery_soc": 85})
        queued = _drain_publish_queue(bridge)
        topic, payload, _, _ = queued[0]
        assert topic == "inverter/state"
        # Must be valid JSON; dashboards do json.loads(payload) on every message.
        data = json.loads(payload)
        assert isinstance(data, dict)
        assert data["g1"] == 1
        assert data["battery_soc"] == 85

    def test_state_topic_payload_safe_encoder_nan(self):
        """`SafeEncoder` must turn NaN/Inf into null — HA/Dashboards choke on
        literal NaN in JSON. Pin the behaviour.
        """
        bridge = _new_bridge()
        bridge.publish_state({"bp": float("nan"), "bc": float("inf"), "ok": 1.0})
        _, payload, _, _ = _drain_publish_queue(bridge)[0]
        parsed = json.loads(payload)
        assert parsed["bp"] is None
        assert parsed["bc"] is None
        assert parsed["ok"] == 1.0

    def test_custom_prefix_propagates(self):
        """`prefix='mybox'` should rename every topic, not just state."""
        bridge = _new_bridge(prefix="mybox")
        # drain after each publish: publish_notification spins up the
        # background publish thread which would otherwise drain the state
        # publish out from under us before we can assert.
        bridge.publish_state({"x": 1})
        state_topics = [q[0] for q in _drain_publish_queue(bridge)]
        bridge.publish_notification("n1", "info", "t", "m")
        notif_topics = [q[0] for q in _drain_publish_queue(bridge)]
        assert "mybox/state" in state_topics
        assert "mybox/notifications" in notif_topics


# ---------------------------------------------------------------------------
# cmd/toggle payload shape (HA + desktop write to this)
# ---------------------------------------------------------------------------


class TestCommandTopics:
    """The control-flag toggles the dashboard and HA switch entity use.
    Pinned so a payload rename here does not silently break HA + Desktop.
    """

    def test_cmd_toggle_handler_accepts_entity_state_payload(self):
        """`inverter/cmd/toggle` payload: {"entity": <key>, "state": "on"|"off"}.
        Registered callbacks receive the parsed dict: {"entity": ..., "state": "on"|"off"}.
        """
        bridge = _new_bridge()
        received: list[dict] = []
        bridge.register_callback("toggle", lambda p: received.append(p))

        msg = MagicMock()
        msg.topic = "inverter/cmd/toggle"
        msg.payload = json.dumps({"entity": "only_charging", "state": "on"}).encode()
        bridge._on_message(bridge._client, None, msg)
        assert received == [{"entity": "only_charging", "state": "on"}]

        msg2 = MagicMock()
        msg2.topic = "inverter/cmd/toggle"
        msg2.payload = json.dumps({"entity": "no_feed", "state": "off"}).encode()
        bridge._on_message(bridge._client, None, msg2)
        assert received[-1] == {"entity": "no_feed", "state": "off"}

    def test_cmd_toggle_rejects_unknown_entity_silently(self):
        """Bad entities must NOT raise — dashboards send a lot of entities and
        we cannot let one typo crash the message loop.
        """
        bridge = _new_bridge()
        bridge.set_boolean = MagicMock()  # type: ignore[attr-defined]

        msg = MagicMock()
        msg.topic = "inverter/cmd/toggle"
        msg.payload = json.dumps({"entity": "nonexistent", "state": "on"}).encode()
        # Should not raise
        bridge._on_message(bridge._client, None, msg)
        bridge.set_boolean.assert_not_called()


# ---------------------------------------------------------------------------
# state payload keys the dashboards depend on
# ---------------------------------------------------------------------------


REQUIRED_STATE_KEYS = {
    # Aggregates the dashboards plot
    "g1",
    "g2",
    "gt",
    "t1",
    "t2",
    "tt",
    "pv_total",
    "solar_total",
    "mppt_total",
    "pv_inverter_total",
    "battery_soc",
    "battery_power",
    "battery_voltage",
    "battery_current",
    # Control loop / display
    "setpoint",
    "filtered_gt",
    "inverter_state",
    "ess_mode",
    "booleans",
    "dry_run",
    "limits",
    "loop_interval",
    "version",
    "uptime",
}


def test_state_payload_contract_keys():
    """Drive `MQTTBridge.publish_state` with a hand-built payload that
    contains every key the dashboards read. The bridge must preserve them
    byte-for-byte (after JSON encoding).
    """
    bridge = _new_bridge()
    full_state = {key: i for i, key in enumerate(sorted(REQUIRED_STATE_KEYS))}
    # `booleans` is a sub-dict in the real shape; add it explicitly
    full_state["booleans"] = {
        "only_charging": False,
        "no_feed": False,
        "house_support": False,
        "charge_battery": False,
        "do_not_supply_charger": False,
        "set_limit_to_ev_charger": False,
        "minimize_charging": False,
    }
    full_state["limits"] = {"min": -2300, "max": 2250}

    bridge.publish_state(full_state)
    _, payload, _, _ = _drain_publish_queue(bridge)[0]
    parsed = json.loads(payload)

    missing = REQUIRED_STATE_KEYS - parsed.keys()
    assert not missing, f"dashboards need these keys in inverter/state, missing: {missing}"


if __name__ == "__main__":  # pragma: no cover - convenience runner
    sys.exit(pytest.main([__file__, "-v"]))
