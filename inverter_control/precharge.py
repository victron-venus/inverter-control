"""Versioned, expiring, at-most-once pre-charge requests shared by HTTP and MQTT."""

import json
import math
import os
import re
import threading
import time
from pathlib import Path


class PrechargeInbox:
    """Record before enqueue: a crash may lose an intent, but cannot replay it.

    The accepted status means queued for the existing one control cycle; it is
    not evidence of physical charging. Unreadable persistence fails closed.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.records = {}
        self.error = False
        try:
            if self.path and self.path.exists():
                self.records = json.loads(self.path.read_text())
                if not isinstance(self.records, dict):
                    raise ValueError("Invalid journal")
                for value in self.records.values():
                    if not isinstance(value, dict) or not isinstance(
                        value.get("until"), (int, float)
                    ):
                        raise TypeError("Invalid journal record")
        except (OSError, ValueError, TypeError):
            self.error = True

    def _save(self, records):
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as stream:
                os.chmod(tmp, 0o600)
                json.dump(records, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self.records = records

    def handle(self, payload, suppressed, accept):
        request_id = payload.get("request_id") if isinstance(payload, dict) else None

        def result(status, code, reason):
            return {
                "request_id": request_id,
                "status": status,
                "reason": reason,
                "http_status": code,
            }

        if (
            not isinstance(payload, dict)
            or not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", request_id)
        ):
            request_id = None
            return result("rejected", 400, "invalid_request_id")
        if payload.get("version") != 1 or payload.get("trigger") != "low_solar_forecast":
            return result("rejected", 400, "invalid_contract")
        for key in ("forecast_energy_wh", "threshold_wh", "issued_at", "expires_at"):
            value = payload.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return result("rejected", 400, "invalid_" + key)
        now = time.time()
        if (
            not now - 300 <= payload["issued_at"] <= now + 30
            or not now < payload["expires_at"] <= payload["issued_at"] + 300
        ):
            return result("rejected", 410, "expired_or_invalid_time")
        if payload["forecast_energy_wh"] >= payload["threshold_wh"]:
            return result("rejected", 400, "forecast_not_below_threshold")
        with self.lock:
            if self.error:
                return result("unavailable", 503, "journal_unavailable")
            previous = self.records.get(request_id)
            if previous and previous["until"] > now:
                return {
                    **result("duplicate", 200, "already_decided"),
                    "original_status": previous["status"],
                }
            try:
                blocked = suppressed()
                outcome = result(
                    "suppressed" if blocked else "accepted",
                    409 if blocked else 202,
                    "expensive_window" if blocked else "queued_one_cycle",
                )
                records = {k: v for k, v in self.records.items() if v["until"] > now}
                if len(records) >= 4096:
                    return result("unavailable", 503, "journal_full")
                records[request_id] = {"status": outcome["status"], "until": now + 172800}
                self._save(records)
                if not blocked:
                    accept()
                return outcome
            except (OSError, ValueError, RuntimeError):
                return result("unavailable", 503, "decision_unavailable")
