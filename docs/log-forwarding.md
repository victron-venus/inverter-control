# Loki log forwarding on Venus OS

The `log-forwarder` daemontools service forwards retained multilog records from
`inverter-control`, `dbus-mqtt-chain1`, `dbus-mqtt-chain2` and `dbus-virtual-chain`.
Streams keep the labels `job="cerbo"` and `service="<service name>"`.

## Device-local endpoint

Set the complete Loki push URL in this plain-text file on each device:

```sh
mkdir -p /data/setupOptions/inverter-control
umask 077
printf '%s\n' 'https://loki.example.com/loki/api/v1/push' \
  > /data/setupOptions/inverter-control/loki_url
svc -t /service/log-forwarder
```

Use the actual site's Loki hostname. The file contains only the URL, without
shell assignments or quotes. It is outside the release directory, so native
updates, PackageManager upgrades and reinstalls do not replace it. The updater
already preserves `/data/setupOptions`; no endpoint is copied from a release.

`LOKI_URL`, when present in the service's environment, overrides the file. An
explicitly empty value disables forwarding even if the file exists.
`LOKI_URL_FILE` can override the file path. Restart only `log-forwarder` when
changing an already active endpoint; the controller does not need a restart.

There is no default network destination. Older versions used a site-specific LAN
address; upgrading such a device requires setting its endpoint explicitly. If
configuration is missing, unreadable or invalid, the service stays alive and
rechecks it every 60 seconds. It emits a diagnostic when the problem changes,
without sending logs or repeatedly restarting. Startup and failure diagnostics
are unbuffered in `/var/log/log-forwarder/current`.

HTTP and HTTPS URLs are supported. HTTPS keeps normal certificate verification;
redirects are refused to prevent sending log content to an unconfigured server.
Push failures identify the exception type or HTTP status, without printing URL
credentials. A ready Loki endpoint should return HTTP 204 for a valid push.

## Recovery and retention

The forwarder acknowledges a batch only after a successful Loki push. A network
outage or HTTP error leaves the byte offset unchanged for retry. If multilog
rotates a file while that batch is pending, the forwarder finds the original
inode in retained `@<TAI64N>.s` / `.u` archives and drains it before newer files.
It also handles the temporary `previous` file and the gap before `current` is
created. Empty, exhausted archives do not block progress. Partial lines in
`current`, including partial UTF-8 characters, stay pending until completed.

Cursor replacement is atomic. Its default location remains
`/run/inverter-control/log-forwarder-state.json`, avoiding frequent flash writes.
A process restart resumes that cursor; a device reboot replays retained files
because `/run` is temporary. An accepted push followed by a crash before saving
its cursor can also be replayed. Delivery is therefore at least once within
available retention, and duplicate records are possible.

Recovery is limited by each source's bounded multilog retention. This change
does not create an additional spool or increase flash use. If rotation has
already removed the saved inode, the service explicitly logs `Retention loss`
and starts at the oldest remaining archive. A truncated source is also reported.
Deleted records cannot be reconstructed. Set source retention to cover the
expected outage duration and Loki's accepted timestamp window accordingly.

Persistent HTTP errors require operator attention: for example, HTTP 400 may
mean Loki rejects the age of retained records, while HTTP 401/403 can indicate
authentication or authorization failure. The forwarder keeps the records
pending instead of treating a rejected push as delivered. Correct the Loki
policy or endpoint, and inspect server logs before deliberately discarding a
backlog.

## Verify after deployment

```sh
svstat /service/log-forwarder
tail -n 30 /var/log/log-forwarder/current
```

Confirm `Loki endpoint configured` followed by `Forwarded ... lines`, then query
Loki for recent `{job="cerbo", service="inverter-control"}` records. A running
process or successful `/ready` response alone does not prove ingestion.
