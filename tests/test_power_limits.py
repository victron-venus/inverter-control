"""Persistent power limits are validated without executing private configuration."""

import json
import os
from unittest.mock import patch

import pytest
from test_config import TestCreepLocalConfig as ConfigHarness

from inverter_control import power_limits as limits


@pytest.fixture
def setup_file(monkeypatch, tmp_path):
    path = tmp_path / "setupOptions" / "power-limits.json"
    monkeypatch.setattr(limits, "SETUP_FILE", path)
    monkeypatch.setattr(limits, "LOCAL_CONFIG", tmp_path / "installed" / "local_config.py")
    monkeypatch.setattr(limits, "SETUP_CONFIG", tmp_path / "setup" / "local_config.py")
    monkeypatch.setattr(limits, "LEGACY_CONFIG", tmp_path / "installed" / "secrets.py")
    monkeypatch.setattr(limits, "SOURCE_ROOT", tmp_path / "source")
    return path


@pytest.mark.parametrize("pair", [(-2300, 2250), (-3000, 3000), (0, 0), (100, 1000), (-2000, -100)])
def test_valid_ranges(pair):
    assert limits.validate_power_limits(*pair) == {"min": pair[0], "max": pair[1]}


@pytest.mark.parametrize(
    "pair", [(True, 1000), (-1000, False), (-1.0, 1), ("-1", 1), (1, -1), (-3001, 1000), (0, 3001)]
)
def test_invalid_ranges(pair):
    with pytest.raises(ValueError):
        limits.validate_power_limits(*pair)


def test_persistent_file_overrides_local_and_defaults(setup_file, monkeypatch):
    assert limits.load_power_limits() == {"min": -2300, "max": 2250}
    loaded = ConfigHarness.load_config(monkeypatch, POWER_LIMIT_MIN=-1700, POWER_LIMIT_MAX=1800)
    assert (loaded.POWER_LIMIT_MIN, loaded.POWER_LIMIT_MAX) == (-1700, 1800)
    limits.write_power_limits({"min": -900, "max": 1100})
    loaded = ConfigHarness.load_config(monkeypatch, POWER_LIMIT_MIN=-1700, POWER_LIMIT_MAX=1800)
    assert (loaded.POWER_LIMIT_MIN, loaded.POWER_LIMIT_MAX) == (-900, 1100)
    assert setup_file.stat().st_mode & 0o777 == 0o600
    assert {name: getattr(loaded, name) for name in loaded.EXPORTED_KEYS}["POWER_LIMIT_MIN"] == -900


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "{}",
        "[]",
        '{"min":-5000,"max":3000}',
        '{"min":false,"max":1}',
        '{"min":0,"max":1,"extra":2}',
    ],
)
def test_invalid_file_does_not_fall_back(setup_file, content):
    setup_file.parent.mkdir()
    setup_file.write_text(content)
    with pytest.raises(ValueError):
        limits.load_power_limits(-1000, 1000)


def test_unreadable_file_does_not_fall_back(setup_file):
    with patch.object(limits.os, "open", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError):
            limits.load_power_limits()


def test_oversized_file_rejected(setup_file):
    setup_file.parent.mkdir()
    setup_file.write_text(" " * (limits.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        limits.load_power_limits()


def test_links_and_nonregular_files_rejected(setup_file, tmp_path):
    target = tmp_path / "other.json"
    target.write_text('{"min":-1000,"max":1000}')
    setup_file.parent.mkdir()
    setup_file.symlink_to(target)
    with pytest.raises(OSError):
        limits.load_power_limits()
    with pytest.raises(ValueError, match="regular file"):
        limits.write_power_limits({"min": -500, "max": 500})
    assert json.loads(target.read_text()) == {"min": -1000, "max": 1000}
    setup_file.unlink()
    os.mkfifo(setup_file)
    with pytest.raises(ValueError, match="regular file"):
        limits.load_power_limits()


def test_failed_replace_preserves_file_and_cleans_temporary(setup_file):
    limits.write_power_limits({"min": -1000, "max": 1000})
    with patch.object(limits.os, "replace", side_effect=OSError("full")):
        with pytest.raises(OSError):
            limits.write_power_limits({"min": -500, "max": 500})
    assert limits.load_power_limits() == {"min": -1000, "max": 1000}
    assert list(setup_file.parent.iterdir()) == [setup_file]


def test_local_config_not_executed(setup_file, tmp_path):
    config = tmp_path / "local_config.py"
    config.write_text(
        "raise RuntimeError('must not run')\nPOWER_LIMIT_MIN=-1700\nPOWER_LIMIT_MAX: int=1800\n"
    )
    assert limits.configured_power_limits(config) == {"min": -1700, "max": 1800}


@pytest.mark.parametrize(
    "source",
    [
        "POWER_LIMIT_MIN = -1000 - 1",
        "if True:\n POWER_LIMIT_MIN=-1000",
        "POWER_LIMIT_MIN, POWER_LIMIT_MAX = (-1000,1000)",
        "POWER_LIMIT_MIN=-1000\nPOWER_LIMIT_MIN+=1",
    ],
)
def test_dynamic_assignments_rejected(setup_file, tmp_path, source):
    config = tmp_path / "local_config.py"
    config.write_text(source)
    with pytest.raises(ValueError, match="literal"):
        limits.configured_power_limits(config)


def test_syntax_error_does_not_echo_secret_source(setup_file, tmp_path, capsys, monkeypatch):
    config = tmp_path / "local_config.py"
    config.write_text("PRIVATE_EXAMPLE = 'do-not-display\n")
    monkeypatch.setattr(limits, "LOCAL_CONFIG", config)
    assert limits.main(["--check"]) == 1
    assert "do-not-display" not in capsys.readouterr().err


def test_cli_check_and_repair_invalid_file(setup_file, capsys):
    args = ["--min", "-1500", "--max", "1600"]
    assert limits.main([*args, "--check"]) == 0
    assert not setup_file.exists()
    assert json.loads(capsys.readouterr().out) == {"min": -1500, "max": 1600}
    setup_file.parent.mkdir()
    setup_file.write_text("invalid")
    assert limits.main([*args, "--install"]) == 0
    assert limits.load_power_limits() == {"min": -1500, "max": 1600}


def test_cli_requires_both_endpoints(setup_file):
    with pytest.raises(SystemExit):
        limits.main(["--min", "-1000", "--install"])
    assert not setup_file.exists()


def test_blank_wizard_preserves_source(setup_file, tmp_path, monkeypatch):
    config = tmp_path / "local_config.py"
    config.write_text("POWER_LIMIT_MIN=-1500\nPOWER_LIMIT_MAX=1600\n")
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(limits, "LOCAL_CONFIG", config)
    assert limits.main(["--interactive", "--install"]) == 0
    assert not setup_file.exists()


def test_wizard_edit_persists_pair(setup_file, tmp_path, monkeypatch):
    config = tmp_path / "local_config.py"
    config.write_text("POWER_LIMIT_MIN=-1500\nPOWER_LIMIT_MAX=1600\n")
    values = iter(["", "1700"])
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    monkeypatch.setattr(limits, "LOCAL_CONFIG", config)
    assert limits.main(["--interactive", "--install"]) == 0
    assert limits.load_power_limits() == {"min": -1500, "max": 1700}


def test_mqtt_persists_applied_values_and_publishes_fresh_state(setup_file):
    from unittest.mock import MagicMock

    from test_main import _make_controller

    import main

    controller, _, _, _ = _make_controller()
    controller.state["limits"] = {"min": -20, "max": 20}
    bridge = MagicMock()
    main._mqtt_safe_limits(controller, {"min": -5000, "max": 1700}, bridge)
    assert limits.load_power_limits() == {"min": -3000, "max": 1700}
    assert bridge.publish_state.call_args.args[0]["limits"] == {"min": -3000, "max": 1700}
    main._mqtt_safe_limits(controller, {"min": -1600}, bridge)
    assert limits.load_power_limits() == {"min": -1600, "max": 1700}
    assert bridge.publish_state.call_args.args[0]["limits"] == {"min": -1600, "max": 1700}
    main._mqtt_safe_limits(controller, {"max": 900, "persist": False}, bridge)
    assert controller.power_limit_max == 900
    assert limits.load_power_limits() == {"min": -1600, "max": 1700}
    assert bridge.publish_state.call_args.args[0]["limits"] == {"min": -1600, "max": 900}


def test_failed_save_keeps_active_limits_and_acknowledges_actual_state(setup_file):
    from unittest.mock import MagicMock

    from test_main import _make_controller

    import main

    controller, _, _, calculator = _make_controller()
    controller.state["limits"] = {"min": -20, "max": 20}
    generation = controller._trim_mode_generation
    bridge = MagicMock()
    with patch("inverter_control.controller.write_power_limits", side_effect=OSError("full")):
        main._mqtt_safe_limits(controller, {"min": -900, "max": 900}, bridge)
    assert (controller.power_limit_min, controller.power_limit_max) == (-2300, 2250)
    assert (calculator.power_limit_min, calculator.power_limit_max) == (-2300, 2250)
    assert controller._trim_mode_generation == generation
    assert bridge.publish_state.call_args.args[0]["limits"] == {"min": -2300, "max": 2250}
    assert not setup_file.exists()


@pytest.mark.parametrize("payload", [{}, [], {"min": -1000, "persist": "true"}])
def test_invalid_mqtt_payload_does_not_persist(setup_file, payload):
    from unittest.mock import MagicMock

    from test_main import _make_controller

    import main

    controller, _, _, _ = _make_controller()
    bridge = MagicMock()
    main._mqtt_safe_limits(controller, payload, bridge)
    assert not setup_file.exists()
    assert bridge.publish_state.call_args.args[0]["limits"] == {"min": -2300, "max": 2250}


def test_slow_persistence_releases_actuator_lock_and_serializes_edits(setup_file):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from test_main import _make_controller

    controller, _, _, calculator = _make_controller()
    saving = threading.Event()
    release = threading.Event()
    calls = []

    def save(pair):
        calls.append(dict(pair))
        if len(calls) == 1:
            saving.set()
            assert release.wait(5)
        return limits.write_power_limits(pair)

    with patch("inverter_control.controller.write_power_limits", side_effect=save):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(controller.set_power_limits, -1000, 1000, persist=True)
            try:
                assert saving.wait(5)
                # A slow disk operation must not stall actual control writes.
                assert controller._watchdog._lock.acquire(timeout=1)
                controller._watchdog._lock.release()
                assert (controller.power_limit_min, controller.power_limit_max) == (-2300, 2250)
                second = pool.submit(controller.set_power_limits, -2000, 2000, persist=True)
            finally:
                release.set()
            assert first.result(timeout=5) == {"min": -1000, "max": 1000}
            assert second.result(timeout=5) == {"min": -2000, "max": 2000}
    assert calls == [{"min": -1000, "max": 1000}, {"min": -2000, "max": 2000}]
    assert limits.load_power_limits() == {"min": -2000, "max": 2000}
    assert (calculator.power_limit_min, calculator.power_limit_max) == (-2000, 2000)


@pytest.mark.parametrize("explicit", [False, True])
def test_bad_local_syntax_rejected_even_with_override(setup_file, tmp_path, explicit, monkeypatch):
    config = tmp_path / "local_config.py"
    config.write_text("return 1\n")
    limits.write_power_limits({"min": -900, "max": 900})
    monkeypatch.setattr(limits, "LOCAL_CONFIG", config)
    args = ["--check"]
    if explicit:
        args.extend(["--min", "-800", "--max", "800"])
    assert limits.main(args) == 1
    assert limits.load_power_limits() == {"min": -900, "max": 900}


def test_valid_persistent_file_can_shadow_dynamic_local_limits(setup_file, tmp_path):
    config = tmp_path / "local_config.py"
    config.write_text('POWER_LIMIT_MIN = int("-800")\nPOWER_LIMIT_MAX = 3001\n')
    limits.write_power_limits({"min": -900, "max": 900})
    assert limits.configured_power_limits(config) == {"min": -900, "max": 900}


@pytest.mark.parametrize("source", limits.CONFIG_SOURCES)
def test_cli_reads_only_the_selected_supported_source(setup_file, source, capsys):
    paths = {
        "installed": limits.LOCAL_CONFIG,
        "setup": limits.SETUP_CONFIG,
        "source": limits.SOURCE_ROOT / "local_config.py",
        "legacy": limits.LEGACY_CONFIG,
        "example": limits.SOURCE_ROOT / "local_config.example.py",
    }
    pairs = {}
    for index, (name, path) in enumerate(paths.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        pair = {"min": -1000 - index, "max": 1000 + index}
        path.write_text(f"POWER_LIMIT_MIN={pair['min']}\nPOWER_LIMIT_MAX={pair['max']}\n")
        pairs[name] = pair
    pairs["defaults"] = {"min": -2300, "max": 2250}
    assert limits.main(["--config-source", source, "--check"]) == 0
    assert json.loads(capsys.readouterr().out) == pairs[source]
    assert not setup_file.exists()


def test_cli_rejects_arbitrary_file_paths_before_reading(setup_file):
    with patch.object(limits.Path, "read_text", side_effect=AssertionError("unexpected read")):
        with pytest.raises(SystemExit):
            limits.main(["--config-source", "/arbitrary/local_config.py", "--check"])
        with pytest.raises(SystemExit):
            limits.main(["--local-config", "/arbitrary/local_config.py", "--check"])
        with pytest.raises(ValueError, match="Unsupported"):
            limits.config_source_path("/arbitrary/local_config.py")


def test_explicit_defaults_source_does_not_read_local_files(setup_file, capsys):
    with patch.object(limits.Path, "read_text", side_effect=AssertionError("unexpected read")):
        assert (
            limits.main(
                ["--config-source", "defaults", "--min", "-900", "--max", "1000", "--check"]
            )
            == 0
        )
    assert json.loads(capsys.readouterr().out) == {"min": -900, "max": 1000}
