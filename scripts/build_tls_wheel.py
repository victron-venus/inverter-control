"""Build the CFFI wheel twice from clean objects and require identical bytes."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess  # nosec B404
import sys
import zipfile
from pathlib import Path


def build_twice() -> None:
    source = Path("/build/cffi")
    destinations = [Path("/bundle/wheels"), Path("/repeat")]
    identities = []
    for output in destinations:
        shutil.rmtree(source / "build", ignore_errors=True)
        for metadata in source.rglob("*.egg-info"):
            shutil.rmtree(metadata)
        # Build-tool path and all arguments are fixed by the immutable build image.
        subprocess.run(  # nosec B603
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--verbose",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                str(source),
                "-w",
                str(output),
            ],
            check=True,
        )
        wheels = list(output.glob("cffi-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one compiled CFFI wheel")
        identities.append(hashlib.sha256(wheels[0].read_bytes()).hexdigest())
    if identities[0] != identities[1]:
        raise ValueError("Clean CFFI builds did not produce identical wheels")
    native = list((source / "build").glob("lib*/_cffi_backend*.so"))
    if len(native) != 1:
        raise ValueError("Expected exactly one compiled native extension")
    with zipfile.ZipFile(wheels[0]) as wheel:
        if wheel.read(native[0].name) != native[0].read_bytes():
            raise ValueError("Wheel must preserve the compiled extension bytes")
    # The pinned build image supplies GNU binutils; no shell is involved.
    sections = subprocess.check_output(["readelf", "--sections", str(native[0])], text=True)  # nosec B603, B607
    debug_requested = any(
        flag == "-g" or flag in {"-g1", "-g2", "-g3", "-ggdb", "-ggdb3"}
        for flag in shlex.split(os.environ.get("CFLAGS", ""))
    )
    debug_present = ".debug_info" in sections
    if debug_requested and not debug_present:
        raise ValueError("Requested debug information was stripped from the wheel")
    Path("/bundle/repeatability.json").write_text(
        json.dumps(
            {
                "clean_builds": 2,
                "sha256": identities[0],
                "debug_requested": debug_requested,
                "debug_info_preserved": debug_present,
                "scope": "Same pinned image, inputs, source path, epoch and compiler flags; clean objects each run",
                "environment": {
                    key: os.environ.get(key, "")
                    for key in (
                        "CC",
                        "CXX",
                        "CPPFLAGS",
                        "CFLAGS",
                        "CXXFLAGS",
                        "LDFLAGS",
                        "SOURCE_DATE_EPOCH",
                    )
                },
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    build_twice()
