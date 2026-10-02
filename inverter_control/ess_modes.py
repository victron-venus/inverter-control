"""Explicit ESS choices; separate from the legacy two-state toggle command."""

import re
import threading
from collections import OrderedDict

MODES = (
    "off",
    "on",
    "optimized_with_battery_life",
    "optimized_without_battery_life",
    "keep_batteries_charged",
    "external_control",
)
BATTERY_LIFE_PATH = "/Settings/CGwacs/BatteryLife/State"


def validate_selection(payload):
    if not isinstance(payload, dict) or set(payload) != {"mode", "request_id"}:
        raise ValueError("Expected mode and request_id")
    mode, request_id = payload["mode"], payload["request_id"]
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("Unknown ESS mode")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise ValueError("Invalid ESS request_id")
    return mode, request_id


def selected_mode(hub4, battery_life, vebus):
    # /Mode is the switch position; /State=Off is not a reliable switch intent.
    if vebus == 4:
        return "off"
    if vebus != 3:
        return None
    if hub4 == 3:
        return "external_control"
    if hub4 in (1, 2):
        if battery_life == 9:
            return "keep_batteries_charged"
        if battery_life in (0, 10, 11, 12):
            return "optimized_without_battery_life"
        if battery_life in range(1, 9):
            return "optimized_with_battery_life"
    return "on"


class EssSelection:
    """Correlate results and prevent a redelivered request from changing a later choice."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen = OrderedDict()
        self._status = {"selection_supported": True, "request_id": None, "error": None}

    def snapshot(self):
        with self._lock:
            return dict(self._status)

    def observe(self, read):
        """Do not pair a pre-write observation with a newer command receipt."""
        with self._lock:
            return {**read(), **self._status}

    def apply(self, payload, write):
        mode, request_id = validate_selection(payload)
        with self._lock:
            if request_id in self._seen:
                return
            self._seen[request_id] = mode
            if len(self._seen) > 128:
                self._seen.popitem(last=False)
            try:
                write(mode)
                error = None
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
            self._status = {
                "selection_supported": True,
                "request_id": request_id,
                "error": error,
            }
