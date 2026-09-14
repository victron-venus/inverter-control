"""Optional aggregate submeter input, independent of Venus' selected grid meter."""

import re
import threading
import time
from typing import Any

from .grid_telemetry import _number

BACKUP_PATHS = (
    "/Connected",
    "/Role",
    "/Ac/Power",
    "/LastUpdate",
    "/DeviceInstance",
    "/CustomName",
    "/ProductName",
)


def parse_backup_snapshot(output: str) -> dict[str, Any]:
    fields = dict.fromkeys(BACKUP_PATHS)
    blocks = re.split(r'string "(/[^"\n]+)"', output)
    for path, block in zip(blocks[1::2], blocks[2::2]):
        if path in fields:
            numeric = re.search(
                r'string "Value"\s+variant\s+(?:double|u?int(?:16|32|64))\s+([^\s]+)',
                block,
            )
            text = re.search(r'string "Value"\s+variant\s+string\s+"([^"\n]+)"', block)
            fields[path] = numeric.group(1) if numeric else text.group(1) if text else None
    return fields


class GridBackup:
    """Select a fresh backup only while the pinned primary is unavailable.

    HA's source timestamp and our monotonic D-Bus read age are independent
    guards. Re-reading an old value cannot renew its measurement lifetime.
    """

    def __init__(
        self, service: str, max_age: float, recovery_seconds: float, *, enabled: bool = False
    ):
        self.service = service
        self.max_age = max_age
        self.recovery_seconds = recovery_seconds
        self.enabled = enabled
        self._lock = threading.RLock()
        self._generation = 0
        self._fields: dict[str, Any] = {}
        self._read_time = 0.0
        self._deadline = 0.0
        self._using_backup = False
        self._primary_since: float | None = None
        self._selection_generation = 0
        self._identity: dict[str, Any] = {"device_instance": None, "name": None}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            self._fields.clear()
            self._deadline = 0.0

    def replace(self, fields: dict[str, Any] | None, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            source = fields or {}
            text_paths = ("/Role", "/CustomName", "/ProductName")
            self._fields = {p: _number(source.get(p)) for p in BACKUP_PATHS if p not in text_paths}
            self._fields.update({p: source.get(p) for p in text_paths})
            instance = self._fields.get("/DeviceInstance")
            if (
                self._fields["/Role"] == "acload"
                and instance is not None
                and instance >= 0
                and instance.is_integer()
            ):
                names = (source.get("/CustomName"), source.get("/ProductName"))
                self._identity = {
                    "device_instance": int(instance),
                    "name": next(
                        (name.strip() for name in names if isinstance(name, str) and name.strip()),
                        None,
                    ),
                }
            self._read_time = time.monotonic()
            timestamp = self._fields.get("/LastUpdate")
            age = time.time() - timestamp if timestamp is not None else float("inf")
            self._deadline = (
                self._read_time + self.max_age - max(0.0, age) if -5 <= age <= self.max_age else 0.0
            )

    def select(self, primary: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            instance = self._fields.get("/DeviceInstance")
            ready = (
                self._fields.get("/Connected") == 1
                and self._fields.get("/Role") == "acload"
                and self._fields.get("/Ac/Power") is not None
                and instance is not None
                and instance >= 0
                and instance.is_integer()
                and now < self._deadline
                and now - self._read_time <= 3.0
            )
            primary_valid = primary.get("_grid_valid") is True
            if primary_valid:
                if self._primary_since is None:
                    self._primary_since = now
            else:
                self._primary_since = None
            use_backup = (
                self.enabled
                and ready
                and (
                    not primary_valid
                    or (self._using_backup and now - self._primary_since < self.recovery_seconds)
                )
            )
            if use_backup != self._using_backup:
                self._selection_generation += 1
                self._using_backup = use_backup
            status = dict(primary)
            status.update(
                _grid_backup=use_backup,
                _grid_backup_available=ready,
                _grid_backup_service=self.service,
                _grid_backup_status={
                    "enabled": self.enabled,
                    "available": ready,
                    "service": self.service,
                    **self._identity,
                    "power": self._fields.get("/Ac/Power") if ready else None,
                    "measurement_time": self._fields.get("/LastUpdate"),
                    "age_seconds": max(0.0, self.max_age - (self._deadline - now))
                    if self._deadline > 0
                    else None,
                },
                _grid_selection_generation=self._selection_generation,
                _grid_primary_valid=primary_valid,
                _grid_primary_reason=primary.get("_grid_invalid_reason"),
                _grid_total_only=False,
                _grid_measurement_time=None,
            )
            if use_backup:
                status.update(
                    g1=None,
                    g2=None,
                    gt=round(self._fields["/Ac/Power"]),
                    _grid_valid=True,
                    _grid_invalid_reason=None,
                    _grid_age=max(0.0, self.max_age - (self._deadline - now)),
                    _grid_phases=None,
                    _grid_source=self.service,
                    _grid_source_instance=int(instance),
                    _grid_total_only=True,
                    _grid_measurement_time=self._fields["/LastUpdate"],
                )
            return status
