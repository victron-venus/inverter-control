"""Hardware-free Venus/D-Bus test stubs.

The real `inverter_control.victron.VictronDBus` is built to talk to a live
Victron Venus OS system bus (subprocess `dbus-send` + a `dbus_fast` native
client). `test_mode=True` already disables the native client and most
subprocess paths, but it does NOT pre-populate the cached values that
control-loop code reads from — most callers need a fully-fleshed fake.

`FakeVictronDBus` provides that: a `VictronDBus` instance with `test_mode=True`
plus a small declarative `set_value` API for tests to pre-seed values without
touching any subprocess.

Usage:
    from tests.stubs import FakeVictronDBus

    def test_grid_estimate(fresh_victron):
        fresh_victron.set_value("vebus_state", 3)        # Hub-4
        fresh_victron.set_value("g1", 100.0)
        fresh_victron.set_value("g2", -50.0)
        # ... exercise code under test
        assert fresh_victron.get_value("g1") == 100.0
"""

from tests.stubs.fake_victron import FakeVictronDBus, fake_victron

__all__ = ["FakeVictronDBus", "fake_victron"]
