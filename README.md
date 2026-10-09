# Inverter Control

[![CI](https://github.com/victron-venus/inverter-control/actions/workflows/ci.yml/badge.svg)](https://github.com/victron-venus/inverter-control/actions/workflows/ci.yml)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15293/badge)](https://www.bestpractices.dev/projects/15293)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/victron-venus/inverter-control)](https://github.com/victron-venus/inverter-control/releases)

Inverter Control is a Python daemon for Victron Venus OS that adjusts an inverter's
AC input setpoint to regulate net grid import/export. It supports split-phase
compensation, solar forecasting, configurable operating modes, and optional
Home Assistant dump-load control. It reads measurements and writes commands
through the local D-Bus; MQTT connects it to independent dashboards and clients.

This is a community project for installations that need custom control beyond
standard ESS behavior. Commission it against your meter topology, inverter,
battery limits and failure cases before unattended operation. A successful
software test does not certify electrical safety or suitability for a site.

## Documentation and support

- Start here: [equipment-free quick start](docs/quick-start.md), installation and configuration below; [operations and recovery](docs/venus-os-operations.md).
- Integration: [external interfaces](docs/interfaces.md), [MQTT flags](docs/mqtt-control-flags.md), [ESS modes](docs/ess-mode-selection.md), [solar delivery](docs/solar-delivery.md), [electricity tariffs](docs/electricity-tariffs.md).
- Control: [algorithm](LOGIC.md), [grid validity and fallback](docs/grid-telemetry-safety.md), [submeter trim](docs/submeter-trim.md), [auxiliary readers](docs/auxiliary-readers.md).
- Implementation: [architecture](.github/docs/system-architecture.md), [native write isolation](docs/native-write-isolation.md), [timing diagnostics](docs/dbus-write-timing.md), [metrics](docs/prometheus-alerts.md), [logging](docs/log-forwarding.md).
- Build and verify: [installation and build conventions](docs/build-and-install.md), [release signature verification](docs/release-signatures.md).
- Contribute: [contribution policy](CONTRIBUTING.md), [development/test guide](docs/development.md), [governance](GOVERNANCE.md), and [one-year roadmap](ROADMAP.md).
- Security: [private reporting and support policy](SECURITY.md), [security design](docs/security-design.md), [scanner policy](docs/security-scanning.md), [OpenSSF evidence](docs/openssf-evidence.md).

Use [GitHub Issues](https://github.com/victron-venus/inverter-control/issues) for
bugs, feature requests and usage questions, and pull requests for proposed
changes. English reports are welcome. Include the release, Venus OS version,
expected/actual behavior and sanitized diagnostics. Report vulnerabilities
[privately](https://github.com/victron-venus/inverter-control/security/advisories/new).
Never attach tokens or a complete private configuration to an issue.

<!-- ci-release-process:start -->
## Release process

See the [release strategy](RELEASING.md) for validation, nightly, beta, RC and stable promotion rules, and the [operator runbook](docs/release-workflow.md) for local commands.
<!-- ci-release-process:end -->

Download tagged archives, checksums and change notes from
[Releases](https://github.com/victron-venus/inverter-control/releases).
`main` is the integration branch; beta/nightly/RC releases are previews.
Select a release deliberately and review its compatibility notes. Stable promotion
requires acceptance of the exact RC artifacts; it is not implied by passing CI.
[CHANGELOG.md](CHANGELOG.md) records release changes.

## Runtime requirements

Device packages target **Python 3.12.x**. Development/CI pin Python 3.12.13 in
[.python-version](.python-version); package metadata accepts 3.12 patch updates.
Use the Venus OS interpreter and matching system D-Bus libraries rather than
replacing firmware Python. The native D-Bus path and CLI fallback depend on the
services and tools supplied by the target image.

Runtime Python dependencies are declared in [pyproject.toml](pyproject.toml),
[requirements.txt](requirements.txt), and the development [uv.lock](uv.lock).
The installer checks imports of `requests`, `paho-mqtt`, and the `cryptography`
certificate/key APIs and stable version >= 50.0.2 before stopping an
existing controller. It does not install missing packages. Resolve dependency
and firmware compatibility before installation. `prometheus-client` is optional
for metrics. HTTPS also requires CPython to expose the verified peer chain;
unsupported runtimes fail closed. Existing HTTP paths remain unchanged.
For ARMv7, use the reviewed, hash-locked [offline dependency bundle](docs/tls-dependencies.md)
for CPython 3.12 on Venus OS 3.60+ (Scarthgap, glibc 2.39, libffi8).
Its build compiles CFFI and tests the complete new dependency closure offline;
an upstream `cryptography` wheel alone is insufficient. Check the actual image
and successful CI artifact provenance before upgrading.
See the development guide for a reproducible desktop environment.

## Configure before installation

The private configuration lives at the repository/package root as
`local_config.py`, alongside `main.py`. Start from
[local_config.example.py](local_config.example.py). Preserve any existing file:

```sh
umask 077
test -e local_config.py || cp local_config.example.py local_config.py
chmod 600 local_config.py
```

Edit the copy locally. Use a validated HTTPS URL for a remote Home Assistant
instance and your actual sensor/switch IDs. Prefer a separate private token file
selected by `HA_TOKEN_FILE`; follow [credential provisioning and rotation](docs/credentials.md).
Inline `HA_TOKEN` remains available for migration from older configurations.
Home Assistant is optional; an unconfigured token disables its integration. EV, water and
PV data can come from independent D-Bus publishers. Set their device instances
only as needed for your deployment.

Only names explicitly imported from `local_config.py` are overrides. The
example documents supported site settings. Algorithm constants, default power
limits and feature defaults live in
[inverter_control/config.py](inverter_control/config.py); placing an arbitrary
name in `local_config.py` does not override those constants. Runtime command
changes such as limits and control flags are documented in the interface guide.
They are not a substitute for review of persistent settings.

For PackageManager, provision the private file at
`/data/setupOptions/inverter-control/local_config.py` **before** choosing Install.
`setup` copies it into `/data/inverter-control/local_config.py`. Both locations
contain secrets and must be writable only by the administrator; installation
restricts private configuration files to mode `0600`.
Direct `update.sh` preserves the installed configuration. A PackageManager
installation replaces it with the persistent setupOptions copy when that copy
exists; make lasting edits there as well. An explicit development
deployment with `PUSH_LOCAL_CONFIG=1` replaces it; inspect deployment options
before use. Python configuration is executable trusted code, not an upload format.

The time-of-use defaults in the template bootstrap settings under
`/Settings/InverterControl` on Venus OS; existing GUI settings take precedence.
The expensive window blocks automatic forecast pre-charge. Manual
`charge_battery` is an explicit override. Review
[solar delivery](docs/solar-delivery.md) before connecting a producer.

For backup metering, configure a signed **whole-grid** source, not a branch-load
meter. `USE_GRID_SUBMETER_AS_BACKUP` defaults to false; the persistent
`use_grid_submeter_as_backup` SetupHelper option overrides it when present.
See [grid validity](docs/grid-telemetry-safety.md) for source identity, freshness,
recovery and hold behavior. Optional [submeter trim](docs/submeter-trim.md) is
separately disabled by default.

## Install or update on Venus OS

Installation restarts the controller and can resume live hardware commands.
Arrange a commissioning/maintenance window, preserve the private configuration,
and keep a known compatible release for recovery. The installer does not make
an automatic rollback archive. Review journal compatibility in the release notes
before downgrading; do not delete pre-charge records to bypass deduplication.

### Verified release archive

1. On a workstation, download the intended release's `inverter-control-<version>.tar.gz`
   and `SHA256SUMS` from the same GitHub release over HTTPS. Verify the downloaded
   archive against its entry in `SHA256SUMS` (`sha256sum -c SHA256SUMS` on Linux,
   or `shasum -a 256 -c SHA256SUMS` on macOS). A checksum is integrity evidence,
   not an independent signature; retain the trusted HTTPS source.
2. Copy the verified archive to the intended device over SSH after checking its
   host key. Extract it in a **new staging directory**, outside the live
   `/data/inverter-control`. The archive contains an `inverter-control/` directory.
3. Ensure `/data/inverter-control/local_config.py` has the site's configuration
   before first launch. For SetupHelper, use the persistent location described
   above and the package's `setup install auto`. Without SetupHelper, run
   `sh <staging-directory>/inverter-control/update.sh` with the device's administrator
   account; this uses the existing live configuration.
4. Check supervisor state, a fresh heartbeat, logs and actual device behavior.
   A failed startup returns an error. Diagnose it and reinstall a compatible
   known release; a heartbeat alone does not prove correct power regulation.

### SetupHelper / PackageManager

Install and operate [SetupHelper](https://github.com/kwindrem/SetupHelper)
according to its own documentation. The package name is `inverter-control`,
GitHub organization `victron-venus`. The shipped `gitHubInfo` points to `main`:
PackageManager's branch-download path installs integration code, not an accepted
stable archive. Choose it only for an intentionally managed development site;
use a verified release for a controlled rollout.

The `setup` entry point delegates to `update.sh`. It keeps service files under
`/data/inverter-control/service/`, creates `/service` links and a persistent
`/data/rc.local` hook, then waits for a fresh controller heartbeat. An interactive
`setup` can prompt for options; `setup install auto` is noninteractive.
`setup uninstall` removes service activation while retaining private data.

Developers can use `deploy.sh <SSH-host-alias>` for an explicitly authorized test
device. This copies working-tree code and restarts services; it is not a release
acceptance test. Read the script and the development guide before deploying.

## Operation and diagnostics

```sh
svstat /service/inverter-control /service/log-forwarder /service/watchdog
tail -n 80 /var/log/inverter-control/current
cat /run/inverter-control/inverter-control.heartbeat
```

Use the native `svc`/`svstat` supervisor tools, not `systemctl`. Do not read
`supervise/ok` with `cat`: it is a FIFO. A controller restart belongs to the
site's maintenance procedure because a separate watchdog/keepalive also owns
fallback behavior. See [operations](docs/venus-os-operations.md).

For an isolated, prepared test installation, `python3 main.py --dry-run` suppresses
automatic grid-regulation writes but still starts integrations and network activity.
It is not a hardware lockout: explicit sustained overrides, legacy ESS commands
and Home Assistant actuators can still operate; an authorized MQTT command can
switch back to live mode. Isolate command publishers and HA actuators for an
observation-only test. See the exact [dry-run scope](docs/interfaces.md#process-and-configuration).
Do not run
a second controller alongside the supervised service. Without `--dry-run`, the
configured default is **live**. The optional positional integer runs one cycle
with a manual setpoint; it is not a harmless connectivity test.

Diagnostic services default to loopback: TCP console `9999`, webhook `8081`,
and the supplied service's optional Prometheus endpoint `9102`. The daemon does
not serve an HTTP dashboard on port 8080. Use an SSH tunnel or authenticated
gateway for remote access; these local listeners have no built-in authentication.
The obsolete `setup_ssl.sh` no longer changes certificates or system trust.

If a service fails, check its log and interpreter/dependencies first. If a
client's state stops updating, inspect MQTT connectivity and timestamps rather
than treating a retained message as a current measurement. A missing or stale
measurement is not zero. [Interfaces](docs/interfaces.md) describes acknowledgements,
state freshness and diagnostics; [security design](docs/security-design.md)
describes broker permissions and remote access requirements.

## License

Released under the [MIT license](LICENSE). Contributions use the same license;
see [CONTRIBUTING.md](CONTRIBUTING.md).
