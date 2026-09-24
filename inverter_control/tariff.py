"""Portable electricity prices for dashboards; independent of inverter control policy.

Run this module as a script to enter or validate a tariff without importing the
controller, credentials, D-Bus or network clients.
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SETUP_FILE = Path("/data/setupOptions/inverter-control/electricity-tariff.json")
DEFAULT_FILE = Path("/data/inverter-control/electricity-tariff.json")
MAX_BYTES = 100_000


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text, at most {limit} characters")
    return value.strip()


def _integer(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _number(value):
    try:
        finite = (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("Every price must be a finite number in currency/kWh; blanks are not zero")
    return value


def _grid(value):
    if not isinstance(value, list) or len(value) != 48:
        raise ValueError("A weekly grid requires 48 half-hour rows")
    result = []
    for row in value:
        if not isinstance(row, list) or len(row) != 7:
            raise ValueError("Each row requires seven prices, Monday through Sunday")
        result.append([_number(rate) for rate in row])
    return result


def _slot(value, *, end=False):
    if end and value == "24:00":
        return 48
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):(?:00|30)", value):
        raise ValueError("Use half-hour times HH:00 or HH:30; 24:00 is only an end time")
    return int(value[:2]) * 2 + value.endswith("30")


def _period_values(period):
    if not isinstance(period, dict):
        raise TypeError("Each period must be an object")
    start, end = _slot(period.get("start")), _slot(period.get("end"), end=True)
    if start >= end:
        raise ValueError("Period end must follow start; split overnight periods at midnight")
    days = period.get("days", list(range(1, 8)))
    if (
        not isinstance(days, list)
        or not days
        or any(not _integer(day, 1, 7) for day in days)
        or len(set(days)) != len(days)
    ):
        raise ValueError("Days must be distinct integers 1 (Monday) through 7 (Sunday)")
    return start, end, days, _number(period.get("rate"))


def periods_grid(periods):
    """Expand human-sized periods; require complete, nonoverlapping weekly coverage."""
    if not isinstance(periods, list) or not periods or len(periods) > 336:
        raise ValueError("Provide a nonempty periods list, at most 336 entries")
    grid = [[None] * 7 for _ in range(48)]
    for period in periods:
        start, end, days, rate = _period_values(period)
        for slot in range(start, end):
            for day in days:
                if grid[slot][day - 1] is not None:
                    raise ValueError("Price periods overlap")
                grid[slot][day - 1] = rate
    return _grid(grid)


def _metadata(data, compact):
    currency = data.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency must be three uppercase letters, for example USD")
    time_zone = _text(data.get("timeZone"), "Time zone", 100)
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown IANA time zone") from exc
    source = data.get("source", "manual" if compact else None)
    if source not in ("manual", "emporia"):
        raise ValueError("Invalid tariff source")
    result = {
        "version": 2,
        "name": _text(data.get("name"), "Name", 120),
        "currency": currency,
        "timeZone": time_zone,
        "source": source,
    }
    if "billingDay" in data:
        if not _integer(data["billingDay"], 1, 31):
            raise ValueError("Billing start day must be an integer 1–31, or omitted")
        result["billingDay"] = data["billingDay"]
    if "reference" in data:
        reference = data["reference"]
        if not isinstance(reference, str) or len(reference) > 200:
            raise ValueError("Invalid tariff reference")
        if reference:
            result["reference"] = reference
    return result


def _season_grids(seasons, convert, grid_key):
    if not isinstance(seasons, list) or len(seasons) > 12:
        raise ValueError("Provide a seasons array with at most 12 seasons")
    used = set()
    result = []
    for season in seasons:
        if not isinstance(season, dict):
            raise TypeError("Each season must be an object")
        months = season.get("months")
        if not isinstance(months, list) or not months:
            raise ValueError("Each season needs calendar months")
        for month in months:
            if not _integer(month, 1, 12) or month in used:
                raise ValueError("Season months must be distinct integers 1–12 with no overlap")
            used.add(month)
        result.append(
            {
                "name": _text(season.get("name"), "Season name", 80),
                "months": sorted(months),
                "rates": convert(season.get(grid_key)),
            }
        )
    return result


def validate_tariff(data):
    """Normalize compact manual schedules or legacy/v2 dashboard exports to v2."""
    if not isinstance(data, dict):
        raise TypeError("Expected a tariff object")
    compact = data.get("type") == "electricity-tariff-schedule"
    version = data.get("version")
    if not _integer(version, 1, 2) or (compact and version != 1):
        raise ValueError("Unsupported tariff version")
    if not compact and version == 1 and ("seasons" in data or "billingDay" in data):
        raise ValueError("Seasonal dashboard exports require version 2")
    convert = periods_grid if compact else _grid
    grid_key = "periods" if compact else "rates"
    result = _metadata(data, compact)
    result["rates"] = convert(data.get(grid_key))
    seasons = data.get("seasons", []) if compact or version == 1 else data.get("seasons")
    result["seasons"] = _season_grids(seasons, convert, grid_key)
    return result


def _read_stream(stream):
    content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Tariff file exceeds 100 KB")
    return validate_tariff(json.loads(content))


def read_tariff(path):
    """Read the operator-configured runtime path, with bounded input size."""
    with Path(path).open("rb") as stream:
        return _read_stream(stream)


def _serialized_tariff(data):
    content = json.dumps(validate_tariff(data), indent=2, allow_nan=False) + "\n"
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Normalized tariff exceeds 100 KB")
    return content


def load_tariff(local_file=DEFAULT_FILE, setup_file=SETUP_FILE):
    """An invalid presentation tariff must never stop the control service."""
    path = setup_file
    try:
        if not Path(setup_file).exists():
            path = local_file
        return read_tariff(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        logging.warning("Electricity tariff unavailable (%s): %s", path, exc)
        return None


def write_tariff(path, data):
    """Validate completely, then atomically replace only the explicitly named file."""
    content = _serialized_tariff(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tariff-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ask_periods():
    print("Enter periods: START END PRICE [DAYS]. Prices are currency/kWh, not cents.")
    print("Example: 00:00 15:00 0.20   or   15:00 24:00 0.40 1,2,3,4,5")
    print("Days default to all seven days. Cover every day from 00:00 to 24:00.")
    periods = []
    while True:
        line = input("Period (blank when complete): ").strip()
        if not line:
            periods_grid(periods)
            return periods
        parts = line.split()
        if len(parts) not in (3, 4):
            raise ValueError("Enter START END PRICE and optional comma-separated DAYS")
        period = {"start": parts[0], "end": parts[1], "rate": float(parts[2])}
        if len(parts) == 4:
            period["days"] = [int(day) for day in parts[3].split(",")]
        periods.append(period)


def interactive_tariff():
    """Read a full manual tariff, without replacing a saved file until validation succeeds."""
    print("Electricity tariff: dashboard energy prices, seasons and billing calendar.")
    data = {
        "type": "electricity-tariff-schedule",
        "version": 1,
        "name": input("Tariff name: ").strip(),
        "currency": input("Currency (e.g. USD): ").strip().upper(),
        "timeZone": input("IANA time zone (e.g. America/Los_Angeles): ").strip(),
    }
    day = input("Billing period start day (1–31; blank if unknown): ").strip()
    if day:
        data["billingDay"] = int(day)
    print("Default schedule for months without a seasonal override:")
    data["periods"] = _ask_periods()
    data["seasons"] = []
    while True:
        name = input("Season name (blank to finish): ").strip()
        if not name:
            return validate_tariff(data)
        months = [int(month) for month in input("Calendar months (e.g. 6,7,8,9): ").split(",")]
        data["seasons"].append({"name": name, "months": months, "periods": _ask_periods()})


def main():
    """Installer/deployment entry point; never prompt unless explicitly requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--interactive", action="store_true", help="manually enter rates and seasons"
    )
    source.add_argument("--stdin", action="store_true", help="read tariff JSON from standard input")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--install", action="store_true", help="atomically save at the fixed SetupHelper path"
    )
    target.add_argument(
        "--normalize", action="store_true", help="write normalized JSON to standard output"
    )
    target.add_argument(
        "--check", action="store_true", help="validate only, without changing files"
    )
    args = parser.parse_args()
    try:
        if args.interactive and args.normalize:
            raise ValueError(
                "Use --stdin with --normalize; interactive entry requires --install or --check"
            )
        plan = interactive_tariff() if args.interactive else _read_stream(sys.stdin.buffer)
        if args.install:
            write_tariff(SETUP_FILE, plan)
        if args.normalize:
            sys.stdout.write(_serialized_tariff(plan))
        else:
            print(
                "Electricity tariff validated"
                + (f" and saved to {SETUP_FILE}" if args.install else "")
            )
        return 0
    except (EOFError, KeyboardInterrupt, OSError, ValueError, TypeError) as exc:
        print(f"Tariff configuration not saved: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
