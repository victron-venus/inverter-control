"""State serialization must preserve wire bytes without cloning finite payloads."""

import json
import queue
from unittest.mock import Mock

import pytest

from inverter_control.mqtt_bridge import MQTTBridge, SafeEncoder


@pytest.fixture
def queued_bridge():
    # Exercise the actual publication method without clients, workers or sockets.
    bridge = MQTTBridge.__new__(MQTTBridge)
    bridge.prefix = "inverter"
    bridge._connected = True
    bridge._publish_queue = queue.Queue(maxsize=100)
    bridge._ensure_publish_thread = Mock()
    return bridge


@pytest.mark.parametrize(
    "state",
    [
        {"perf": {"cpu_percent": 45.1}, "flags": [False, True], "optional": None},
        {"nested": {"tuple": (1, -0.0, "Unicode €")}, 2.5: "key", None: "null key"},
        {"value": float("nan")},
        {"nested": (float("inf"), {"minus": -float("inf")})},
        {float("nan"): "NaN key", float("inf"): "Inf key", "value": 0},
    ],
)
def test_state_encoding_keeps_exact_legacy_wire_bytes(queued_bridge, state):
    expected = json.dumps(state, cls=SafeEncoder)
    queued_bridge.publish_state(state)
    assert queued_bridge._publish_queue.get_nowait() == (
        "inverter/state",
        expected,
        0,
        True,
    )


def test_finite_state_does_not_walk_the_sanitizer(queued_bridge, monkeypatch):
    sanitize = Mock(side_effect=AssertionError("finite state must skip recursive copy"))
    monkeypatch.setattr(SafeEncoder, "_sanitize", sanitize)
    state = {"ui_config": {"rates": [[0.2] * 7 for _ in range(48)]}}
    queued_bridge.publish_state(state)
    assert queued_bridge._publish_queue.get_nowait()[1] == json.dumps(state)
    sanitize.assert_not_called()


def test_nonfinite_fallback_does_not_mutate_state_or_encoder_defaults(queued_bridge):
    values = [float("nan"), -float("inf")]
    state = {"values": values}
    queued_bridge.publish_state(state)
    assert queued_bridge._publish_queue.get_nowait()[1] == '{"values": [null, null]}'
    assert state["values"] is values
    assert str(values[0]) == "nan" and values[1] == -float("inf")
    queued_bridge.publish_state({"value": -0.0})
    assert queued_bridge._publish_queue.get_nowait()[1] == '{"value": -0.0}'
    # This public encoder API remains unchanged, including custom defaults.
    assert SafeEncoder().allow_nan is True

    class CustomEncoder(SafeEncoder):
        def default(self, value):
            raise ValueError("custom_default_error")

    with pytest.raises(ValueError, match="^custom_default_error$"):
        json.dumps({"value": object()}, cls=CustomEncoder)


@pytest.mark.parametrize("kind", ["unsupported", "circular"])
def test_unencodable_state_keeps_existing_refusal(queued_bridge, kind):
    state = {"value": object()}
    error = TypeError
    if kind == "circular":
        state = {}
        state["self"] = state
        error = RecursionError
    with pytest.raises(error):
        json.dumps(state, cls=SafeEncoder)
    queued_bridge.publish_state(state)
    assert queued_bridge._publish_queue.empty()
    queued_bridge._ensure_publish_thread.assert_not_called()
