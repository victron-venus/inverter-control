"""Run the real stdlib runtime selector inside non-root fake-device tests."""

import os
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def install_selector(package: Path, data: Path) -> Path:
    """Remap only the device root and root UID into the test's private tree."""
    selector = package / "runtime_python.py"
    selector.write_text(
        (REPO / "runtime_python.py")
        .read_text()
        .replace("/data/", f"{data}/")
        .replace("OWNER_UID = 0", f"OWNER_UID = {os.getuid()}")
    )
    return data / "setupOptions/inverter-control/runtime-python"


def real_selector_bootstrap(script: str) -> str:
    """Application Python may be stubbed; the selector must still really execute."""
    return script.replace("python3 -I", f"{shlex.quote(sys.executable)} -I")
