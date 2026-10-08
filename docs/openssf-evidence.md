# OpenSSF Best Practices evidence

This is the evidence index for the project's **Passing** self-assessment against
the [OpenSSF criteria](https://www.bestpractices.dev/en/criteria/0?details=true&rationale=true).
It is not an assertion that a badge has been awarded. The badge application must
reflect the current released software and actual maintainer practices. A
successful scanner run does not establish every criterion.

The public [application is project 15293](https://www.bestpractices.dev/en/projects/15293).
[.bestpractices.json](../.bestpractices.json) supplies proposed answers in the
official repository automation format. Its `?` answers intentionally leave
unverified items unanswered; automation ignores these placeholders rather than
certifying or clearing them. Review the resulting form before saving it.

Evidence was reviewed on **2026-10-08 UTC**. The baseline release is
[v1.23.5-beta.9](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.5-beta.9),
source `c651e9f3e7114f050fbfa641b16c8c885032186e`, with a successful
[release validation run](https://github.com/victron-venus/inverter-control/actions/runs/37710820520).
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

`report_responses` and `enhancement_responses` need a maintainer's confirmation
of any reports received through other channels during the chosen 2–12 month
window. There are no public issue reports on which to calculate a response rate;
do not describe this as a measured 100% response rate. These criteria do not
offer N/A in the Passing questionnaire.

`vulnerability_report_process` and `vulnerability_report_private` have **Met**
evidence in [SECURITY.md](../SECURITY.md) and the
[private report form](https://github.com/victron-venus/inverter-control/security/advisories/new).
`vulnerability_report_response` can be **N/A** if a maintainer confirms no
vulnerability reports arrived through any channel in the preceding six months.
Otherwise check the actual acknowledgement timestamps against the 14-day limit.
A newly written response policy does not prove historical response times.

## Tests, warnings, and analysis

The following criteria have evidence supporting **Met**:

- `test`, `test_invocation`, `test_continuous_integration`: the public
  [pytest suite](../tests), [CI script](../scripts/ci.sh), and
  [quality gate](../.github/workflows/quality-gate.yml) run unit, regression,
  release-contract, and mock-integration checks before releases.
- `test_most`: the baseline reports 1,777 passing pytest tests and 96.43% measured
  statement coverage. That percentage is not a claim of branch coverage,
  exhaustive input coverage, or testing on physical inverters.
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

`dynamic_analysis_unsafe` is **N/A** for this project's own Python and shell
source: it does not produce C/C++ or other manually memory-managed components.
CPython and native libraries still require dependency and platform updates.
`warnings_strict` is a **qualified Met candidate**: the project enforces its
configured checks, but `pyproject.toml` contains documented rule exceptions.
Review those exceptions; do not claim that every available warning is enabled.

## Cryptography, delivery, and known vulnerabilities

- **Met evidence — `crypto_published`, `crypto_call`, `crypto_floss`:** the code
  uses SHA-256 through Python `hashlib` and delegates HTTPS to requests/urllib3
  and the platform TLS implementation. It does not implement a cipher or TLS
  protocol. See the [security design](security-design.md) for deployment limits.
- **Met evidence for the supported runtime — `crypto_keylength`,
  `crypto_working`, `crypto_weaknesses`, `crypto_pfs`:** the audited locked Python
  environment's urllib3 TLS context used OpenSSL 3.5.7, TLS 1.2 minimum,
  security level 2, certificate/hostname verification, and TLS 1.3 or ephemeral
  ECDHE/DHE suites with at least 128-bit encryption. SHA-256 exceeds the required
  hash size. There is no project override weakening that context. This is not a
  claim that plaintext MQTT/HTTP is encrypted, nor evidence about an unknown
  device's OpenSSL configuration. Keep the supported deployment requirements
  and external TLS terminator's policy consistent with this claim.
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

## Maintainer declarations still required

The following cannot be truthfully supplied by an automated code audit:

1. **`know_secure_design` and `know_common_errors`:** at least one actual primary
   developer must confirm their knowledge. The [security design](security-design.md)
   maps the relevant principles and errors to this project, but publishing that
   document does not establish that a person understands it. Record the
   maintainer and date in the badge application's justification once confirmed.
   The [OpenSSF secure development course](https://openssf.org/training/courses/)
   is available for preparation; a paid certificate is not required.
2. Confirm whether reports arrived outside GitHub, and check their historical
   response times as described above. Do not expose confidential report details.

Only after those declarations and checks should all applicable required
criteria be marked Met. The public application can accurately remain in
progress while an answer is unknown. Badge status and Scorecard branch
protection are separate assessments.
