"""D-Bus client for VUE sensors from dbus-emporia-vue service."""

import logging
import re
import subprocess
from typing import Any

logger = logging.getLogger("inverter-control")


class VUESensorDBusClient:
    """Retrieve VUE sensor power values via D-Bus from dbus-emporia-vue.

    Parameters
    ----------
    vue_sensor_mapping: dict mapping sensor key (e.g., 'garage') to
        expected custom_name string (e.g., "Garage") as defined in VUE_SENSORS.
    """

    def __init__(self, vue_sensor_mapping: dict[str, str]):
        self._vue_sensor_mapping = vue_sensor_mapping
        self._vue_proxies: dict[str, Any] = {}  # sensor key -> Properties interface
        self._vue_services: dict[str, str] = {}  # sensor key -> dbus service name
        self._bus = None
        self._available = False
        self._setup_dbus()

    def _key_for_custom_name(self, custom_name: str) -> str:
        """Find key in mapping or generate slugified key from CustomName."""
        for key, expected_name in self._vue_sensor_mapping.items():
            if str(expected_name) == custom_name:
                return key
        slug = re.sub(r'[^a-zA-Z0-9_]', '', custom_name.lower().replace(' ', '_'))
        return slug or "acload"

    def _setup_dbus(self) -> None:
        """Set up D-Bus connection or service mapping."""
        # Try dbus-fast / dbus-next first
        try:
            from dbus_fast import BusType, MessageBus
        except ImportError:
            try:
                from dbus_next import BusType, MessageBus
            except ImportError:
                BusType = None
                MessageBus = None

        if MessageBus is not None:
            try:
                self._bus = MessageBus(BusType.SYSTEM).connect()
                service_names = self._bus.list_names()
                prefix = "com.victronenergy.acload."
                acload_names = [name for name in service_names if name.startswith(prefix)]
                for service_name in acload_names:
                    try:
                        introspection = self._bus.introspect(service_name, "/")
                        proxy = self._bus.get_proxy_object(service_name, "/", introspection)
                        props = proxy.get_interface("org.freedesktop.DBus.Properties")
                        custom_name = props.Get("com.victronenergy.BusItem", "/CustomName")
                        custom_name_str = str(getattr(custom_name, "value", custom_name))
                        key = self._key_for_custom_name(custom_name_str)
                        self._vue_proxies[key] = props
                    except Exception as e:
                        logger.warning(f"Failed to process service {service_name}: {e}")
            except Exception as e:
                logger.warning(f"Failed to connect via dbus_fast/next: {e}")
                self._bus = None

        # Fallback to dbus-send CLI tool (native on Victron Venus OS) if proxies empty
        if not self._vue_proxies:
            self._setup_dbus_send()

        self._available = bool(self._vue_proxies or self._vue_services)
        if self._available:
            count = len(self._vue_proxies) or len(self._vue_services)
            logger.info(f"D-Bus VUE client initialized with {count} sensors")
        else:
            logger.warning("No VUE sensor services or proxies could be created")

    def _setup_dbus_send(self) -> None:
        """Discover acload services using dbus-send CLI tool."""
        try:
            cmd = [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.ListNames",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
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
                "dbus-send",
                "--system",
                "--print-reply",
                f"--dest={service}",
                "/CustomName",
                "com.victronenergy.BusItem.GetValue",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                m = re.search(r'string "([^"]+)"', result.stdout)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def update_all(self, vue_sensors: dict[str, Any]) -> None:
        """Update vue_sensors dictionary in-place from D-Bus."""
        if not self._available:
            return

        # 1. Update from dbus_fast/next proxies
        for key, props in self._vue_proxies.items():
            try:
                power = props.Get("com.victronenergy.BusItem", "/Ac/Power")
                val = getattr(power, "value", power)
                vue_sensors[key] = float(val)
            except Exception as e:
                logger.warning(f"Failed to update VUE sensor {key} from D-Bus proxy: {e}")

        # 2. Update from dbus-send services
        for key, service in self._vue_services.items():
            try:
                cmd = [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/Ac/Power",
                    "com.victronenergy.BusItem.GetValue",
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                if res.returncode == 0:
                    m = re.search(r'(?:double|int32|variant\s+(?:double|int32))\s+([-\d\.]+)', res.stdout)
                    if m:
                        vue_sensors[key] = float(m.group(1))
            except Exception as e:
                logger.warning(f"Failed to update VUE sensor {key} via dbus-send: {e}")