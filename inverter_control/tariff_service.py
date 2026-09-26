"""Controller-owned tariff editing, separate from inverter operating policy."""

import copy
import hashlib
import json
import re
import threading

from .tariff import MAX_BYTES, SETUP_FILE, validate_tariff, write_tariff


def revision(plan):
    """Stable optimistic concurrency token; not an authentication credential."""
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class TariffService:
    """Validate and durably save before publishing a successful acknowledgement."""

    def __init__(self, plan=None, path=SETUP_FILE):
        self._plan = copy.deepcopy(plan)
        self._path = path
        self._status = {"revision": revision(plan), "request_id": None, "error": None}
        self._lock = threading.Lock()

    def snapshot(self):
        """One coherent plan/status pair for every dashboard transport."""
        with self._lock:
            return copy.deepcopy(
                {
                    "electricity_tariff": self._plan,
                    "electricity_tariff_status": {"writable": True, **self._status},
                }
            )

    def apply(self, payload):
        """Compare the caller's revision and acknowledge only atomic persistence."""
        with self._lock:
            request_id = payload.get("request_id") if isinstance(payload, dict) else None
            if not isinstance(request_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", request_id
            ):
                request_id = None
            try:
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"request_id", "revision", "plan"}
                    or request_id is None
                ):
                    raise ValueError("Expected request_id, revision and plan")
                if (
                    len(
                        json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                        ).encode()
                    )
                    > MAX_BYTES
                ):
                    raise ValueError("Tariff command exceeds 100 KB")
                if not isinstance(payload["revision"], str) or not re.fullmatch(
                    r"[a-f0-9]{64}", payload["revision"]
                ):
                    raise ValueError("Invalid tariff revision")
                plan = None if payload["plan"] is None else validate_tariff(payload["plan"])
                next_revision = revision(plan)
                if (
                    payload["revision"] != self._status["revision"]
                    and next_revision != self._status["revision"]
                ):
                    raise ValueError(
                        "The controller tariff changed. Reload it before saving again."
                    )
                # Repeated delivery of the already committed plan is idempotent.
                if payload["revision"] == self._status["revision"]:
                    write_tariff(self._path, plan)
                self._plan = plan
                self._status = {"revision": next_revision, "request_id": request_id, "error": None}
            except (OSError, ValueError, TypeError, RecursionError) as error:
                self._status = {**self._status, "request_id": request_id, "error": str(error)}
