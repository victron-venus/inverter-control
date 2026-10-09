# Prometheus alerts for inverter-control

The controller exposes metrics on `:9102/metrics` when the service runs with
`INVERTER_METRICS_PORT` set (shipped in `service/inverter-control/run`). This
document gives ready-made alert rules; Grafana/datasource wiring lives in the
`venus-os-observability` repo.

## Remote scraping on Venus OS

The listener defaults to loopback and has no built-in TLS or authentication.
Keep that default. For remote collection, run an authenticated SSH tunnel from
the monitoring host, or use an authenticated TLS monitoring gateway. For
example, after verifying the GX device's SSH host key:

```sh
ssh -N -L 19102:127.0.0.1:9102 root@cerbo
```

Configure the monitoring host's Prometheus to scrape `127.0.0.1:19102`, supervise
the tunnel, and verify `up{job="inverter-control"} == 1` through that path.
Do not disable SSH host-key verification. Use a dedicated restricted forwarding
account where the platform permits it; a manually started tunnel is an example,
not a durable production service configuration. A TLS gateway must authenticate
and authorize every scrape and keep the daemon-facing link on the trusted host.

Legacy installations may set `INVERTER_METRICS_HOST` in
`/data/inverter-control/metrics.env` (mode 0600) to a non-loopback address. The
service still supports this explicit override for compatibility, but it sends
metrics without transport encryption and trusts every host that can connect.
A private LAN alone does not provide the authenticated encrypted transport
required by the recommended deployment profile. Remove the override and
restart under the supervisor to restore loopback binding; package updates
preserve this administrator-owned file. A successful localhost request does
not prove the remote tunnel or gateway is working.

The listener starts in a background supervisor. If the configured LAN address is
not available yet during boot, or the port is temporarily occupied, inverter
control continues while the supervisor retries after 1, 2, 4, 8, 16, then at most
30 seconds between attempts. It always uses the configured address; recovery does
not expose the endpoint on other interfaces. Metrics continue updating while
binding is pending, and the same gauges are used when the listener recovers.
If the HTTP serving thread exits, the supervisor releases its socket and starts a
replacement. Normal service shutdown cancels pending retries and closes the
listener. Startup and recovery messages include the bind address and port.

`start()` reports whether background metrics service was enabled, not whether an
HTTP socket is already ready. Verify recovery from the actual Prometheus server,
especially after a reboot. An invalid port or missing `prometheus_client` disables
the exporter with a log message and must be fixed in configuration or packaging.

## Metrics of interest

| Metric | Labels | Meaning |
| ------ | ------ | ------- |
| `inverter_control_cycle_ms` | `quantile=p50\|p95\|max` | Control cycle duration (budget: `LOOP_INTERVAL`=330ms) |
| `inverter_control_missed_deadlines_total` | — | Cycles exceeding the loop interval |
| `inverter_control_stage_ms` | `stage`, `quantile` | Per-stage durations (`get_system_data`, `calculate_setpoint`, `console_render`, …) |
| `inverter_control_setvalue_ms` | `quantile` | Grid setpoint write duration |
| `inverter_control_failed_writes_total` | — | Failed setpoint writes |
| `inverter_control_signals_healthy` | — | 1 = D-Bus fast-signal path armed and receiving data |
| `inverter_control_dbus_subprocess_calls_total` | — | dbus-send spawns (canary for polling storms) |
| `inverter_control_snapshot_age_ms` | `quantile` | Telemetry age at calculation time |

## Example rules

```yaml
groups:
  - name: inverter-control
    rules:
      # Verify reachability independently of metrics emitted by the process.
      - alert: InverterControlAgentUnreachable
        expr: up{job="inverter-control"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "inverter-control agent unreachable from Prometheus"

      # Fast-signal path down >2 min: control degrades to 1s tree polls.
      - alert: InverterControlSignalPathDown
        expr: inverter_control_signals_healthy == 0
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "inverter-control D-Bus signal path unhealthy"

      # Any missed deadline in 10 min means the loop cannot keep 3Hz.
      - alert: InverterControlMissedDeadlines
        expr: increase(inverter_control_missed_deadlines_total[10m]) > 0
        labels:
          severity: warning
        annotations:
          summary: "control loop missing deadlines (cycle_ms p95={{ $values.p95 }})"

      # Cycle p95 over budget (330ms) for 5 minutes.
      - alert: InverterControlCycleLatencyHigh
        expr: inverter_control_cycle_ms{quantile="p95"} > 300
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "cycle_ms p95 above LOOP_INTERVAL budget"
          # Use inverter_control_stage_ms to find the guilty stage.

      # dbus-send spawn storm canary: sustained >5 spawns/sec.
      - alert: InverterControlDbusSubprocessStorm
        expr: rate(inverter_control_dbus_subprocess_calls_total[5m]) > 5
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "dbus-send spawn rate suggests a polling storm"

      # Any failed setpoint write must page: grid control is blind.
      - alert: InverterControlSetpointWriteFailures
        expr: increase(inverter_control_failed_writes_total[10m]) > 0
        labels:
          severity: critical
        annotations:
          summary: "grid setpoint writes failing"

      # Stale telemetry feeding decisions (>2s snapshot age p50).
      - alert: InverterControlSnapshotStale
        expr: inverter_control_snapshot_age_ms{quantile="p50"} > 2000
        for: 5m
        labels:
          severity: warning
```

## ESS-mode mismatch

The GX silently ignores `AcPowerSetpoint` outside External control. The
controller publishes a warning notification on `inverter/notifications`
(consumed by inverter-desktop banners); wire that topic into Alertmanager if
you want it paged too.
