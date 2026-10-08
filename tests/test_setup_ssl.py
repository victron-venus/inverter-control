"""Obsolete SSL instructions must not mutate trust or contact a device."""

import os
import shutil

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
from pathlib import Path


def test_retired_ssl_entry_point_fails_without_external_actions(tmp_path):
    script = Path(__file__).resolve().parents[1] / "setup_ssl.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = tmp_path / "commands"
    for name in ("openssl", "ssh", "scp", "sudo", "security", "mkdir", "cat", "sed"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name}" >> "{commands}"\nexit 99\n')
        stub.chmod(0o755)
    shell = shutil.which("sh")
    assert shell is not None
    env = {**os.environ, "PATH": str(bin_dir)}
    # Run only the checked-in guard; all historical external actions are stubs.
    result = subprocess.run(  # nosec B603
        [shell, str(script), "test-device"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "retired" in result.stderr
    assert "docs/security-design.md" in result.stderr
    assert not commands.exists()
    assert list(tmp_path.iterdir()) == [bin_dir]
