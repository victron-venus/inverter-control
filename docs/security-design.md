# Security design and deployment boundaries

This document describes the implemented security model of Inverter Control.
It supports review and operation; it is not a penetration-test report,
electrical-safety certification, or evidence that a maintainer has completed a
training course. Report defects using the [security policy](../SECURITY.md).

## Assets and scope

The most important assets are authorized inverter setpoints and operating
modes, availability of the control loop, reliable and fresh power measurements,
Home Assistant credentials, and configuration and release integrity. MQTT
commands and some integrations can also affect charging or auxiliary devices.
Power measurements, logs, and portal identifiers can reveal household activity.

The expected attacker may reach an exposed network service, publish to a
misconfigured broker, submit malformed input, replay an old command, tamper with
a download in transit, or persuade an operator to install an untrusted change.
A compromised Venus OS root account or authorized configuration/deployment
account is outside the daemon's isolation boundary: those accounts can replace
the program, modify D-Bus peers, read credentials, and control hardware directly.

Device protections, BMS limits, electrical protection, and correct site-specific
configuration remain necessary. Software watchdogs and command validation do
not replace them. See [grid telemetry safety](grid-telemetry-safety.md),
[native write isolation](native-write-isolation.md), and
[Venus OS operations](venus-os-operations.md) before commissioning.

## Interfaces and trust boundaries

### Local process, configuration, and D-Bus

The service entry point is [`service/inverter-control/run`](../service/inverter-control/run),
which invokes `main.py`. It inherits supervisor privileges; a normal Venus OS
installation uses root. There is no per-request user identity inside the daemon.
The system D-Bus and the peers supplying measurements and receiving writes are
trusted infrastructure. Input plausibility and age checks detect some broken
data; they cannot authenticate a malicious local D-Bus service.

`local_config.py` is imported Python, and `metrics.env` is sourced by the service
shell. Both are administrator-controlled code, not safe formats for untrusted
user submissions. Protect their containing directories as well as the files.
The JSON tariff interface is the appropriate bounded data format for tariff
updates; do not turn network input into Python configuration.

`setup` and `update.sh` enforce mode `0600` for `local_config.py` in both the live
and persistent locations, and for a legacy `secrets.py` before migration.
Explicitly pushed configuration is restricted at its source and destinations.
Copies use a private creation mask so a new file is not briefly readable by
other users. Symlinks and non-regular configuration paths are rejected, and
permission failures stop installation. Existing configuration is checked before
the updater stops services. If an old installation uses a configuration symlink,
an administrator must replace it with a regular private file before updating.
These checks do not protect against an administrator or a process able to modify
the containing directories.

D-Bus fallback commands use explicit argument arrays and fixed executable
paths, including `/usr/bin/dbus` and `/usr/bin/dbus-send`; daemon fallback calls
do not interpolate commands into a shell. Preserve those properties when adding
new calls. The host administrator still controls those executables and the
system bus. See [`victron.py`](../inverter_control/victron.py),
[`dbus.py`](../inverter_control/dbus.py), and
[write timing](dbus-write-timing.md).

### MQTT command and telemetry channel

[`MQTTBridge`](../inverter_control/mqtt_bridge.py) defaults to the local broker
at `localhost:1883`. It publishes state/diagnostics and consumes commands under
the configured prefix, plus forecast topics. See the
[command reference](mqtt-control-flags.md) and [solar delivery contract](solar-delivery.md).

The client implements neither MQTT authentication nor TLS configuration.
Authorization belongs to the broker and the network boundary. A principal
allowed to publish to a command topic can request the corresponding operation.
Give monitoring-only clients subscribe-only access. Restrict command publishers
and forecast writers to the specific topics they need. The broker may expose
other listeners independently of this daemon's loopback default; review its
configuration and firewall too. For a remote broker, provide an authenticated
encrypted tunnel with a local endpoint rather than send control traffic across
an untrusted network.

Control commands received as retained MQTT messages are rejected, so reconnects
cannot silently reapply an old command. The pre-charge request path validates
identity, expiry, numeric values, and persistent deduplication state. Retained
telemetry is different from a command and remains supported where documented.
These checks reduce accidental replay; they do not provide cryptographic sender
authentication or defend against an authorized malicious publisher.

### HTTP webhook

[`WebhookServer`](../inverter_control/webhook_server.py) listens on
`127.0.0.1:8081` by default. `GET /health` reports server health;
`POST /api/v1/pre-charge` and `POST /api/v1/forecast` submit JSON. Requests use
bounded bodies (4096 bytes), require an object, and reject ambiguous length or
transfer framing. Pre-charge validation is shared with MQTT delivery.

There is no built-in HTTP authentication, authorization, TLS, or general request
rate limit. A local process that can reach the listener is trusted to submit
requests. Keep `WEBHOOK_SERVER_HOST` at loopback. If an integration needs remote
access, use SSH port forwarding or an authenticated TLS gateway restricted to
that integration; apply connection, timeout, and rate limits at that gateway.
Binding directly to a LAN or public address expands trust to every client able
to connect. Python's threaded HTTP server is not an Internet-facing gateway.

### Console and metrics

The diagnostic TCP console binds to `127.0.0.1:9999`; the explicit override is
`INVERTER_CONSOLE_HOST`. It streams console output and does not expose a command
shell. It has no authentication or encryption. Slow clients are dropped and
diagnostics use bounded buffers to keep writes away from the control loop.
Logs may still disclose operational details; restrict access.

The optional Prometheus endpoint uses `INVERTER_METRICS_HOST` (default loopback)
and `INVERTER_METRICS_PORT`. The supplied service enables port `9102` when the
Prometheus dependency is available. It exposes `/metrics` without authentication
or TLS. Use a tunnel or a restricted authenticated monitoring gateway for remote
collection. See [metrics and alerts](prometheus-alerts.md).

For example, after verifying the device's SSH host key, an operator can forward
the console and read it locally:

```sh
ssh -N -L 19999:127.0.0.1:9999 root@cerbo
# In another local terminal:
nc 127.0.0.1 19999
```

The daemon has no current port-8080 dashboard listener. A separately deployed
dashboard does not inherit authentication from Inverter Control.

### Outbound Home Assistant and log delivery

[`HomeAssistantClient`](../inverter_control/homeassistant.py) sends its bearer
token to the operator-configured `HA_URL`. HTTPS uses Requests' normal
certificate verification; the code does not set `verify=False`. HTTP remains
supported and is plaintext, including the authorization header. Use HTTPS for
remote hosts and provision a trusted CA when necessary. Keep the token private,
use a dedicated least-privilege account, and rotate it on exposure. The client
uses request timeouts, a circuit breaker, and a background poller; stale cached
HA data is not proof that the remote service is healthy.

The optional [log forwarder](log-forwarding.md) accepts HTTPS endpoints and
permits plaintext HTTP only for literal loopback addresses. It remains idle
without a configured endpoint. Configure the destination carefully: logs leave
the device and may contain sensitive operational data. Its endpoint file and
environment are administrator-controlled settings.

### Dry-run is not a hardware isolation boundary

DRY suppresses ordinary automatic inverter setpoint writes and their automatic
watchdog fallback. It does not disable every actuator or command interface.
An explicit persistent setpoint override still writes hardware, the legacy
ESS-mode toggle can change the inverter mode, HA toggle/press commands can
operate remote devices, and configured `minimize_charging` logic can call HA
switch services. An authorized MQTT client can also change the dry-run flag.

Use mocked D-Bus/HA services and an isolated test broker for bench/software
tests. If connecting to real equipment, isolate command publishers and auxiliary
actuators and verify the actual operating state independently. Do not treat a
DRY label as a lockout, an emergency stop, or proof that a test cannot energize
equipment.

## Secure implementation rules and review examples

The project applies the following practices during design and review:

- **Small mechanisms:** reuse the Python standard library, Requests, Paho, and
  platform D-Bus rather than inventing transport or cryptographic protocols.
- **Restrictive defaults:** keep unauthenticated listeners on loopback, reject
  invalid commands before side effects, and disable unconfigured integrations.
- **Checks at each entry point:** validate HTTP and MQTT inputs independently;
  share domain validation so another transport cannot bypass a safety rule.
  Broker/gateway access checks are required in addition to value validation.
- **Public design:** document interfaces and trust assumptions; secrecy of a
  topic, URL, or implementation is not authorization.
- **Separated responsibilities:** use distinct monitoring and command-publisher
  rights at the broker, and review changes before release. The daemon itself
  does not implement a second authorization factor for hardware operations.
- **Minimum necessary access:** keep credentials and configuration private and
  restrict integration accounts. The root service is a platform limitation,
  not a claim that the process has been reduced to an unprivileged sandbox.
- **Limited shared mutable state:** use bounded queues and locks for concurrent
  readers/writers, and atomic replacement for persistent validated state. Do not
  use predictable shared temporary paths for secrets.
- **Understandable operation:** distinguish accepted commands from observed
  hardware state; publish errors and document restart and rollback behavior.
- **Small exposed surface:** disable unused integrations, retain loopback binds,
  and leave public-facing TLS/authentication to a reviewed gateway.
- **Positive validation:** accept known command names and valid types, finite
  numbers, ranges, timestamps, and schema shapes before committing a change.

Reviewers should specifically look for shell injection, executable configuration
from untrusted sources, path traversal, missing authorization, stale/replayed
commands, non-finite numeric values, resource exhaustion, concurrency errors,
and credential disclosure. In Python, malicious input can still exhaust memory
or CPU even without a native buffer overflow. Not every MQTT payload path has
an application-level size limit; the broker must enforce appropriate message
and connection limits. Dependencies and native OS components remain part of the
attack surface.

Examples of implemented mitigations include fixed executable paths with
`shell=False`, a 100000-byte tariff limit, schema and finite-number checks,
temporary-file creation followed by atomic replacement, persistent pre-charge
deduplication, and bounded asynchronous diagnostics. Relevant regression suites
include `test_dbus_send_provenance.py`, `test_tariff.py`,
`test_precharge_request_boundaries.py`, `test_webhook_server.py`,
`test_console_server.py`, and `test_security_gate.py`. Fuzzing exercises the
tariff parser; it is not evidence that all network protocols have been fuzzed.

## Cryptography, delivery, and secrets

The project does not implement custom encryption or user-password storage.
It relies on FLOSS TLS implementations through Python/Requests for HTTPS,
OpenSSH for SSH deployments, and `hashlib` SHA256 for content hashes. Keep the
underlying OS trust store, TLS policy, and crypto libraries maintained; do not
enable obsolete algorithms or suppress certificate/host-key verification to
work around connection errors. Forward secrecy and permitted TLS algorithms
depend on the actual client and server configuration, so deployment validation
must inspect those endpoints rather than infer them from a green source scan.

The supported Python 3.12 environment uses TLS 1.2 or later, certificate and
hostname verification, and cipher suites with at least 128-bit symmetric
strength. OpenSSL security level 2 alone does **not** prove an exact RSA 2048-bit
minimum: a synthetic trusted RSA 2047-bit root was accepted by all three clients
on CPython 3.12.13/OpenSSL 3.5.7. That earlier assumption has been withdrawn.

Home Assistant and both Loki transports now inspect the **same connection's
verified chain**, including its trust anchor, before sending HTTP headers or
bodies. `cryptography` reads the public keys: RSA modulus >= 2048 bits, EC >= 224,
DSA p >= 2048/q >= 224, and Ed25519/Ed448. Unknown key types, unavailable chain
APIs, malformed certificates or missing `cryptography` fail closed. The code
uses CPython 3.12's private verified-chain API, or the public API where available;
this runtime boundary must be retested after interpreter upgrades.

Standard hostname/chain checks run first. Requests retains its CA environment,
explicit CA file/directory and client-certificate configuration. Its session-local
adapter checks both a TLS CONNECT proxy and the tunneled origin; other Requests
users are unaffected. The stdlib fallback retains its native HTTP CONNECT proxy
and SSL_CERT_FILE/SSL_CERT_DIR handling. It rejects HTTPS-scheme proxies before
connecting because stdlib would otherwise send their CONNECT and proxy
credentials without TLS; a matching `no_proxy` entry still permits a direct
connection. No system/user trust store is modified.
The supported Requests proxy schemes for this policy are HTTP and HTTPS CONNECT;
SOCKS transports are not part of this profile. Loki still rejects redirects;
Home Assistant retains Requests' redirect handling, including its credential
stripping rules, and validates every new HTTPS connection.

The loopback suite in `tests/test_tls_policy.py` covers the three actual client
paths, RSA 2047/1024 chains, strong RSA/EC, hostname and trust failures, proxies,
mTLS and plaintext compatibility. These synthetic tests do not validate a
physical device's installed interpreter, CA store, entropy or external gateway.
Plaintext MQTT/HTTP is not encrypted by this policy. Keep external TLS gateways
and platform libraries maintained; OpenSSL supplies TLS randomness.

The historical `setup_ssl.sh` is a retired entry point that fails with migration
guidance. It performs no certificate generation, trust-store modification,
remote connection, or service mutation. It cannot enable encryption for the
current daemon; configure the separate gateway or use an SSH tunnel instead.

Fetch releases and their checksums over HTTPS from the project's GitHub release
page. A SHA256 checksum detects content changes but does not authenticate a
publisher if the archive and checksum both come from a compromised source.
Review the tag, release notes, and expected repository before running its
installer with device privileges. SSH deployment must verify the destination
host key. Do not disable that check or blindly replace a changed key.

Git exclusions prevent ordinary accidental adds of local configuration and
keys; they do not stop `git add -f`, pasted tokens, or secrets in history. Review
diffs and scan releases. On a leak, revoke the credential first, then remove it
and investigate access. Never use an example credential as a real shared secret.

## Evidence and remaining limits

[Security scanning](security-scanning.md) describes the enforced analysis and
how to reproduce it. [CONTRIBUTING](../CONTRIBUTING.md) defines testing and review
expectations. Recheck these controls when adding an endpoint, changing a default
bind, adding a dependency, or changing a hardware-write path.

For [OpenSSF Passing](https://www.bestpractices.dev/en/criteria/0), this document
is design and process evidence. The `know_secure_design` and
`know_common_errors` answers additionally require a primary developer who
actually understands these subjects. Creating this document or having an agent
review code does not attest to any person's knowledge. Likewise, response-time
and vulnerability-remediation claims must be checked against real records.
