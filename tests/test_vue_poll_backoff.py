"""Unavailable VUE channels back off independently without slowing healthy data."""

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from inverter_control import dbus

SERVICE = "com.victronenergy.acload.test"


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100.0)
    monkeypatch.setattr(dbus, "time", SimpleNamespace(monotonic=lambda: value.now))
    return value


@pytest.fixture
def failed_cli(monkeypatch):
    call = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
    monkeypatch.setattr(dbus.subprocess, "run", call)
    return call


def client(getter=None, keys=("home",)):
    with patch.object(dbus.VUESensorDBusClient, "_setup_dbus"):
        value = dbus.VUESensorDBusClient({}, native_get=getter)
    value._available = True
    value._vue_services = {key: f"{SERVICE}_{key}" for key in keys}
    return value


def test_retry_delays_grow_and_stay_capped(clock, failed_cli):
    getter = Mock(return_value=None)
    value = client(getter)
    cache = {}
    value.update_all(cache)

    for attempts, delay in enumerate((5, 10, 20, 40, 60, 60, 60), start=1):
        previous = clock.now
        clock.now = previous + delay - 0.001
        value.update_all(cache)
        assert getter.call_count == attempts
        assert failed_cli.call_count == attempts

        clock.now = previous + delay
        value.update_all(cache)
        assert getter.call_count == attempts + 1
        assert failed_cli.call_count == attempts + 1
        assert cache == {}  # No synthetic zero for an unavailable channel.


def test_no_executor_or_transport_calls_while_all_channels_wait(clock, failed_cli):
    getter = Mock(return_value=None)
    value = client(getter)
    value.update_all({})
    getter.reset_mock()
    failed_cli.reset_mock()
    clock.now += 4

    with patch.object(dbus, "ThreadPoolExecutor") as pool:
        value.update_all({})

    pool.assert_not_called()
    getter.assert_not_called()
    failed_cli.assert_not_called()


@pytest.mark.parametrize("power", ["0", "-125.5", "310.25"])
def test_healthy_channel_keeps_normal_cadence_beside_missing_channel(clock, failed_cli, power):
    def read(service, *_args, **_kwargs):
        return power if service.endswith("_home") else None

    getter = Mock(side_effect=read)
    value = client(getter, keys=("home", "missing"))
    cache = {}
    for second in range(5):
        clock.now = 100 + second
        value.update_all(cache)

    home_calls = [call for call in getter.call_args_list if call.args[0].endswith("_home")]
    missing_calls = [call for call in getter.call_args_list if call.args[0].endswith("_missing")]
    assert len(home_calls) == 5
    assert len(missing_calls) == 1
    assert failed_cli.call_count == 1
    assert cache == {"home": float(power)}


def test_recovery_resumes_fast_polling_and_resets_next_failure_delay(clock, failed_cli):
    getter = Mock(return_value=None)
    value = client(getter)
    cache = {}
    for second in (100, 105, 115):
        clock.now = second
        value.update_all(cache)

    getter.return_value = "0"
    clock.now = 134.999
    value.update_all(cache)
    assert getter.call_count == 3
    clock.now = 135
    value.update_all(cache)
    assert cache == {"home": 0.0}
    clock.now = 136
    value.update_all(cache)
    assert getter.call_count == 5

    getter.return_value = None
    clock.now = 137
    value.update_all(cache)
    clock.now = 141.999
    value.update_all(cache)
    assert getter.call_count == 6
    clock.now = 142
    value.update_all(cache)
    assert getter.call_count == 7  # Success reset the prior 20-second delay.


def test_cli_success_is_recovery_even_while_native_reader_fails(clock, failed_cli):
    getter = Mock(side_effect=OSError("native connection unavailable"))
    value = client(getter)
    cache = {}
    value.update_all(cache)
    clock.now += 5
    failed_cli.return_value = SimpleNamespace(returncode=0, stdout="variant double -12.5")
    value.update_all(cache)
    clock.now += 1
    value.update_all(cache)
    assert getter.call_count == 3
    assert failed_cli.call_count == 3
    assert cache == {"home": -12.5}

    failed_cli.return_value = SimpleNamespace(returncode=1, stdout="")
    clock.now += 1
    value.update_all(cache)
    clock.now += 5
    value.update_all(cache)
    assert getter.call_count == 5


def test_retry_wait_starts_after_failed_attempt_finishes(clock, failed_cli):
    def read(*_args, **_kwargs):
        clock.now += 1.5

    getter = Mock(side_effect=read)
    value = client(getter)
    value.update_all({})
    assert clock.now == 101.5

    clock.now = 105
    value.update_all({})
    assert getter.call_count == 1
    clock.now = 106.5
    value.update_all({})
    assert getter.call_count == 2


@pytest.mark.parametrize("transport", ["native", "cli"])
def test_late_success_does_not_clear_backoff_or_publish_zero(clock, failed_cli, transport):
    def read(*_args, **_kwargs):
        if transport == "native":
            clock.now += 2
            return "0"
        return None

    def cli(*_args, **_kwargs):
        clock.now += 2
        return SimpleNamespace(returncode=0, stdout="variant double 0")

    getter = Mock(side_effect=read)
    failed_cli.side_effect = cli
    value = client(getter)
    cache = {}
    value.update_all(cache)
    assert cache == {}
    assert clock.now == 102
    clock.now = 106.999
    value.update_all(cache)
    assert getter.call_count == 1
    assert cache == {}
    clock.now = 107
    value.update_all(cache)
    assert getter.call_count == 2
    assert cache == {}
    assert failed_cli.call_count == (2 if transport == "cli" else 0)


def test_cli_only_reader_also_backs_off(clock, failed_cli):
    value = client()
    value.update_all({})
    clock.now += 1
    value.update_all({})
    assert failed_cli.call_count == 1
    clock.now += 4
    value.update_all({})
    assert failed_cli.call_count == 2


def test_five_minutes_of_one_live_and_eighteen_offline_channels(clock, failed_cli):
    attempts = defaultdict(list)

    def read(service, *_args, **_kwargs):
        attempts[service].append(clock.now)
        return "275" if service.endswith("_home") else None

    keys = ("home", *(f"offline{i}" for i in range(18)))
    value = client(read, keys=keys)
    cache = {}
    for second in range(0, 300, 2):
        clock.now = second
        value.update_all(cache)

    assert len(attempts[f"{SERVICE}_home"]) == 150
    for key in keys[1:]:
        assert attempts[f"{SERVICE}_{key}"] == [0, 6, 16, 36, 76, 136, 196, 256]
    assert failed_cli.call_count == 18 * 8  # Previously 18 * 150 CLI calls.
    assert cache == {"home": 275.0}
