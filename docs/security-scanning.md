# Security scanning

Run `bash scripts/ci.sh bandit` for the same Bandit gate used by pull requests,
releases and the scheduled Python Security Scan. Every unresolved severity
blocks the gate. Parser errors, missing metrics, empty scans and scanner failures
also block it. A complete scan with findings still produces SARIF so GitHub can
display the findings; an incomplete scan never replaces the previous analysis.

The canonical configuration is `.github/bandit.yml`. It excludes generated
virtual environments and build output, not production modules, release scripts
or tests. Only pytest assertions receive a test-file exception. Other exceptions
are specific rule IDs on individual reviewed lines:

- B404/B603: explicit subprocess argument vectors.
  Privileged D-Bus operations use fixed system executable paths. Release tools
  use reviewed repository operations and validate their GitHub/resource inputs.
  Local release policy commands intentionally support trusted repository shell
  scripts; no release arguments or remote event data are interpolated into them.
- B607: developer tools and isolated installer tests intentionally use the
  caller's toolchain or a fixture PATH; these are not privileged daemon commands.
- B105/B108: documented placeholder credentials and invalid-path test fixtures.
- B110: optional diagnostics and failure-path delays must not interrupt hardware
  control, overwrite transport results or prevent recovery.

Do not add blanket rule skips or exclude tests to clear alerts. New exceptions
need a local explanation and review. Keep vendored release-tool annotations when
refreshing the toolkit and rerun the gate afterwards.

Both `pyproject.toml` and `requirements.txt` require `idna>=3.15` to exclude
CVE-2026-45409. Updating `uv.lock` alone does not protect installations that
resolve `requirements.txt` or reuse an older system dependency. Requests' minimum
version is also consistent across both installation paths.

`fuzz/fuzz_tariff.py` uses Atheris to exercise the bounded tariff JSON import path.
It checks rejection of malformed input, stable normalization and strict JSON
round trips. CI replays the committed valid/invalid seeds and fuzzes for 60 seconds
on every PR and release validation; the full quality gate also runs weekly.
To reproduce on Linux with Python 3.12:

```sh
python -m pip install --require-hashes --only-binary=:all: -r .github/requirements-fuzz.txt
python fuzz/fuzz_tariff.py fuzz/corpus/tariff -max_total_time=60 -max_len=100001 -timeout=5 -rss_limit_mb=1024
```

The OpenSSF best-practices badge requires a separate, truthful maintainer
self-assessment. Code changes do not by themselves earn that certification.
Scorecard's maximum branch-protection score additionally expects two independent
approvals and no administrator bypass. Preserve an achievable review process;
do not manufacture approvals or hide those organizational findings.

Trivy's standalone workflow publishes every severity to the Security tab on
pushes to `main` and weekly. It retains the original `trivy-fs.yml:trivy-scan`
configuration identity so the dashboard does not become stale. The release
security workflow independently blocks HIGH/CRITICAL vulnerabilities, secrets
and misconfiguration before a release.

## GitHub Code Quality review (2026-10-07)

Standard findings are separate from code-scanning alerts. Empty fallback
handlers now explain the fallback or handle the failure explicitly; imports,
unused state and test assertions are cleaned up. Async test waits have deadlines
so missed cancellation or refresh signals fail rather than hang CI.

The eleven AI findings shown during this review were snapshots from July 18–19,
not an analysis of the current branch. GitHub reported that the organization's
AI usage limit blocked further scans until October 31. Their individual outcomes:

- `config.py`: inverter state 0 already reads `Off`.
- `homeassistant.py`: hardcoded recliner/garage entities have been removed.
- `victron.py`: singleton creation/reset now share a lock; time-to-go formatting
  uses explicit hours, remaining seconds and minutes. The subprocess warning is
  a false positive: `subprocess.run(timeout=...)` kills and waits for the direct
  D-Bus client before raising `TimeoutExpired`; it is not a bare `Popen` call.
- Former `main.py` controller findings: duplicate `start_time` was removed,
  MPPT powers now use one collected list, SIGALRM cycle handling was replaced,
  dry-run defaults already use `args.dry_run or None`, and one-shot execution
  already performs shared shutdown in `finally`, including MQTT cleanup.
- `setup`: the obsolete `site_config.example.py` copy no longer exists. Setup
  delegates to `update.sh` and checks its exit status.

Do not enable paid AI overages or claim a fresh AI scan to clear these historical
snapshots. Reconcile them against source, and retain the ordinary CodeQL,
Code Quality, Sonar, Bandit, Trivy and test checks.
