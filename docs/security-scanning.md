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
