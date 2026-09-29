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
A reservation is fsynced in `/data/inverter-control/precharge-requests.json`
before enqueue. Only after the enqueue callback returns successfully is the
decision saved as `accepted` with `queued: true`, before returning 202. IDs are
remembered for 48 hours, across restart and ordinary upgrades. An uncertain
reservation returns 503 `unavailable` with reason `decision_uncertain` on retry;
it is never enqueued again. An enqueue or confirmation-write failure also returns
503 and keeps the in-memory reservation uncertain. If the confirmation reached
disk before a later filesystem error, its queued marker still records the
completed callback; it does not prove the subsequent control cycle ran.

Old `accepted` journal records have no queued marker and cannot prove whether
enqueue happened. They return 503 `legacy_decision_unverified` without replay
until their original retention expires. Old `suppressed` records remain terminal
duplicates. Unreadable/unwritable journals fail closed. A crash before enqueue or
before the control cycle uses a queued intent can still lose the request; no
startup recovery or replay is attempted. QoS1 does not imply exactly-once physical
action. Retained commands are ignored. Retries retain the same ID; changing the
forecast during the same local day does not create another intent.

Downgrading to an older controller is not journal-compatible when an `uncertain`
entry exists: the old reader rejects the journal and returns 503 for pre-charge
requests. It cannot prune that unknown entry, even after its retention expires.
Keep the upgraded reader; do not delete or rewrite journal entries to bypass
this fail-closed boundary. No automatic downgrade migration is performed.

Display-only reads run on a 2-second background worker; snapshots older than
8 seconds become unknown. Control measurements and setpoint writes retain their
existing path. This removes telemetry I/O from update_state without claiming
hard realtime scheduling on Linux/D-Bus.

The `solar_forecast/<site>` namespace is deliberately outside Venus `N/<portal>`.
The [Venus broker plugin](https://github.com/victronenergy/dbus-flashmq/blob/master/src/flashmq-dbus-plugin.cpp)
reserves `N/<portal>` for its own notifications and denies external publishers.
A MQTT 3.1.1 PUBACK alone does not prove subscriber delivery on that namespace.
Upgrade both producer and controller before using the new topic pair.
