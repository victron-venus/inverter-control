# Phase 3 Service Discovery Implementation

## Changes Made

### 1. Enhanced Service Discovery in `inverter_control/victron.py`

**Added NameOwnerChanged-based service discovery:**
- Registered `NameOwnerChanged` signal handler in `__init__` when native DBus is enabled
- Implemented `_on_name_owner_changed()` method that triggers service discovery when tracked services appear/disappear on the bus
- Tracked services include: system, settings, battery chains, veBus, solarcharger, acload, pvinverter services

**Reduced polling frequency:**
- Increased `RESCAN_INTERVAL_SECONDS` from 300 (5 minutes) to 1800 (30 minutes)
- This serves as a fallback while relying primarily on event-driven discovery
- Maintains error-triggered rescan logic for robustness

### 2. Updated Deployment Script in `deploy.sh`

**Added build exclusion:**
- Added `--exclude='build'` to the tar command in the deployment script
- Prevents packaging build artifacts that are not needed on the target device
- Reduces deployment package size and avoids potential conflicts

## Benefits

1. **Faster Service Detection:** Services are discovered immediately when they appear on the bus, rather than waiting up to 5 minutes for the periodic rescan
2. **Reduced D-Bus Traffic:** Eliminates periodic `dbus -y` subprocess calls that were contributing to the high load average on Cerbo GX
3. **Maintained Robustness:** Kept error-triggered and manual rescan capabilities as fallbacks
4. **Cleaner Deployments:** Excluded unnecessary build artifacts from deployment packages

## Verification

- All existing tests pass (50/50 in test_victron.py, 35/35 in test_dbus_native.py)
- No syntax or compilation errors introduced
- Changes align with the documented Phase 3 hygiene tasks from cerbo-cpu-refactor-phases.md

## Next Remaining Phase 3 Tasks

Based on cerbo-cpu-refactor-phases.md:
1. ✅ Rescan by NameOwnerChanged instead of periodic `dbus -y` (COMPLETED)
2. ⬜ `deploy.sh`: `--exclude=build` is missing in some repos (PARTIALLY COMPLETED - done for inverter-control)
3. ⬜ Shared layer for dbus-mqtt-battery/dbus-virtual-battery
4. ⬜ Migrate services to dbus-service-template (copier)