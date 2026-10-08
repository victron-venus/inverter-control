# OpenSSF Best Practices evidence

This is the evidence index for the project's **Passing** self-assessment against
the [OpenSSF criteria](https://www.bestpractices.dev/en/criteria/0?details=true&rationale=true).
The project achieved the **Passing badge on 2026-10-08 at 02:37 UTC**. The
[public assessment](https://www.bestpractices.dev/en/projects/15293/passing)
records 100% completion: 63 criteria marked Met and four marked N/A with
justifications. This is a voluntary self-assessment, not an independent security
certification. It must continue to reflect the released software and actual
maintainer practices; a successful scanner run does not establish every criterion.

The public [application is project 15293](https://www.bestpractices.dev/en/projects/15293).
[.bestpractices.json](../.bestpractices.json) supplies proposed answers in the
official repository automation format. All 67 proposed statuses matched the
published assessment when the badge was awarded. Review new evidence and the
resulting form before saving future updates; do not mark unverified criteria Met.

Evidence was reviewed on **2026-10-08 UTC**. The baseline release is
[v1.23.5-beta.9](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.5-beta.9),
source `c651e9f3e7114f050fbfa641b16c8c885032186e`, with a successful
[release validation run](https://github.com/victron-venus/inverter-control/actions/runs/37710820520).
The documentation and security improvements were subsequently merged in
[PR #303](https://github.com/victron-venus/inverter-control/pull/303) and published
in [v1.23.5-beta.10](https://github.com/victron-venus/inverter-control/releases/tag/v1.23.5-beta.10),
source `17143487b07bcbdd8f77f0878ed9b88373e2dd8d`, before the assessment was submitted.
The final [quality gate](https://github.com/victron-venus/inverter-control/actions/runs/37714862769)
passed 1,809 tests with 94.27% statement coverage, and the
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
these declarations resolve the report-history questions for this assessment.

The assessment was saved and submitted, and its Passing status was verified on
OpenSSF and through the public project API on **2026-10-08 UTC**.
Badge status and Scorecard branch protection are separate assessments.

The October 2026 maintenance imports the release identity and metadata parser
from `venus-os-ci-toolkit` revision `9dd211a`, including bounded TOML parsing and
source-bound release-note validation. Consumer release-contract suites verify
the imported engine. Workflow-validator refactoring preserves this repository's
existing policy; it does not imply all newer toolkit workflow guarantees are enabled.
