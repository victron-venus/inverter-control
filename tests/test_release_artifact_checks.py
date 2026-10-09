"""Reject incomplete native artifacts before they become release evidence.

Small real archives exercise artifact checks. External compiler commands are
fixture producers here; executed ARMv7 and ASan builds remain separate evidence.
"""

import hashlib
import importlib
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import build_tls_wheel, check_cffi_sanitizer, package_release


@pytest.fixture
def tls_package(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    packager = importlib.import_module("scripts.package_tls_bundle")
    bundle, output = tmp_path / "bundle", tmp_path / "output"
    bundle.mkdir()
    output.mkdir()
    (bundle / "verification.json").write_text(json.dumps({"offline_imports": True}))
    (bundle / "repeatability.json").write_text(json.dumps({"clean_builds": 2}))
    (bundle / "wheels").mkdir()
    (bundle / "wheels/cffi.whl").write_bytes(b"small artifact fixture")
    (tmp_path / "version").write_text("1.2.3")
    source = output / "inverter-control-1.2.3.tar.gz"
    package_release.archive(tmp_path, ["version"], source, "inverter-control")
    return packager, bundle, output, source


def test_tls_package_inventory_and_checksums_cover_exact_stable_bytes(tls_package):
    packager, bundle, output, source = tls_package
    original_source = source.read_bytes()
    packager.package(bundle, output, "1.2.3")
    native = output / "tls-dependencies-cp312-armv7-1.2.3.tar.gz"
    with tarfile.open(native) as archive:
        members = archive.getnames()
        assert members == [
            "tls-armv7/repeatability.json",
            "tls-armv7/verification.json",
            "tls-armv7/wheels/cffi.whl",
        ]
        for member in members:
            assert (
                archive.extractfile(member).read()
                == (bundle / member.removeprefix("tls-armv7/")).read_bytes()
            )
    checksums = {
        name: digest
        for digest, name in (
            line.split("  ") for line in (output / "SHA256SUMS").read_text().splitlines()
        )
    }
    assert checksums == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, native)
    }
    first = native.read_bytes()
    os.utime(bundle / "wheels/cffi.whl", (1234567890, 1234567890))
    packager.package(bundle, output, "1.2.3")
    assert native.read_bytes() == first
    assert source.read_bytes() == original_source


@pytest.mark.parametrize("failure", ["source", "verification", "repeatability", "symlink"])
def test_tls_package_rejects_incomplete_or_linked_input_before_writing(tls_package, failure):
    packager, bundle, output, source = tls_package
    if failure == "source":
        source.unlink()
    elif failure == "verification":
        (bundle / "verification.json").write_text("{}")
    elif failure == "repeatability":
        (bundle / "repeatability.json").write_text('{"clean_builds": 1}')
    else:
        target = bundle.parent / "operator-secret"
        target.write_text("private fixture")
        (bundle / "wheels/leaked.whl").symlink_to(target)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValueError):
        packager.package(bundle, output, "1.2.3")
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


@pytest.fixture
def cffi_build(tmp_path, monkeypatch):
    source, bundle, repeat = tmp_path / "source", tmp_path / "bundle", tmp_path / "repeat"
    source.mkdir()
    (bundle / "wheels").mkdir(parents=True)
    repeat.mkdir()
    # Both the ordinary build directory and nested metadata must be cleaned.
    (source / "build").mkdir()
    (source / "build/stale-object").write_text("previous compilation")
    (source / "src/cffi.egg-info").mkdir(parents=True)
    paths = {
        "/build/cffi": source,
        "/bundle/wheels": bundle / "wheels",
        "/repeat": repeat,
        "/bundle/repeatability.json": bundle / "repeatability.json",
    }
    monkeypatch.setattr(build_tls_wheel, "Path", lambda name: paths.get(str(name), Path(name)))
    monkeypatch.setenv("CFLAGS", "-O2 -g -fno-omit-frame-pointer")
    state = {"failure": None, "builds": 0, "sections": ".text .debug_info .debug_line"}

    def compile_wheel(argv, *, check):
        assert check
        assert not (source / "build").exists()
        assert not list(source.rglob("*.egg-info"))
        state["builds"] += 1
        (source / "src/cffi.egg-info").mkdir()
        native = source / "build/lib.fixture/_cffi_backend.fixture.so"
        native.parent.mkdir(parents=True)
        compiled = b"compiled extension fixture"
        native.write_bytes(compiled)
        if state["failure"] == "missing_native":
            native.unlink()
        content = b"changed compiled bytes" if state["failure"] == "stripped" else compiled
        if state["failure"] == "nonrepeatable" and state["builds"] == 2:
            content += b"nondeterministic timestamp"
        output = Path(argv[argv.index("-w") + 1])
        if state["failure"] != "missing_wheel":
            with zipfile.ZipFile(output / "cffi-fixture.whl", "w") as archive:
                # Freeze ZIP metadata so this test checks payload bytes, not wall time.
                member = zipfile.ZipInfo(native.name, date_time=(2020, 1, 1, 0, 0, 0))
                archive.writestr(member, content)

    monkeypatch.setattr(build_tls_wheel.subprocess, "run", compile_wheel)
    monkeypatch.setattr(
        build_tls_wheel.subprocess, "check_output", lambda *args, **kwargs: state["sections"]
    )
    return state, bundle


def test_clean_cffi_build_preserves_bytes_debug_information_and_flags(cffi_build):
    state, bundle = cffi_build
    build_tls_wheel.build_twice()
    evidence = json.loads((bundle / "repeatability.json").read_text())
    assert state["builds"] == 2
    assert evidence["clean_builds"] == 2
    assert evidence["debug_requested"] is True
    assert evidence["debug_info_preserved"] is True
    assert evidence["environment"]["CFLAGS"] == "-O2 -g -fno-omit-frame-pointer"
    assert (
        evidence["sha256"]
        == hashlib.sha256((bundle / "wheels/cffi-fixture.whl").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("nonrepeatable", "identical wheels"),
        ("stripped", "compiled extension bytes"),
        ("missing_wheel", "exactly one compiled CFFI wheel"),
        ("missing_native", "exactly one compiled native extension"),
        ("debug", "debug information was stripped"),
    ],
)
def test_cffi_build_rejects_invalid_native_evidence(cffi_build, failure, message):
    state, bundle = cffi_build
    state["failure"] = failure
    if failure == "debug":
        state["sections"] = ".text .data"
    with pytest.raises(ValueError, match=message):
        build_tls_wheel.build_twice()
    assert not (bundle / "repeatability.json").exists()


@pytest.fixture
def asan_check(tmp_path, monkeypatch):
    runtime = tmp_path / "libasan.so"
    runtime.write_bytes(b"runtime fixture")
    receipt = tmp_path / "sanitizer-result.json"
    extension = tmp_path / "_cffi_backend.so"
    extension.write_bytes(b"extension fixture")
    monkeypatch.setattr(
        check_cffi_sanitizer,
        "Path",
        lambda name: receipt if str(name) == "/sanitizer-result.json" else Path(name),
    )
    state = {"failure": None, "fuzz_ran": False}

    def external_tool(argv, **kwargs):
        if argv[:2] == ["gcc", "-print-file-name=libasan.so"]:
            return str(runtime)
        if argv[0] == "gcc":
            # Run a real, isolated executable for the canary process boundary.
            # The fixture producer replaces compilation, not rejection logic.
            binary = Path(argv[argv.index("-o") + 1])
            status = 0 if state["failure"] == "canary_succeeded" else 1
            message = (
                "unrelated crash"
                if state["failure"] == "unrelated_crash"
                else "heap-buffer-overflow"
            )
            binary.write_text(
                f"#!{sys.executable}\nimport os, sys\n"
                f"os.write(2, {message.encode()!r})\nsys.exit({status})\n"
            )
            binary.chmod(0o755)
            return ""
        if argv[0] == "readelf":
            return "libc.so.6" if state["failure"] == "unlinked" else "libasan.so.8"
        assert kwargs["env"]["LD_PRELOAD"] == str(runtime)
        assert kwargs["env"]["ASAN_OPTIONS"] == "detect_leaks=0:abort_on_error=1"
        if argv[1] == "-c":
            return str(extension)
        state["fuzz_ran"] = True
        return json.dumps({"seed": 15293, "cases": 2000})

    monkeypatch.setattr(check_cffi_sanitizer, "checked", external_tool)
    return state, runtime, receipt


def test_asan_receipt_requires_calibration_native_linkage_and_completed_fuzzing(asan_check):
    state, _, receipt = asan_check
    check_cffi_sanitizer.main()
    evidence = json.loads(receipt.read_text())
    assert state["fuzz_ran"]
    assert evidence["address_sanitizer_linked"] is True
    assert evidence["intentional_overflow_detected"] is True
    assert evidence["fuzz"] == {"seed": 15293, "cases": 2000}
    assert evidence["leak_detection"] is False
    assert "not instrumentation of prebuilt" in evidence["scope"]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("missing_runtime", "runtime is unavailable"),
        ("canary_succeeded", "did not detect"),
        ("unrelated_crash", "did not detect"),
        ("unlinked", "not linked with ASan"),
    ],
)
def test_failed_asan_calibration_cannot_produce_a_success_receipt(asan_check, failure, message):
    state, runtime, receipt = asan_check
    state["failure"] = failure
    if failure == "missing_runtime":
        runtime.unlink()
    with pytest.raises(ValueError, match=message):
        check_cffi_sanitizer.main()
    assert state["fuzz_ran"] is False
    assert not receipt.exists()
