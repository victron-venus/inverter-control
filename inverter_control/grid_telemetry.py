"""Validity of the grid measurement used by the split-phase control loop.

Bus subscription health is transport health, not proof of a usable grid
measurement. Only grid data can validate this snapshot; unrelated battery and
PV traffic cannot extend its lifetime.
"""

import math
import re
import threading
import time
from typing import Any

GRID_PHASE_COUNT_PATH = "/Ac/Grid/NumberOfPhases"
GRID_POWER_PATHS = {f"/Ac/Grid/L{phase}/Power": phase for phase in (1, 2)}
GRID_SOURCE_PATHS = tuple(
    f"/Ac/In/{index}/{field}"
    for index in (0, 1)
    for field in ("Source", "ServiceName", "DeviceInstance")
)
GRID_PATHS = (GRID_PHASE_COUNT_PATH, *GRID_POWER_PATHS, *GRID_SOURCE_PATHS)
GRID_METER_PATHS = ("/Connected", "/NrOfPhases")


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _parse_fields(
    output: str, paths: tuple[str, ...], include_missing: bool
) -> dict[str, str | None]:
    fields: dict[str, str | None] = {}
    for path in paths:
        key_pattern = rf'string "/?{re.escape(path.lstrip("/"))}"'
        if not include_missing and re.search(key_pattern, output) is None:
            continue
        match = re.search(key_pattern + r"\s*\n[^\n]*variant\s+(\S+)\s+([^\n]+)", output)
        raw = None
        if match:
            kind, value = match.groups()
            if kind == "string":
                raw = value.strip().strip('"')
            elif kind in ("double", "int32", "uint32", "int64", "uint64", "int16", "uint16"):
                raw = value.strip()
        fields[path] = raw
    return fields


def parse_grid_snapshot(output: str) -> dict[str, str | None]:
    """Read required fields without turning absent/invalid variants into zero."""
    return _parse_fields(output, GRID_PATHS, include_missing=True)


def parse_grid_meter_snapshot(output: str) -> dict[str, str | None]:
    """Distinguish an absent optional capability from an invalid declared one."""
    return _parse_fields(output, GRID_METER_PATHS, include_missing=False)


class GridTelemetry:
    """Keep a coherent control snapshot with independent monotonic freshness.

    The topology/source requirements established during this process survive
    service replacement. Venus computes NumberOfPhases from currently available
    readings, so accepting a smaller count would disguise a lost phase.
    """

    def __init__(self, max_age: float, expected_service: str = "", expected_phases: int = 0):
        self._max_age = max_age
        self._generation = 0
        self._lock = threading.RLock()
        self._fields: dict[str, Any] = {}
        self._updated: dict[str, float] = {}
        self._last_good = {1: 0, 2: 0}
        self._required_phases = expected_phases
        self._expected_phases = expected_phases
        self._source: str | None = expected_service or None
        self._source_instance: int | None = None
        self._meter_fields: dict[tuple[str, str], float | None] = {}
        self._meter_updated: dict[tuple[str, str], float] = {}
        self._meter_ready: set[str] = set()
        self._declared_phases_required: set[str] = set()
        self._owner_error: str | None = None

    def update(self, path: str, raw: Any) -> None:
        """Apply one observed grid field; invalid values invalidate that field."""
        if path not in GRID_PATHS:
            return
        with self._lock:
            normalized = raw if path.endswith("/ServiceName") else _number(raw)
            if (path not in GRID_POWER_PATHS and normalized != self._fields.get(path)) or (
                path in GRID_POWER_PATHS and normalized is None
            ):
                self._generation += 1
            self._update(path, raw, time.monotonic())

    def _update(self, path: str, raw: Any, now: float) -> None:
        value = raw if path.endswith("/ServiceName") else _number(raw)
        if path.endswith("/ServiceName") and (not isinstance(value, str) or not value):
            value = None
        self._fields[path] = value
        self._updated[path] = now
        phase = GRID_POWER_PATHS.get(path)
        if phase is not None and value is not None:
            self._last_good[phase] = round(value)

    @property
    def generation(self) -> int:
        """Read token used to reject replies spanning an input invalidation."""
        with self._lock:
            return self._generation

    def replace(self, fields: dict[str, Any], generation: int | None = None) -> int | None:
        """Apply a complete authoritative read, invalidating every missing field."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return None
            self._generation += 1
            now = time.monotonic()
            for path in GRID_PATHS:
                self._update(path, fields.get(path), now)
            self._owner_error = None
            return self._generation

    def _source_candidates(self) -> list[tuple[str, int, list[str]]]:
        candidates = []
        for index in (0, 1):
            paths = [
                f"/Ac/In/{index}/{field}" for field in ("Source", "ServiceName", "DeviceInstance")
            ]
            kind, name, instance = (self._fields.get(path) for path in paths)
            valid_instance = instance is not None and instance >= 0 and instance.is_integer()
            if (
                kind in (1, 3)
                and isinstance(name, str)
                and name.startswith("com.victronenergy.")
                and valid_instance
            ):
                candidates.append((name, int(instance), paths))
        return candidates

    def selected_meter(self) -> str | None:
        """Candidate external meter for metadata reads, before validating power."""
        with self._lock:
            candidates = self._source_candidates()
            if candidates and candidates[0][0].startswith("com.victronenergy.grid."):
                return candidates[0][0]
            return None

    def update_meter(
        self, service: str | None, path: str, raw: Any, generation: int | None = None
    ) -> bool:
        """Observe selected meter health and optional declared phase topology."""
        with self._lock:
            if service is None or service != self.selected_meter() or path not in GRID_METER_PATHS:
                return False
            if generation is not None and generation != self._generation:
                return False
            key = (service, path)
            value = _number(raw)
            if generation is None and (
                (path == "/Connected" and value != 1)
                or (path == "/NrOfPhases" and value != self._meter_fields.get(key))
            ):
                self._generation += 1
            if path == "/NrOfPhases":
                self._declared_phases_required.add(service)
            self._meter_fields[key] = value
            self._meter_updated[key] = time.monotonic()
            return True

    def replace_meter(self, service: str, fields: dict[str, Any] | None, generation: int) -> bool:
        """Publish one complete metadata reply, never a partially acquired contract."""
        with self._lock:
            if generation != self._generation or service != self.selected_meter():
                return False
            if fields is None:
                self._meter_ready.discard(service)
                self._generation += 1
                return False
            self._generation += 1
            now = time.monotonic()
            for path in GRID_METER_PATHS:
                self._meter_fields[(service, path)] = _number(fields.get(path))
                self._meter_updated[(service, path)] = now
            if "/NrOfPhases" in fields:
                self._declared_phases_required.add(service)
            self._meter_ready.add(service)
            return True

    def owner_changed(self, service: str) -> bool:
        """Invalidate critical values until fresh data from the new owner arrive."""
        with self._lock:
            if service != "com.victronenergy.system" and service not in (
                self._source,
                self.selected_meter(),
            ):
                return False
            self._generation += 1
            self._fields.clear()
            self._updated.clear()
            self._meter_ready.clear()
            self._meter_fields.clear()
            self._meter_updated.clear()
            self._owner_error = f"Grid source owner changed: {service}"
            return True

    def unavailable(self, reason: str) -> None:
        """A failed authoritative read cannot preserve a previously valid cache."""
        with self._lock:
            self._generation += 1
            self._fields.clear()
            self._updated.clear()
            self._meter_ready.clear()
            self._meter_fields.clear()
            self._meter_updated.clear()
            self._owner_error = reason

    def _reason(self, now: float) -> tuple[str | None, float | None]:
        # Each guard preserves a specific operator diagnosis without nested branching.
        # pylint: disable=too-many-return-statements
        if self._owner_error:
            return self._owner_error, None
        phases = self._fields.get(GRID_PHASE_COUNT_PATH)
        if phases not in (1, 2):
            return f"Unsupported or unavailable grid phase count: {phases}", None
        if self._expected_phases and phases != self._expected_phases:
            return "Grid phase count differs from configured topology", None
        if phases < self._required_phases:
            return "Grid phase count decreased; waiting for established topology", None
        required_paths = [GRID_PHASE_COUNT_PATH]
        for phase in range(1, int(phases) + 1):
            path = f"/Ac/Grid/L{phase}/Power"
            if self._fields.get(path) is None:
                return f"Grid L{phase} power unavailable", None
            required_paths.append(path)

        candidates = self._source_candidates()
        if not candidates:
            return "Grid source identity unavailable", None
        if len({(name, instance) for name, instance, _ in candidates}) != 1:
            return "Ambiguous grid source identity", None
        source, instance, source_paths = candidates[0]
        required_paths.extend(source_paths)
        if self._source is not None and source != self._source:
            return "Grid measurement source changed; waiting for established source", None
        if self._source_instance is not None and instance != self._source_instance:
            return "Grid device instance changed; waiting for established source", None
        meter_age = 0.0
        if source.startswith("com.victronenergy.grid."):
            if source not in self._meter_ready:
                return "External grid meter metadata unavailable", None
            connected_key = (source, "/Connected")
            if self._meter_fields.get(connected_key) != 1:
                return "External grid meter is not connected", None
            meter_age = now - self._meter_updated.get(connected_key, float("-inf"))
            declared = self._meter_fields.get((source, "/NrOfPhases"))
            # Older/other meters may omit NrOfPhases. When supplied it must
            # agree with supported system power data, including at startup.
            if (
                source in self._declared_phases_required or declared is not None
            ) and declared != phases:
                return "Grid power does not match the meter phase topology", None
        age = max(now - self._updated.get(path, float("-inf")) for path in required_paths)
        age = max(age, meter_age)
        if age > self._max_age:
            return "Grid measurement revalidation overdue", age
        self._required_phases = max(self._required_phases, int(phases))
        self._source = source
        self._source_instance = instance
        return None, age

    def snapshot(self) -> dict[str, Any]:
        """Return diagnostic display values plus an explicit control validity flag."""
        with self._lock:
            reason, age = self._reason(time.monotonic())
            phases = self._required_phases or 1
            g1 = self._last_good[1]
            g2 = self._last_good[2] if phases == 2 else 0
            return {
                "g1": g1,
                "g2": g2,
                "gt": g1 + g2,
                "_grid_valid": reason is None,
                "_grid_invalid_reason": reason,
                "_grid_age": age,
                "_grid_phases": self._required_phases or None,
                "_grid_source": self._source,
                "_grid_source_instance": self._source_instance,
            }
