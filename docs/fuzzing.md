# Release-candidate fuzz testing

See the [illustrated RC fuzzing process and verified rollout results](https://github.com/victron-venus/venus-os-ci-toolkit/blob/main/docs/rc-fuzzing-results.md)
for the shared release flow, evidence requirements and a dated cross-project
snapshot. The project-specific scope and reproduction guidance below still apply.

## Scope and assertions

**Bounded tariff import.** The target calls the production tariff stream reader and validator. It checks stable normalization and strict JSON round trips using compact, dashboard-shaped and invalid tariff seeds. It does not exercise the physical control loop or D-Bus writes.

The harness is [fuzz/fuzz_tariff.py](../fuzz/fuzz_tariff.py), with versioned
synthetic inputs in [fuzz/corpus/tariff](../fuzz/corpus/tariff). It exercises project code rather than
only a standard-library JSON decoder. Expected validation rejections are handled
at their documented boundary; assertion failures and unexpected exceptions fail
the run. Seeds contain no credentials, device identities or captured user data.

## When it runs and what it costs

The canonical release pipeline calls the reusable fuzz workflow in
`victron-venus/venus-os-ci-toolkit`, pinned by commit in `.release-policy.json`.
`.github/fuzz.json` declares this repository's setup commands, target and seed
corpus. Only a **public repository release candidate (RC)** starts the campaign.
Normal pull requests, branch pushes, beta releases and schedules do not run it.
Private repositories are rejected by the shared eligibility check.

This target has a 60-second mutation budget, a 10-second per-input timeout and a
2048 MiB libFuzzer memory limit. Setup, seed replay and artifact upload add some
wall time; 60 seconds is not a promise of total Actions runtime. Shared limits cap
the number of targets and combined budget. Ordinary unit tests still run on their
existing cadence. Regression tests for fixed findings remain fast unit tests.

An RC must pass the fuzz gate before release publication. The receipt records
source revision, manifest hash and result. Committed corpus files are bound
transitively by that source revision; there is no separate seed digest or complete
toolchain receipt. Stable promotion
reuses the accepted RC result for the same source/artifacts; it is not a second
campaign. A changed source or failed gate requires a corrected RC and new evidence.
Do not bypass a failed gate or treat a skipped job as a passing campaign.

## Isolation and reproducibility

CI installs the hash-pinned Python 3.12/Linux x86-64 runtime from
`.github/requirements-fuzz.txt` in `.fuzz-venv`. Setup uses explicit argument arrays;
the shared runner executes the target in a network-isolated environment. No device
credentials, production broker, Cerbo, Home Assistant or live actuator is needed.
The harness uses fresh state for each input (or immutable fixtures) and bounded
inputs. Fuzz results are evidence about these targets, not a certification of
hardware safety or exhaustive coverage of every component.

On a disposable Linux x86-64 development environment with Python 3.12:

```sh
python3.12 -m venv .fuzz-venv
.fuzz-venv/bin/python -m pip install --require-hashes --only-binary=:all: -r .github/requirements-fuzz.txt
.fuzz-venv/bin/python fuzz/fuzz_tariff.py -max_len=100001 fuzz/corpus/tariff -runs=0
.fuzz-venv/bin/python fuzz/fuzz_tariff.py -max_len=100001 fuzz/corpus/tariff -max_total_time=60 -timeout=10 -rss_limit_mb=2048
```

The first target command replays the committed corpus; the second runs mutations.
Use a copied corpus for local campaigns if you do not want libFuzzer to add seeds
to the working tree. The pinned Atheris wheel is Linux x86-64 specific. A local
unit-test or seed-only replay on another platform is not an instrumented fuzz run.

## Handling a failure

1. Download the failing run's diagnostics and minimized/crashing input from its
   Actions artifacts. Record the exact source SHA, manifest and tool versions.
2. Replay only that input with the same target/runtime, for example
   `.fuzz-venv/bin/python fuzz/fuzz_tariff.py -max_len=100001 path/to/failing-input`.
3. Determine whether the failed assertion expresses the intended public contract.
   Fix project code or an incorrect oracle with review; do not broadly catch
   exceptions, drop inputs or weaken the gate to hide a failure.
4. Add a small deterministic regression test and a synthetic corpus seed, run the
   relevant unit tests, and publish a new RC for another full bounded campaign.
5. Report a security-sensitive finding through [SECURITY.md](../SECURITY.md),
   avoiding sensitive data in public artifacts or issues.

The README badge **Fuzzing: RC only** describes this configured policy. It is a
static label, not a live success indicator or claim of Google OSS-Fuzz enrollment.
The specific RC's Actions result and receipt are the execution evidence.

## Change from the previous cadence

The former standalone Atheris job ran from ordinary CI. It has moved to the
shared public-RC gate. The Docker/ASan CFFI random-input campaign is retained in
`native-memory-check.yml` as an explicitly manual diagnostic; it no longer runs
on each PR, beta or scheduled quality run. This reduces automatic native
mutation coverage and Actions cost. Its historical results remain historical
evidence, not proof that the current RC ran ASan. Ordinary deterministic tests
and the ARMv7 dependency build keep their existing cadence.


## Deterministic preflight in ordinary CI

For relevant changes in a public repository, the existing CI configuration
contract job installs the hash-locked Python 3.12 fuzz dependencies, runs
`pip check`, and replays the committed corpus with Atheris `-runs=0`.
This is deterministic seed replay: it does not start the timed mutation
campaign. Timed fuzzing remains exclusive to release candidates. The shared
preflight has a four-minute total budget, including dependency setup.

The `preflight-paths` entries in `.github/fuzz.json` select the relevant
production, dependency and harness changes. Shared contract, manifest and
workflow changes also trigger this check; missing comparison history runs
the check conservatively. Unrelated changes can skip this extra replay.

Before replay, the shared helper verifies that the following declarations
refer to project-local Python functions already instrumented by the
harness. Missing import instrumentation fails the check instead of
silently counting executions with incomplete feedback:

- `inverter_control.tariff._read_stream`
- `inverter_control.tariff.validate_tariff`

Update these declarations when production functions move or are renamed.
They prove that the selected code is instrumented; they do not prove
exhaustive path coverage. Keep positive and rejected-input corpus cases
and independent semantic assertions alongside the existing unit tests.

## Release workflow retries

Fresh release inputs are selected by the exact GitHub workflow run ID and run
attempt.

Before publication starts, use **Re-run all jobs** only if the allocator and
release policy permit reuse of that run's reservation. Recreate every required
input in the same attempt; failed-only or individual retries can leave inputs
missing, and older artifacts must not fill those gaps.

For a fresh candidate or final build, once publication has started
(`begin_publication`), or its external publication result is partial or uncertain,
inspect the existing release, tags and assets before retrying. A consumed build
number requires a new workflow run and reservation authorized by the release
policy; an existing immutable tag may also require a new base version. Never
reset the release ledger, overwrite assets or retag to force a retry. Historical
byte promotion of an accepted RC keeps its reserved number and follows the
existing promotion policy.

Artifacts from older attempts may remain available for diagnostics; they are not
inputs to the new attempt and do not need to be deleted. Filtering artifact names
alone does not prove package completeness or receipt identity. Complete package
inventory and receipt identity checks remain separate release requirements.

See the shared [package identity and retry contract](https://github.com/victron-venus/venus-os-ci-toolkit/blob/main/docs/VERSIONING.md#package-identity-across-reruns).

### Package receipt identity

Fresh publication verifies each staged package receipt against the independently
supplied repository, source commit, workflow run and attempt. Receipts created
before this migration, or offline without an Actions identity, cannot qualify a
fresh publication. Run all required producers in one complete workflow attempt
after migration; an earlier attempt cannot fill missing inputs.

Historical promotion and retained-evidence validation keep their existing checks.
Receipt coverage validates the staged files; the expected packages and platforms
still require a separate completeness check. Existing release approval and device
acceptance policies continue to apply.

See the shared [receipt migration guide](https://github.com/victron-venus/venus-os-ci-toolkit/blob/main/docs/release-workflow.md#migrate-package-receipt-identity-without-replacing-the-release-engine).

### Complete package producer inventory

`versioning.required_build_receipts` declares the exact package and signature
receipt filenames required by this project's final publisher. Each authenticated
receipt must match the repository, source commit, workflow run and attempt. A
missing producer or an unexpected receipt stops fresh publication, even when the
remaining packages are valid. RC fuzz evidence is checked separately; pre-signing
checks do not require a signature receipt that has not been created yet.

Before publication begins, rerun all required producers only when the allocator
and release policy permit reusing that reservation. After publication begins or
its outcome is uncertain, inspect existing tags, releases and assets before a
policy-authorized new run; never reset the ledger or mix earlier-attempt packages.
Historical RC promotion keeps its accepted source policy. Keep this explicit
inventory aligned with producer changes; it does not replace package-format,
signature or device acceptance checks.
