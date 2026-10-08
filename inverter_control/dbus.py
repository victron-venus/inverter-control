"""D-Bus client for VUE sensors from dbus-emporia-vue service."""

import logging
import math
import re

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

logger = logging.getLogger("inverter-control")
DBUS_SEND = "/usr/bin/dbus-send"


class VUESensorDBusClient:
    """Retrieve VUE sensor power values via D-Bus from dbus-emporia-vue.

    Parameters
    ----------
    vue_sensor_mapping: dict mapping sensor key (e.g., 'garage') to
        expected custom_name string (e.g., "Garage") as defined in VUE_SENSORS.
    """

    def __init__(
        self,
        vue_sensor_mapping: dict[str, str],
        *,
        native_get: Callable[..., str | None] | None = None,
    ):
        self._vue_sensor_mapping = vue_sensor_mapping
        # Borrow an already-connected read client; do not own/close its transport.
        self._native_get = native_get
        self._vue_services: dict[str, str] = {}  # sensor key -> dbus service name
        self._available = False
        self._setup_dbus()

    def _key_for_custom_name(self, custom_name: str) -> str:
        """Find key in mapping or generate slugified key from CustomName."""
        for key, expected_name in self._vue_sensor_mapping.items():
            if str(expected_name) == custom_name:
                return key
        slug = re.sub(r"\W", "", custom_name.lower().replace(" ", "_"))
        return slug or "acload"

    def _setup_dbus(self) -> None:
        """Set up D-Bus connection or service mapping."""
        # Discovery stays on the existing CLI path. Polling can borrow a native
        # read connection without creating another loop or reconnecting here.
        self._setup_dbus_send()
        self._available = bool(self._vue_services)
        if self._available:
            logger.info("D-Bus VUE client initialized with %d sensors", len(self._vue_services))
        else:
            logger.warning("No VUE sensor services could be discovered")

    def _setup_dbus_send(self) -> None:
        """Discover acload services using dbus-send CLI tool."""
        try:
            cmd = [
                DBUS_SEND,
                "--system",
                "--print-reply",
                "--dest=org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.ListNames",
            ]
            # Repository-controlled argv; no shell interpolation or external command text.
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)  # nosec B603
            if result.returncode != 0:
                return

            services = re.findall(r'string "(com\.victronenergy\.acload\.[^"]+)"', result.stdout)
            for service in services:
                custom_name = self._get_custom_name_dbus_send(service)
                if custom_name:
                    key = self._key_for_custom_name(custom_name)
                    self._vue_services[key] = service
        except Exception as e:
            logger.debug(f"dbus-send discovery failed: {e}")

    def _get_custom_name_dbus_send(self, service: str) -> str | None:
        """Get CustomName via dbus-send."""
        try:
            cmd = [
                DBUS_SEND,
                "--system",
                "--print-reply",
                f"--dest={service}",
                "/CustomName",
                "com.victronenergy.BusItem.GetValue",
            ]
            # Repository-controlled argv; no shell interpolation or external command text.
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)  # nosec B603
            if result.returncode == 0:
                m = re.search(r'string "([^"]+)"', result.stdout)
                if m:
                    return m.group(1)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("VUE name lookup failed: %s", type(exc).__name__)
        return None

    def _native_power(self, service: str, remaining: float, deadline: float) -> float | None:
        try:
            raw = self._native_get(service, "/Ac/Power", timeout=min(0.25, remaining))
            if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
                value = float(raw)
                if math.isfinite(value) and time.monotonic() < deadline:
                    return value
        except Exception as exc:
            # Keep CLI availability on endpoint failures/invalid data.
            # Cancellation (BaseException) is deliberately not swallowed.
            logger.debug("Native VUE read failed; using CLI: %s", type(exc).__name__)
        return None

    @staticmethod
    def _cli_power(key: str, service: str, remaining: float, deadline: float) -> float | None:
        try:
            cmd = [
                DBUS_SEND,
                "--system",
                "--print-reply",
                f"--dest={service}",
                "/Ac/Power",
                "com.victronenergy.BusItem.GetValue",
            ]
            # Repository-controlled argv; no shell interpolation or external command text.
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining)  # nosec B603
            if res.returncode == 0:
                m = re.search(
                    r"(?:double|int32|variant\s+(?:double|int32))\s+([-\d\.]+)", res.stdout
                )
                if m:
                    value = float(m.group(1))
                    if math.isfinite(value) and time.monotonic() < deadline:
                        return value
        except Exception as e:
            logger.warning(f"Failed to update VUE sensor {key} via dbus-send: {e}")
        return None

    def _query_power(self, key: str, service: str) -> tuple[str, float | None, float]:
        """Share one deadline across the native read and its CLI fallback."""
        deadline = time.monotonic() + 2.0
        if self._native_get is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return key, None, deadline
            value = self._native_power(service, remaining, deadline)
            if value is not None:
                return key, value, deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return key, None, deadline
        return key, self._cli_power(key, service, remaining, deadline), deadline

    def update_all(self, vue_sensors: dict[str, Any]) -> None:
        """Update vue_sensors dictionary in-place from D-Bus."""
        if not self._available:
            return

        if not self._vue_services:
            return

        with ThreadPoolExecutor(max_workers=len(self._vue_services)) as pool:
            futures = [
                pool.submit(self._query_power, key, svc) for key, svc in self._vue_services.items()
            ]
            for future in as_completed(futures):
                key, value, deadline = future.result()
                if value is not None and time.monotonic() < deadline:
                    vue_sensors[key] = value
