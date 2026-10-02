# Explicit ESS mode selection

The header mode picker uses `inverter/cmd/set_ess_mode` (with the configured
MQTT prefix). This is separate from the legacy `ess_mode` empty-payload toggle:
old controllers must never interpret a new selection as a toggle.

A request contains exactly `mode` and `request_id`. IDs are 1–128 ASCII letters,
digits, dots, underscores, colons or hyphens. Supported modes:

- `off`: VE.Bus `/Mode = 4`; preserve the ESS profile.
- `on`: VE.Bus `/Mode = 3`; preserve the ESS profile.
- `optimized_with_battery_life`: BatteryLife state 1, Hub4 mode 1 (preserve 2).
- `optimized_without_battery_life`: BatteryLife state 10, Hub4 mode 1 (preserve 2).
- `keep_batteries_charged`: BatteryLife state 9, Hub4 mode 1 (preserve 2).
- `external_control`: Hub4 mode 3; preserve BatteryLife state.

Profile choices leave the power switch unchanged. Power changes require a
known VE.Bus service advertising `/ModeIsAdjustable = 1`. Failed writes stop the
sequence and invalidate the observation cache; no rollback or automatic retry
is attempted. DRY mode rejects all explicit selections without hardware writes.

`ess_mode` in the normal state publication adds `selection_supported`,
`selected`, `vebus_mode`, `request_id`, and `error`. Selection is derived from
observed switch/settings, not the requested value. With power on, the active
ESS profile takes precedence over the generic `on` state; charger-only,
inverter-only and missing switch readings are not invented as one of these six
choices. BatteryLife states 1–8 and 10–12 map to their corresponding profiles;
legacy state 0 is displayed as optimized without BatteryLife.

The controller rejects retained command messages and remembers the last 128
request IDs for this process, including failures. Re-delivery cannot reapply a
remembered request after a later selection. An acknowledged write and the
observed selection can differ; clients must show the actual observation and
must not infer success from transport acceptance. Capability-aware clients use
QoS 0, no retention and no automatic resend.

Mappings follow the [official Victron service definitions](https://github.com/victronenergy/node-red-contrib-victron/blob/master/src/services/services.json).
This change is source-tested with fake D-Bus; changing real inverter modes is
not part of automated UI testing.
