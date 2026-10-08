"""Fuzz untrusted tariff JSON and verify normalization is stable and bounded."""

import io
import json
import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with atheris.instrument_imports():
    from inverter_control.tariff import _read_stream, validate_tariff


def test_one_input(data: bytes) -> None:
    # Exercise the actual bounded import path, including oversized input.
    try:
        tariff = _read_stream(io.StringIO(data.decode("utf-8", errors="replace")))
    except (ValueError, TypeError):
        return
    if tariff is None:
        return
    normalized = validate_tariff(tariff)
    if normalized != tariff:
        raise AssertionError("Tariff normalization must be idempotent")
    restored = validate_tariff(json.loads(json.dumps(tariff, allow_nan=False)))
    if restored != tariff:
        raise AssertionError("Valid tariffs must round-trip through strict JSON")


if __name__ == "__main__":
    atheris.Setup(sys.argv, atheris.instrument_func(test_one_input))
    atheris.Fuzz()
