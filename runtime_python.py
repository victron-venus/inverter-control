"""Select the package's optional, operator-managed private Python runtime.

This bootstrap uses only the system Python standard library. It never installs
dependencies, changes the selector, or falls back from a broken private runtime.
"""

import os
import re
import stat
import subprocess  # nosec B404 -- fixed argument vector, validated local executable
import sys
from pathlib import Path

SELECTOR = Path("/data/setupOptions/inverter-control/runtime-python")
RUNTIME_ROOT = Path("/data/inverter-control-runtimes")
OWNER_UID = 0
MAX_SELECTOR_BYTES = 4096
PREFIX_CHECK = (
    "import sys; "
    "sys.exit(0 if sys.prefix == sys.argv[1] and sys.base_prefix != sys.prefix "
    "and sys.version_info[:2] == (3, 12) and sys.implementation.name == 'cpython' else 1)"
)


def trusted_metadata(metadata: os.stat_result) -> None:
    """Reject operator configuration or runtime paths writable by other users."""
    if metadata.st_uid != OWNER_UID or metadata.st_mode & 0o022:
        raise ValueError("private runtime paths must be root-owned and not group/world-writable")


def open_selector() -> int | None:
    """Walk trusted real directories without following ancestor or leaf links."""
    parent = None
    try:
        directories = (SELECTOR.parents[2], SELECTOR.parents[1].name, SELECTOR.parent.name)
        for directory in directories:
            descriptor = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            if parent is not None:
                os.close(parent)
            parent = descriptor
            trusted_metadata(os.fstat(parent))
        return os.open(SELECTOR.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    finally:
        if parent is not None:
            os.close(parent)


def read_selector() -> str | None:
    """Only a truly absent selector opts into the legacy system interpreter."""
    descriptor = open_selector()
    if descriptor is None:
        return None
    with os.fdopen(descriptor, "rb") as selector:
        metadata = os.fstat(selector.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("runtime selector must be a regular file, not a link or device")
        trusted_metadata(metadata)
        content = selector.read(MAX_SELECTOR_BYTES + 1)
    if len(content) > MAX_SELECTOR_BYTES:
        raise ValueError("runtime selector is too large")
    return content.decode("ascii")


def private_interpreter(content: str) -> Path:
    """Accept exactly one versioned absolute interpreter path, never shell text."""
    expression = re.escape(str(RUNTIME_ROOT)) + r"/[A-Za-z0-9][A-Za-z0-9._-]*/bin/python3\n?"
    if re.fullmatch(expression, content, re.ASCII) is None:
        raise ValueError("runtime selector must contain one versioned absolute Python path")
    interpreter = Path(content.removesuffix("\n"))
    # Reject links or writable ancestors inside the operator-managed runtime tree.
    for directory in (RUNTIME_ROOT, interpreter.parent.parent, interpreter.parent):
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("private runtime directories must be real directories, not links")
        trusted_metadata(metadata)
    metadata = interpreter.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(interpreter, os.X_OK):
        raise ValueError("private Python must be an executable regular file (venv --copies)")
    trusted_metadata(metadata)
    return interpreter


def select_python() -> str:
    """Validate the private venv prefix before any installer or service starts."""
    content = read_selector()
    if content is None:
        return "python3"
    # The bootstrap/probe are isolated. Do not accept a later application launch
    # whose inherited environment would silently change the selected runtime.
    if any(os.environ.get(name) for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE")):
        raise ValueError(
            "private runtime requires no PYTHONHOME/PYTHONPATH/PYTHONUSERBASE override"
        )
    interpreter = private_interpreter(content)
    result = subprocess.run(  # nosec B603 -- checked local executable, no shell or user arguments
        [str(interpreter), "-I", "-B", "-c", PREFIX_CHECK, str(interpreter.parent.parent)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            "selected Python is not a usable venv with CPython 3.12 at the selected path"
        )
    return str(interpreter)


def main() -> int:
    """Emit only the selected executable, or a nonzero failure without fallback."""
    try:
        interpreter = select_python()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Cannot select inverter-control Python: {error}", file=sys.stderr)
        return 1
    print(interpreter)
    return 0


if __name__ == "__main__":
    sys.exit(main())
