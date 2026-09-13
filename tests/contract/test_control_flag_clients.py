"""Desktop metadata and HA MQTT switches share the daemon control contract.

Exercise the real command handler and retained publish queue with no HA object,
broker, D-Bus service, or control loop. Render the shipped HA templates to catch
command/state mismatches in mqtt.yaml.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests"))

from test_main import _make_controller

import main
from inverter_control.config import get_ui_config
from inverter_control.control_flags import CONTROL_FLAG_KEYS


@pytest.fixture
def controller_without_ha(mqtt_bridge_stub):
    controller, _, _, _ = _make_controller(ui_config=get_ui_config())
    # Any accidental HA property access or API call now fails the test.
    controller.ha = object()
    bridge = mqtt_bridge_stub
    with (
        patch("inverter_control.controller.ENABLE_HA", False),
        patch.object(bridge, "_ensure_publish_thread"),
        patch("inverter_control.mqtt_bridge.get_mqtt_bridge", return_value=bridge),
    ):
        bridge.register_callback("toggle", lambda payload: main._handle_toggle(controller, payload))
        yield controller, bridge


def test_metadata_and_retained_state_are_available_before_telemetry_without_ha(
    controller_without_ha,
):
    controller, bridge = controller_without_ha
    assert controller.state == {}
    assert controller._get_ha_status() == {"ha_connected": False, "ha_uptime": 0}
    controller._load_control_flags()
    bridge.publish_state(controller.get_state_for_mqtt())
    topic, payload, qos, retained = bridge._publish_queue.get_nowait()
    state = json.loads(payload)
    assert (topic, qos, retained) == ("inverter/state", 0, True)
    assert state["booleans"] == dict.fromkeys(CONTROL_FLAG_KEYS, False)

    toggles = state["ui_config"]["header_toggles"]
    assert len(toggles) == len(CONTROL_FLAG_KEYS) == 7
    assert [toggle["id"] for toggle in toggles] == list(CONTROL_FLAG_KEYS)
    assert [toggle["entity"] for toggle in toggles] == list(CONTROL_FLAG_KEYS)
    assert all(toggle["label"] for toggle in toggles)

    # Desktop sends each advertised bare entity; the daemon confirms via MQTT.
    for toggle in toggles:
        message = MagicMock(
            topic="inverter/cmd/toggle",
            payload=json.dumps({"entity": toggle["entity"], "state": "on"}).encode(),
        )
        bridge._on_message(None, None, message)
        _, payload, _, retained = bridge._publish_queue.get_nowait()
        state = json.loads(payload)
        assert retained is True
        assert state["booleans"][toggle["id"]] is True
        assert state["ui_config"]["header_toggles"] == toggles


def test_ha_switch_commands_and_confirmed_state_round_trip_without_ha(controller_without_ha):
    _, bridge = controller_without_ha
    mqtt_config = yaml.safe_load((_ROOT / "mqtt.yaml").read_text())
    switches = mqtt_config["switch"]
    assert len(switches) == len(CONTROL_FLAG_KEYS)
    assert {json.loads(switch["payload_on"])["entity"] for switch in switches} == set(
        CONTROL_FLAG_KEYS
    )
    templates = Environment(undefined=StrictUndefined)

    for switch in switches:
        for action, expected in (("on", True), ("off", False)):
            command = json.loads(switch[f"payload_{action}"])
            assert command["state"] == action
            bridge._on_message(
                None,
                None,
                MagicMock(
                    topic=switch["command_topic"],
                    payload=switch[f"payload_{action}"].encode(),
                ),
            )
            topic, payload, _, retained = bridge._publish_queue.get_nowait()
            state = json.loads(payload)
            assert state["booleans"][command["entity"]] is expected
            assert topic == switch["state_topic"] == "inverter/state"
            assert retained is True
            rendered = templates.from_string(switch["value_template"]).render(value_json=state)
            # HA uses payload_on/off as fallback when state_on/off is absent.
            assert rendered == switch.get(f"state_{action}", switch[f"payload_{action}"])
            assert not bridge._publish_queue.qsize()


def test_legacy_python_api_still_uses_canonical_control_flags(controller_without_ha):
    controller, _ = controller_without_ha
    controller.set_boolean("only_charging", True)
    assert controller.get_control_flag("only_charging") is True
    controller.set_control_flag("only_charging", False)
    assert controller.get_boolean("only_charging") is False
