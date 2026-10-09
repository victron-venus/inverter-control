"""Versioned, expiring, at-most-once pre-charge requests shared by HTTP and MQTT."""

import json
import math
import os
import re
import threading
import time
from pathlib import Path


def _invalid_numeric_field(payload):
    for key in ("forecast_energy_wh", "threshold_wh", "issued_at", "expires_at"):
        value = payload.get(key)
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            return "invalid_" + key
    return None


def _request_expired(payload, now):
    return (
        not now - 300 <= payload["issued_at"] <= now + 30
        or not now < payload["expires_at"] <= payload["issued_at"] + 300
    )


class PrechargeInbox:
    """Reserve before enqueue: an uncertain intent is never replayed.

    The accepted status means queued for the existing one control cycle; it is
    not evidence of physical charging. Only a completed callback gets the
    durable queued marker. Unreadable persistence fails closed.
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
                for request_id, value in self.records.items():
                    if (
                        not isinstance(request_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", request_id)
                        or not isinstance(value, dict)
                        or value.get("status") not in ("accepted", "suppressed", "uncertain")
                        or (
                            "queued" in value
                            and (value["status"] != "accepted" or value["queued"] is not True)
                        )
                        or type(value.get("until")) not in (int, float)
                        or not math.isfinite(value["until"])
                        or value["until"] < 0
                    ):
                        raise TypeError("Invalid journal record")
        except (OSError, ValueError, TypeError, OverflowError):
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

    def _accept_request_locked(self, request_id, now, result, suppressed, accept):
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
            records[request_id] = {
                "status": "suppressed" if blocked else "uncertain",
                "until": now + 172800,
            }
            if not blocked:
                # Keep the reservation in memory even if persistence raises
                # after replace/fsync. A retry must never call accept again.
                self.records = records
            self._save(records)
            if not blocked:
                accept()
                confirmed = {
                    **records,
                    request_id: {**records[request_id], "status": "accepted", "queued": True},
                }
                try:
                    self._save(confirmed)
                except (OSError, ValueError, RuntimeError):
                    # _save may have published before failing. Locally keep
                    # uncertainty sticky; on disk queued=True still proves
                    # the callback returned, never physical actuation.
                    self.records = records
                    raise
            return outcome
        except (OSError, ValueError, RuntimeError):
            return result("unavailable", 503, "decision_unavailable")

    def _previous_result(self, previous, result):
        if previous["status"] == "uncertain":
            return result("unavailable", 503, "decision_uncertain")
        if previous["status"] == "accepted" and previous.get("queued") is not True:
            return result("unavailable", 503, "legacy_decision_unverified")
        return {
            **result("duplicate", 200, "already_decided"),
            "original_status": previous["status"],
        }

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
        if (
            type(payload.get("version")) is not int
            or payload["version"] != 1
            or payload.get("trigger") != "low_solar_forecast"
        ):
            return result("rejected", 400, "invalid_contract")
        invalid_numeric = _invalid_numeric_field(payload)
        if invalid_numeric:
            return result("rejected", 400, invalid_numeric)
        now = time.time()
        if _request_expired(payload, now):
            return result("rejected", 410, "expired_or_invalid_time")
        if payload["forecast_energy_wh"] >= payload["threshold_wh"]:
            return result("rejected", 400, "forecast_not_below_threshold")
        with self.lock:
            if self.error:
                return result("unavailable", 503, "journal_unavailable")
            previous = self.records.get(request_id)
            if previous and previous["until"] > now:
                return self._previous_result(previous, result)
            # Another HTTP/MQTT delivery may have held the lock across durable
            # writes and enqueue. Revalidate new decisions using the current time;
            # previously persisted outcomes above retain their existing responses.
            now = time.time()
            if _request_expired(payload, now):
                return result("rejected", 410, "expired_or_invalid_time")
            return self._accept_request_locked(request_id, now, result, suppressed, accept)
