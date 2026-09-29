"""VUE borrows the connected read client without adding I/O ownership or budget."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from inverter_control import dbus, homeassistant, victron

SERVICE = "com.victronenergy.acload.fixture"


class Clock:
    value = 10.0

    def monotonic(self):
        return self.value


@pytest.fixture
def clock(monkeypatch):
    result = Clock()
    monkeypatch.setattr(dbus, "time", result)
    return result


def client(getter=None, count=1):
    with patch.object(dbus.VUESensorDBusClient, "_setup_dbus"):
        value = dbus.VUESensorDBusClient({}, native_get=getter)
    value._available = True
    value._vue_services = {f"sensor{i}": f"{SERVICE}{i}" for i in range(count)}
    return value


def test_discovery_remains_cli_even_when_native_reader_is_injected():
    getter = Mock()
    answers = [
        f'string "{SERVICE}0"\nstring "{SERVICE}1"',
        'string "Configured"',
        'string "Unconfigured"',
    ]
    with patch.object(dbus.subprocess, "run") as cli:
        cli.side_effect = [SimpleNamespace(returncode=0, stdout=x) for x in answers]
        value = dbus.VUESensorDBusClient({"configured": "Configured"}, native_get=getter)
    getter.assert_not_called()
    assert cli.call_count == 3
    assert value._vue_services == {"configured": f"{SERVICE}0", "unconfigured": f"{SERVICE}1"}


def test_all_healthy_queries_share_reader_and_fork_nothing():
    getter = Mock(return_value="123.5")
    value = client(getter, count=3)
    cache = {}
    with patch.object(dbus.subprocess, "run") as cli:
        for _ in range(3):
            value.update_all(cache)
    cli.assert_not_called()
    assert getter.call_count == 9
    assert cache == {f"sensor{i}": 123.5 for i in range(3)}
    assert all(call.kwargs == {"timeout": 0.25} for call in getter.call_args_list)


@pytest.mark.parametrize(
    "raw", [None, "invalid", "nan", "inf", "-inf", "1e999", True, {}, object()]
)
def test_unavailable_or_invalid_native_value_uses_remaining_cli_budget(clock, raw):
    def getter(*_args, **_kwargs):
        clock.value += 0.2
        return raw

    value = client(getter)
    cache = {"sensor0": 9}
    with patch.object(dbus.subprocess, "run") as cli:
        cli.return_value = SimpleNamespace(returncode=0, stdout="variant double 41.5")
        value.update_all(cache)
    assert cli.call_args.kwargs["timeout"] == pytest.approx(1.8)
    assert cache == {"sensor0": 41.5}


def test_native_exception_falls_back(clock):
    getter = Mock(side_effect=OSError("unavailable"))
    with patch.object(dbus.subprocess, "run") as cli:
        cli.return_value = SimpleNamespace(returncode=0, stdout="variant int32 -20")
        cache = {}
        client(getter).update_all(cache)
    assert cache == {"sensor0": -20.0}
    assert cli.call_args.kwargs["timeout"] == 2.0


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(), BaseException("cancel")])
def test_native_cancellation_is_propagated_without_cli(error):
    cache = {"sensor0": 9}
    with patch.object(dbus.subprocess, "run") as cli:
        with pytest.raises(type(error)) as caught:
            client(Mock(side_effect=error)).update_all(cache)
    assert caught.value is error
    cli.assert_not_called()
    assert cache == {"sensor0": 9}


@pytest.mark.parametrize("error", [None, OSError("late failure")])
def test_late_native_response_neither_updates_nor_starts_cli(clock, error):
    def getter(*_args, **_kwargs):
        clock.value += 2
        if error:
            raise error
        return "50"

    cache = {"sensor0": 9}
    with patch.object(dbus.subprocess, "run") as cli:
        client(getter).update_all(cache)
    cli.assert_not_called()
    assert cache == {"sensor0": 9}


def test_expired_before_native_dispatch_does_not_start_io(monkeypatch):
    times = iter([10.0, 12.0])
    monkeypatch.setattr(dbus, "time", SimpleNamespace(monotonic=lambda: next(times)))
    getter = Mock()
    with patch.object(dbus.subprocess, "run") as cli:
        client(getter).update_all({})
    getter.assert_not_called()
    cli.assert_not_called()


def test_cli_only_keeps_two_second_budget_and_discards_late_reply(clock):
    def cli(*_args, **kwargs):
        assert kwargs["timeout"] == 2.0
        clock.value += 2
        return SimpleNamespace(returncode=0, stdout="variant double 40.0")

    cache = {"sensor0": 9}
    with patch.object(dbus.subprocess, "run", side_effect=cli):
        client().update_all(cache)
    assert cache == {"sensor0": 9}


def test_parallel_reads_do_not_serialize_native_waits():
    barrier = threading.Barrier(3, timeout=1)

    def getter(*_args, **_kwargs):
        barrier.wait()
        return "40.0"

    cache = {}
    with patch.object(dbus.subprocess, "run") as cli:
        client(getter, count=3).update_all(cache)
    cli.assert_not_called()
    assert cache == {f"sensor{i}": 40.0 for i in range(3)}


def test_facade_uses_only_connected_reader_and_has_no_fallback():
    value = object.__new__(victron.VictronDBus)
    value._native = Mock()
    value._native.get_value_connected.return_value = "41"
    value._native_write = Mock()
    value._safe_subprocess = Mock()
    assert value.dbus_get_connected(SERVICE, "/Ac/Power", timeout=0.2) == "41"
    value._native.get_value_connected.assert_called_once_with(SERVICE, "/Ac/Power", timeout=0.2)
    assert [call[0] for call in value._native.method_calls] == ["get_value_connected"]
    assert not value._native_write.mock_calls
    value._safe_subprocess.assert_not_called()
    value._native = None
    assert value.dbus_get_connected(SERVICE, "/Ac/Power") is None
    value._safe_subprocess.assert_not_called()


def test_first_singleton_construction_binds_reader_before_polling(monkeypatch):
    getter, later = Mock(), Mock()
    monkeypatch.setattr(homeassistant, "_ha_client", None)
    with (
        patch.object(dbus.VUESensorDBusClient, "_setup_dbus"),
        patch.object(homeassistant.HomeAssistantClient, "start") as start,
    ):
        value = homeassistant.get_ha(vue_native_get=getter)
        assert value._vue_dbus_client._native_get is getter
        assert homeassistant.get_ha(vue_native_get=later) is value
        assert value._vue_dbus_client._native_get is getter
        start.assert_called_once()
    value._session.close()


def test_standalone_ha_does_not_own_or_close_injected_client():
    getter = Mock()
    with patch.object(dbus.VUESensorDBusClient, "_setup_dbus"):
        value = homeassistant.HomeAssistantClient(vue_native_get=getter)
    assert value.stop(timeout=0) is True
    assert not getter.mock_calls
    with patch.object(dbus.VUESensorDBusClient, "_setup_dbus"):
        standalone = homeassistant.HomeAssistantClient()
    assert standalone._vue_dbus_client._native_get is None
    standalone.stop(timeout=0)


def test_controller_injects_reader_from_victron_created_first():
    from inverter_control.controller import InverterController

    source = Mock()
    stop = RuntimeError("stop after dependency wiring")
    calls = []

    def create_victron():
        calls.append("victron")
        return source

    def create_ha(*, vue_native_get):
        calls.append("ha")
        assert vue_native_get is source.dbus_get_connected
        raise stop

    with (
        patch("inverter_control.controller.get_victron", side_effect=create_victron),
        patch("inverter_control.controller.get_ha", side_effect=create_ha),
        pytest.raises(RuntimeError) as caught,
    ):
        InverterController()
    assert caught.value is stop
    assert calls == ["victron", "ha"]


def test_result_waiting_for_cache_assignment_is_not_accepted_after_deadline(clock, monkeypatch):
    original = dbus.as_completed

    def delayed_collect(futures):
        for future in original(futures):
            clock.value += 2
            yield future

    monkeypatch.setattr(dbus, "as_completed", delayed_collect)
    cache = {"sensor0": 9}
    with patch.object(dbus.subprocess, "run") as cli:
        client(Mock(return_value="40")).update_all(cache)
    cli.assert_not_called()
    assert cache == {"sensor0": 9}


def test_active_borrowed_read_survives_ha_stop_timeout_until_poll_exits():
    entered, release = threading.Event(), threading.Event()
    owner = Mock()

    def getter(*_args, **_kwargs):
        entered.set()
        assert release.wait(1)
        return "40"

    owner.get_value_connected.side_effect = getter
    with patch.object(dbus.VUESensorDBusClient, "_setup_dbus"):
        value = homeassistant.HomeAssistantClient(vue_native_get=owner.get_value_connected)
    value._vue_dbus_client._available = True
    value._vue_dbus_client._vue_services = {"sensor0": SERVICE}
    value._poll_all = lambda: value._vue_dbus_client.update_all(value._vue_sensors)
    value._session.close = Mock()
    try:
        value.start()
        assert entered.wait(1)
        assert value.stop(timeout=0) is False
        value._session.close.assert_not_called()
        owner.close.assert_not_called()
    finally:
        release.set()
        assert value.stop(timeout=1) is True
    assert value._vue_sensors["sensor0"] == 40
    value._session.close.assert_called_once()
    owner.close.assert_not_called()
