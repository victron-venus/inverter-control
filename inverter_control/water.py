#!/usr/bin/env python3
"""
Water system reader over D-Bus (dbus-pump services).

dbus-pump runs on the GX and exposes the site water system as native Venus
services - this module is the only water data source (no Home Assistant):

    com.victronenergy.tank.ha_tank{N}     /Level   tank level, 0-100 %
    com.victronenergy.pump.startstop{P}   /State   well pump (0=stopped)
    com.victronenergy.pump.startstop{V}   /State   city water valve

Reads are cached with a short TTL so the control loop pays at most one round
of busitem reads per TTL window. A missing service yields None ("no data"),
never 0/False, so consumers can distinguish outage from an empty tank.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from .config import WATER_PUMP_INSTANCE, WATER_TANK_INSTANCE, WATER_VALVE_INSTANCE

logger = logging.getLogger("inverter-control")

CACHE_TTL = 2.0  # seconds between actual D-Bus read passes


class WaterSystemReader:
    """Reads tank level and pump/valve state from dbus-pump D-Bus services."""

    def __init__(self, dbus_get: Callable[[str, str], str | None]):
        """dbus_get: callable(service, path) -> str | None (e.g. VictronDBus.dbus_get)."""
        self._dbus_get = dbus_get
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0

    @staticmethod
    def _tank_service(instance: int) -> str:
        return f"com.victronenergy.tank.ha_tank{instance}"

    @staticmethod
    def _startstop_service(instance: int) -> str:
        return f"com.victronenergy.pump.startstop{instance}"

    def _read_level(self) -> float | None:
        raw = self._dbus_get(self._tank_service(WATER_TANK_INSTANCE), "/Level")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _read_startstop(self, instance: int) -> bool | None:
        raw = self._dbus_get(self._startstop_service(instance), "/State")
        if raw is None:
            return None
        try:
            return float(raw) != 0
        except ValueError:
            return None

    def read(self, force: bool = False) -> dict[str, Any]:
        """Return {"water_level", "water_valve", "pump_switch"}; values may be None."""
        now = time.time()
        if not force and self._cache is not None and now - self._cache_time < CACHE_TTL:
            return self._cache
        state = {
            "water_level": self._read_level(),
            "water_valve": self._read_startstop(WATER_VALVE_INSTANCE),
            "pump_switch": self._read_startstop(WATER_PUMP_INSTANCE),
        }
        self._cache = state
        self._cache_time = now
        return state


# Singleton wiring (mirrors get_ha/get_victron pattern)
_water: WaterSystemReader | None = None


def get_water(dbus_get: Callable[[str, str], str | None]) -> WaterSystemReader:
    """Get or create the shared water reader bound to a dbus_get callable."""
    global _water  # pylint: disable=global-statement
    if _water is None:
        _water = WaterSystemReader(dbus_get)
    return _water


def reset_water_for_testing() -> None:
    """Drop the singleton so tests can install their own reader."""
    global _water  # pylint: disable=global-statement
    _water = None
