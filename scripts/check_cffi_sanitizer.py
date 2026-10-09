"""Calibrate ASan detection, check native linkage, then run bounded CFFI fuzzing."""

import json
import os
import platform
import subprocess  # nosec B404
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def checked(argv: list[str], **kwargs) -> str:
    # Image-local compiler/interpreter and fixed arguments; no shell commands.
    return subprocess.check_output(argv, text=True, timeout=120, **kwargs)  # nosec B603


def main() -> None:
    library = checked(["gcc", "-print-file-name=libasan.so"]).strip()
    if not Path(library).is_file():
        raise ValueError("Compiler ASan runtime is unavailable")
    environment = {
        **os.environ,
        "LD_PRELOAD": library,
        "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=1",
    }
    with TemporaryDirectory(prefix="asan-canary-") as temporary:
        source = Path(temporary) / "canary.c"
        binary = Path(temporary) / "canary"
        source.write_text(
            "#include <stdlib.h>\nint main(void) { char *p = malloc(4); p[4] = 7; return p[4]; }\n"
        )
        checked(["gcc", "-O0", "-g", "-fsanitize=address", str(source), "-o", str(binary)])
        # Only our private, freshly compiled calibration executable is invoked.
        canary = subprocess.run(  # nosec B603
            [str(binary)], env=environment, capture_output=True, text=True, timeout=5
        )
    if canary.returncode == 0 or "heap-buffer-overflow" not in canary.stderr:
        raise ValueError("ASan canary did not detect the intentional heap overflow")
    extension = checked(
        ["/tools/bin/python", "-c", "import _cffi_backend; print(_cffi_backend.__file__)"],
        env=environment,
    ).strip()
    if "libasan" not in checked(["readelf", "--dynamic", extension]):
        raise ValueError("CFFI native extension is not linked with ASan")
    result = json.loads(
        checked(["/tools/bin/python", "/check/fuzz_cffi_sanitized.py"], env=environment)
    )
    Path("/sanitizer-result.json").write_text(
        json.dumps(
            {
                "machine": platform.machine(),
                "python": sys.version,
                "address_sanitizer_linked": True,
                "intentional_overflow_detected": True,
                "leak_detection": False,
                "reason": "CPython process lifetime allocations are outside this bounded memory-access check",
                "fuzz": result,
                "scope": "CFFI native API, not instrumentation of prebuilt cryptography/OpenSSL wheels",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
