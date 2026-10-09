# Security policy

Inverter Control can change physical inverter and auxiliary-device operation.
Report security defects privately, including issues that can cause unauthorized
control, unsafe command replay, credential exposure, or loss of availability.
The [security design](docs/security-design.md) describes the trust boundaries,
deployment requirements, and limitations of the current implementation. The
[security assurance map](docs/security-assurance.md) connects requirements to
implementation and tests; [credential handling](docs/credentials.md) explains
provisioning, rotation, and recovery.

## Supported versions

Security maintenance follows the latest released version and `main`. The active
release line is **1.23.x**; older lines, including 1.3.x, do not receive separate
security backports. Report a vulnerability found in any version: maintainers
will assess whether it also affects the maintained code.

Use the [release page](https://github.com/victron-venus/inverter-control/releases)
to identify the newest version and read its upgrade notes. A beta or RC is a
pre-release, not a hardware-safety certification or a stable-release promise.
Validate candidate releases on the intended installation before unattended use.
`main` is a development branch; record a tag or commit when deploying it.

The native runtime targets Python 3.12.x on Venus OS. Keep the operating system,
its Python/OpenSSL libraries, MQTT broker, and other D-Bus services updated;
their security fixes are supplied by their respective maintainers.

## Report a vulnerability privately

[Report a vulnerability through GitHub](https://github.com/victron-venus/inverter-control/security/advisories/new).
Private vulnerability reporting is enabled for this repository. This HTTPS form
sends the report to repository maintainers without creating a public issue.
See [GitHub's private reporting instructions](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
for access and notification details.

Include, where available:

- Affected release or commit, Venus OS/Python versions, and relevant optional
  integrations.
- Expected and observed behavior, attack prerequisites, and potential impact.
- Minimal reproduction steps, a test, or a proof of concept that can run without
  energizing real equipment.
- Sanitized logs and the configuration settings necessary to reproduce it.

Remove tokens, passwords, private keys, personal data, and installation-specific
identifiers from attachments. Do not test an exploit against somebody else's
device. Do not publish working exploit details in ordinary issues while a
private report is being coordinated.

Maintainers will acknowledge new vulnerability reports within **14 calendar
days**, with a target of **2 days** for reports describing critical impact.
The first response may request more information; it is not a promise that a
patch will be ready within that interval. If a report has no response after
14 days, follow up in the private report. If GitHub reporting is unavailable,
open an issue asking for a private contact channel **without including exploit
details or secrets**.

## Triage, fixes, and disclosure

Maintainers assess affected versions, reproducibility, severity, physical
impact, and mitigations with the reporter. Confirmed critical vulnerabilities
receive immediate priority; the target is a mitigation or fix within **7 days**.
Confirmed medium-or-higher vulnerabilities must be resolved before they have
been publicly known for **60 days**. These are maintenance commitments and
targets, not a claim about the response history of earlier reports.

If a fix needs more time, maintainers communicate the reason and available
mitigations in the private report. Measures may include disabling an optional
integration, restricting network access, or returning to the device vendor's
supported control mode. Operators must choose a safe electrical operating
state for their installation; abruptly stopping a controller is not a universal
mitigation.

For a confirmed issue, maintainers add a regression test where feasible, run the
relevant analysis and release checks, and coordinate publication of the fix and
advisory with the reporter. A public advisory describes affected and fixed
versions, impact, mitigations, and credit for every reporter unless that reporter
requests anonymity. Confirm the preferred public name or anonymous treatment
during coordination; do not publish a reporter's private identity or contact details. Release
notes identify publicly known runtime vulnerabilities fixed by the release,
including CVE/GHSA identifiers when assigned, and explain required operator
action. Credential exposure requires revocation/rotation; deleting a file or a
commit alone does not invalidate a secret.

## Deployment requirements

- Treat the Venus OS host, local accounts, D-Bus peers, MQTT broker, configuration
  files, and deployment account as trusted parts of the control system. The
  supplied service runs with the privileges of its supervisor, normally root
  on Venus OS; it does not provide a sandbox against a compromised host.
- Keep the webhook, console, and metrics listeners on their default loopback
  addresses. They do **not** implement user authentication or TLS. Remote access
  requires an authenticated encrypted tunnel or a correctly configured gateway
  that restricts every request. Do not port-forward them to the Internet.
- The MQTT bridge defaults to `localhost:1883` and implements no broker
  credentials or TLS of its own. Restrict broker access and command-topic
  publication. Any client allowed to publish control commands must be trusted
  to operate the equipment.
- For remote Home Assistant, configure `HA_URL` with HTTPS and a certificate
  trusted by the device. HTTP is supported for local deployments but transmits
  its bearer token without encryption. Use a dedicated account with only the
  access needed for the configured entities. Do not disable certificate checks.
- Store the Home Assistant bearer token separately using `HA_TOKEN_FILE`, following
  [credential handling](docs/credentials.md). Keep the token file private and
  rotate it without rebuilding the software; restart the daemon to load the
  replacement. Inline `HA_TOKEN` remains a legacy compatibility path and must
  receive the same protection.
- Store private configuration in `local_config.py`, normally under
  `/data/setupOptions/inverter-control/`, with a protected deployed copy under
  `/data/inverter-control/`. Both the file and its parent directories must be
  writable only by the deployment administrator; restrict private files to
  that account, for example mode `0600`. Python configuration is executable
  code, so permission to modify it is permission to execute code as the daemon.
  The installers enforce mode `0600` on the live and persistent configuration,
  including legacy migration and explicitly pushed copies. They reject symlinks
  and non-regular configuration files and stop if permissions cannot be applied.
- Use HTTPS for release downloads and SSH with a verified host key for
  deployment. Obtain SHA256 files through the same trusted release channel;
  checksums alone do not authenticate a download. Never pipe an unreviewed
  download directly into a privileged shell.
- Do not use DRY mode as an electrical lockout. It suppresses automatic inverter
  setpoint writes, but explicit overrides and some ESS/HA actuator paths remain
  active. Use isolated mock integrations for software tests; see the
  [dry-run boundary](docs/security-design.md#dry-run-is-not-a-hardware-isolation-boundary).

The daemon does not serve the historical port-8080 web dashboard. Current
external interfaces and their security boundaries are documented in
[the security design](docs/security-design.md); a separate dashboard or proxy
needs its own authentication and TLS configuration. The retired `setup_ssl.sh`
entry point exits with an explanation and does not generate keys, modify trust
stores, connect to a device, or change services.

## Verification and scope

The project uses automated tests, fuzzing, dependency checks, secret scanning,
and static analysis. See [security scanning](docs/security-scanning.md) and
[contribution requirements](CONTRIBUTING.md). A green scan is evidence from a
specific revision, not proof of absence of vulnerabilities or a substitute for
physical commissioning.

The [OpenSSF Passing criteria](https://www.bestpractices.dev/en/criteria/0)
also require evidence about report-response history and developer knowledge.
This policy cannot establish either fact by itself. Maintainers must verify
those answers against actual records before making an attestation.
