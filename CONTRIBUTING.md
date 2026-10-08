# Contributing to Inverter Control

Inverter Control manages a Victron installation through Venus OS D-Bus, MQTT,
and optional Home Assistant integrations. Contributions to code, tests,
documentation, and reproducible bug reports are welcome. Public contributions
and discussions can be in English.

## Report a bug, request a feature, or ask a question

Search the [issue tracker](https://github.com/victron-venus/inverter-control/issues)
and [pull requests](https://github.com/victron-venus/inverter-control/pulls) first.
For a new bug, use the bug-report template and include:

- The release version or Git commit, Venus OS version, and relevant hardware.
- Steps to reproduce, the expected result, and the actual result.
- Whether the controller was in dry-run or live mode.
- Relevant configuration and logs with credentials, tokens, private addresses,
  and identifying site information removed.

Open feature requests in the same issue tracker. Describe the problem, the
intended users, and a concrete example of the desired behavior. Discuss changes
to control behavior, public interfaces, or dependencies before substantial
implementation work. Questions can also go to
[Discussions](https://github.com/victron-venus/inverter-control/discussions).
Issues, PRs, and their responses provide a public, searchable history.

Report suspected vulnerabilities through [the security policy](SECURITY.md),
including its private reporting route, rather than posting exploit details or
credentials in a public issue. Maintainers should acknowledge incoming reports
and feature requests, ask for missing information, and record a disposition;
an acknowledgment does not promise implementation or a release date.

## Set up a development checkout

Use Git, [uv](https://docs.astral.sh/uv/), and Python 3.12. The exact Python version
used by CI is recorded in [.python-version](.python-version).

```bash
git clone https://github.com/victron-venus/inverter-control.git
cd inverter-control
git switch -c fix/describe-the-change
uv python install "$(cat .python-version)"
uv sync --locked --all-extras
```

External contributors should fork the repository and clone their fork instead.
Run commands from the repository root. The default tests use test doubles for
the device integrations; they do not require a private `local_config.py` or a
connected inverter. See the [development guide](docs/development.md) for focused
tests, security checks, packaging, and device acceptance.

## Submit a pull request

1. Make a focused change on a branch and explain the problem it solves.
2. Add or update automated tests for changed behavior. Major new functionality
   must include tests; bug fixes should include a regression test that fails
   without the fix. Exercise invalid inputs and failure/recovery paths as well
   as the normal case. If a meaningful automated test is not possible, explain
   why and provide a reproducible manual procedure in the PR.
3. Update user or interface documentation for changed commands, configuration,
   outputs, or behavior. Explain compatibility and migration effects.
4. Run the checks below and inspect your diff for secrets and unrelated files.
5. Commit the intended files, push the branch, and open a PR targeting `main`.
   Fill in the PR template, link related issues, and list the exact checks and
   outcomes. State explicitly which hardware tests were or were not performed.
6. Respond to review feedback and rerun affected checks after changes. Required
   checks and the repository's review rules must be satisfied before merge.

```bash
bash scripts/ci.sh
bash scripts/ci.sh bandit
git diff --check
git diff
```

The first command checks formatting, lint, and the full pytest suite with
coverage. Security, mock integration, dependency, and release-tooling checks
also run in GitHub Actions. A green local run is only part of PR validation.
Do not use administrative bypasses as a substitute for fixing a failed check.
PR approval and release promotion remain subject to repository policy; the
legacy `commit.sh` helper is not required to contribute.

## Standards for acceptable changes

- Follow existing module conventions and the Ruff configuration in
  [pyproject.toml](pyproject.toml): Python 3.12 syntax and a 100-character line
  length. Use `uv run --locked ruff format .` to format changed Python code and
  review the resulting diff.
- Keep functions focused. Explain non-obvious control, concurrency, unit,
  sign-convention, and recovery decisions in code or documentation.
- Validate external inputs and bound waits, queues, and resource use. Preserve
  dry-run behavior and fail-safe paths when modifying device control. Prefer
  existing, maintained libraries over custom security or cryptographic code.
- Fix lint and security findings. Any justified suppression must be narrow,
  documented beside the code, and reviewed; do not disable a rule or exclude
  tests simply to make a scanner pass. See [security scanning](docs/security-scanning.md).
- Keep dependencies minimal. When dependencies change, update the appropriate
  manifest and `uv.lock` together; keep runtime bounds in `pyproject.toml` and
  `requirements.txt` consistent. Explain the purpose and compatibility impact.
- Never commit real tokens, credentials, device configuration, or logs containing
  them. Use explicit placeholders and isolated fixtures.
- Treat reviewers and other contributors respectfully. Explain technical
  disagreements with evidence and reproducible examples.

## Review, release, and license

Maintainers review behavior, safety implications, tests, security findings, and
documentation. A requested change may be deferred or declined with an
explanation. Automated review supports this process; it is not evidence that a
person independently tested hardware or reviewed the change.

Version allocation, release notes, candidate validation, stable promotion, and
rollback follow [RELEASING.md](RELEASING.md) and the
[release runbook](docs/release-workflow.md). Describe user-visible changes and
known limitations in the PR so maintainers can prepare useful release notes.
Identify published vulnerability IDs when a release fixes them. Building or
publishing an artifact does not authorize deployment to a physical installation.

By contributing, you agree that your contribution can be distributed under the
project's [MIT License](LICENSE). Submit only material you have the right to
contribute and retain required third-party notices.
