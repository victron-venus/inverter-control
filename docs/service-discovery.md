# D-Bus service discovery

[`VictronDBus`](../inverter_control/victron.py) discovers services at startup and
refreshes them from its background polling thread. When native D-Bus is enabled,
it subscribes to `NameOwnerChanged` so relevant service additions and removals
can request another scan without waiting for the periodic fallback.

The callback invalidates affected cached state and sets a discovery event. The
polling thread consumes that event and performs the synchronous discovery work;
the callback does not run the blocking service-listing command on the D-Bus loop.
Tracked names include system and settings services, configured battery chains,
VE.Bus, solar chargers, AC loads, PV inverters, batteries and EV chargers, plus
the selected grid telemetry service.

Two fallback paths remain:

- A periodic scan every 1,800 seconds (30 minutes).
- A scan after five consecutive errors, with a 60-second cooldown between
  error-triggered scans.

Discovery is serialized separately from control-loop reads and writes. Service
presence alone does not establish measurement freshness or acknowledge a control
command. See [runtime checks](venus-os-operations.md) and
[grid telemetry safety](grid-telemetry-safety.md) for those boundaries.

The [deployment helper](../deploy.sh) excludes `build` and other generated paths
when packaging a checkout. Installation and release procedures are documented in
[build and installation](build-and-install.md) and [RELEASING.md](../RELEASING.md).

Validate changes with the applicable discovery and native-D-Bus tests under
[`tests`](../tests), following [the developer guide](development.md). Test counts
and deployment observations belong to the specific change and tested revision;
this page does not claim a current test run or device deployment.
