"""Additional tests to improve coverage of victron.py"""

import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from inverter_control import victron


class TestVictronCoverage:
    """Test victron.py methods for coverage"""

    def setup_method(self):
        """Reset singleton before each test"""
        victron.reset_victron_for_testing()

    def teardown_method(self):
        """Reset singleton after each test"""
        victron.reset_victron_for_testing()

    def test_fast_targets_with_shunt_service(self):
        """Cover lines in _fast_targets that involve shunt service"""
        v = victron.get_victron(test_mode=False)  # test_mode=False to possibly get native client
        # Manually set services to avoid needing real D-Bus discovery
        v._shunt_service = "com.victronenergy.battery.ttyUSB0"
        v._vebus_service = None
        v._mppt_services = []
        v._pv_inverter_services = []
        v._acload_services = []
        targets = v._fast_targets()
        # Should have added shunt service paths
        assert any(t[0] == v._shunt_service for t in targets)
        # Also check that the paths are from SHUNT_SIGNAL_PATHS
        from inverter_control.victron import SHUNT_SIGNAL_PATHS

        for service, path in targets:
            if service == v._shunt_service:
                assert path in SHUNT_SIGNAL_PATHS

    def test_fast_targets_with_vebus_service(self):
        """Cover lines in _fast_targets that involve vebus service"""
        v = victron.get_victron(test_mode=False)
        v._shunt_service = None
        v._vebus_service = "com.victronenergy.vebus.ttyUSB0"
        v._mppt_services = []
        v._pv_inverter_services = []
        v._acload_services = []
        targets = v._fast_targets()
        assert any(t[0] == v._vebus_service for t in targets)
        from inverter_control.victron import VEBUS_STATE_PATH, VEBUS_INV_POWER_PATH

        for service, path in targets:
            if service == v._vebus_service:
                assert path in (VEBUS_STATE_PATH, VEBUS_INV_POWER_PATH)

    def test_fast_targets_with_mppt_services(self):
        """Cover lines in _fast_targets that involve MPPT services"""
        v = victron.get_victron(test_mode=False)
        v._shunt_service = None
        v._vebus_service = None
        v._mppt_services = ["com.victronenergy.solarcharger.ttyUSB0"]
        v._pv_inverter_services = []
        v._acload_services = []
        targets = v._fast_targets()
        assert any(t[0] == v._mppt_services[0] for t in targets)
        from inverter_control.victron import MPPT_SIGNAL_PATHS

        for service, path in targets:
            if service == v._mppt_services[0]:
                assert path in MPPT_SIGNAL_PATHS

    def test_fast_targets_with_pv_inverter_services(self):
        """Cover lines in _fast_targets that involve PV inverter services"""
        v = victron.get_victron(test_mode=False)
        v._shunt_service = None
        v._vebus_service = None
        v._mppt_services = []
        v._pv_inverter_services = ["com.victronenergy.vebus.ttyUSB0"]  # reuse
        v._acload_services = []
        targets = v._fast_targets()
        assert any(t[0] == v._pv_inverter_services[0] for t in targets)
        from inverter_control.victron import PV_SIGNAL_PATHS

        for service, path in targets:
            if service == v._pv_inverter_services[0]:
                assert path in PV_SIGNAL_PATHS

    def test_fast_targets_with_acload_services(self):
        """Cover lines in _fast_targets that involve aload services"""
        v = victron.get_victron(test_mode=False)
        v._shunt_service = None
        v._vebus_service = None
        v._mppt_services = []
        v._pv_inverter_services = []
        v._acload_services = ["com.victronenergy.acload.ttyUSB0"]
        targets = v._fast_targets()
        assert any(t[0] == v._acload_services[0] for t in targets)
        from inverter_control.victron import ACLOAD_NAME_PATH, AC_POWER_PATH

        for service, path in targets:
            if service == v._acload_services[0]:
                assert path in (ACLOAD_NAME_PATH, AC_POWER_PATH)

    def test_setup_fast_signals_native_none(self):
        """Cover line 253: early return when _native is None"""
        v = victron.get_victron(test_mode=True)  # test_mode=True => _native = None
        # Mock the method to avoid actually subscribing (but we want to hit the early return)
        # We can just call the method; it should return immediately.
        # We'll also need to ensure _signal_handler_attached is False to avoid AttributeError?
        # Actually the early return is before touching _signal_handler_attached.
        v._native = None
        v._setup_fast_signals()  # Should just return
        # No assertion needed; if we get here without exception, the early return worked.

    def test_setup_fast_signals_with_shunt_service(self):
        """Cover line 262: subscribed append for shunt service"""
        # We need to patch NativeDbusClient BEFORE creating VictronDBus instance
        with patch("inverter_control.victron.NativeDbusClient") as MockNative:
            mock_native_instance = MagicMock()
            MockNative.return_value = mock_native_instance
            # Make the mock's methods return something truthy so that all(subscribed) works
            mock_native_instance.subscribe_service_items.return_value = True
            mock_native_instance.subscribe_busitem.return_value = True

            v = victron.get_victron(test_mode=False)
            # We need _native not None and _shunt_service not None.
            v._shunt_service = "com.victronenergy.battery.ttyUSB0"
            v._vebus_service = None
            v._mppt_services = []
            v._pv_inverter_services = []
            v._acload_services = []
            # Now call _setup_fast_signals
            v._setup_fast_signals()
            # Verify that subscribe_service_items was called with the shunt service
            mock_native_instance.subscribe_service_items.assert_any_call(v._shunt_service)

    def test_setup_fast_signals_with_vebus_service(self):
        """Cover lines 265-267: vebus service signals"""
        # We need to patch NativeDbusClient BEFORE creating VictronDBus instance
        with patch("inverter_control.victron.NativeDbusClient") as MockNative:
            mock_native_instance = MagicMock()
            MockNative.return_value = mock_native_instance
            # Make the mock's methods return something truthy so that all(subscribed) works
            mock_native_instance.subscribe_service_items.return_value = True
            mock_native_instance.subscribe_busitem.return_value = True

            v = victron.get_victron(test_mode=False)
            v._shunt_service = None
            v._vebus_service = "com.victronenergy.vebus.ttyUSB0"
            v._mppt_services = []
            v._pv_inverter_services = []
            v._acload_services = []
            v._setup_fast_signals()
            # Check that the vebus service calls were made
            from inverter_control.victron import VEBUS_STATE_PATH, VEBUS_INV_POWER_PATH

            mock_native_instance.subscribe_service_items.assert_any_call(v._vebus_service)
            mock_native_instance.subscribe_busitem.assert_any_call(
                v._vebus_service, VEBUS_STATE_PATH
            )
            mock_native_instance.subscribe_busitem.assert_any_call(
                v._vebus_service, VEBUS_INV_POWER_PATH
            )

    def test_setup_fast_signals_discovered_services(self):
        """Cover line 272: loop over discovered services"""
        # We need to patch NativeDbusClient BEFORE creating VictronDBus instance
        with patch("inverter_control.victron.NativeDbusClient") as MockNative:
            mock_native_instance = MagicMock()
            MockNative.return_value = mock_native_instance
            # Make the mock's methods return something truthy so that all(subscribed) works
            mock_native_instance.subscribe_service_items.return_value = True

            v = victron.get_victron(test_mode=False)
            v._shunt_service = None
            v._vebus_service = None
            v._mppt_services = ["com.victronenergy.solarcharger.ttyUSB0"]
            v._pv_inverter_services = []
            v._acload_services = []
            v._setup_fast_signals()
            mock_native_instance.subscribe_service_items.assert_any_call(v._mppt_services[0])

    def test_setup_fast_signals_all_subscribed_true(self):
        """Cover line 275: setting _last_signal_ok_monotonic when all subscribed succeed"""
        # We need to patch NativeDbusClient BEFORE creating VictronDBus instance
        with patch("inverter_control.victron.NativeDbusClient") as MockNative:
            mock_native_instance = MagicMock()
            MockNative.return_value = mock_native_instance
            # Make the mock's methods return something truthy so that all(subscribed) works
            mock_native_instance.subscribe_service_items.return_value = True
            mock_native_instance.subscribe_busitem.return_value = True

            v = victron.get_victron(test_mode=False)
            v._shunt_service = "com.victronenergy.battery.ttyUSB0"
            v._vebus_service = None
            v._mppt_services = []
            v._pv_inverter_services = []
            v._acload_services = []
            v._setup_fast_signals()
            # If all(subscribed) is True, then _last_signal_ok_monotonic should be set
            assert v._last_signal_ok_monotonic is not None

    # Now we need to cover other missing lines, e.g., in _get_float_nolock, get_all_batteries, etc.
    # We'll add more tests as time permits.
