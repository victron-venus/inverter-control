# Solar delivery contract v1

The controller consumes `solar_forecast/<PORTAL_ID>/forecast_json` (retained,
QoS 1) and `solar_forecast/<PORTAL_ID>/pre_charge_request` (non-retained,
QoS 1). Configure the producer's `SITE_ID` to the GX portal ID and
`MQTT_BROKER` to the reachable GX address. The legacy `solar/forecast` topic
remains supported. Use the site's trusted MQTT network/access controls; this
change does not expose the loopback HTTP service or add internet access.

A request contains version=1, request_id (1–96 alphanumeric/underscore/hyphen
characters), trigger=low_solar_forecast, forecast_energy_wh, threshold_wh,
issued_at and expires_at (Unix seconds). Energy must be finite/nonnegative,
forecast below threshold; lifetime is at most 300 seconds, future clock skew
at most 30 seconds. The producer uses SHA256 of
`precharge-v1:<site_id>:<local-calendar-date>` as its daily request ID.

Subscribe to `solar_forecast/<PORTAL_ID>/pre_charge_ack/<request_id>` before
publishing. The receiver acknowledges with status/reason/request_id/http_status.
HTTP `/api/v1/pre-charge` uses the same inbox and returns the indicated status:
202 accepted (queued_one_cycle), 409 suppressed (expensive_window), 200 duplicate
(with original_status), 400 rejected, 410 expired, or 503 unavailable.
Legacy requests without an ID/version/expiry now receive 400.

Accepted means queued for the existing **one control cycle**, not confirmed
charging or a sustained charging policy. The tariff gate is rechecked at use.
The decision is fsynced before enqueue into `/data/inverter-control/precharge-requests.json`.
IDs are remembered for 48 hours, across restart and ordinary upgrades. An
unreadable/unwritable journal fails closed. A crash after recording but before
enqueue can lose a request; it cannot repeat it. QoS1 does not imply exactly-once
physical action. Retained commands are ignored. Retries retain the same ID;
changing the forecast during the same local day does not create another intent.

Display-only reads run on a 2-second background worker; snapshots older than
8 seconds become unknown. Control measurements and setpoint writes retain their
existing path. This removes telemetry I/O from update_state without claiming
hard realtime scheduling on Linux/D-Bus.

The `solar_forecast/<site>` namespace is deliberately outside Venus `N/<portal>`.
The [Venus broker plugin](https://github.com/victronenergy/dbus-flashmq/blob/master/src/flashmq-dbus-plugin.cpp)
reserves `N/<portal>` for its own notifications and denies external publishers.
A MQTT 3.1.1 PUBACK alone does not prove subscriber delivery on that namespace.
Upgrade both producer and controller before using the new topic pair.
