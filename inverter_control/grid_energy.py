"""Daily deltas of a physical grid meter's cumulative kWh counters.

No power integration, phase summation, tariff calculation or device I/O. A
midnight crossed between unequal readings cannot be apportioned to either day.
Only an unchanged, fresh counter bracket proves the new day's baseline.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from datetime import datetime, timedelta
from datetime import time as midnight
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATE_FILE = Path("/data/setupOptions/inverter-control/grid-energy-state.json")
MAX_AGE = 90.0
MIDNIGHT_BRACKET = 30.0
PERSIST_INTERVAL = 60.0
CLOCK_TOLERANCE = 5.0
_COUNTERS = ("/Ac/Energy/Forward", "/Ac/Energy/Reverse")
_FIELDS = (*_COUNTERS, "/DeviceInstance", "/Serial", "/Connected")


def parse_energy_snapshot(output: str) -> dict:
    """Reuse the existing CLI tree reply when native D-Bus is unavailable."""
    result = {}
    for path in _FIELDS:
        match = re.search(
            rf'string "/?{re.escape(path.lstrip("/"))}"\s*\n[^\n]*variant\s+(\S+)\s+([^\n]+)',
            output,
        )
        if match:
            kind, value = match.groups()
            if kind == "string":
                result[path] = value.strip().strip('"')
            elif kind in {"double", "int32", "uint32", "int64", "uint64", "int16", "uint16"}:
                result[path] = value.strip()
    return result


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def parse_meter_source(service, fields):
    """Validate and normalize physical meter identity from a complete D-Bus reply."""
    instance = _number(fields.get("/DeviceInstance"))
    serial = fields.get("/Serial")
    if (
        not isinstance(service, str)
        or not service.startswith("com.victronenergy.grid.")
        or service == "com.victronenergy.grid."
        or len(service) > 255
        or instance is None
        or not instance.is_integer()
        or instance > 2147483647
        or (serial is not None and (not isinstance(serial, str) or not serial or len(serial) > 128))
    ):
        return None
    return {"service": service, "device_instance": int(instance), "serial": serial}


def _calendar(at, name):
    if not isinstance(name, str) or not name or _number(at) is None:
        raise ValueError("Invalid clock or timezone")
    local = datetime.fromtimestamp(float(at), ZoneInfo(name))
    boundary = datetime.combine(local.date(), midnight(), local.tzinfo).timestamp()
    return local.date().isoformat(), boundary


class GridEnergyLedger:
    """Thread-safe memory ledger; callers explicitly persist on a non-control worker."""

    def __init__(self, path: Path | None = STATE_FILE, *, wall=time.time, monotonic=time.monotonic):
        self._path = path
        self._wall = wall
        self._mono = monotonic
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._state = None
        self._observed_mono = None
        self._read_error = "awaiting_meter"
        self._reset_pending = None
        self._reset_observation = False
        self._dirty = False
        self._revision = 0
        self._last_save_attempt = None
        self._save_error = False
        if path is not None:
            self._load()

    def _load(self):
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("Energy state must be a regular file")
                raw = handle.read(16385)
            if len(raw) > 16384:
                raise ValueError("Oversized energy state")
            state = json.loads(raw)
            self._validate_state(state)
            self._state = state
        except FileNotFoundError:
            # First start has no persisted baseline yet.
            pass
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self._read_error = "state_unavailable"

    @staticmethod
    def _validate_state(state):
        if (
            type(state["schema"]) is not int
            or state["schema"] != 1
            or type(state["generation"]) is not int
            or state["generation"] < 1
        ):
            raise ValueError("Invalid energy state schema")
        source = state["source"]
        if type(source["device_instance"]) is not int or source != parse_meter_source(
            source["service"],
            {"/DeviceInstance": source["device_instance"], "/Serial": source["serial"]},
        ):
            raise ValueError("Invalid saved meter identity")
        if not isinstance(state["complete"], bool) or not isinstance(state["reason"], str):
            raise TypeError("Invalid coverage")
        latest = state["latest"]
        numeric = [state["started_at"], latest["observed_at"]]
        for key in ("import_kwh", "export_kwh"):
            numeric.extend((state["baseline"][key], latest[key]))
        if any(type(value) not in (int, float) or _number(value) is None for value in numeric):
            raise ValueError("Saved readings must be finite JSON numbers")
        date, boundary = _calendar(latest["observed_at"], state["time_zone"])
        if (
            date != state["date"]
            or _number(state["started_at"]) is None
            or not boundary <= state["started_at"] <= latest["observed_at"]
            or (state["complete"] and state["started_at"] != boundary)
        ):
            raise ValueError("Invalid saved date boundary")
        for key in ("import_kwh", "export_kwh"):
            if (
                _number(state["baseline"][key]) is None
                or _number(latest[key]) is None
                or latest[key] < state["baseline"][key]
            ):
                raise ValueError("Invalid saved counter")

    def invalidate(self, reason="meter_unavailable", *, reset=False):
        """Invalidate display freshness without doing filesystem or D-Bus work."""
        with self._lock:
            self._read_error = reason
            if reset:
                self._reset_pending = reason

    def observe(self, service, fields, time_zone, *, observed_at=None, observed_mono=None):
        """Consume one accepted complete meter reply, timestamped at read completion."""
        at = self._wall() if observed_at is None else observed_at
        mono = self._mono() if observed_mono is None else observed_mono
        try:
            date, boundary = _calendar(at, time_zone)
        except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError):
            self.invalidate("timezone_unavailable")
            return
        if not isinstance(fields, dict) or _number(mono) is None:
            self.invalidate()
            return
        at, mono = float(at), float(mono)
        source = parse_meter_source(service, fields)
        counters = [_number(fields.get(path)) for path in _COUNTERS]
        if source is None or None in counters or _number(fields.get("/Connected")) != 1:
            self.invalidate()
            return
        latest = dict(zip(("import_kwh", "export_kwh"), counters))
        latest["observed_at"] = at
        with self._lock:
            previous = self._state
            reason = self._reseed_reason(source, time_zone, latest, mono)
            if previous is None or reason or previous["date"] != date:
                complete = self._midnight_proved(date, latest, mono, reason)
                self._state = {
                    "schema": 1,
                    "generation": previous["generation"] + 1 if previous else 1,
                    "source": source,
                    "date": date,
                    "time_zone": time_zone,
                    "started_at": boundary if complete else at,
                    "baseline": {key: latest[key] for key in ("import_kwh", "export_kwh")},
                    "latest": latest,
                    "complete": complete,
                    "reason": "midnight_verified" if complete else reason or "incomplete_day",
                }
            else:
                self._state["latest"] = latest
            self._reset_observation = bool(reason)
            self._observed_mono = mono
            self._read_error = None
            self._reset_pending = None
            self._dirty = True
            self._revision += 1

    def _reseed_reason(self, source, time_zone, latest, mono):
        previous = self._state
        if previous is None:
            return None
        if self._reset_pending:
            return self._reset_pending
        if source != previous["source"]:
            return "source_changed"
        if time_zone != previous["time_zone"]:
            return "timezone_changed"
        old = previous["latest"]
        if any(latest[key] < old[key] for key in ("import_kwh", "export_kwh")):
            return "counter_reset"
        elapsed = latest["observed_at"] - old["observed_at"]
        if elapsed < 0 or (
            self._observed_mono is not None
            and abs(elapsed - (mono - self._observed_mono)) > CLOCK_TOLERANCE
        ):
            return "clock_changed"
        return None

    def _midnight_proved(self, date, latest, mono, reason):
        previous = self._state
        if previous is None or reason or self._read_error or self._observed_mono is None:
            return False
        old = previous["latest"]
        next_date = (
            (datetime.fromisoformat(previous["date"]) + timedelta(days=1)).date().isoformat()
        )
        return (
            date == next_date
            and 0 <= mono - self._observed_mono <= MIDNIGHT_BRACKET
            and 0 <= latest["observed_at"] - old["observed_at"] <= MIDNIGHT_BRACKET
            and all(latest[key] == old[key] for key in ("import_kwh", "export_kwh"))
        )

    def snapshot(self):
        """Pure cache read, including current freshness; never read or save a file."""
        at, mono = self._wall(), self._mono()
        with self._lock:
            state = self._state
            if state is None:
                return {
                    "date": None,
                    "time_zone": None,
                    "import_kwh": None,
                    "export_kwh": None,
                    "observed_at": None,
                    "started_at": None,
                    "complete": False,
                    "source": None,
                    "status": "unknown",
                    "reason": self._read_error or "awaiting_meter",
                }
            result = {
                key: copy.deepcopy(state[key])
                for key in ("date", "time_zone", "started_at", "complete", "source", "reason")
            }
            observed = state["latest"]["observed_at"]
            result.update(observed_at=observed, import_kwh=None, export_kwh=None)
            status, reason = self._availability(at, mono)
            result["status"] = status
            if status in {"complete", "partial"}:
                for key in ("import_kwh", "export_kwh"):
                    result[key] = round(state["latest"][key] - state["baseline"][key], 9)
            else:
                result["complete"] = False
            if reason:
                result["reason"] = reason
            elif self._save_error:
                result["reason"] = "persistence_unavailable"
            return result

    def _availability(self, at, mono):
        if self._reset_pending or self._reset_observation:
            return "reset", self._reset_pending or self._state["reason"]
        if self._observed_mono is None:
            return "unknown", self._read_error or "awaiting_meter"
        elapsed = mono - self._observed_mono
        wall_elapsed = at - self._state["latest"]["observed_at"]
        if elapsed < 0 or abs(wall_elapsed - elapsed) > CLOCK_TOLERANCE:
            return "reset", "clock_changed"
        if self._read_error:
            return "stale", self._read_error
        if elapsed > MAX_AGE or _calendar(at, self._state["time_zone"])[0] != self._state["date"]:
            return "stale", "stale_reading"
        return ("complete" if self._state["complete"] else "partial"), None

    def persist(self):
        """Bounded atomic write on a display worker; no ledger lock spans disk I/O."""
        if self._path is None or not self._save_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                now = self._mono()
                if not self._dirty or (
                    self._last_save_attempt is not None
                    and now - self._last_save_attempt < PERSIST_INTERVAL
                ):
                    return
                self._last_save_attempt = now
                revision = self._revision
                state = copy.deepcopy(self._state)
            try:
                self._save(state)
            except OSError:
                with self._lock:
                    self._save_error = True
            else:
                with self._lock:
                    self._save_error = False
                    if revision == self._revision:
                        self._dirty = False
        finally:
            self._save_lock.release()

    def _save(self, state):
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".grid-energy-", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, allow_nan=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
