"""Installer tariffs stay portable, complete and separate from live control policy."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from inverter_control.tariff import (
    interactive_tariff,
    load_tariff,
    periods_grid,
    read_tariff,
    validate_tariff,
    write_tariff,
)

REPO = Path(__file__).resolve().parents[1]


def schedule():
    return {
        "type": "electricity-tariff-schedule",
        "version": 1,
        "name": "Example utility",
        "currency": "USD",
        "timeZone": "America/Los_Angeles",
        "billingDay": 17,
        "periods": [{"start": "00:00", "end": "24:00", "rate": 0.2}],
        "seasons": [
            {
                "name": "Summer",
                "months": [6, 7, 8, 9],
                "periods": [
                    {"start": "00:00", "end": "15:00", "rate": 0.2},
                    {"start": "15:00", "end": "16:00", "rate": 0.4},
                    {"start": "16:00", "end": "21:00", "rate": 0.5},
                    {"start": "21:00", "end": "24:00", "rate": 0.4},
                ],
            }
        ],
    }


def test_manual_schedule_roundtrip(tmp_path):
    plan = validate_tariff(schedule())
    assert plan["rates"] == [[0.2] * 7] * 48
    assert plan["billingDay"] == 17
    assert plan["seasons"][0]["rates"][30] == [0.4] * 7
    assert plan["seasons"][0]["rates"][32] == [0.5] * 7
    assert plan["seasons"][0]["rates"][42] == [0.4] * 7
    target = tmp_path / "tariff.json"
    write_tariff(target, plan)
    assert read_tariff(target) == plan
    assert target.stat().st_mode & 0o777 == 0o600


def test_day_groups_and_negative_or_zero_prices():
    periods = [
        {"start": "00:00", "end": "24:00", "rate": 0, "days": [1, 2, 3, 4, 5]},
        {"start": "00:00", "end": "24:00", "rate": -0.1, "days": [6, 7]},
    ]
    assert periods_grid(periods)[0] == [0, 0, 0, 0, 0, -0.1, -0.1]


@pytest.mark.parametrize(
    "change",
    [
        {"billingDay": True},
        {"billingDay": 0},
        {"billingDay": 17.5},
        {"billingDay": 32},
        {"currency": "usd"},
        {"timeZone": "Not/AZone"},
        {"source": "unknown"},
        {"seasons": [{"name": "Bad", "months": [1, 1], "periods": []}]},
        {"seasons": [{"name": "Bad", "months": [13], "periods": []}]},
        {"periods": [{"start": "00:00", "end": "23:30", "rate": 0.2}]},
        {"periods": [{"start": "00:15", "end": "24:00", "rate": 0.2}]},
        {"periods": [{"start": "00:00", "end": "24:00", "rate": float("nan")}]},
        {"periods": [{"start": "00:00", "end": "24:00", "rate": 10**400}]},
        {"periods": [{"start": "00:00", "end": "24:00", "rate": "0.2"}]},
        {"periods": [{"start": "00:00", "end": "24:00", "rate": 0.2}] * 2},
    ],
)
def test_invalid_manual_data_never_overwrites_existing_tariff(tmp_path, change):
    target = tmp_path / "saved.json"
    target.write_text("previous configuration")
    with pytest.raises(ValueError):
        write_tariff(target, {**schedule(), **change})
    assert target.read_text() == "previous configuration"


def test_setup_option_precedence_and_invalid_runtime_price_is_nonfatal(tmp_path, caplog):
    local, setup = tmp_path / "local.json", tmp_path / "setup.json"
    write_tariff(local, schedule())
    assert load_tariff(local, setup)["billingDay"] == 17
    write_tariff(setup, {**schedule(), "billingDay": 12})
    assert load_tariff(local, setup)["billingDay"] == 12
    setup.write_text("{}")
    assert load_tariff(local, setup) is None
    assert "Electricity tariff unavailable" in caplog.text
    setup.unlink()
    local.unlink()
    assert load_tariff(local, setup) is None


def test_legacy_import_and_size_limit(tmp_path):
    legacy = validate_tariff(schedule())
    legacy.pop("seasons")
    legacy.pop("billingDay")
    legacy["version"] = 1
    plan = validate_tariff(legacy)
    assert plan["version"] == 2 and plan["seasons"] == [] and "billingDay" not in plan
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 100_001)
    with pytest.raises(ValueError, match="100 KB"):
        read_tariff(path)


def test_interactive_entry_and_cancellation_preserve_file(tmp_path, monkeypatch):
    answers = iter(
        [
            "Manual",
            "usd",
            "America/Los_Angeles",
            "17",
            "00:00 24:00 0.2",
            "",
            "Summer",
            "6,7,8,9",
            "00:00 24:00 0.4",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    plan = interactive_tariff()
    assert plan["billingDay"] == 17 and plan["seasons"][0]["rates"][0][0] == 0.4
    target = tmp_path / "saved.json"
    target.write_text("previous")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "inverter_control/tariff.py"),
            "--interactive",
            "--output",
            str(target),
        ],
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1 and target.read_text() == "previous"


def test_cli_converts_manual_file_without_prompting(tmp_path):
    source, target = tmp_path / "schedule.json", tmp_path / "tariff.json"
    source.write_text(json.dumps(schedule()))
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "inverter_control/tariff.py"),
            "--input",
            str(source),
            "--output",
            str(target),
        ],
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert read_tariff(target)["billingDay"] == 17


def test_invalid_fallback_path_cannot_stop_controller(tmp_path):
    assert load_tariff(None, tmp_path / "missing.json") is None
