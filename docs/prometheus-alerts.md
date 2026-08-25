# Prometheus alerts for inverter-control

The controller exposes metrics on `:9102/metrics` when the service runs with
`INVERTER_METRICS_PORT` set (shipped in `service/inverter-control/run`). This
document gives ready-made alert rules; Grafana/datasource wiring lives in the
`venus-os-observability` repo.

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
