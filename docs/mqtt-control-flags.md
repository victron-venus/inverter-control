# Inverter control flags over MQTT

`inverter-control` owns the seven inverter flags. Inverter Desktop and Home
Assistant are independent MQTT clients. Neither creating the desktop buttons nor
reading or changing a flag requires a Home Assistant connection.

The definitions and display labels live in `inverter_control/control_flags.py`.
`InverterController._control_flags` holds the current values; the controller uses
`get_control_flag()` and `set_control_flag()` internally. The older Python methods
`get_boolean()` and `set_boolean()` remain compatibility aliases.

## Published state and button definitions

The daemon publishes JSON to `<prefix>/state` (`inverter/state` by default),
with QoS 0 and `retain=true`. The existing `booleans` field contains the current
flag values. `ui_config.header_toggles` advertises the buttons as objects with
`id`, `label`, and `entity`; `id` and `entity` are bare control keys.

For example, a subset of a state message is:

```json
{
  "booleans": {"only_charging": false},
  "ui_config": {
    "header_toggles": [
      {"id": "only_charging", "label": "ONLY CHARGING", "entity": "only_charging"}
    ]
  }
}
```

The full message advertises `only_charging`, `no_feed`, `house_support`,
`charge_battery`, `do_not_supply_charger`, `set_limit_to_ev_charger`, and
`minimize_charging`. Metadata is included even if a command causes a state
publication before the first telemetry sweep. This is an additive extension to
the existing `ui_config` payload; older clients can keep their fallback labels.

## Commands and state ownership

Clients publish commands without retention to `<prefix>/cmd/toggle`:

```json
{"entity": "only_charging", "state": "on"}
```

Use explicit `"on"` or `"off"` for deterministic setters. Booleans, numeric
`0`/`1`, and string `true`/`false`/`0`/`1` are also accepted. An omitted `state`
toggles the latest daemon value. An invalid explicit state is ignored. Legacy
`input_boolean.<key>` aliases remain accepted for these seven known keys; they
do not invoke an HA API. Other entities retain the separate legacy HA command
forwarding behavior.

On acceptance the daemon updates its flag, mirrors its value to
`/Settings/InverterControl/<PascalCaseKey>` on Venus, and republishes its state.
The in-process value remains authoritative if the Settings mirror fails. Flags
start **off on every daemon restart**; existing Settings or HA values do not
restore them. Writing the Settings mirror directly does not change a daemon
flag. Use the command topic to change a flag.

## EV exclusion when the site measurement is unavailable

`DO NOT SUPPLY EV` uses the configured home charger meter. Vehicle telemetry is
not a substitute: a vehicle may report charging at another location. A measured
zero remains a valid reading; missing, disconnected, expired or invalid site
power is unknown.

With a valid site measurement, `DO NOT SUPPLY EV` subtracts the EV load from
raw, filtered and derived-grid calculations. It does not impose a solar-only
output cap: the battery can support the remaining house load unless a separate
operating mode restricts it. `ONLY CHARGING` retains its solar-only limit;
`NO FEED` retains its own target and existing mode priority.

If site power is unknown and both `ONLY CHARGING` and `NO FEED` are off,
normal load regulation continues. `[NoEV:exclusion-unavailable]` indicates that
the controller cannot currently separate EV demand from house demand. This
explicitly prioritizes house support; some EV demand can temporarily be supplied.
No stale EV estimate or vehicle measurement is substituted for the missing input.

If either `ONLY CHARGING` or `NO FEED` is on together with `DO NOT SUPPLY EV`,
unknown site power applies a nonnegative automatic setpoint floor. This stops
requesting AC output without forcing the `CHARGE BATTERY` target. The
`[NoEV:site-unknown]` diagnostic identifies this restriction. The floor is applied
after mode priorities, slew limits and ordinary power limits. An incompatible
negative-only range yields zero and `[NoEV:limits-conflict]`.

The bounded-age cache and all three flags are checked again at the final write.
Availability diagnostics describe that final check. If a restrictive mode is
switched off after calculation, the already restricted candidate may be sent
for that cycle; the next cycle calculates ordinary house support. The write
boundary does not reconstruct an earlier candidate from a stale setpoint.
Only an accepted write becomes the next control baseline.

These rules govern automatic commands only. Explicit one-shot/manual overrides
and the existing grid-loss watchdog retain their ownership. They do not validate
the accuracy of a non-null meter reading or change the configured meter/channel
binding. A valid zero measurement remains different from unknown.

## Optional Home Assistant switches

The repository's `mqtt.yaml` is an optional HA MQTT configuration, included under
`mqtt:` in HA. It subscribes to `inverter/state` and creates binary sensors and
switches. Each switch sends the same explicit JSON commands as Desktop and waits
for the daemon's state message to confirm its state. If a custom topic prefix is
used, update this HA configuration to match.

The state template emits `ON`/`OFF` while command payloads contain JSON. Therefore
each switch explicitly declares `state_on: "ON"` and `state_off: "OFF"`; otherwise
HA compares the template result against the JSON command payload. See the
[Home Assistant MQTT Switch contract](https://www.home-assistant.io/integrations/switch.mqtt/).

The daemon does not currently publish Home Assistant MQTT discovery messages.
`ui_config.header_toggles` is desktop presentation metadata, not HA discovery.

`minimize_charging` is also a daemon-owned flag, but its current dump-load
automation uses HA's `net_usage` sensor and HA switch service calls. Reading and
toggling this flag works without HA; operating those dump loads still requires
the optional HA integration.

## Local verification

`tests/contract/test_control_flag_clients.py` exercises advertised desktop
commands and the shipped HA switch configuration against the real command
handler and retained MQTT publish queue, with HA absent and broker/D-Bus mocked.
It renders the HA value templates and checks the confirmation values. These tests
verify the software contract; they do not confirm an installed HA configuration
or operation on a physical inverter.
