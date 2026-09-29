"""Tariff writes cannot acknowledge failed persistence or overwrite stale edits."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from inverter_control.tariff import load_tariff, write_tariff
from inverter_control.tariff_service import TariffService, revision


def plan(rate=0.2):
    return {
        "version": 2,
        "name": "Test tariff",
        "currency": "USD",
        "source": "manual",
        "timeZone": "America/Los_Angeles",
        "rates": [[rate] * 7 for _ in range(48)],
        "seasons": [
            {
                "name": "Summer",
                "months": [6, 7, 8, 9],
                "rates": [[rate + 0.1] * 7 for _ in range(48)],
            }
        ],
        "billingDay": 17,
    }


def command(value, expected=None, request_id="test"):
    return {"request_id": request_id, "revision": revision(expected), "plan": value}


def test_persist_restart_idempotence_and_explicit_clear_suppress_fallback(tmp_path):
    file = tmp_path / "tariff.json"
    fallback = tmp_path / "fallback.json"
    write_tariff(fallback, plan(0.9))
    service = TariffService(path=file)
    service.apply(command(plan()))
    assert service.snapshot()["electricity_tariff_status"]["error"] is None
    assert load_tariff(setup_file=file) == plan()
    restarted = TariffService(load_tariff(setup_file=file), path=file)
    assert restarted.snapshot()["electricity_tariff"] == plan()
    with patch(
        "inverter_control.tariff_service.write_tariff",
        side_effect=AssertionError("duplicate write"),
    ):
        restarted.apply(command(plan()))
    assert restarted.snapshot()["electricity_tariff_status"]["error"] is None
    restarted.apply(command(None, plan(), "clear"))
    assert file.read_text().strip() == "null"
    assert load_tariff(local_file=fallback, setup_file=file) is None


def test_failed_persistence_and_stale_edits_preserve_current_plan(tmp_path):
    file = tmp_path / "tariff.json"
    write_tariff(file, plan())
    service = TariffService(plan(), file)
    with patch("inverter_control.tariff_service.write_tariff", side_effect=OSError("disk full")):
        service.apply(command(plan(0.3), plan()))
    assert service.snapshot()["electricity_tariff_status"]["error"] == "disk full"
    assert service.snapshot()["electricity_tariff"] == plan()
    service.apply(command(plan(0.4), None, "stale"))
    assert "changed" in service.snapshot()["electricity_tariff_status"]["error"]
    assert service.snapshot()["electricity_tariff"] == plan()
    assert load_tariff(setup_file=file) == plan()


def test_clear_is_a_valid_persistent_setting_for_later_package_updates(tmp_path):
    import subprocess
    import sys

    service = TariffService(path=tmp_path / "tariff.json")
    service.apply(command(None))
    content = (tmp_path / "tariff.json").read_text()
    result = subprocess.run(
        [sys.executable, "inverter_control/tariff.py", "--stdin", "--check"],
        input=content,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0


def test_snapshot_and_command_objects_cannot_mutate_committed_plan(tmp_path):
    service = TariffService(path=tmp_path / "tariff.json")
    value = plan()
    service.apply(command(value))
    value["rates"][0][0] = 9
    snapshot = service.snapshot()
    snapshot["electricity_tariff"]["rates"][0][0] = 8
    snapshot["electricity_tariff_status"]["revision"] = "wrong"
    assert service.snapshot()["electricity_tariff"] == plan()
    assert service.snapshot()["electricity_tariff_status"]["revision"] == revision(plan())


def test_oversized_command_never_writes_or_changes_current_plan(tmp_path):
    file = tmp_path / "tariff.json"
    service = TariffService(plan(), file)
    service.apply(command({**plan(), "padding": "x" * 100_000}, plan()))
    assert "100 KB" in service.snapshot()["electricity_tariff_status"]["error"]
    assert service.snapshot()["electricity_tariff"] == plan()
    assert not file.exists()


def test_mqtt_edit_publishes_current_tariff_before_telemetry_without_changing_controls(tmp_path):
    from test_main import _make_controller

    import main

    controller, victron, _, _ = _make_controller(ui_config={"electricity_tariff": plan()})
    controller.tariff = TariffService(plan(), tmp_path / "tariff.json")
    flags = dict(controller._control_flags)
    dry_run = controller.dry_run
    assert controller.state == {}
    with (
        patch("main.MQTT_AVAILABLE", True),
        patch("inverter_control.config.MQTT_BROKER", "test-broker"),
        patch("main.get_mqtt_bridge") as get_bridge,
    ):
        bridge = get_bridge.return_value
        main._setup_mqtt_bridge(controller)
        callbacks = dict(call.args for call in bridge.register_callback.call_args_list)
        callbacks["electricity_tariff"](command(plan(0.4), plan(), "editor-save"))
    published = bridge.publish_state.call_args.args[0]["ui_config"]
    assert published["electricity_tariff"] == plan(0.4)
    assert published["electricity_tariff_status"] == {
        "writable": True,
        "revision": revision(plan(0.4)),
        "request_id": "editor-save",
        "error": None,
    }
    assert controller.get_state()["ui_config"] == published
    assert controller._control_flags == flags and controller.dry_run == dry_run
    victron.set_grid_setpoint.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        command({}),
        command(plan(), request_id="bad/id"),
        {**command(plan()), "revision": "bad"},
        {**command(plan()), "extra": True},
    ],
)
def test_invalid_commands_never_write(tmp_path, payload):
    file = tmp_path / "tariff.json"
    service = TariffService(path=file)
    service.apply(payload)
    assert service.snapshot()["electricity_tariff_status"]["error"]
    assert service.snapshot()["electricity_tariff"] is None
    assert not file.exists()


@pytest.mark.parametrize("persistence_fails", [False, True])
def test_control_snapshot_does_not_wait_for_tariff_fsync(tmp_path, persistence_fails):
    from test_main import _make_controller

    file = tmp_path / "tariff.json"
    original, changed = plan(), plan(0.4)
    write_tariff(file, original)
    controller, victron, _, _ = _make_controller()
    controller.tariff = TariffService(original, file)
    previous = controller.get_state_for_mqtt()["ui_config"]
    fsync_entered, release_fsync = threading.Event(), threading.Event()
    real_fsync = os.fsync

    def delayed_fsync(fd):
        fsync_entered.set()
        if not release_fsync.wait(2):
            raise TimeoutError("test did not release persistence")
        if persistence_fails:
            raise OSError("disk full")
        real_fsync(fd)

    with (
        ThreadPoolExecutor(max_workers=2) as workers,
        patch("inverter_control.tariff.os.fsync", side_effect=delayed_fsync),
    ):
        write = workers.submit(controller.tariff.apply, command(changed, original, "saving"))
        try:
            assert fsync_entered.wait(1)
            # Exercise the real controller publication path, not a test-only getter.
            read = workers.submit(controller.get_state_for_mqtt)
            during = read.result(timeout=0.5)["ui_config"]
            assert during == previous
            assert not write.done()
            assert load_tariff(setup_file=file) == original
        finally:
            release_fsync.set()
        write.result(timeout=1)

    after = controller.get_state_for_mqtt()["ui_config"]
    expected = original if persistence_fails else changed
    assert after["electricity_tariff"] == expected
    assert after["electricity_tariff_status"] == {
        "writable": True,
        "revision": revision(expected),
        "request_id": "saving",
        "error": "disk full" if persistence_fails else None,
    }
    assert load_tariff(setup_file=file) == expected
    victron.set_grid_setpoint.assert_not_called()


@pytest.mark.parametrize("chained_revision", [False, True])
def test_overlapping_tariff_edits_serialize_revision_and_persistence(tmp_path, chained_revision):
    file = tmp_path / "tariff.json"
    original, first, second = plan(), plan(0.3), plan(0.4)
    write_tariff(file, original)
    service = TariffService(original, file)
    first_fsync, release_first, second_started, second_fsync = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    fsync_calls = []
    real_fsync = os.fsync

    def delayed_first_fsync(fd):
        fsync_calls.append(fd)
        if len(fsync_calls) == 1:
            first_fsync.set()
            if not release_first.wait(2):
                raise TimeoutError("test did not release persistence")
        else:
            second_fsync.set()
        real_fsync(fd)

    def second_edit():
        second_started.set()
        service.apply(command(second, first if chained_revision else original, "second"))

    with (
        ThreadPoolExecutor(max_workers=2) as workers,
        patch("inverter_control.tariff.os.fsync", side_effect=delayed_first_fsync),
    ):
        write_one = workers.submit(service.apply, command(first, original, "first"))
        try:
            assert first_fsync.wait(1)
            write_two = workers.submit(second_edit)
            assert second_started.wait(1)
            assert not write_one.done()
            assert not second_fsync.wait(0.1)
            assert len(fsync_calls) == 1
        finally:
            release_first.set()
        write_one.result(timeout=1)
        write_two.result(timeout=1)

    result = service.snapshot()
    expected = second if chained_revision else first
    assert result["electricity_tariff"] == expected
    assert result["electricity_tariff_status"]["revision"] == revision(expected)
    assert result["electricity_tariff_status"]["request_id"] == "second"
    if chained_revision:
        assert result["electricity_tariff_status"]["error"] is None
    else:
        assert "changed" in result["electricity_tariff_status"]["error"]
    assert len(fsync_calls) == (2 if chained_revision else 1)
    assert load_tariff(setup_file=file) == expected
