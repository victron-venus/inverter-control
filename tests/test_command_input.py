"""Untrusted MQTT values and HA entity paths are checked before side effects."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import main
from inverter_control.controller import InverterController
from inverter_control.homeassistant import HA_URL, HomeAssistantClient


@pytest.mark.parametrize("value", [True, False, 1.2, float("nan"), float("inf"), "1.2", None, []])
def test_invalid_setpoint_never_reaches_controller(value):
    controller = MagicMock()
    main._mqtt_safe_setpoint(controller, {"value": value})
    controller.set_manual_setpoint.assert_not_called()


@pytest.mark.parametrize("value,expected", [(42, 42), (-5, -5), (3.0, 3), ("-2300", -2300)])
def test_integer_setpoints_remain_compatible(value, expected):
    controller = MagicMock()
    main._mqtt_safe_setpoint(controller, {"value": value})
    controller.set_manual_setpoint.assert_called_once_with(expected)


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), "NaN", "Infinity", None, 10**400]
)
def test_invalid_interval_never_reaches_controller(value):
    controller = MagicMock()
    main._mqtt_safe_loop_interval(controller, {"interval": value})
    controller.set_loop_interval.assert_not_called()


@pytest.mark.parametrize("value,expected", [("0.33", 0.33), (1, 1.0), (0.01, 0.01), (10, 10.0)])
def test_finite_intervals_reach_existing_clamping(value, expected):
    controller = MagicMock()
    main._mqtt_safe_loop_interval(controller, {"interval": value})
    controller.set_loop_interval.assert_called_once_with(expected)


def test_direct_controller_calls_preserve_state_on_invalid_values():
    controller = object.__new__(InverterController)
    controller.loop_interval = 0.33
    controller.manual_setpoint = 500
    for invalid in (True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            controller.set_loop_interval(invalid)
        with pytest.raises(TypeError):
            controller.set_manual_setpoint(invalid)
    assert controller.loop_interval == 0.33
    assert controller.manual_setpoint == 500


def test_existing_numeric_limits_are_still_applied():
    controller = object.__new__(InverterController)
    controller._watchdog = SimpleNamespace(_lock=threading.RLock())
    controller._trim_mode_generation = 0
    controller.power_limit_min, controller.power_limit_max = -2300, 2250
    assert controller.set_loop_interval(0.01) == 0.1
    assert controller.set_loop_interval(10) == 5.0
    controller.set_manual_setpoint(5000)
    assert controller.manual_setpoint == 2250


@pytest.fixture
def ha_client():
    client = object.__new__(HomeAssistantClient)
    client._session = MagicMock()
    client._session.post.return_value.status_code = 200
    client._session.get.return_value.status_code = 200
    client._session.get.return_value.json.return_value = {"state": "on"}
    return client


@pytest.mark.parametrize(
    "entity",
    [
        None,
        [],
        123,
        "",
        "switch",
        "switch../admin",
        "switch.test/../../config",
        "switch.%2fadmin",
        "switch.test?x=1",
        "switch.test\n",
        "switch.test#fragment",
        "switch." + "a" * 249,
    ],
)
def test_invalid_entity_never_sends_http(ha_client, entity):
    for method in (
        ha_client.toggle_entity,
        ha_client.press_button,
        ha_client.turn_on,
        ha_client.turn_off,
    ):
        assert method(entity) is False
    assert ha_client._get_state(entity) is None
    ha_client._session.post.assert_not_called()
    ha_client._session.get.assert_not_called()


@pytest.mark.parametrize(
    "entity",
    [
        "switch.kitchen_2",
        "input_boolean.no_feed",
        "button.restart",
        "light.room_1",
        "sensor." + "a" * 248,
    ],
)
def test_valid_entity_keeps_exact_url_and_target(ha_client, entity):
    assert ha_client.toggle_entity(entity)
    arguments, keywords = ha_client._session.post.call_args
    assert arguments == (f"{HA_URL}/api/services/{entity.partition('.')[0]}/toggle",)
    assert keywords["json"] == {"entity_id": entity}
    assert ha_client._get_state(entity) == "on"
    assert ha_client._session.get.call_args.args == (f"{HA_URL}/api/states/{entity}",)


@pytest.mark.parametrize(
    "domain,action",
    [
        ("../../config", "toggle"),
        ("light", "toggle"),
        ("switch", "../../admin"),
        ("switch", "delete"),
    ],
)
def test_direct_service_path_validation(ha_client, domain, action):
    assert not ha_client._call_service(domain, action, "switch.test")
    ha_client._session.post.assert_not_called()
