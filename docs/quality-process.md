# Quality evidence and maintenance

The [contribution requirements](../CONTRIBUTING.md) define acceptable changes. Python is the primary implementation language. Its style baseline is PEP 8 with the repository's 100-character line length and Ruff configuration; `ruff format --check` and `ruff check` run in the normal CI gate. Keep exceptions explicit and reviewable. Shell launch/install scripts must retain their declared shell syntax, quote paths and arguments, and preserve the tested installation contracts.

## Automated validation

The [release pipeline](../.github/workflows/release-pipeline.yml) triggers on every push to `main` and calls the [CI workflow](../.github/workflows/ci.yml). Its scope stage forces full validation. Pull requests use the [quality gate](../.github/workflows/quality-gate.yml). `scripts/ci.sh` runs locked dependency installation, lint, format checks, and pytest. The workflow publishes pass/fail results; failed checks block normal merging under repository rules.

The pytest coverage configuration measures Python statements using the FLOSS `coverage.py`/`pytest-cov` tools. The standard command uses `--cov=.` and writes `coverage.xml`; its minimum is 81%, above the Silver 80% threshold. Test source, example configuration and external virtual environments are excluded from both collection and reporting. Namespace discovery includes unimported build/release scripts; all executable project Python files remain in the denominator. This prevents executing the tests themselves from inflating the software's coverage.

At merged source `120e3b5ae977975aeb9d080b4216d62f793934fa` on 2026-10-08, [main CI](https://github.com/victron-venus/inverter-control/actions/runs/37861640088/job/113598901119) reported 1,937 passing tests and 94.33% under the older measurement, which included test source. Do not compare that percentage directly with the corrected production-only result. Coverage is a dated source result, not a permanent claim for future commits or proof of hardware acceptance.

The [complete-source audit](evidence/coverage-2026-10-09.json) records 89.2186% statement coverage across all 67 executable project Python files on 2026-10-09. Its per-file hashes identify the measured source.

The unit suite, mock MQTT/D-Bus integration, fuzz target, security checks, installer checks, and release contract tests cover different boundaries. Follow [development and validation](development.md), [security scanning](security-scanning.md), and the release instructions for the applicable commands. Do not replace a missing test with a statement that coverage is high.

## Regression records

For each fixed defect, add a test that exercises the previously failing input, sequence, timing, or recovery path and would detect its recurrence. Record the defect and the test in the PR. When a fix includes several unrelated defects, list them separately. Tests of a success path or unrelated file do not count as regression coverage for the defect.

If an automated regression is impractical, record why and provide a bounded manual reproduction/validation procedure. The exception remains visible in the historical regression inventory. A test added later may close that gap, but must link the original defect and the later test change.

Review the rolling six-month inventory before updating the OpenSSF regression-test claim. Count distinct defects, including installer and release defects, rather than assuming that every PR is one bug or that any change to a test file proves coverage. Record the source interval, exclusions, unresolved ambiguities, and exact tests. High statement coverage and a work-item-level estimate alone are insufficient evidence for the per-defect 50% criterion.

The [dated regression audit](regression-audit.md) records the source interval, per-defect mappings, exclusions, and retrospective comparisons. It must be extended through the final merged source before badge submission.

The retrospective tests in [tests/test_runtime_regressions.py](../tests/test_runtime_regressions.py) cover historical failures including log-pipe backpressure escaping into callers, overlapping discovery subprocesses, and failure to parse typed D-Bus inverter-state replies. Their historical fixes were [#167](https://github.com/victron-venus/inverter-control/pull/167), [f0a16fc](https://github.com/victron-venus/inverter-control/commit/f0a16fc), and [#168](https://github.com/victron-venus/inverter-control/pull/168). They also exercise SmartShunt source selection, a blocked console sender, unit-bearing HA values, and nonfinite D-Bus values. [Service entrypoint tests](../tests/test_service_entrypoints.py) execute private fixtures for the deployed Python/logging commands and headless setup guard.

## Current interfaces and documentation

Use current APIs available on the supported Python 3.12/Venus OS profile. Remove obsolete imports and APIs when their supported replacement is available, while keeping explicit compatibility paths for supported systems. Dependency updates must include an appropriate API/behavior review; a vulnerability scan does not establish that every API is current.

One accepted test-only warning is the `CryptographyDeprecationWarning` from the public FFDH parameter loader in [the TLS key-exchange regression](../tests/test_tls_key_exchange.py). Cryptography 50 deprecates FFDH, but this test must still calibrate the exact 2047/2048-bit fixtures independently before proving weak-key rejection without private HTTP bytes. The warning remains visible; it is not an ignored production diagnostic or a claim that the deprecated API is current. When the loader is removed, replace the fixture parser while retaining the real handshake assertions. Do not downgrade cryptography, switch to its private APIs, or suppress unrelated warnings.

The shared [Trivy configuration](../trivy.yaml) loads the [path-scoped exception file](../.trivyignore.yaml). Its sole Dockerfile exception is DS-0026 (missing runtime healthcheck) for the offline ARMv7 dependency build's artifact-only scratch export. A build output has no running service to probe. Other rules and other Dockerfiles retain this check; reassess the exception if the target becomes runnable. Trivy cannot attach an inline exception to an absent instruction, so the rule and exact file path are recorded together instead.

Documentation changes and known inconsistencies follow the same issue/PR process as code defects. The architecture, interface reference, security requirements, contribution guide, and roadmap are linked from the README. Version-specific examples must identify their version. Record external achievements within 48 hours as required by the contribution policy; keep their live status linked rather than copying an outdated badge level into prose.
