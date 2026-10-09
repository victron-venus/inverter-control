"""Shared forecast validation cannot change state on malformed input."""

import pytest

from inverter_control.controller import InverterController
from inverter_control.forecast_input import validate_forecast


@pytest.mark.parametrize(
    "payload", [{"today_kwh": 0}, {"tomorrow_kwh": 24.12}, {"today_kwh": 1, "tomorrow_kwh": 2}]
)
def test_partial_producer_summaries_remain_supported(payload):
    assert validate_forecast(payload) == payload


def test_optional_producer_metadata_and_unknown_fields():
    payload = {
        "today_kwh": 0.94,
        "date": "2026-10-09",
        "generated_at": "2026-10-09T03:00:00.123456+00:00",
        "site_id": "my-site",
    }
    assert validate_forecast({**payload, "future_field": {"not_stored": True}}) == payload


@pytest.mark.parametrize(
    "value", [True, False, -1, float("nan"), float("inf"), -float("inf"), "12", None, {}, 10**400]
)
def test_invalid_energy_preserves_existing_controller_state(value):
    controller = object.__new__(InverterController)
    original = {"today_kwh": 1, "tomorrow_kwh": 2}
    controller._solar_forecast = original
    assert not controller._handle_forecast_webhook({"today_kwh": value})
    assert controller._solar_forecast is original


@pytest.mark.parametrize("payload", [None, [], 1, True, "forecast", {}, {"site_id": "site"}])
def test_invalid_container_or_absent_energy(payload):
    with pytest.raises((TypeError, ValueError)):
        validate_forecast(payload)


@pytest.mark.parametrize(
    "extra",
    [
        {"date": "2026-02-30"},
        {"date": "20260101"},
        {"date": 123},
        {"generated_at": "yesterday"},
        {"generated_at": "2026-10-09"},
        {"site_id": "a" * 257},
        {"site_id": "line\nforged-log"},
        {"site_id": ""},
    ],
)
def test_invalid_metadata(extra):
    with pytest.raises(ValueError):
        validate_forecast({"today_kwh": 1, **extra})


def test_http_contract_still_requires_both_days():
    with pytest.raises(ValueError):
        validate_forecast({"today_kwh": 1}, require_both=True)
