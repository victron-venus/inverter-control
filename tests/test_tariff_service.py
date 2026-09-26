"""Tariff writes cannot acknowledge failed persistence or overwrite stale edits."""

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
