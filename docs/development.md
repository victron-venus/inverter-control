# Development and validation

This guide describes how to work on Inverter Control without relying on a live
installation. For contribution and review policy, see
[CONTRIBUTING.md](../CONTRIBUTING.md). For operator installation instructions, use
the [README](../README.md).

## Toolchain and dependencies

Development and CI use Python 3.12; [.python-version](../.python-version) selects
the exact interpreter. Install Git and
[uv using its documented installation instructions](https://docs.astral.sh/uv/getting-started/installation/),
then run from the repository root:

```bash
uv python install "$(cat .python-version)"
uv sync --locked --all-extras
uv run --locked python --version
```

`uv sync` creates `.venv`, installs the project, and includes the development and
test extras. `--locked` checks that the dependency lock matches the manifest and
refuses to silently rewrite it. Keep `uv.lock` under version control. The hosted
workflow pins its uv version in [.github/workflows/ci.yml](../.github/workflows/ci.yml).

When intentionally adding or updating a dependency, edit its manifest, regenerate
the lock with `uv lock`, and inspect the lockfile diff. Keep runtime dependency
bounds in `requirements.txt` consistent with `pyproject.toml`; the former is also
used by device installation. Rerun the checks and explain the dependency change
in the PR. Security-tool requirements in `.github/requirements-*.txt` are separate
hash-pinned environments and need their own updates.

## Automated tests and lint

The normal local gate is:

```bash
bash scripts/ci.sh
```

It synchronizes the locked environment, checks Ruff lint and formatting, and
runs pytest with coverage. The current minimum coverage threshold is configured
in `pyproject.toml`, rather than this guide. Coverage and lint failures fail the
command. CI runs the same entry point.

Equivalent commands are useful while developing:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest --cov=. --cov-report=xml
```

To work on one area, run a focused test without the whole-project coverage
threshold, then run the complete gate before requesting review:

```bash
uv run --locked pytest tests/test_tariff.py --no-cov
uv run --locked pytest tests/test_console_server.py --no-cov
```

Tests live in `tests/`, including public interface checks in `tests/contract/`.
`tests/conftest.py` and `tests/stubs.py` provide isolated state and device test
doubles. Prefer deterministic inputs and fake clocks/transports; use bounded
waits for asynchronous tests. A new test must not operate a real inverter,
require private credentials, or depend on a contributor's site configuration.
Keep Python assertions enabled; do not run the suite with `python -O`.

Add tests alongside new functionality and regressions. For control changes,
cover unavailable or stale telemetry, invalid inputs, command rejection,
shutdown, and recovery when relevant. Tests of a pure calculation do not prove
that its D-Bus integration or hardware behavior is correct; record which layers
were exercised.

## Security and fuzzing

Run the same validated Bandit report gate used in CI:

```bash
bash scripts/ci.sh bandit
```

This command creates an isolated hash-pinned tool environment. Every unresolved
finding severity fails the gate; an incomplete or invalid report also fails.
See [security scanning](security-scanning.md) for the scope and rules for
documented exceptions.

For the full local security entry point, install
[Trivy](https://trivy.dev/) and run:

```bash
bash scripts/ci.sh security
```

This also checks high/critical vulnerabilities, secrets, and misconfiguration.
Trivy requires access to its vulnerability database. Dependency review and CodeQL
are additional hosted checks, so local Bandit and Trivy success does not replace
the GitHub checks.

The tariff parser has an Atheris fuzz target and committed valid/invalid seeds.
The pinned Atheris wheel used here targets Linux and Python 3.12; use that
environment for local reproduction. Follow the commands in
[security scanning](security-scanning.md) or inspect the
[fuzz workflow](../.github/workflows/fuzz.yml). A minimized fuzz failure should
become a regression test before the fix is merged.

## MQTT/D-Bus mock integration

The integration gate requires a local Docker daemon with Compose and Buildx,
plus Git and network access to fetch the pinned shared test repository and
container dependencies:

```bash
bash scripts/ci.sh integration
```

The runner accepts local Unix/named-pipe Docker endpoints, creates an isolated
Compose project, and removes it and its volumes at the end. It tests shared
MQTT/D-Bus mocks from the revision pinned in `scripts/mock_integration.py`.
It does not test Venus OS device drivers or a physical installation. If Docker
is unavailable locally, state that in the PR and check the hosted integration
result; do not report the local command as passed.

## Build a local device archive

The native package is a gzip-compressed tar archive, not a compiled firmware
image. The package builder, selected inputs, and release policy are in
`scripts/package_release.py`, `.release-package.json`, and `.release-policy.json`.
From a checkout with matching version metadata and no frozen release plan, build
a local validation archive into a new or empty directory:

```bash
uv run --locked python scripts/package_release.py "$(cat version)" rc --output release-dist
```

The builder uses files known to Git, checks the version fields, and writes
`SHA256SUMS` beside the archive. It copies the working-tree contents of tracked
files, so inspect your diff and explicitly stage intended new files before
building. Untracked files, including a private `local_config.py`, are not package
inputs. Do not force-add secrets to make them part of a package. The output
directory must be empty; use a new directory for another build.

This is a local build check. It does not allocate an official candidate version,
publish a release, or deploy to a device. Published candidates and stable
promotion must use the [release runbook](release-workflow.md), including its
frozen version plan and evidence checks.

When changing release tooling, run its contract tests as well:

```bash
uv run --locked python -m unittest discover -s .github/release-tests -p 'test_*.py' -v
```

Some release scripts are vendored from `victron-venus/venus-os-ci-toolkit`; follow
the source/update instructions in the release runbook before changing them.

The consumer currently carries a narrow addition to `scripts/release_control.py`
for `release_notes: "CHANGELOG.md"`. Preserve it until the shared toolkit adopts
the hook: publishing reads the changelog at the exact package source SHA and
requires one matching base-version section with nonempty `### Upgrade` and
`### Security` guidance. The generated build provenance is retained below it.
Update these notes in the same PR as user-visible or security changes; do not
mark a future feature or an unperformed validation as already released.
`tests/test_release_notes.py` verifies this contract. Regenerating vendored
files without carrying the hook forward would remove this publication check.

## Configuration and device acceptance

The automated unit suite does not require a private configuration. For an
explicitly authorized test installation, copy the current template only if a
private configuration does not already exist:

```bash
test -e local_config.py || cp local_config.example.py local_config.py
```

Edit `local_config.py` with the test site's values and keep it out of Git.
The old `secrets.py` setup is obsolete. Device installation, persistent
configuration, and platform dependencies are described in the README; a laptop
virtual environment does not reproduce Venus OS.

Begin device control verification with the explicit dry-run switch:

```bash
python3 main.py --dry-run
```

Run that command only on the intended test installation with its required
dependencies and service ownership arranged. Dry-run suppresses automatic grid
regulation writes; an explicit persistent override and some manual ESS or Home
Assistant actions can still write, and an MQTT command can turn dry-run off.
Isolate command publishers and disable Home Assistant actuators in the test
environment. The controller also initializes integrations, reads telemetry,
and can create network listeners or publish diagnostics. Review the
[security policy](../SECURITY.md) and [interface reference](interfaces.md)
before device tests. Avoid a second controller competing with an already-running
service. Running without `--dry-run` uses the configured default, which is
currently live operation.

For changes affecting hardware, maintainers must record the target model,
Venus OS version, source/release version, relevant settings, expected and observed
results, and tested failure/recovery cases before accepting a candidate for that
installation. Start with dry-run observations, then perform any live test under
the operator's control. Use the relevant
[grid telemetry safety](grid-telemetry-safety.md) and feature documentation to
select acceptance cases. Do not infer hardware acceptance from a successful
unit test, mock integration, fuzz run, or archive build.
