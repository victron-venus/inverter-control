#!/usr/bin/env python3
"""
Simple test to verify caching is working in the VictronDBus class.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import victron


def test_mppt_data_caching():
    """Test that get_mppt_data caches results"""
    print("Testing MPPT data caching...")

    # Reset singleton
    victron._victron = None
    v = victron.VictronDBus(test_mode=True)
    v._mppt_services = ["service1", "service2"]

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        m.returncode = 0
        if "/Yield/Power" in args[0]:
            m.stdout = "variant       double 500.0\n"
        elif "/Dc/0/Current" in args[0]:
            m.stdout = "variant       double 10.5\n"
        else:
            m.stdout = ""
        return m

    with (
        patch("inverter_control.victron.subprocess.run", side_effect=side_effect),
        patch("inverter_control.victron.GROUP_CACHE_TTL", 0.1),
    ):
        # First call - cache unpopulated, so triggers actual D-Bus calls
        data1 = v.get_mppt_data()
        first_call_count = call_count

        # Second call immediately after - should use cache
        data2 = v.get_mppt_data()
        second_call_count = call_count

        # Wait well past the refresh window
        time.sleep(0.15)

        # Third call - getter is now pure-cache (5Hz poll thread owns refresh),
        # so it must NOT re-read D-Bus even after time passes.
        data3 = v.get_mppt_data()
        third_call_count = call_count

        # Verify data consistency
        assert data1 == data2 == data3
        assert data1["mppt0"]["w"] == 500.0
        assert data1["mppt0"]["a"] == 10.5
        assert data1["mppt1"]["w"] == 500.0
        assert data1["mppt1"]["a"] == 10.5

        # Verify caching behavior
        # First call: 2 services * 2 calls each (power + current) = 4 subprocess calls
        assert first_call_count == 4, (
            f"Expected 4 calls on first invocation, got {first_call_count}"
        )
        # Second call: should use cache, so no additional calls
        assert second_call_count == 4, (
            f"Expected 4 calls total after second invocation (cached), got {second_call_count}"
        )
        # Third call: pure-cache - no additional D-Bus calls even after expiry
        assert third_call_count == 4, (
            f"Expected 4 calls total after third invocation (pure cache), got {third_call_count}"
        )

        print("✓ MPPT data caching test passed")


def test_pv_inverter_power_caching():
    """Test that get_pv_power caches results"""
    print("Testing PV inverter power caching...")

    # Reset singleton
    victron._victron = None
    v = victron.VictronDBus(test_mode=True)
    # Mock discovered PV inverter services
    v._pv_inverter_services = [
        "com.victronenergy.pvinverter.pvinverter_120",
        "com.victronenergy.pvinverter.pvinverter_121",
    ]

    call_count = 0

    service1 = v._pv_inverter_services[0]
    service2 = v._pv_inverter_services[1]

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        m.returncode = 0
        if args[0] == [
            "dbus-send",
            "--system",
            "--print-reply=literal",
            f"--dest={service1}",
            "/Ac/Power",
            "com.victronenergy.BusItem.GetValue",
        ]:
            m.stdout = "variant       double 1200.0\n"
        elif args[0] == [
            "dbus-send",
            "--system",
            "--print-reply=literal",
            f"--dest={service2}",
            "/Ac/Power",
            "com.victronenergy.BusItem.GetValue",
        ]:
            m.stdout = "variant       double 800.0\n"
        else:
            m.stdout = ""
        return m

    with (
        patch("inverter_control.victron.subprocess.run", side_effect=side_effect),
        patch("inverter_control.victron.GROUP_CACHE_TTL", 0.1),
    ):
        # First call - cache unpopulated, so triggers actual D-Bus calls
        powers1 = v.get_pv_power()
        first_call_count = call_count

        # Second call immediately after - should use cache
        powers2 = v.get_pv_power()
        second_call_count = call_count

        # Wait well past the refresh window
        time.sleep(0.15)

        # Third call - getter is now pure-cache (5Hz poll thread owns refresh),
        # so it must NOT re-read D-Bus even after time passes.
        powers3 = v.get_pv_power()
        third_call_count = call_count

        # Verify data consistency
        assert powers1 == powers2 == powers3
        assert powers1 == [1200.0, 800.0]

        # Verify caching behavior
        # First call: 2 services * 1 call each = 2 subprocess calls
        assert first_call_count == 2, (
            f"Expected 2 calls on first invocation, got {first_call_count}"
        )
        # Second call: should use cache, so no additional calls
        assert second_call_count == 2, (
            f"Expected 2 calls total after second invocation (cached), got {second_call_count}"
        )
        # Third call: pure-cache - no additional D-Bus calls even after expiry
        assert third_call_count == 2, (
            f"Expected 2 calls total after third invocation (pure cache), got {third_call_count}"
        )

        print("✓ PV inverter power caching test passed")


def test_battery_chain_socs_caching():
    """Test that get_battery_chain_socs caches results"""
    print("Testing battery chain SoC caching...")

    # Reset singleton
    victron._victron = None
    v = victron.VictronDBus(test_mode=True)

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        m.returncode = 0
        cmd = args[0]
        if any("mqtt_chain1" in x for x in cmd) and "/Soc" in cmd:
            m.stdout = "variant       double 75.0\n"
        elif any("mqtt_chain2" in x for x in cmd) and "/Soc" in cmd:
            m.stdout = "variant       double 80.0\n"
        else:
            m.stdout = ""
        return m

    with patch("inverter_control.victron.subprocess.run", side_effect=side_effect):
        # First call - cache unpopulated, so triggers actual D-Bus calls
        socs1 = v.get_battery_chain_socs()
        first_call_count = call_count

        # Second call immediately after - should use cache
        socs2 = v.get_battery_chain_socs()
        second_call_count = call_count

        # Wait well past what was the old 2.0s getter TTL
        time.sleep(2.1)

        # Third call - getter is now pure-cache (5Hz poll thread owns refresh),
        # so it must NOT re-read D-Bus even after time passes.
        socs3 = v.get_battery_chain_socs()
        third_call_count = call_count

        # Verify data consistency
        assert socs1 == socs2 == socs3
        assert socs1 == [75.0, 80.0]

        # Verify caching behavior
        # First call: 2 services * 1 call each = 2 subprocess calls
        assert first_call_count == 2, (
            f"Expected 2 calls on first invocation, got {first_call_count}"
        )
        # Second call: should use cache, so no additional calls
        assert second_call_count == 2, (
            f"Expected 2 calls total after second invocation (cached), got {second_call_count}"
        )
        # Third call: pure-cache getter must not re-read, so still 2 calls total
        assert third_call_count == 2, (
            f"Expected 2 calls total after third invocation (pure cache), got {third_call_count}"
        )

        print("✓ Battery chain SoC caching test passed")


def test_inverter_state_caching():
    """Test that get_inverter_state caches results"""
    print("Testing inverter state caching...")

    # Reset singleton
    victron._victron = None
    v = victron.VictronDBus(test_mode=True)
    v._vebus_service = "test.service"

    # The getter is now pure-cache: it reads natively only on the very first
    # (unpopulated) call. Count native reads to verify it never re-reads.
    call_count = 0

    def native_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return "9"

    with patch(
        "inverter_control.victron.VictronDBus._dbus_get_native_only",
        side_effect=native_side_effect,
    ):
        # First call - cache unpopulated, so triggers a native read
        state1 = v.get_inverter_state()
        first_call_count = call_count

        # Second call immediately after - should use cache
        state2 = v.get_inverter_state()
        second_call_count = call_count

        # Wait well past what was the old 2.0s getter TTL
        time.sleep(2.1)

        # Third call - getter is now pure-cache (5Hz poll thread owns refresh),
        # so it must NOT re-read even after time passes.
        state3 = v.get_inverter_state()
        third_call_count = call_count

        # Verify data consistency
        assert state1 == state2 == state3
        assert state1 == (9, "Inverting")

        # Verify caching behavior
        # First call: exactly one native read (populates the cache)
        assert first_call_count == 1, (
            f"Expected 1 native read on first invocation, got {first_call_count}"
        )
        # Second call: cached, no additional reads
        assert second_call_count == 1, (
            f"Expected 1 native read total after second invocation, got {second_call_count}"
        )
        # Third call: pure-cache getter must not re-read, so still 1 total
        assert third_call_count == 1, (
            f"Expected 1 native read total after third invocation, got {third_call_count}"
        )

        print("✓ Inverter state caching test passed")


if __name__ == "__main__":
    print("Running caching tests...\n")

    test_mppt_data_caching()
    test_pv_inverter_power_caching()
    test_battery_chain_socs_caching()
    test_inverter_state_caching()

    print("\n✅ All caching tests passed!")
