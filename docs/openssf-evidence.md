# OpenSSF Best Practices evidence

This index records the project's **Passing** assessment and the evidence under
review for **Silver** against the [OpenSSF criteria](https://www.bestpractices.dev/en/criteria/1?details=true&rationale=true).
Silver has not been awarded; the pending requirements below must be resolved
before making that claim.
The project first achieved the **Passing badge on 2026-10-08 at 02:37 UTC**.
After correcting and retesting its TLS key-strength claim, the project restored
Passing on **2026-10-09 at 00:08 UTC**. The
[public assessment](https://www.bestpractices.dev/en/projects/15293/passing)
recorded 100% completion at restoration: 63 criteria marked Met and four marked N/A with
justifications. This is a voluntary self-assessment, not an independent security
certification. It must continue to reflect the released software and actual
maintainer practices; a successful scanner run does not establish every criterion.

The public [application is project 15293](https://www.bestpractices.dev/en/projects/15293).
[.bestpractices.json](../.bestpractices.json) supplies proposed answers in the
official repository automation format. The repository's TLS answer has been
synchronized with the restored public assessment, so a later import does not
restore the superseded answer. Review new evidence and the
resulting form before saving future updates; do not mark unverified criteria Met.

Evidence was reviewed on **2026-10-08 and 2026-10-09 UTC**. The original baseline release is
[v1.23.5-beta.9](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.5-beta.9),
source `c651e9f3e7114f050fbfa641b16c8c885032186e`, with a successful
[release validation run](https://github.com/victron-venus/inverter-control/actions/runs/37710820520).
The documentation and security improvements were subsequently merged in
[PR #303](https://github.com/victron-venus/inverter-control/pull/303) and published
in [v1.23.5-beta.10](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.5-beta.10),
source `17143487b07bcbdd8f77f0878ed9b88373e2dd8d`, before the assessment was submitted.
The final [quality gate](https://github.com/victron-venus/inverter-control/actions/runs/37714862769)
passed 1,809 tests and reported 94.27% statement coverage under the historical
measurement that included test source, and the
[release workflow](https://github.com/victron-venus/inverter-control/actions/runs/37715259908)
succeeded. These checks do not constitute physical-device acceptance.
This index should be updated when the application, release process, or security
boundaries change. Do not publish private report contents or credentials as
evidence.

## Project, license, and participation

The following criteria have public evidence supporting **Met**:

- `description_good`, `interact`, `documentation_basics`: the [README](../README.md)
  explains the controller's purpose, installation, configuration, operation,
  and links to contribution and security guidance.
- `contribution`, `contribution_requirements`, `english`: [CONTRIBUTING.md](../CONTRIBUTING.md)
  describes the pull-request process, acceptance requirements, testing, and
  English-language reports. [Development instructions](development.md) give
  reproducible local commands.
- `floss_license`, `floss_license_osi`, `license_location`: the project publishes
  its source and tests under the top-level [MIT license](../LICENSE).
- `documentation_interface`: the README and linked references describe inputs
  and outputs, including [MQTT controls](mqtt-control-flags.md),
  [explicit ESS mode](ess-mode-selection.md), [tariff JSON and CLI](electricity-tariffs.md),
  [solar forecast/pre-charge messages and HTTP responses](solar-delivery.md),
  [grid energy telemetry](daily-grid-energy.md), and [metrics](prometheus-alerts.md).
- `sites_https`: the [project](https://github.com/victron-venus/inverter-control),
  [repository](https://github.com/victron-venus/inverter-control.git), and
  [release downloads](https://github.com/victron-venus/inverter-control/releases)
  use HTTPS.
- `discussion`, `report_process`, `report_tracker`, `report_archive`: public
  [issues](https://github.com/victron-venus/inverter-control/issues) and
  [pull requests](https://github.com/victron-venus/inverter-control/pulls) provide
  searchable, URL-addressable discussions, including bug and feature templates.
- `maintained`, `repo_public`, `repo_track`, `repo_interim`, `repo_distributed`:
  the public Git repository records authors, dates, and changes; reviewed
  intermediate changes are available as pull requests. The repository was
  active and not archived at the audit date.

## Releases and build

- **Met evidence — `version_unique`, `version_semver`, `version_tags`:**
  [versioning](release-versioning.md) and [release workflow](release-workflow.md)
  describe version allocation, Git tags, manifests, and promotion of tested
  bytes. Published releases have distinct version tags and source identities.
- **Met evidence — `release_notes`:** on 2026-10-08 UTC, 98 historical release
  descriptions received human-readable changes, upgrade guidance, and security
  notes while retaining the original build provenance. The retrospective notes
  do not claim a new build or hardware test. See
  [stable v1.23.4](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.4)
  and [v1.23.5-beta.9](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.5-beta.9).
  Future publication reads [CHANGELOG.md](../CHANGELOG.md) from the exact source
  commit, selects that release's base version, and rejects missing or incomplete
  notes before creating a release. The policy is enforced in
  [release_control.py](../scripts/release_control.py) and checked by
  [release-note tests](../tests/test_release_notes.py).
- **N/A candidate — `release_notes_vulns`:** no public advisory for a
  vulnerability in this project's own code was found in the GitHub repository
  advisory inventory. Maintainers must confirm that there are no other assigned
  public vulnerability identifiers to include. Dependency advisories, such as
  the IDNA fix, should still be explained when they affect an upgrade decision.
- **Met evidence — `build`, `build_common_tools`, `build_floss_tools`:**
  [package_release.py](../scripts/package_release.py) creates the native package
  from tracked source with Python's standard library and Git, with SHA-256
  checksums. The working build is exercised by release CI. The local process
  uses FLOSS tools and does not require the GitHub service to construct an
  archive. See [development](development.md) for the supported Python/uv setup
  and exact command. Hardware acceptance remains a separate operator task.

## Report response history

The audit queried the GitHub REST API with repository administrative access:

- All issue pages updated since 2025-10-08, excluding pull requests: **zero
  issues**. A separate all-state issue query found no non-PR issues either.
- The repository was created on 2026-03-31. Its public history contained 300
  closed pull requests, of which 290 were merged, at the audit date. This is
  evidence of active change handling, not an invented bug-response statistic.
- Repository security advisory inventory: **zero advisories returned**.
  GitHub private vulnerability reporting: **enabled**.

`report_responses` and `enhancement_responses` are **Met** for this review.
On 2026-10-08 UTC the primary maintainer also confirmed that no bug reports or
enhancement requests arrived outside GitHub during the preceding 12 months.
There were no reports awaiting a response; this is not a measured 100% response
rate. Reassess actual report handling when reports arrive.

`vulnerability_report_process` and `vulnerability_report_private` have **Met**
evidence in [SECURITY.md](../SECURITY.md) and the
[private report form](https://github.com/victron-venus/inverter-control/security/advisories/new).
`vulnerability_report_response` is **N/A** for this review: on 2026-10-08 UTC
the primary maintainer confirmed that no vulnerability reports arrived outside
GitHub during the preceding six months, alongside the empty GitHub inventory.
There were no reports requiring a response in that interval. Future reports
must be checked against actual acknowledgement timestamps and the 14-day limit;
a newly written policy alone does not prove historical response times.

## Tests, warnings, and analysis

The following criteria have evidence supporting **Met**:

- `test`, `test_invocation`, `test_continuous_integration`: the public
  [pytest suite](../tests), [CI script](../scripts/ci.sh), and
  [quality gate](../.github/workflows/quality-gate.yml) run unit, regression,
  release-contract, and mock-integration checks before releases.
- `test_most`: the baseline reported 1,777 passing pytest tests and 96.43%
  statement coverage, including test source. That historical percentage is not
  production-only coverage and is not used for the Silver 80% claim. The suite
  exercises control, transport, validation and recovery behavior; its results
  do not establish exhaustive inputs or testing on physical inverters.
- `test_policy`, `tests_documented_added`, `tests_are_added`: contribution rules
  require tests for new functionality and regressions. Recent
  [security fixes](https://github.com/victron-venus/inverter-control/pull/301/files)
  add tests for PATH substitution, failed cleanup, console replay, and malformed
  scanner reports; [quality fixes](https://github.com/victron-venus/inverter-control/pull/302/files)
  also update regression checks.
- `warnings`, `warnings_fixed`: Ruff checks and formatting run in the blocking
  CI script. [Scanner policy](security-scanning.md) requires review of each
  exception. Passing CI demonstrates the configured checks were addressed.
- `static_analysis`, `static_analysis_common_vulnerabilities`,
  `static_analysis_often`: Bandit, CodeQL, Trivy, and dependency review run in
  PR/release validation, with push or scheduled scanning as additional checks.
  Bandit is a FLOSS security analyzer, covering the language-specific
  vulnerability-analysis requirement independently of hosted tools.
- `static_analysis_fixed`: recent remediation and the published release provide
  evidence that confirmed problems are fixed. At the baseline, no open CodeQL,
  Bandit, or Trivy alerts remained. The two open Scorecard findings concern the
  badge and review governance; they are not runtime CVEs. Recheck current
  [code scanning](https://github.com/victron-venus/inverter-control/security/code-scanning)
  before submitting or renewing the assessment.
- `dynamic_analysis`, `dynamic_analysis_enable_assertions`: the
  [Atheris harness](../fuzz/fuzz_tariff.py) varies tariff input and checks
  normalization/JSON round-trip invariants. The
  [fuzz job](../.github/workflows/fuzz.yml) runs before release; pytest assertions
  remain enabled. This evidence does not rely on confusing statement coverage
  with the criteria's alternative branch-coverage threshold.
- `dynamic_analysis_fixed`: no unresolved exploitable issue was found by the
  audited fuzz runs. Future confirmed findings must follow [SECURITY.md](../SECURITY.md).

`dynamic_analysis_unsafe` is **pending**, not N/A: the optional ARMv7 dependency
bundle builds CFFI native code. The new [native memory-safety check](build-and-install.md#native-memory-safety-checks)
rebuilds CFFI with AddressSanitizer, verifies linkage and an intentional-overflow
canary, and runs 2,000 deterministic native-API fuzz iterations. The local ARM64
check passed; completion of the required hosted x86-64 job must be recorded.
Leak detection is disabled for CPython lifetime allocations. No claim is made
that prebuilt cryptography/OpenSSL wheels are instrumented.

`warnings_strict` has practical **Met** evidence: blocking Ruff diagnostics now
include bare-except `E722` and unused-variable `F841`; their global ignores were
removed and resulting violations fixed. Remaining exceptions are documented;
Bandit findings and incomplete scans fail the gate. The known FFDH deprecation
warning is confined to the exact key-exchange boundary fixture and is retained
explicitly while a supported replacement is evaluated. It is an `interfaces_current`
SHOULD exception, not a claim that every available diagnostic is enabled.

## Cryptography, delivery, and known vulnerabilities

- **Met evidence — `crypto_published`, `crypto_call`, `crypto_floss`:** the code
  uses SHA-256 through Python `hashlib` and delegates HTTPS to requests/urllib3
  and the platform TLS implementation. It does not implement a cipher or TLS
  protocol. See the [security design](security-design.md) for deployment limits.
- **Corrected `crypto_keylength` evidence:** the earlier OpenSSL security-level-2
  inference was insufficient. On CPython 3.12.13/OpenSSL 3.5.7, Home Assistant,
  Loki Requests and Loki urllib accepted a trusted RSA 2047-bit root. The public
  criterion was corrected while the fix was reviewed. The source now verifies
  exact public-key minima on the complete verified chain of the connection
  before sending application data; see [the security design](security-design.md)
  and `tests/test_tls_policy.py`. The repair was merged in
  [PR #307](https://github.com/victron-venus/inverter-control/pull/307), source
  `120e3b5ae977975aeb9d080b4216d62f793934fa`. Its
  [main validation](https://github.com/victron-venus/inverter-control/actions/runs/37861640088)
  passed 1,937 tests and reported 94.33% statement coverage including test source;
  the ARMv7 dependency build
  also passed. The public answer and Passing status were verified again on
  2026-10-09 UTC. Existing equipment still needs the compatible dependency
  bundle and an operator-authorized update; this does not establish that every
  deployment runs the corrected client.
- **Evidence for `crypto_working`, `crypto_weaknesses`, `crypto_pfs`:** the audited
  locked Python environment uses TLS 1.2 minimum, certificate/hostname
  verification, and TLS 1.3 or ephemeral ECDHE/DHE suites with at least 128-bit
  encryption. SHA-256 exceeds the required hash size. Plaintext MQTT/HTTP and
  independently configured gateways remain outside this client profile.
- **N/A — `crypto_password_storage`:** the controller has no external-user
  account/password database. Operator-provided upstream credentials are client
  credentials that must be protected on disk; they are not user-password hashes.
- **Met evidence — `crypto_random`:** project code has no custom cryptographic
  random generator. TLS delegates randomness to the platform OpenSSL library.
  The obsolete `setup_ssl.sh` is now a deprecation guard that makes no key,
  trust-store, or network changes; it no longer claims to enable a TLS listener.
- **Met evidence — `delivery_mitm`, `delivery_unsigned`:** official sources and
  release assets, including SHA256SUMS, are delivered over GitHub HTTPS.
  [Operator guidance](../SECURITY.md) requires trusted delivery and checksum
  verification. A checksum fetched beside an archive provides integrity, not a
  separate publisher signature.
- **Met evidence — `vulnerabilities_fixed_60_days`,
  `vulnerabilities_critical_fixed`, `no_leaked_credentials`:** the audit found
  zero open Dependabot alerts, zero open GitHub secret-scanning alerts, and no
  published project advisories. The released IDNA dependency minimum excludes
  the reported vulnerable range. The security gate checks source, dependency,
  and secret findings; packaging takes tracked inputs and excludes local
  operator configuration. This is evidence of the checked inventories, not
  proof that an undiscovered vulnerability or credential cannot exist. Recheck
  all reported issues and supported releases before the final declaration.

## Maintainer declarations

These declarations were obtained from the primary maintainer, independently
of the automated code audit:

The primary project maintainer personally confirmed `know_secure_design` and
`know_common_errors` on **2026-10-08 UTC**: familiarity with secure design
principles, including least privilege, safe defaults and access control, and
common Python/network application vulnerabilities and their prevention. The
[security design](security-design.md) maps these principles to the project.
This is a maintainer attestation, not a training certificate or an independent audit.

The maintainer also confirmed no vulnerability reports outside GitHub in the
preceding six months and no other bug reports or enhancement requests outside
GitHub in the preceding 12 months. Combined with the GitHub inventory above,
these declarations resolve the Passing report-response questions for that
review. They do not resolve Silver's separate 12-month reporter-credit requirement.

The assessment was saved and submitted, and its restored Passing status was
verified on OpenSSF and through the public project API on **2026-10-09 UTC**.
Badge status and Scorecard branch protection are separate assessments.

The October 2026 maintenance imports the release identity and metadata parser
from `venus-os-ci-toolkit`, including bounded TOML parsing and source-bound
release-note validation. Consumer release-contract suites exercise the vendored
release modules from this checkout. Workflow-validator refactoring preserves this repository's
existing policy; it does not imply all newer toolkit workflow guarantees are enabled.


## Silver review: evidence and open requirements

The repository proposals cover all 55 Silver criteria. They describe the reviewed
source; merging documentation alone does not establish historical practices or
publish a signed release. The public application must be checked against the
final merged source and required CI before saving updated answers.

### Governance and documentation

[GOVERNANCE.md](../GOVERNANCE.md) records the decision process, named owner,
release/security roles, contributor duties and the limits of bot automation.
[CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) sets participation expectations.
[ROADMAP.md](../ROADMAP.md) covers intended work and exclusions through October
2027. [CONTRIBUTING.md](../CONTRIBUTING.md) names PEP 8/Ruff conventions and
requires tests, current documentation and timely achievement updates. The
[quick start](quick-start.md) exercises the software without connected equipment;
[architecture](../ARCHITECTURE.md) and [interfaces](interfaces.md) describe its
components and contracts.

**Pending — `access_continuity`.** Public source, a permissive license and
runbooks do not prove someone else can administer this repository and release
within one week. A real human continuity arrangement and usable lawful access
must be confirmed. No successor, permission or recovery exercise is invented.

**Pending — `vulnerability_report_credit`.** [SECURITY.md](../SECURITY.md)
requires credit for every reporter unless anonymity is requested. Actual
resolved-report history over the preceding 12 months remains unconfirmed.
The earlier six-month outside-report statement must not be extended to a year.
The documented intake, triage, mitigation, disclosure and rotation procedure
supports `vulnerability_response_process` independently of that history.

The justified SHOULD exceptions are `dco` (no verified legal assertion from all
historical nontrivial contributors), `bus_factor` (one documented human
maintainer), and `internationalization` (no complete message catalog).
[Accessibility guidance](accessibility.md) records keyboard/text operation,
`NO_COLOR`, explicit valve-state text and the limits of formal conformance.
The default GitHub-hosted project sites satisfy the site-password criterion;
the project does not operate an additional user-password database.

### Build, installation and testing

[Build and installation guidance](build-and-install.md) explains the standard
`make install`/`uninstall` paths, nonprivileged `DESTDIR` staging and locked
`uv` developer environment. Tests exercise path confinement and preservation of
operator files. The native builder accepts compiler/linker variables, preserves
requested debug information and compares two clean CFFI builds. Recorded local
builds produced equal wheel and package bytes in the same pinned environment.
No recursive cross-directory build dependency graph is used. Dependency locks,
native input manifests, update bots and security scans identify components for
monitoring and replacement.

The [regression inventory](regression-audit.md) combines 475 screened baseline
commits with reviewed PR305 and Silver fixes: **171 of 307 conservative repair
candidates (55.70%)** have meaningful automated regressions. Two reviewers
independently sampled mappings; disputed cases were removed, split or given
stronger assertions. This satisfies the numerical `regression_tests_added50`
threshold only for the recorded source interval and merged mapped tests.

**Met evidence — `test_statement_coverage80`.** The [complete-source report](evidence/coverage-2026-10-09.json)
records **10,642 of 11,928 statements covered (89.2186%)** across all 67 tracked
executable Python files, including build/release scripts. The runtime, release
and workflow run passed 2,476 cases; 55 additional native/change-scope cases
were added to the same measured data. Two optional cases were skipped. Ruff
checking/formatting passed and the 81% coverage gate passed.

Namespace discovery includes scripts that were never imported; identical run
and report exclusions remove test source, the example configuration and external
virtual environments. The first namespace-aware report exposed a virtualenv
scope error; the same test data were successfully reported after correcting that
boundary. Per-file hashes and commands in the report make the final scope
reviewable. Earlier percentages containing test code or missing scripts are not
used for the Silver claim. This is statement coverage, not hardware acceptance.

The testing and review policies support `test_policy_mandated`,
`tests_documented_added`, `coding_standards` and `coding_standards_enforced`.
`interfaces_current` remains a justified SHOULD exception for the deprecated
FFDH API used only by its exact boundary fixture. That fixture is not removed
merely to suppress the warning.

### Security and signed delivery

The [assurance case](security-assurance.md) states threats and trust boundaries,
connects security requirements to implementations and tests, and records
limitations. It supports secure design and input validation with explicit
checks for MQTT/HTTP framing, bounded payloads, finite forecast values, numeric
commands, Home Assistant paths and install/release inputs. The supported TLS
clients retain certificate verification before private HTTP data. TLS/OpenSSH
library negotiation provides algorithm agility; the [credential guide](credentials.md)
documents separate token/key files and replacement without rebuilding.

`crypto_used_network` is a justified SHOULD exception: trusted local MQTT,
webhook, console and metrics retain plaintext defaults, and legacy Home
Assistant HTTP remains configurable. Remote operation requires an authenticated
encrypted tunnel/gateway or verified HTTPS. This is not described as universal
encryption. Native CFFI defaults use stack protection and RELRO/NOW; Python
memory management and bounded parsers/queues provide additional hardening.

**Pending — `signed_releases`.** The new [signature procedure](release-signatures.md)
authenticates both source and ARMv7 payloads through a signed checksum manifest.
It binds verification to the trusted issuer, workflow, source repository/ref
and independently obtained reviewed SHA. Key material exists only in an
ephemeral signing process, outside the public release store. The actual hosted
signing job, public release assets and recipient verification still need to be
observed; implementation and mock tests do not establish a signed release.
Historical unsigned releases remain unsigned. `version_tags_signed` is an
unfulfilled suggestion because Git tags remain lightweight.

**Pending — `dynamic_analysis_unsafe`.** Record the required hosted native
sanitizer run described above. Existing Python Atheris fuzzing and the clean
Bandit security gate are separate evidence; neither substitutes for native
memory-safety detection.
