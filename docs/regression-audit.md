# Regression history audit

This is a dated source-history snapshot for the six-month UTC interval from
2026-04-09 through 2026-10-08, ending at
`120e3b5ae977975aeb9d080b4216d62f793934fa`. It is not a permanent claim about the
current branch. Before a badge submission, extend it through the final merged
source and confirm that the mapped tests, including the retrospective additions,
are present in that source.

The [machine-readable inventory](evidence/regression-audit-2026-10-09.json)
screens all 475 first-parent commits in that interval. It contains 283 separately
identified repair candidates in 166 work items. A source/test review maps 149
candidates to a regression that exercises the described failure condition;
134 receive no test credit. The observed conservative ratio is 52.65%. Uncertain
legacy repair candidates remain in the denominator without credit. Independent
sample review and the final merged-source extension are required before using
this snapshot for an OpenSSF assertion.

## Counting method

Start with every first-parent commit, including extensionless `setup` and
service `run` files, then inspect change descriptions and relevant source/test
diffs. Split a mixed change by its separately identifiable failures. Count a
repeated repair of the same failure once; preserve the duplicate explanation in
the inventory. Include runtime, installer, packaging, and CI failures. Exclude
new capabilities, style-only edits, documentation, and routine dependency
maintenance unless a particular repaired defect is identified.

A test-file change alone earns no credit. Review what the assertions prove and
whether they exercise the defect's input, timing, or recovery behavior. Tests
added later can protect an earlier repair. One parameterized test can cover
several failure conditions, but each condition requires its own evidence; the
number of test cases is not the number of fixed defects. Statement coverage is
reported separately and cannot substitute for this review.

The selected mapped tests passed as 381 pytest cases and eight subtests, followed
by two additional release cases and two subtests. The new retrospective group
passed as 23 cases. These are software and mock-boundary results; they do not
establish physical equipment acceptance.

## Retrospective checks

[Runtime regressions](../tests/test_runtime_regressions.py) exercise log-pipe
backpressure, overlapping discovery, typed D-Bus state replies, SmartShunt
selection after service-name changes, a blocked console sender, unit-bearing
Home Assistant values, and nonfinite legacy D-Bus values.
[Service regressions](../tests/test_service_entrypoints.py) execute the actual
entrypoint text in a private filesystem with fixed fake commands. They check
module paths, unbuffered Python, the three logger entrypoints, and rejection of
a headless setup prompt.

The [initial comparisons](evidence/regression-before-after-2026-10-09.json) and
[additional comparisons](evidence/regression-additional-proof-2026-10-09.json)
record 11 isolated old/current cases with exact historical revisions. Historical
code was reduced to the relevant unchanged function definitions or entrypoint
text; all transports and paths were replaced by bounded local fixtures. Missing
historical entrypoints are distinguished from an executed failing function.
These comparisons do not claim that an entire historical release was executed.

When repeating the inventory, retain the original source SHA and UTC interval,
record any changed classification and its reason, and attach the final CI result.
Keep prospective policy in [quality-process.md](quality-process.md) separate from
observed historical evidence.
