"""Tests for battery daily energy integration (power -> kWh accumulator)."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron


@pytest.fixture
def v():
    victron.reset_victron_for_testing()
    with patch("inverter_control.victron.subprocess.run") as mock_run:
        mock_result = MagicMock()
        mock_result.stdout = "com.victronenergy.vebus.ttyUSB2\n"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        inst = victron.get_victron(test_mode=True)
    inst._battery_energy_file = os.path.join(
        os.path.dirname(__file__), f".bat_energy_test_{id(inst)}.json"
    )
    yield inst
    if os.path.exists(inst._battery_energy_file):
        os.remove(inst._battery_energy_file)
    victron.reset_victron_for_testing()


def _tick(v, bp, seconds_ago):
    """Run one integration tick as if the last sample was seconds_ago ago."""
    v._system_data["bp"] = bp
    v._last_battery_daily_energy_time = time.time() - 6  # pass throttle
    v._battery_energy_last_time = time.time() - seconds_ago
    v._poll_battery_daily_energy()


def test_charge_accumulation(v):
    _tick(v, 1000.0, 5)
    charge, discharge = v.get_battery_daily_energy()
    assert abs(charge - 1000 * 5 / 3600000) < 0.0005
    assert discharge == 0.0


def test_discharge_accumulation(v):
    _tick(v, -500.0, 10)
    charge, discharge = v.get_battery_daily_energy()
    assert charge == 0.0
    assert abs(discharge - 500 * 10 / 3600000) < 0.0005


def test_first_sample_and_gap_skipped(v):
    _tick(v, 1000.0, 60)  # gap > 30s -> skipped
    assert v.get_battery_daily_energy() == (0.0, 0.0)


def test_persist_load_roundtrip(v):
    v._battery_energy_date = time.localtime().tm_yday
    _tick(v, 2000.0, 5)
    saved = v._battery_energy_file
    charge_before, _ = v.get_battery_daily_energy()

    with (
        patch("inverter_control.victron.subprocess.run") as mock_run,
        patch.object(victron, "BATTERY_ENERGY_STATE_FILE", saved),
    ):
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        v2 = victron.VictronDBus(test_mode=True)

    assert v2.get_battery_daily_energy()[0] == pytest.approx(charge_before, abs=1e-6)


def test_midnight_reset(v):
    v._cached_battery_daily_energy = (1.5, 0.5)
    v._battery_energy_date = time.localtime().tm_yday + 1  # pretend stored date is stale
    _tick(v, 0.0, 5)
    assert v.get_battery_daily_energy() == (0.0, 0.0)
    with open(v._battery_energy_file, encoding="utf-8") as f:
        assert json.load(f)["charge"] == 0.0


def test_midnight_promotes_today_to_yesterday(v):
    v._cached_battery_daily_energy = (2.5, 1.25)
    v._battery_energy_date = time.localtime().tm_yday + 1  # stale -> rollover on tick
    _tick(v, 0.0, 5)
    assert v.get_battery_yesterday_energy() == (2.5, 1.25)
    assert v.get_battery_daily_energy() == (0.0, 0.0)
    with open(v._battery_energy_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["y_charge"] == 2.5
    assert data["y_discharge"] == 1.25


def test_load_yesterday_date_file_promoted(v):
    from datetime import timedelta

    yesterday_doy = (v._local_now() - timedelta(days=1)).timetuple().tm_yday
    with open(v._battery_energy_file, "w", encoding="utf-8") as f:
        json.dump({"date": yesterday_doy, "charge": 3.0, "discharge": 4.0}, f)

    inst = victron.VictronDBus(test_mode=True)
    inst._battery_energy_file = v._battery_energy_file
    inst._load_battery_daily_energy()
    assert inst.get_battery_yesterday_energy() == (3.0, 4.0)
    # date must be today so the first poll doesn't roll yesterday back to zero
    assert inst._battery_energy_date == v._local_today()
    assert inst.get_battery_daily_energy() == (0.0, 0.0)


def test_local_today_uses_venus_timezone(v):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    v._tz_name = "America/Los_Angeles"
    expected = datetime.now(ZoneInfo("America/Los_Angeles")).timetuple().tm_yday
    assert v._local_today() == expected


def test_local_today_falls_back_on_bad_tz(v):
    v._tz_name = "Not/AZone"
    assert v._local_today() == time.localtime().tm_yday
    assert v._tz_name == ""  # cleared after failure
