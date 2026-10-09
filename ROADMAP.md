# Roadmap: October 2026–October 2027

This roadmap describes the project's intended direction for at least the next year. It is a maintenance plan, not a promise of delivery dates or hardware certification. The lead maintainer reviews it quarterly; priorities may change in response to field evidence, vulnerabilities, and contributor capacity. Propose changes through an issue or PR under [GOVERNANCE.md](GOVERNANCE.md).

## October–December 2026: trustworthy maintenance

- Complete evidence-backed OpenSSF Silver work: document responsibilities and recovery arrangements, verify repeatable builds, authenticate published release artifacts, and keep security requirements tied to executable checks.
- Preserve the new TLS/SSH key-strength policy and test the ARMv7 dependency closure on the supported Venus OS profile. Treat unsupported runtimes explicitly rather than weakening verification.
- Keep controller timing, fresh telemetry, shutdown, restart, and offline behavior measurable. Expand regression tests for observed failures and record which observations come from mocks versus real installations.
- Improve onboarding, upgrade/recovery instructions, and accessibility of textual diagnostics. Assess internationalization without changing machine-facing protocol identifiers.

## January–April 2027: compatibility and resilience

- Validate supported Python, OpenSSL, Venus OS, D-Bus, and MQTT dependency updates through candidate builds and clearly documented acceptance results.
- Review stale/unknown data handling across grid, battery, PV, and auxiliary readers. Maintain bounded work and predictable fallback behavior when optional services fail.
- Exercise release recovery and the confirmed maintainer continuity arrangement. Keep runtime manifests, lock files, artifact verification instructions, and third-party notices consistent.
- Improve interface contracts and migration guidance for dashboards, forecast producers, and other ecosystem consumers while retaining documented compatibility where practical.

## May–October 2027: sustainable operation

- Use resolved issues and field measurements to prioritize reliability and performance work. Add regression cases rather than speculative control modes without evidence.
- Reassess documented threat boundaries, dependency vulnerability monitoring, and security-report response performance.
- Make contributor tasks and maintainer responsibilities easier to share; evaluate broader localization and accessible diagnostic output with actual users.
- Review supported release lines and deprecations in advance. Publish migration and rollback limitations before removing a supported interface.

## Deliberate limits

During this period the project does not plan to replace Venus OS firmware, battery/inverter protection systems, or professional electrical commissioning. It does not aim to expose an unauthenticated controller directly to the Internet, host another general-purpose web dashboard inside the daemon, or provide an autonomous cloud service that deploys to users' equipment.

New hardware, protocols, and control algorithms require a maintainer-approved use case, an interface contract, tests, and an operator-controlled acceptance plan. A badge, passing CI, or a roadmap item is not evidence that a feature has shipped or that a particular installation is safe.
