# External interface reference

This reference describes the current daemon. Commands can affect physical
hardware. Deploy clients within the access boundaries in
[security design](security-design.md); the application does not authenticate
individual MQTT or local HTTP callers. Examples describe wire formats, not an
instruction to send commands to a running inverter.

## Process and configuration

The entry point is `python3 main.py [setpoint] [--dry-run]`:

- With no positional argument, run continuous control and diagnostic services.
- `setpoint` is an integer in watts. Run one controller cycle with that manual
  setpoint, then shut down workers. This still initializes integrations.
- `--dry-run` enables dry-run behavior regardless of the configured default.
  It suppresses automatic grid-regulation writes, not all possible device actions.
- `--help` prints argparse usage. Invalid command-line syntax exits nonzero.

**Dry-run is not a hardware lockout.** A persistent `setpoint_override` deliberately
operates even in DRY mode. Legacy `ess_mode`, forwarded HA `toggle`/`press`, and
HA dump-load automation can also actuate devices; `cmd/dry_run` can re-enable live
regulation. Isolate command publishers and disable HA actuators for an
observation-only test. The explicit `set_ess_mode` command does reject DRY mode.

The default configuration is live operation. In continuous mode the default
cycle interval is 0.33 seconds; this is a scheduling target, not a real-time
latency guarantee. SIGINT/SIGTERM initiate shutdown. See
[native write isolation](native-write-isolation.md) for its limits.

`local_config.py` at the package root is trusted executable Python. Supported
site overrides are documented in [the template](../local_config.example.py)
and imported by [config.py](../inverter_control/config.py). Default runtime
parameters are not all overridable by adding a same-named local variable.
SetupHelper persistent files and GUI settings have explicit precedence; see
[installation](../README.md#configure-before-installation).
Tariff files instead use a bounded JSON schema and a separate
[CLI and deployment contract](electricity-tariffs.md).

## D-Bus inputs and outputs

The daemon discovers local Victron services and reads grid phases, consumption,
inverter power, battery data, solar chargers, PV inverters and optional auxiliary
services. Missing/unavailable data is distinct from zero. Device instances are
matched to discovered service names, not appended as bus-name suffixes.

The command path writes the inverter grid setpoint and, for explicit mode
selection, supported VE.Bus/settings paths. Positive AC-input setpoints request
import/charging; negative setpoints request export. The requested value is not
proof of actual measured flow. A write is acknowledged only by an explicit
numeric zero return from the service. Read
[ESS mode selection](ess-mode-selection.md), [control logic](../LOGIC.md),
[grid validity](grid-telemetry-safety.md) and [write timing](dbus-write-timing.md)
for paths, fallback and acceptance behavior.

## MQTT

The default broker is `localhost:1883`, default prefix `inverter`.
`MQTT_BROKER`, `MQTT_PORT`, and `MQTT_TOPIC_PREFIX` are defined in `config.py`.
An empty broker disables the bridge. The bridge does not configure broker
credentials or TLS: use local broker access controls or an authenticated tunnel.

Clients send non-retained JSON objects to `<prefix>/cmd/<command>`. Retained
control messages are ignored. There is no generic successful-command response;
observe the documented state/acknowledgement for that command. MQTT delivery
alone does not prove that a command was accepted or acted on.

Supported command suffixes:

- `toggle`: `{"entity":"only_charging","state":"on"}`. Seven daemon-owned
  flags, accepted states and legacy HA forwarding are specified in
  [the flag contract](mqtt-control-flags.md). Omitted state toggles and is not
  idempotent. Prefer explicit values.
- `dry_run`: `{"value":true}` or `{"value":false}` sets the runtime mode.
  A missing value retains the legacy toggle behavior. Only JSON booleans are
  accepted as an explicit value. Observe `dry_run` in the state publication.
- `setpoint`: `{"value":500}` sets the legacy manual setpoint, constrained by
  current power limits. Prefer a JSON integer; the compatibility parser also
  converts integer strings. This is distinct from the sustained override below.
- `setpoint_override`: `{"value":500,"request_id":"operator-1"}` sets a
  sustained override; `{"value":null,"request_id":"operator-2"}` clears it.
  This command deliberately bypasses DRY and the automatic power-limit clamp.
  The value must be an int32 integer or null; booleans are rejected. The optional
  request ID is a correlation string, not authentication. The override lives in
  the daemon, persists across client disconnects, and resets on daemon restart.
  Read `<prefix>/setpoint_override` for `value`, `last_error` and
  `request_id`. Do not infer a successful physical write from the requested value.
- `limits`: `{"min":-2300,"max":2250}` sets ordered integer limits in watts.
  The controller clamps each endpoint to the supported range `[-3000,3000]`.
  Invalid ordering, booleans and fractional values are rejected. The state
  publication exposes effective limits.
- `loop_interval`: `{"interval":0.33}` sets a runtime interval in seconds,
  constrained to `[0.1,5.0]`. Observe the effective `loop_interval` in state.
- `set_ess_mode`: `{"mode":"external_control","request_id":"operator-3"}`.
  Use only the modes, request constraints and observed-state rules in
  [ESS mode selection](ess-mode-selection.md). The legacy `ess_mode` command
  toggles; it is not an alias for an explicit selection.
- `electricity_tariff`: bounded JSON settings described in
  [electricity tariffs](electricity-tariffs.md), including revision/acknowledgement.
- `forecast` and `pre_charge`: forecast display data and versioned pre-charge
  requests. Prefer the dedicated producer topics described below.
- `press`: `{"entity":"button.example"}` forwards a legacy button request to
  the optional Home Assistant integration. It does not press a daemon flag.

Unknown commands are ignored. Malformed inputs are rejected or logged by the
relevant handler; legacy commands do not share a uniform error envelope.
Clients should use the specified JSON object formats instead of relying on
fallback string coercion.

Published topics:

- `<prefix>/state`: JSON, QoS 0, retained. Contains daemon flags (`booleans`),
  `ui_config`, `dry_run`, `ess_mode`, tariff/forecast/daily state and diagnostics.
  Default slim state omits data already available from Victron services, such as
  grid/battery/setpoint mirrors; consumers must not assume those keys exist.
  See [daily grid energy](daily-grid-energy.md) and
  [auxiliary readers](auxiliary-readers.md) for individual state semantics.
- `<prefix>/setpoint_override`: JSON status, QoS 1, retained. Re-published on
  reconnect; authoritative about the current daemon's override intent/status.
- `<prefix>/portal`: detected portal identifier as text, QoS 0, retained.
- `<prefix>/console`: diagnostic text lines, QoS 0, not retained. It may drop
  lines under backpressure and is not an audit log or stable control API.
- `<prefix>/notifications`: JSON with `id`, `level`, `title`, `body`, `source`,
  `ts`; QoS 0, not retained. Send the alert ID as text to `<prefix>/alert/ack`
  to acknowledge it. Stored unacknowledged alerts may be replayed on reconnect.

Retained state can outlive the publishing process. Check timestamps and freshness
where supplied; absence of a new message is not proof of an unchanged live
measurement. Remote diagnostics are bounded/best effort, not guaranteed delivery.

The dedicated forecast topics are
`solar_forecast/<PORTAL_ID>/forecast_json` and
`solar_forecast/<PORTAL_ID>/pre_charge_request`. Acknowledgements use
`solar_forecast/<PORTAL_ID>/pre_charge_ack/<request_id>`.
[Contract v1](solar-delivery.md) defines fields, units, expiry, deduplication,
HTTP status mapping and downgrade restrictions. The legacy `solar/forecast`
display topic remains accepted. Forecast data is not a replacement grid sensor.

## HTTP webhook

Default listener: `127.0.0.1:8081`. The host can be explicitly overridden with
`WEBHOOK_SERVER_HOST` in the private configuration. There is no authentication
or TLS in this listener. Do not expose it directly to an untrusted network.

- `GET /health` returns status 200 and `{"status":"ok"}`. This checks the
  HTTP listener, not inverter safety, telemetry freshness or successful writes.
- `POST /api/v1/forecast` accepts a JSON object with numeric `today_kwh` and
  `tomorrow_kwh`, plus optional display metadata such as `date`. Successful
  storage returns 200 `{"status":"forecast stored"}`.
- `POST /api/v1/pre-charge` accepts the versioned request from
  [solar delivery](solar-delivery.md). It returns a decision with
  `status`, `reason`, `request_id` and the corresponding HTTP status.
  A 202 means queued for a control cycle, not confirmed charging.
- Unknown paths return 404 JSON. Malformed framing/JSON/object types return 400;
  bodies over 4096 bytes return 413. Unconfigured callbacks return 503; callback
  failures return 500. Requests must have one valid `Content-Length` and no
  `Transfer-Encoding`. Successful JSON responses include `Content-Type` and
  `Content-Length`.

## Console, metrics and logs

The TCP console binds to `127.0.0.1:9999` by default; `INVERTER_CONSOLE_HOST`
changes the host. It streams recent and live UTF-8 diagnostic lines, not a shell
or command interface. It can drop lines or disconnect slow clients. Output
format may evolve; clients needing structured data should use MQTT/metrics.

Prometheus exposes `/metrics` when `INVERTER_METRICS_PORT` is set and
`prometheus-client` is installed. The supplied supervisor sets port 9102 and
`INVERTER_METRICS_HOST=127.0.0.1` by default. Missing optional dependencies do
not stop the controller. Metric names/units, alert rules and freshness are
specified in [metrics and alerts](prometheus-alerts.md).

The supervisor logs stdout/stderr to bounded multilog storage, normally
`/var/log/inverter-control/current`. File logging and log forwarding are optional;
see [logging](log-forwarding.md) and [operations](venus-os-operations.md).
The heartbeat under `/run/inverter-control/` is a liveness signal, not an
acknowledgement that power targets were achieved.
