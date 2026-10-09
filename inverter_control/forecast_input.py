"""Validate the shared, display-only solar forecast summary."""

import math
from datetime import date, datetime


def _validate_energy(value: object) -> None:
    if type(value) not in (int, float):
        raise ValueError("Forecast energy must be a finite nonnegative number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise ValueError("Forecast energy must be a finite nonnegative number")


def _validate_metadata(key: str, value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
        raise ValueError("Forecast metadata must be bounded printable text")
    if key == "date" and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
        raise ValueError("Forecast date must use YYYY-MM-DD")
    if key == "generated_at":
        if len(value) > 64 or "T" not in value:
            raise ValueError("Forecast generated_at must be an ISO timestamp")
        datetime.fromisoformat(value)


def validate_forecast(payload: object, *, require_both: bool = False) -> dict:
    """Return known fields only, preserving partial MQTT daily summaries.

    The producer may have a forecast for only today or only tomorrow. HTTP has
    historically required both. Metadata is optional and never an authority to
    issue control commands; the separate pre-charge schema governs those.
    """
    if not isinstance(payload, dict):
        raise TypeError("Forecast must be a JSON object")
    days = ("today_kwh", "tomorrow_kwh")
    present = [key for key in days if key in payload]
    if not present or (require_both and len(present) != len(days)):
        raise ValueError("Forecast must contain numeric today_kwh/tomorrow_kwh")
    validated = {}
    for key in present:
        value = payload[key]
        _validate_energy(value)
        validated[key] = value
    for key in ("date", "generated_at", "site_id"):
        if key not in payload:
            continue
        value = payload[key]
        _validate_metadata(key, value)
        validated[key] = value
    return validated
