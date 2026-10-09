# Security assurance case

This is a source-level argument for the security requirements below, for the
supported Python 3.12.x runtime and the deployment boundaries in the
[security design](security-design.md). The [architecture](../ARCHITECTURE.md)
identifies components and data flows. This document is maintained with changes
to those components; it is not a penetration-test report, proof of a particular
installation's configuration, or electrical-safety certification.

## Context, threats, and trust boundaries

The assets are authorized control of inverter/auxiliary equipment, continuity
of the control loop, fresh and plausible measurement input, confidential Home
Assistant credentials and operating data, and integrity of installed software.

A network attacker may send malformed or replayed messages, publish to an
incorrectly secured broker, impersonate an HTTPS endpoint, or tamper with a
download. A malicious local account may read badly protected secrets or change
configuration. A dependency or source change may introduce a vulnerability.
A broker, gateway, D-Bus peer, or measurement device may become unavailable.

Trust changes at each of these boundaries:

- MQTT publisher to broker and broker to command callback: topic ACLs and
  publisher authentication are external; payload validation is local.
- HTTP caller to the loopback webhook: the host/tunnel/gateway establishes who
  may connect; the webhook validates request framing and the domain payload.
- HTTPS client to Home Assistant or Loki: the TLS implementation authenticates
  the endpoint and the application checks the selected verified chain before
  private HTTP data is sent.
- Configuration/credential file to daemon: only the deployment administrator
  may supply executable Python configuration; token data is loaded separately.
- D-Bus peer to controller and controller to equipment: Venus OS and the system
  bus are trusted infrastructure, while plausibility, freshness, write policy,
  and watchdog behavior constrain accidental faults.
- Repository/release to operator/device: change review, tests, scanning and the
  documented release verification procedure establish the software to install.

An account that can replace the program, its Python configuration, trusted CA
store, broker ACLs, or system D-Bus services is inside the trusted computing
base. The daemon normally runs as root under Venus OS and does not defend the
hardware from a compromised host administrator. A valid command publisher is
also trusted to operate the equipment. DRY mode is not an electrical lockout.

## Requirements, arguments, and evidence

### S1: commands have a defined authority and cannot silently replay

**Requirement.** Only the deployment's authorized local/tunnel/gateway clients
and MQTT publishers may reach command handlers. Retained MQTT control messages
must not execute. Pre-charge requests must meet the request identity, time,
energy and deduplication contract before a control intent is queued.

**Argument.** Loopback listener defaults minimize exposure. MQTT and gateway
ACLs separate monitoring from control rights. In code, every MQTT command
passes namespace and retention checks, and the pre-charge inbox validates
request contents and persists its reservation before accepting an intent.
HTTP and MQTT share that inbox. Validation is not authentication: incorrectly
opening the broker or listener invalidates the deployment assumption.

**Evidence.** [MQTT dispatch](../inverter_control/mqtt_bridge.py),
[pre-charge inbox](../inverter_control/precharge.py),
[webhook framing](../inverter_control/webhook_server.py),
[command replay tests](../tests/test_mqtt_bridge.py),
[pre-charge boundary tests](../tests/test_precharge_request_boundaries.py),
and the [solar delivery contract](solar-delivery.md).

### S2: malformed inputs do not change control or forecast state

**Requirement.** Restricted input must be checked before side effects. Numeric
values must meet the relevant domain bounds; typed fields and message framing
must be valid. Failure must preserve previously accepted state.

**Argument.** Command callbacks are allowlisted. Non-object JSON does not reach
command callbacks; legacy plain text remains a bounded `value` wrapper for
compatible handlers. MQTT control/forecast/ack inputs are limited to 4096 bytes
before decoding, except tariff documents whose existing limit is 100000 bytes.
HTTP bodies are bounded and ambiguous framing is rejected. Forecast validation
requires finite nonnegative numeric energy, rejects booleans and malformed
metadata, and stores only known fields after validation. Tariffs, setpoint
overrides, ESS selections and pre-charge requests have their own domain checks.
Manual setpoints reject fractional/non-finite/boolean values, and loop intervals
reject non-finite/boolean values before conversion or clamping changes state.
Home Assistant URL construction permits only bounded `domain.object_id` entity
IDs and supported service actions, preventing path/query injection.

**Evidence.** [Forecast validator](../inverter_control/forecast_input.py),
[forecast state tests](../tests/test_forecast_input.py),
[HTTP roundtrip tests](../tests/test_webhook_forecast.py),
[MQTT boundary tests](../tests/test_mqtt_bridge.py),
[numeric and entity-path tests](../tests/test_command_input.py),
[tariff parser](../inverter_control/tariff.py),
[tariff tests](../tests/test_tariff.py),
and [override tests](../tests/test_mqtt_override_isolation.py).

**Limit.** Size limits act after the MQTT library has received a message; the
broker must also enforce packet and connection limits. This is not a proof
against all denial of service. Unknown forecast extension fields are ignored
rather than stored or executed. Python configuration is trusted executable
code, not a network-input format.

### S3: HTTPS protects endpoint identity and private HTTP data

**Requirement.** Supported HTTPS paths use TLS 1.2 or newer, retain CA and
hostname verification, enforce documented key minima on the actual selected
chain, and do so before private headers or bodies are sent.

**Argument.** Requests and the standard-library fallback use their normal trust
verification. The session-local adapter and fallback validate the same
connection's verified chain, including its trust anchor. Missing inspection
capability or unsupported keys fail closed. The supported OpenSSL policy
rejects undersized DHE parameters. No application cryptographic primitive is
invented. TLS and modern OpenSSH provide algorithm negotiation; OS/library
updates can replace algorithms without changing the control algorithm.

**Evidence.** [TLS policy](../inverter_control/tls_policy.py),
[Requests adapter](../inverter_control/requests_tls.py),
[loopback HTTPS tests](../tests/test_tls_policy.py),
[DHE regression](../tests/test_tls_key_exchange.py),
and [native bundle contract](../tests/test_tls_bundle.py).
The [security design](security-design.md#cryptography-delivery-and-secrets)
documents key minima, TLS proxy behavior and the CPython API dependency.

**Limit.** These guarantees apply to HTTPS and the documented supported runtime.
They do not turn plaintext HTTP/MQTT into TLS. Actual CA stores, remote gateways,
DNS routing, operator SSH configuration, and platform library updates remain
operational responsibilities. External `ProxyJump`/`ProxyCommand` transports
need their own key/cipher policy. A green test does not inspect a live device.

### S4: credential bytes are separate, private, and replaceable

**Requirement.** Operators can keep authentication credentials and private keys
outside ordinary configuration, replace them without recompilation, and avoid
silently falling back to a stale credential after a failed replacement.

**Argument.** `HA_TOKEN_FILE` takes precedence over the legacy inline setting.
The bounded loader opens the final path without following a symlink, inspects
the opened descriptor, and requires a regular file owned by the service user
with mode 0400 or 0600. It rejects malformed content without printing it.
Rotation uses private-file preparation, atomic replacement, a controlled
restart, and revocation at the provider. Authenticated Loki uses Requests with
a separate netrc credential store; a fresh session reloads it on each batch.
The optional standard-library transport does not claim netrc support. SSH keys and TLS client keys use their
existing separate files; the project does not embed them in source.

**Evidence.** [Credential loader](../inverter_control/credentials.py),
[startup configuration](../inverter_control/config.py),
[loader and rotation tests](../tests/test_credentials.py),
[actual Loki netrc rotation test](../tests/test_log_forwarder.py),
and [credential operations](credentials.md).

**Limit.** Legacy inline `HA_TOKEN` remains compatible and must be migrated by
the operator. Administrator-controlled parent directories and backups must be
protected. The process necessarily holds the active token in memory. Runtime
file changes do not revoke the token or reload an existing process by themselves.

### S5: stale data and slow auxiliary work have bounded control effects

**Requirement.** The controller must distinguish accepted commands from actual
hardware state and reject unavailable/stale control measurements according to
its grid-loss policy. Slow optional telemetry and diagnostics must not block
hardware-write admission indefinitely.

**Argument.** Measurement validation, watchdog/write isolation, bounded queues,
and background readers separate control timing from optional work. Persistent
write overrides are explicit and do not survive a daemon restart. The grid-loss
and DRY behaviors are stated as contracts, not advertised as electrical safety
mechanisms. Physical BMS/current limits and site commissioning remain required.

**Evidence.** [Grid telemetry safety](grid-telemetry-safety.md),
[native write isolation](native-write-isolation.md),
[grid telemetry tests](../tests/test_grid_telemetry.py),
[write timing](dbus-write-timing.md),
and [override isolation tests](../tests/test_mqtt_override_isolation.py).

### S6: changes and known vulnerabilities have a review and response path

**Requirement.** Reviewable source, automated regression tests, security analysis,
updatable dependencies, and private reporting support timely correction.
Releases must be verified using the current documented process before install.

**Argument.** Contributions are reviewed through pull requests, CI executes the
published test/scanning configuration, and dependency manifests identify
components to update. The security policy assigns triage, response and disclosure
steps. A checksum alone is not publisher authentication; use the current release
verification instructions and do not claim a release is signed merely because a
signing workflow exists in source.

**Evidence.** [Contributing](../CONTRIBUTING.md),
[security scanning](security-scanning.md), [security policy](../SECURITY.md),
[release instructions](../RELEASING.md), and
[dependency manifest](../pyproject.toml).

## Secure design and common weakness review

Economy of mechanism is supported by reusing Python, Requests/OpenSSL, Paho,
OpenSSH and platform D-Bus instead of new cryptographic or authentication
protocols. Fail-safe defaults are loopback binding, disabled unconfigured HA,
rejected malformed commands, and fail-closed credential/TLS inspection.
Complete mediation is applied at each transport entry and the shared domain
handler; external authority checks must cover every broker/gateway path.
Open design comes from public contracts, source and regression tests.
Separation of duties uses broker publish/subscribe ACLs and source review.
Least privilege is enforced for credential files and recommended for external
accounts; root execution is a documented platform limitation. Bounded queues,
per-client state, and small critical sections limit shared mutable mechanisms.
Clear status messages and explicit restart/recovery procedures help operators
understand failures rather than bypass protection.

The following common implementation weaknesses have concrete countermeasures:

- Injection: D-Bus subprocess arguments are arrays with fixed executable paths;
  network data is not evaluated as Python or shell. See
  [subprocess provenance tests](../tests/test_dbus_send_provenance.py).
- Broken access control and replay: S1 defines the broker/host authority boundary,
  exact topic dispatch, and pre-charge identity/expiry/deduplication.
- Input/type/resource errors: S2 and S5 enforce bounded requests, finite domain
  values, known schemas, rejection before state changes, and bounded queues.
- Cryptographic/identity errors: S3 checks the actual verified connection before
  private application data, including proxies and certificate-chain key sizes.
- Secret disclosure: S4 separates private files, rejects unsafe permissions, and
  documents provider-side revocation; error messages do not echo token bytes.
- Vulnerable dependencies and unsafe updates: S6 identifies dependency scanning,
  update/review procedures and verification limits rather than equating a green
  scan or checksum with publisher trust.

Python reduces native memory-corruption risks in project-owned control code,
but does not remove them from the interpreter, OpenSSL, `cryptography`, MQTT
libraries or OS. Native component hardening and security updates must be checked
for the actual platform. The loopback servers are not hardened Internet-facing
servers and require an external gateway for remote access.

## OpenSSF interpretation and maintenance

This case supports `documentation_security`, `implement_secure_design`,
`input_validation`, `crypto_credential_agility`, `crypto_tls12`,
`crypto_certificate_verification`, `crypto_verification_private`, and
`assurance_case` with reviewable source/test evidence. It does not establish a
human training attestation, a report response history, or access continuity.

The `crypto_used_network` recommendation is **not met literally**: local MQTT,
webhook, console and metrics use plaintext by default, and legacy HTTP Home
Assistant remains configurable. This is a documented compatibility exception
for the trusted Venus OS host/broker boundary. Remote deployments require
verified HTTPS or an authenticated encrypted tunnel/gateway. A badge answer
must state that exception rather than call all default networking encrypted.

Update this case when adding an endpoint, credential, parser, actuator path,
crypto library/runtime, build artifact, or deployment mode. For each security
change, record the relevant requirement, negative and positive regression
coverage, actual CI result, and remaining deployment assumptions. If a claim
fails, track and correct it; do not weaken the tests or revise the claim to hide
the failure.
