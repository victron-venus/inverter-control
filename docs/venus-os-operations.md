# Venus OS installation and runtime checks

Keep code, configuration and service templates under `/data/inverter-control`. `/service` is volatile and is recreated through the package's `/data/rc.local` hook. The native supervisor commands are `svc` and `svstat`, not `systemctl` or `sv`.

`setup install auto` and `update.sh` share the same installer. It checks syntax/dependencies before stopping services, preserves local configuration and supervisor directory inodes, handles installation from the package directory itself, and waits for a fresh heartbeat before ending maintenance keepalive. A failed startup returns an error; inspect logs and restore a known release using the documented update procedure. Reboot and firmware-upgrade recovery still require a planned live check.

```sh
svstat /service/inverter-control /service/log-forwarder /service/watchdog
tail -n 80 /var/log/inverter-control/current
cat /run/inverter-control/inverter-control.heartbeat
```

Never read `supervise/ok` using `cat`: it is a FIFO. `svc -s` suspends the process and is not a status command.

Normal logs go through bounded multilog. `/var/log` points to `/data/log` on the audited Venus OS v3.75 device, so a duplicate unbounded DEBUG file writes flash twice. `INVERTER_CONTROL_LOG_FILE` is now opt-in; when set it retains a current 512 KiB file and two backups. Existing legacy log files are not deleted automatically. Watchdog stdout is also captured by multilog; an optional `WATCHDOG_LOG` duplicate is bounded. The log-forwarder's frequently updated position file defaults to `/run/inverter-control/log-forwarder-state.json`; it resets on reboot and may replay currently retained log lines.

Both native and CLI SetValue paths require an explicit numeric zero result to acknowledge a write. Native reconnect failures enter cooldown; subscriptions must be armed on the current connection, and NameOwnerChanged schedules discovery on the polling thread. Fallback polling includes inverter power. A live connection is not proof of per-source measurement freshness; validate source dropout behavior before unattended control changes.

EV and charger instances are matched against `/DeviceInstance` on actual names from background D-Bus discovery. A numeric instance must never be appended as a bus-name component. Missing or ambiguous matches remain unavailable and metadata is retried after 30 seconds or a change in discovered names. Local request-validation failures do not disconnect the shared native bus or force unrelated grid reads and setpoint writes into CLI fallback.

The September 2026 audit deployed only the log-forwarder cursor/storage fix from this repository. The controller transport, installer and logging changes are separate reviewable source changes. The audit itself did not issue mode or setpoint commands. A separate concurrent task deployed controller 1.23.1.
