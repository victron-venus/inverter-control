"""FakeVictronDBus — deterministic `VictronDBus` instance for tests.

Bypasses the Venus OS D-Bus system bus entirely. No subprocess. No native
client. No event-loop thread. Just a `VictronDBus(test_mode=True)` whose
getters and method-level caches return pre-seeded values from an in-memory
store.

What the fake covers
--------------------
- All cached properties (`_cached_mppt_data`, `_cached_pv_powers`,
  `_cached_battery_chain_socs`, `_cached_inverter_state`,
  `_cached_battery_daily_energy`, …) read directly.
- Public getters that build their return value from the cached properties
  AND from per-service reads (`get_acload_powers`, `get_tasmota_pv_power`,
  `get_mppt_data`, `get_battery_chain_socs`) — these now read from the same
  store the seeds populate.
- Subprocess path is disabled: `_safe_subprocess` returns the seeded
  payload or empty string (no `dbus-send`).

What the fake does NOT cover
----------------------------
- The native `dbus_fast` client. `test_mode=True` already skips it; this
  stub does not add a fake one — tests that exercise the native path
  should continue to inject a `FakeBus` directly (see `test_dbus_native.py`).
- Service discovery (`dbus -y`). `test_mode=True` already short-circuits
  this; tests that need discovered services should set them via
  `fake._vebus_service = …` etc.
- Background poll thread. `test_mode=True` skips the thread start; we
  expose `tick()` so tests can drive the reconciler manually if needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

# tests/ is the runner's CWD; allow `from inverter_control import ...`
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from inverter_control import victron


class FakeVictronDBus:
    """Deterministic wrapper around a `VictronDBus(test_mode=True)`.

    Seeds a small set of values that the control loop and the dashboards
    consume. Tests may override or extend at any point via `set_value` /
    `set_services`.
    """

    DEFAULTS: ClassVar[dict[str, Any]] = {
        # AC grid phases (W), positive = import
        "g1": 100.0,
        "g2": -50.0,
        "g3": 0.0,
        # AC consumption phases (W)
        "t1": 300.0,
        "t2": 200.0,
        "t3": 0.0,
        # DC PV power (W)
        "pv_total": 1043.0,
        # Inverter state: 3 = Hub-4, label
        "vebus_state": 3,
        "vebus_state_label": "Hub-4",
        # Inverter AC in / out (W)
        "ac_in": 0.0,
        "ac_out": 0.0,
        # MPPT chargers: list of (service, {"power": W, "current": A, "yield_kwh": kWh})
        "mppt": [
            ("com.victronenergy.solarcharger.ttyUSB0", {"power": 500.0, "current": 4.0, "yield": 3.5}),
            ("com.victronenergy.solarcharger.ttyUSB1", {"power": 543.0, "current": 4.5, "yield": 4.0}),
        ],
        # Tasmota PV inverters: list of (service, {"power": W, "voltage": V, "current": A})
        "pv_inverter": [
            ("com.victronenergy.pvinverter.tasmota_1", {"power": 350.0, "voltage": 230.0, "current": 1.5}),
        ],
        # Battery chain: list of (service, {"soc": pct, "voltage": V, "current": A, "power": W})
        "battery": [
            ("com.victronenergy.battery.ttyUSB0", {"soc": 85.0, "voltage": 53.2, "current": 10.0, "power": 532.0}),
        ],
        # Emporia Vue / acload: list of (service, {"name": str, "power": W})
        "acload": [
            ("com.victronenergy.acload.emporia_1", {"name": "Garage", "power": 50.0}),
        ],
        # Daily energy (kWh) — in, out
        "battery_daily_energy": (1.5, 2.0),
        "battery_yesterday_energy": (1.0, 1.5),
        "mppt_daily_yields": [3.5, 4.0],
        "pv_inverter_daily_yields": [2.0],
    }

    def __init__(self) -> None:
        # Reset singleton so a previous test's instance does not leak
        victron.reset_victron_for_testing()
        self._v: victron.VictronDBus = victron.VictronDBus(test_mode=True)
        self._apply_defaults()
        # Patch the subprocess path so any stray read returns "" instead of
        # trying to invoke dbus-send. The cache-fill code paths all look
        # at cached_* properties first, so this is belt-and-braces.
        self._patches = [
            patch.object(
                self._v,
                "_safe_subprocess",
                return_value="",
            ),
            patch.object(
                self._v,
                "_safe_subprocess_tracked",
                return_value="",
            ),
            patch.object(
                self._v,
                "_discover_services_sync",
                return_value=None,
            ),
        ]
        for p in self._patches:
            p.start()

    # -- teardown ---------------------------------------------------------

    def close(self) -> None:
        for p in self._patches:
            p.stop()
        victron.reset_victron_for_testing()

    def __enter__(self) -> FakeVictronDBus:  # noqa: PYI034
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- public api -------------------------------------------------------

    @property
    def raw(self) -> victron.VictronDBus:
        """Underlying VictronDBus instance (test_mode=True)."""
        return self._v

    def set_value(self, key: str, value: Any) -> None:
        """Override one of the default values and refresh dependent caches."""
        self._v._cached_values[key] = value
        self._refresh_caches()

    def set_services(
        self,
        *,
        vebus: str | None = "com.victronenergy.vebus.ttyUSB0",
        shunt: str | None = "com.victronenergy.battery.ttyUSB0",
        mppt: list[str] | None = None,
        pv_inverter: list[str] | None = None,
        acload: list[str] | None = None,
    ) -> None:
        """Set discovered-service names without going through D-Bus."""
        if vebus is not None:
            self._v._vebus_service = vebus
        if shunt is not None:
            self._v._shunt_service = shunt
        if mppt is not None:
            self._v._mppt_services = mppt
        if pv_inverter is not None:
            self._v._pv_inverter_services = pv_inverter
        if acload is not None:
            self._v._acload_services = acload
        self._refresh_caches()

    def tick(self) -> None:
        """Drive the background poll thread once. test_mode=True does not
        spawn it, so tests that need a refresh should call this. No-op in
        the current fake (caches are pre-populated) — provided for forward
        compatibility when the reconciler gains an in-process code path."""

    # -- internals --------------------------------------------------------

    def _apply_defaults(self) -> None:
        # Store raw values keyed by name; getters read from here.
        self._v._cached_values = dict(self.DEFAULTS)
        # Mirror services from defaults
        self._v._vebus_service = "com.victronenergy.vebus.ttyUSB0"
        self._v._shunt_service = "com.victronenergy.battery.ttyUSB0"
        self._v._mppt_services = [m[0] for m in self.DEFAULTS["mppt"]]
        self._v._pv_inverter_services = [m[0] for m in self.DEFAULTS["pv_inverter"]]
        self._v._acload_services = [m[0] for m in self.DEFAULTS["acload"]]
        self._refresh_caches()

    def _refresh_caches(self) -> None:
        """Push `_cached_values` into the public cached_* properties that
        the hot-path getters read from.
        """
        cv = self._v._cached_values

        # System data dict — keys match what victron.py writes
        self._v._cached_system_data = {
            "g1": int(cv["g1"]),
            "g2": int(cv["g2"]),
            "g3": int(cv["g3"]),
            "t1": int(cv["t1"]),
            "t2": int(cv["t2"]),
            "t3": int(cv["t3"]),
            "pv_total": int(cv["pv_total"]),
        }
        # Battery / inverter state
        self._v._cached_inverter_state = (int(cv["vebus_state"]), str(cv["vebus_state_label"]))
        self._v._cached_inverter_state_time = 1e18  # never stale
        # MPPT
        self._v._cached_mppt_data = {
            svc: dict(reading) for svc, reading in cv["mppt"]
        }
        self._v._last_mppt_time = 1e18
        # PV inverters (Tasmota)
        self._v._cached_pv_powers = [dict(reading) for _, reading in cv["pv_inverter"]]
        self._v._last_pv_time = 1e18
        # Battery chains
        self._v._cached_battery_chain_socs = [
            {"service": svc, "soc": r["soc"], "voltage": r["voltage"], "current": r["current"], "power": r["power"]}
            for svc, r in cv["battery"]
        ]
        self._v._last_battery_chain_soc_time = 1e18
        # acload
        self._v._acload_powers_by_service = {
            svc: r["power"] for svc, r in cv["acload"]
        }
        self._v._acload_names = {svc: r["name"] for svc, r in cv["acload"]}
        self._v._last_acload_time = 1e18
        # Daily energy
        self._v._cached_battery_daily_energy = tuple(cv["battery_daily_energy"])  # type: ignore[assignment]
        self._v._last_battery_daily_energy_time = 1e18
        self._v._cached_mppt_daily_yields = list(cv["mppt_daily_yields"])
        self._v._cached_pv_inverter_daily_yields = list(cv["pv_inverter_daily_yields"])


def fake_victron() -> FakeVictronDBus:
    """Factory for pytest fixtures."""
    return FakeVictronDBus()
