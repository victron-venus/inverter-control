"""Daemon-owned inverter flags and their MQTT dashboard presentation.

Home Assistant and desktop are clients of this contract. These keys are not
HA entity IDs; ``input_boolean.<key>`` is only a legacy command alias.
"""

from typing import NamedTuple


class ControlFlag(NamedTuple):
    key: str
    label: str


CONTROL_FLAGS = (
    ControlFlag("only_charging", "ONLY CHARGING"),
    ControlFlag("no_feed", "NO FEED"),
    ControlFlag("house_support", "HOUSE SUPPORT"),
    ControlFlag("charge_battery", "CHARGE BATTERY"),
    ControlFlag("do_not_supply_charger", "DO NOT SUPPLY EV"),
    ControlFlag("set_limit_to_ev_charger", "LIMIT TO EV"),
    ControlFlag("minimize_charging", "MINIMIZE CHARGING"),
)
CONTROL_FLAG_KEYS = tuple(flag.key for flag in CONTROL_FLAGS)


def get_control_toggle_config() -> list[dict[str, str]]:
    """Return the existing desktop ``header_toggles`` wire schema."""
    return [{"id": flag.key, "label": flag.label, "entity": flag.key} for flag in CONTROL_FLAGS]
