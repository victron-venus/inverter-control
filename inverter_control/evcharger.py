#!/usr/bin/env python3
"""
EV charger / vehicle reader over D-Bus (dbus-evcharger + dbus-ev services).

Runs on the Cerbo GX and reads EV data from native Venus D-Bus services.
Two service prefixes exist:

    com.victronenergy.evcharger.<suffix> → wallbox EV charger (dbus-evcharger)
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
DISCOVERY_TTL = 30.0  # retry unavailable metadata without polling on every read


class EvChargerReader:
    """Reads EV charger power and vehicle SoC from D-Bus services."""

    def __init__(
        self,
        dbus_get: Callable[[str, str], str | None],
        get_service_names: Callable[[], tuple[str, ...]] | None = None,
    ):
        """dbus_get: callable(service, path) -> str | None (e.g. VictronDBus.dbus_get)."""
        self._dbus_get = dbus_get
        self._get_service_names = get_service_names or (lambda: ())
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0
        self.vehicle_service: str | None = None
        self.wallbox_service: str | None = None
        self._services_discovered = False
        self._discovered_names: tuple[str, ...] = ()
        self._discovery_time = 0.0

    def _service_for_instance(self, prefix: str, instance: int) -> str | None:
        matches = [
            service
            for service in self._discovered_names
            if service.startswith(prefix)
            and self._dbus_get(service, "/DeviceInstance") == str(instance)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "Multiple %s services have DeviceInstance %s; skipping", prefix, instance
            )
        return None

    def _is_vehicle(self, service: str | None) -> bool:
        if service is None:
            return False
        connection = self._dbus_get(service, "/Mgmt/Connection")
        if not connection or not connection.startswith("evcharger:"):
            return False
        return (
            self._dbus_get(service, "/Soc") is not None
            or self._dbus_get(service, "/VIN") is not None
        )

    def _discover_services(self, force: bool = False) -> None:
        """Discover vehicle and wallbox services from D-Bus.

        Vehicle services are identified by:
        - /Mgmt/Connection starting with "evcharger:" (paired with a wallbox)
        - AND presence of /Soc and/or /VIN
        """
        names = tuple(
            sorted(
                name
                for name in self._get_service_names()
                if name.startswith(("com.victronenergy.ev.", "com.victronenergy.evcharger."))
            )
        )
        now = time.monotonic()
        if (
            self._services_discovered
            and not force
            and names == self._discovered_names
            and now - self._discovery_time < DISCOVERY_TTL
        ):
            return
        self._discovered_names = names
        self._discovery_time = now
        self.wallbox_service = self._service_for_instance(
            "com.victronenergy.evcharger.", EVCHARGER_INSTANCE
        )
        self.vehicle_service = self._service_for_instance("com.victronenergy.ev.", EV_INSTANCE)

        # A service name and instance alone do not establish vehicle semantics.
        if not self._is_vehicle(self.vehicle_service):
            self.vehicle_service = None

        # Validate wallbox: ensure it's not actually a vehicle
        if self._is_vehicle(self.wallbox_service):
            # This wallbox has /Soc or /VIN: reclassify as vehicle
            logger.warning(
                "Service %s exposes vehicle metadata; treating as vehicle", self.wallbox_service
            )
            self.vehicle_service = self.wallbox_service
            self.wallbox_service = None
        else:
            logger.debug("Found wallbox service: %s", self.wallbox_service)

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


def get_evcharger(
    dbus_get: Callable[[str, str], str | None],
    get_service_names: Callable[[], tuple[str, ...]] | None = None,
) -> EvChargerReader:
    """Get or create the shared EV reader bound to a dbus_get callable."""
    global _evcharger  # pylint: disable=global-statement
    if _evcharger is None:
        _evcharger = EvChargerReader(dbus_get, get_service_names)
    return _evcharger


def reset_evcharger_for_testing() -> None:
    """Drop the singleton so tests can install their own reader."""
    global _evcharger  # pylint: disable=global-statement
    _evcharger = None
