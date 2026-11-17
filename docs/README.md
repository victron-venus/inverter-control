# Documentation index

These documents describe installing, configuring and running the Inverter
Control daemon, its D-Bus and MQTT interfaces, safety behaviour and release
process.

## Get started

| Document | Description |
| --- | --- |
| [Quick start](quick-start.md) | Equipment-free first run of the controller's calculation tests. |
| [Building and installing the native package](build-and-install.md) | Native package build and installation conventions. |
| [Venus OS installation and runtime checks](venus-os-operations.md) | Device installation, runtime checks and diagnostics workers. |
| [Development and validation](development.md) | Working on the controller without a live installation. |
| [Offline TLS dependencies on ARMv7](tls-dependencies.md) | The hash-locked offline dependency bundle for ARMv7. |

## Configure

| Document | Description |
| --- | --- |
| [Credentials and rotation](credentials.md) | Storing and rotating Home Assistant credentials outside Python config. |
| [Power limits](power-limits.md) | Supported setpoint range and saved-setting behaviour. |
| [Explicit ESS mode selection](ess-mode-selection.md) | The header mode picker and the `set_ess_mode` command. |
| [Electricity tariffs](electricity-tariffs.md) | Controller-owned tariffs for dashboard prices and billing periods. |
| [Solar delivery contract v1](solar-delivery.md) | Consuming retained solar forecast payloads. |
| [Slow submeter zero trim](submeter-trim.md) | Optional trim that keeps fast load changes on the primary meter. |
| [Auxiliary readers and control timing](auxiliary-readers.md) | Water and EV D-Bus reads in dedicated background workers. |
| [Offline tariff schedule example](examples/electricity-tariff.schedule.json) | Example JSON tariff schedule. |

## Integrate

| Document | Description |
| --- | --- |
| [External interface reference](interfaces.md) | The daemon's commands, MQTT topics and observable effects. |
| [Inverter control flags over MQTT](mqtt-control-flags.md) | The seven flag keys and how clients drive them. |
| [D-Bus service discovery](service-discovery.md) | How the daemon discovers and refreshes D-Bus services. |
| [Loki log forwarding on Venus OS](log-forwarding.md) | The `log-forwarder` service and retained multilog records. |
| [Prometheus alerts for inverter-control](prometheus-alerts.md) | Metrics on `:9102/metrics` and the shipped alert rules. |

## Safety and diagnostics

| Document | Description |
| --- | --- |
| [Grid telemetry validity and recovery](grid-telemetry-safety.md) | When a setpoint may be written and how invalid data is held. |
| [Physical grid import and export energy](daily-grid-energy.md) | Publishing `daily_stats.grid_energy` for the physical meter. |
| [Independent native write connection](native-write-isolation.md) | Separate telemetry and `SetValue` connections and event loops. |
| [Attributing slow acknowledged D-Bus writes](dbus-write-timing.md) | Phase durations recorded for slow `SetValue` calls. |
| [Read-only D-Bus diagnostics](dbus-diagnostics.md) | Parsing saved write-timing and writer-provenance records. |
| [Regression history audit](regression-audit.md) | Six-month baseline and the reviewed fixes after it. |

## Security and assurance

| Document | Description |
| --- | --- |
| [Security design and deployment boundaries](security-design.md) | The implemented security model of the controller. |
| [Security scanning](security-scanning.md) | Running the Bandit gate locally used by pull requests. |
| [Security assurance case](security-assurance.md) | Source-level argument for the security requirements and limits. |
| [Accessibility and localization](accessibility.md) | Contribution and documentation accessibility expectations. |
| [Quality evidence and maintenance](quality-process.md) | Contribution requirements, style baseline and maintenance evidence. |
| [OpenSSF Best Practices evidence](openssf-evidence.md) | Index of the passing assessment and its evidence. |
| [Screenshot artwork](images/README.md) | Source of the companion console illustration. |

## Release

| Document | Description |
| --- | --- |
| [CI orchestration](CI-ORCHESTRATION.md) | How the quality gate and release pipeline divide validation. |
| [Release-candidate fuzz testing](fuzzing.md) | The bounded RC fuzz target for the production payload parser. |
| [Release version inputs](release-versioning.md) | The `versioning` fields that identify a release. |
| [Required release notes](release-notes-policy.md) | The `CHANGELOG.md` entry a release source commit must contain. |
| [Verifying release signatures](release-signatures.md) | What the signed release pipeline provides and how to check it. |

Shared repository policy: [release-workflow.md](release-workflow.md), [runner-preflight.md](runner-preflight.md), [release-read-safety.md](release-read-safety.md), [VERSIONING.md](VERSIONING.md) and [rc-fuzzing.md](rc-fuzzing.md) are generated or shared boilerplate maintained in [victron-venus/venus-os-ci-toolkit](https://github.com/victron-venus/venus-os-ci-toolkit).
