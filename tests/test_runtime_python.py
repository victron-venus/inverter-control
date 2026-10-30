"""Private runtime selection must fail closed before any service is interrupted."""

import importlib.util
import json
import os
import shutil
import subprocess  # nosec B404 -- local fixture processes only
import sys
import venv
from unittest.mock import patch

import pytest
from runtime_fixtures import REPO, install_selector, real_selector_bootstrap
from test_installer import fake_device
from test_setup_options import prepare_setup

SPEC = importlib.util.spec_from_file_location("runtime_python", REPO / "runtime_python.py")
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.fixture
def selection(tmp_path, monkeypatch):
    selector = tmp_path / "data/setupOptions/inverter-control/runtime-python"
    selector.parent.mkdir(parents=True)
    root = tmp_path / "data/inverter-control-runtimes"
    root.mkdir()
    monkeypatch.setattr(runtime, "SELECTOR", selector)
    monkeypatch.setattr(runtime, "RUNTIME_ROOT", root)
    monkeypatch.setattr(runtime, "OWNER_UID", os.getuid())
    for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
        monkeypatch.delenv(variable, raising=False)
    return selector, root


def make_venv(root):
    target = root / "1.23.9-test"
    venv.EnvBuilder(with_pip=False, symlinks=False, system_site_packages=True).create(target)
    return target / "bin/python3"


def test_absent_selector_preserves_legacy_even_with_python_path(selection, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/legacy/site")
    with patch.object(runtime.subprocess, "run") as probe:
        assert runtime.select_python() == "python3"
    probe.assert_not_called()


@pytest.mark.parametrize("newline", ["", "\n"])
def test_private_runtime_uses_actual_venv_prefix(selection, newline):
    selector, root = selection
    interpreter = make_venv(root)
    selector.write_text(str(interpreter) + newline)
    assert runtime.select_python() == str(interpreter)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "python3",
        "/usr/bin/python3\n",
        "{root}/../outside/bin/python3",
        "{root}/./bin/python3",
        "{root}/v1/bin/python3 extra",
        "{root}/v1/bin/python3\n\n",
        "{root}/v1/bin/python3\r\n",
        " {root}/v1/bin/python3\n",
        "{root}/v1/bin/python3\n/usr/bin/python3\n",
        "{root}/$(touch marker)/bin/python3",
        "{root}/`touch marker`/bin/python3",
        "x" * 4097,
    ],
)
def test_malformed_selector_never_probes_or_falls_back(selection, content):
    selector, root = selection
    selector.write_text(content.format(root=root))
    with patch.object(runtime.subprocess, "run") as probe, pytest.raises(ValueError):
        runtime.select_python()
    probe.assert_not_called()


def test_non_ascii_selector_fails(selection):
    selector, _ = selection
    selector.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        runtime.select_python()


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_non_regular_selector_is_not_legacy(selection, kind):
    selector, _ = selection
    if kind in ("symlink", "dangling"):
        target = selector.with_name("target")
        if kind == "symlink":
            target.write_text("/usr/bin/python3")
        selector.symlink_to(target)
    elif kind == "directory":
        selector.mkdir()
    else:
        os.mkfifo(selector)
    with pytest.raises((OSError, ValueError)):
        runtime.select_python()


@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "writable", "regular-file"])
def test_selector_ancestor_cannot_be_untrusted_even_when_leaf_is_absent(selection, level, kind):
    selector, _ = selection
    directory = selector.parents[level]
    if kind == "writable":
        directory.chmod(0o777)
    elif kind == "regular-file":
        shutil.rmtree(directory)
        directory.write_text("not a directory")
    else:
        moved = directory.with_name(directory.name + "-moved")
        directory.rename(moved)
        target = moved if kind == "symlink" else directory.with_name("missing")
        directory.symlink_to(target, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        runtime.read_selector()


@pytest.mark.parametrize("level", [0, 1, 2])
def test_missing_real_ancestor_preserves_legacy_default(selection, level):
    selector, _ = selection
    shutil.rmtree(selector.parents[level])
    assert runtime.select_python() == "python3"


def test_existing_selector_ancestor_requires_trusted_owner(selection, monkeypatch):
    monkeypatch.setattr(runtime, "OWNER_UID", os.getuid() + 1)
    with pytest.raises(ValueError, match="root-owned"):
        runtime.read_selector()


def test_selector_read_stays_in_validated_directory_if_parent_path_is_replaced(
    selection, monkeypatch
):
    selector, _ = selection
    selector.write_text("original trusted selector")
    original_open = os.open
    moved = selector.parent.with_name("validated-parent")
    replacement = selector.parent.with_name("replacement")
    replacement.mkdir()
    (replacement / selector.name).write_text("replacement selector")

    def replace_parent_before_leaf(path, flags, *args, **kwargs):
        if path == selector.name:
            selector.parent.rename(moved)
            selector.parent.symlink_to(replacement, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runtime.os, "open", replace_parent_before_leaf)
    assert runtime.read_selector() == "original trusted selector"


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_writable_selector_is_rejected_before_executable_probe(selection, mode):
    selector, root = selection
    selector.write_text(str(root / "v1/bin/python3"))
    selector.chmod(mode)
    with patch.object(runtime.subprocess, "run") as probe, pytest.raises(ValueError):
        runtime.select_python()
    probe.assert_not_called()


def test_wrong_selector_owner_rejected(selection, monkeypatch):
    selector, _ = selection
    selector.write_text("/usr/bin/python3")
    monkeypatch.setattr(runtime, "OWNER_UID", os.getuid() + 1)
    with pytest.raises(ValueError, match="root-owned"):
        runtime.select_python()


@pytest.mark.parametrize("override", ["PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"])
def test_private_runtime_rejects_environment_that_changes_imports(selection, monkeypatch, override):
    selector, root = selection
    selector.write_text(str(root / "v1/bin/python3"))
    monkeypatch.setenv(override, "/outside")
    with (
        patch.object(runtime.subprocess, "run") as probe,
        pytest.raises(ValueError, match="override"),
    ):
        runtime.select_python()
    probe.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "symlink", "not-executable", "writable", "directory"])
def test_unusable_interpreter_rejected(selection, kind):
    selector, root = selection
    interpreter = root / "v1/bin/python3"
    interpreter.parent.mkdir(parents=True)
    selector.write_text(str(interpreter))
    if kind == "symlink":
        interpreter.symlink_to(sys.executable)
    elif kind == "directory":
        interpreter.mkdir()
    elif kind != "missing":
        interpreter.write_text("#!/bin/sh\nexit 0\n")
        interpreter.chmod(0o644 if kind == "not-executable" else 0o777)
    with patch.object(runtime.subprocess, "run") as probe, pytest.raises((OSError, ValueError)):
        runtime.select_python()
    probe.assert_not_called()


@pytest.mark.parametrize("level", ["root", "version", "bin"])
@pytest.mark.parametrize("kind", ["writable", "symlink"])
def test_untrusted_runtime_directory_rejected(selection, level, kind):
    selector, root = selection
    interpreter = root / "v1/bin/python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    selector.write_text(str(interpreter))
    directory = {"root": root, "version": root / "v1", "bin": root / "v1/bin"}[level]
    if kind == "writable":
        directory.chmod(0o777)
    else:
        moved = directory.with_name(directory.name + "-moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    with patch.object(runtime.subprocess, "run") as probe, pytest.raises(ValueError):
        runtime.select_python()
    probe.assert_not_called()


def test_missing_venv_configuration_does_not_pass_prefix_check(selection):
    selector, root = selection
    interpreter = make_venv(root)
    (interpreter.parent.parent / "pyvenv.cfg").unlink()
    selector.write_text(str(interpreter))
    with pytest.raises(ValueError, match="not a usable venv"):
        runtime.select_python()


@pytest.mark.parametrize(
    "version,implementation,prefix,base_prefix,expected",
    [
        ((3, 12), "cpython", "/selected", "/system", 0),
        ((3, 11), "cpython", "/selected", "/system", 1),
        ((3, 13), "cpython", "/selected", "/system", 1),
        ((3, 12), "pypy", "/selected", "/system", 1),
        ((3, 12), "cpython", "/wrong", "/system", 1),
        ((3, 12), "cpython", "/selected", "/selected", 1),
    ],
)
def test_runtime_probe_requires_supported_abi_and_selected_venv(
    version, implementation, prefix, base_prefix, expected
):
    setup = (
        "import sys, types; "
        f"sys.version_info = {version!r}; "
        f"sys.implementation = types.SimpleNamespace(name={implementation!r}); "
        f"sys.prefix = {prefix!r}; sys.base_prefix = {base_prefix!r}; "
    )
    result = subprocess.run(  # nosec B603 -- fixed test values, real production probe
        [sys.executable, "-I", "-c", setup + runtime.PREFIX_CHECK, "/selected"],
        check=False,
    )
    assert result.returncode == expected


def test_hung_prefix_probe_has_bounded_failure_without_fallback(selection, capsys):
    selector, root = selection
    interpreter = make_venv(root)
    selector.write_text(str(interpreter))
    with patch.object(
        runtime.subprocess, "run", side_effect=subprocess.TimeoutExpired("prefix probe", 10)
    ) as probe:
        assert runtime.main() == 1
    assert probe.call_args.kwargs["timeout"] == 10
    output = capsys.readouterr()
    assert output.out == ""
    assert "Cannot select" in output.err


@pytest.mark.parametrize("kind", ["malformed", "missing-runtime", "writable", "dangling"])
def test_invalid_private_selection_stops_actual_update_before_svc_or_config_change(tmp_path, kind):
    package, services, env = fake_device(tmp_path)
    selector = package.parent / "setupOptions/inverter-control/runtime-python"
    selector.parent.mkdir(parents=True)
    if kind == "dangling":
        selector.symlink_to(selector.with_name("missing"))
    else:
        selector.write_text(
            str(package.parent / "inverter-control-runtimes/v1/bin/python3")
            if kind == "missing-runtime"
            else "/usr/bin/python3"
        )
        if kind == "writable":
            selector.chmod(0o666)
    inode = (services / "inverter-control/supervise").stat().st_ino
    config = package / "local_config.py"
    before = config.read_bytes()
    result = subprocess.run(  # nosec B603, B607 -- fake device commands only
        ["sh", "update.sh", str(package)], cwd=package, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "Cannot select" in result.stderr
    assert not (tmp_path / "commands").exists()
    assert config.read_bytes() == before
    assert (services / "inverter-control/supervise").stat().st_ino == inode


def test_invalid_private_selection_stops_setup_before_copying_config(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    (data / "setupOptions/inverter-control/runtime-python").write_text("/usr/bin/python3")
    result = subprocess.run(  # nosec B603
        ["/bin/bash", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert (package / "local_config.py").read_text() == "EXISTING_PRIVATE = 9\n"
    assert not (data / "updated").exists()


def instrument_private_runtime(root, record):
    """Use a real venv and real imports, recording child interpreter provenance."""
    interpreter = make_venv(root)
    site = root / "1.23.9-test/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = site / "site-packages"
    # Reuse only the local test environment's installed dependencies; no downloads.
    (site / "test-dependencies.pth").write_text(
        "\n".join(path for path in sys.path if path.endswith("site-packages")) + "\n"
    )
    (site / "sitecustomize.py").write_text(
        "import json, sys\n"
        f"with open({str(record)!r}, 'a') as output:\n"
        "    output.write(json.dumps({'prefix': sys.prefix, 'argv': sys.argv}) + '\\n')\n"
    )
    return interpreter


def test_update_preflight_and_both_tariff_steps_use_private_runtime_and_preserve_it(tmp_path):
    from test_tariff import schedule

    package, services, env = fake_device(tmp_path, python_status=43)
    record = tmp_path / "interpreter-calls"
    root = package.parent / "inverter-control-runtimes"
    interpreter = instrument_private_runtime(root, record)
    selector = package.parent / "setupOptions/inverter-control/runtime-python"
    selector.parent.mkdir(parents=True)
    selector.write_text(str(interpreter) + "\n")
    (package / "tariff-install.json").write_text(json.dumps(schedule()))
    (package / "inverter_control/tariff.py").write_text(
        (REPO / "inverter_control/tariff.py").read_text().replace("/data/", f"{package.parent}/")
    )
    inode = (services / "inverter-control/supervise").stat().st_ino
    before = interpreter.read_bytes()
    result = subprocess.run(  # nosec B603 -- real updater within the fake device
        ["/bin/sh", "update.sh", str(package)],
        cwd=package,
        env={key: value for key, value in env.items() if not key.startswith("PYTHON")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    assert all(call["prefix"] == str(interpreter.parent.parent) for call in calls)
    assert any(call["argv"] == ["-", str(package)] for call in calls)
    assert any(call["argv"][1:] == ["--stdin", "--check"] for call in calls)
    assert any(call["argv"][1:] == ["--stdin", "--install"] for call in calls)
    assert "python3" not in (tmp_path / "commands").read_text()
    assert interpreter.read_bytes() == before
    assert selector.read_text() == str(interpreter) + "\n"
    assert (services / "inverter-control/supervise").stat().st_ino == inode
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"


def test_setup_tariff_validation_uses_private_runtime(tmp_path):
    from test_tariff import schedule

    data, package, script = prepare_setup(tmp_path, None)
    record = tmp_path / "setup-interpreter-calls"
    interpreter = instrument_private_runtime(data / "inverter-control-runtimes", record)
    (data / "setupOptions/inverter-control/runtime-python").write_text(str(interpreter))
    (data / "setupOptions/inverter-control/electricity-tariff.json").write_text(
        json.dumps(schedule())
    )
    (package / "inverter_control").mkdir(exist_ok=True)
    shutil.copy2(REPO / "inverter_control/tariff.py", package / "inverter_control/tariff.py")
    result = subprocess.run(  # nosec B603
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("PYTHON")},
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    assert all(call["prefix"] == str(interpreter.parent.parent) for call in calls)
    assert any(call["argv"][1:] == ["--stdin", "--check"] for call in calls)
    assert (data / "updated").exists()


@pytest.mark.parametrize("service", ["inverter-control", "log-forwarder"])
def test_actual_service_runs_selected_venv_not_system_python(tmp_path, service):
    data = tmp_path / "data"
    package = data / "inverter-control"
    package.mkdir(parents=True)
    selector = install_selector(package, data)
    selector.parent.mkdir(parents=True)
    root = data / "inverter-control-runtimes"
    interpreter = make_venv(root)
    selector.write_text(str(interpreter) + "\n")
    target = "main.py" if service == "inverter-control" else "inverter_control/log_forwarder.py"
    module = package / target
    module.parent.mkdir(exist_ok=True)
    module.write_text("import sys; print(sys.prefix); print(sys.argv)\n")
    launcher = tmp_path / "run"
    launcher.write_text(
        real_selector_bootstrap(
            (REPO / f"service/{service}/run")
            .read_text()
            .replace("/data/inverter-control", str(package))
        )
    )
    result = subprocess.run(  # nosec B603
        ["/bin/sh", str(launcher)],
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("PYTHON")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(interpreter.parent.parent), repr([target])]


@pytest.mark.parametrize("service", ["inverter-control", "log-forwarder"])
def test_invalid_selector_does_not_start_service_target(tmp_path, service):
    package = tmp_path / "package"
    package.mkdir()
    selector = install_selector(package, tmp_path / "data")
    selector.parent.mkdir(parents=True)
    selector.write_text("python3")
    launcher = tmp_path / "run"
    launcher.write_text(
        real_selector_bootstrap(
            (REPO / f"service/{service}/run")
            .read_text()
            .replace("/data/inverter-control", str(package))
        )
    )
    result = subprocess.run(["/bin/sh", str(launcher)], capture_output=True, text=True)  # nosec B603
    assert result.returncode == 1
    assert "Cannot select" in result.stdout


def test_service_rechecks_private_runtime_after_persistent_environment_is_loaded(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    data = tmp_path / "data"
    selector = install_selector(package, data)
    selector.parent.mkdir(parents=True)
    selector.write_text(str(data / "inverter-control-runtimes/v1/bin/python3"))
    (package / "metrics.env").write_text("export PYTHONPATH=/unexpected\n")
    launcher = tmp_path / "run"
    launcher.write_text(
        real_selector_bootstrap(
            (REPO / "service/inverter-control/run")
            .read_text()
            .replace("/data/inverter-control", str(package))
        )
    )
    result = subprocess.run(  # nosec B603
        ["/bin/sh", str(launcher)],
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("PYTHON")},
    )
    assert result.returncode == 1
    assert "requires no PYTHONHOME/PYTHONPATH/PYTHONUSERBASE override" in result.stdout


@pytest.mark.parametrize("entrypoint", [["bash", "setup", "install"], ["sh", "update.sh"]])
def test_destdir_does_not_execute_or_read_device_selector(tmp_path, entrypoint):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("setup", "update.sh", "install.py"):
        shutil.copy2(REPO / name, source / name)
    # The stager only copies this payload. Executing it would fail the command.
    (source / "runtime_python.py").write_text("raise RuntimeError('selector must not execute')\n")
    # Populate the remaining shipped payload without using the actual device.
    spec = importlib.util.spec_from_file_location("fixture_install", source / "install.py")
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    for name in installer.PAYLOAD:
        if (source / name).exists():
            continue
        item = REPO / name
        if item.is_dir():
            shutil.copytree(
                item, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        else:
            shutil.copy2(item, source / name)
    stage = tmp_path / "destdir"
    result = subprocess.run(  # nosec B603 -- fixture entrypoint vector only
        entrypoint,
        cwd=source,
        env=os.environ | {"DESTDIR": str(stage)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (stage / "data/inverter-control/runtime_python.py").read_bytes() == (
        source / "runtime_python.py"
    ).read_bytes()
