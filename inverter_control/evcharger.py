#!/usr/bin/env python3
"""
EV charger / vehicle reader over D-Bus (dbus-evcharger + dbus-ev services).

Runs on the Cerbo GX and reads EV data from native Venus D-Bus services.
Two service prefixes exist:

    com.victronenergy.evcharger.<N>  → wallbox EV charger (dbus-evcharger)
    com.victronenergy.ev.<suffix>    → vehicle (dbus-ev)

The vehicle is distinguished by presence of /Soc and/or /VIN AND
/Mgmt/Connection matching "evcharger:<n>" (the instance of its paired charger).
The wallbox has neither /Soc nor /VIN.

Reads are cached with a short TTL so the control loop pays at most one round
of busitem reads per TTL window. A missing service yields None ("no data"),
never 0/False, so consumers can distinguish outage from real values.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from .config import EV_INSTANCE, EVCHARGER_INSTANCE

logger = logging.getLogger("inverter-control")

CACHE_TTL = 2.0  # seconds between actual D-Bus read passes


def _wallbox_service_name(instance: int) -> str:
    return f"com.victronenergy.evcharger.{instance}"


def _vehicle_service_name(instance: int) -> str:
    return f"com.victronenergy.ev.{instance}"


class EvChargerReader:
    """Reads EV charger power and vehicle SoC from D-Bus services."""

    def __init__(self, dbus_get: Callable[[str, str], str | None]):
        """dbus_get: callable(service, path) -> str | None (e.g. VictronDBus.dbus_get)."""
        self._dbus_get = dbus_get
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0
        self.vehicle_service: str | None = None
        self.wallbox_service: str | None = None
        self._services_discovered = False

    def _discover_services(self, force: bool = False) -> None:
        """Discover vehicle and wallbox services from D-Bus.

        Vehicle services are identified by:
        - /Mgmt/Connection starting with "evcharger:" (paired with a wallbox)
        - AND presence of /Soc and/or /VIN
        """
        if self._services_discovered and not force:
            return

        # Start with the configured-instance defaults; clear if not validated.
        self.wallbox_service = _wallbox_service_name(EVCHARGER_INSTANCE)
        self.vehicle_service = _vehicle_service_name(EV_INSTANCE)

        # Validate vehicle: must have /Mgmt/Connection=="evcharger:*" + /Soc or /VIN
        conn = self._dbus_get(self.vehicle_service, "/Mgmt/Connection")
        soc = self._dbus_get(self.vehicle_service, "/Soc")
        vin = self._dbus_get(self.vehicle_service, "/VIN")
        if conn and conn.startswith("evcharger:") and (soc is not None or vin is not None):
            logger.debug(
                f"Found vehicle service: {self.vehicle_service} (conn={conn}, soc={soc}, vin={vin})"
            )
        else:
            logger.debug(
                f"Service {self.vehicle_service} does not look like a vehicle "
                f"(conn={conn}, soc={soc}, vin={vin})"
            )
            self.vehicle_service = None

        # Validate wallbox: ensure it's not actually a vehicle
        conn = self._dbus_get(self.wallbox_service, "/Mgmt/Connection")
        soc = self._dbus_get(self.wallbox_service, "/Soc")
        vin = self._dbus_get(self.wallbox_service, "/VIN")
        if conn and conn.startswith("evcharger:") and (soc is not None or vin is not None):
            # This wallbox has /Soc or /VIN: reclassify as vehicle
            logger.warning(
                f"Service {self.wallbox_service} appears to be a vehicle, not a wallbox "
                f"(conn={conn}, soc={soc}, vin={vin}); treating as vehicle"
            )
            self.vehicle_service = self.wallbox_service
            self.wallbox_service = None
        else:
            logger.debug(f"Found wallbox service: {self.wallbox_service} (conn={conn})")

        self._services_discovered = True

    def _read_numeric(self, service: str, path: str) -> float | None:
        raw = self._dbus_get(service, path)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _read_int(self, service: str, path: str) -> int | None:
        raw = self._dbus_get(service, path)
        if raw is None:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    def read(self, force: bool = False) -> dict[str, Any]:
        """Return {"ev_power", "car_soc", "ev_charging_kw"}; values may be None."""
        now = time.time()
        if not force and self._cache is not None and now - self._cache_time < CACHE_TTL:
            return self._cache

        self._discover_services()

        state: dict[str, Any] = {"ev_power": None, "car_soc": None, "ev_charging_kw": None}

        # Prefer vehicle for car_soc (has /Soc)
        if self.vehicle_service:
            soc = self._read_int(self.vehicle_service, "/Soc")
            if soc is not None:
                state["car_soc"] = soc
            # Vehicle also reports AC power on /Ac/Power
            pwr = self._read_numeric(self.vehicle_service, "/Ac/Power")
            if pwr is not None:
                state["ev_power"] = pwr
                state["ev_charging_kw"] = pwr / 1000.0

        # Fall back to wallbox for power if vehicle didn't provide
        if state["ev_power"] is None and self.wallbox_service:
            pwr = self._read_numeric(self.wallbox_service, "/Ac/Power")
            if pwr is not None:
                state["ev_power"] = pwr
                state["ev_charging_kw"] = pwr / 1000.0
            else:
                # Derive power from current and voltage
                curr = self._read_numeric(self.wallbox_service, "/Current")
                volt = self._read_numeric(self.wallbox_service, "/Ac/L1/Voltage")
                if curr is not None and volt is not None:
                    state["ev_power"] = curr * volt
                    state["ev_charging_kw"] = state["ev_power"] / 1000.0

        # If wallbox has /Soc (rare), use as car_soc fallback
        if state["car_soc"] is None and self.wallbox_service:
            soc = self._read_int(self.wallbox_service, "/Soc")
            if soc is not None:
                state["car_soc"] = soc

        self._cache = state
        self._cache_time = now
        return state


# Singleton wiring (mirrors get_water/get_ha/get_victron pattern)
_evcharger: EvChargerReader | None = None


def get_evcharger(dbus_get: Callable[[str, str], str | None]) -> EvChargerReader:
    """Get or create the shared EV reader bound to a dbus_get callable."""
    global _evcharger  # pylint: disable=global-statement
    if _evcharger is None:
        _evcharger = EvChargerReader(dbus_get)
    return _evcharger


def reset_evcharger_for_testing() -> None:
    """Drop the singleton so tests can install their own reader."""
    global _evcharger  # pylint: disable=global-statement
    _evcharger = None
