"""The service must export persistent site overrides without widening defaults."""

import os

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
from pathlib import Path

import pytest


@pytest.mark.parametrize("override", [None, "192.0.2.10"])
def test_metrics_service_environment(tmp_path, override):
    root = Path(__file__).resolve().parents[1]
    script = (root / "service/inverter-control/run").read_text()
    script = script.replace("/data/inverter-control", str(tmp_path))
    (tmp_path / "run").write_text(script)
    if override:
        (tmp_path / "metrics.env").write_text(f"INVERTER_METRICS_HOST={override}\n")
    fake_python = tmp_path / "python3"
    fake_python.write_text(
        '#!/bin/sh\nprintf "%s:%s\\n" "$INVERTER_METRICS_HOST" "$INVERTER_METRICS_PORT"\n'
    )
    fake_python.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("INVERTER_METRICS_")}
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["sh", str(tmp_path / "run")], env=env, check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == f"{override or '127.0.0.1'}:9102"
