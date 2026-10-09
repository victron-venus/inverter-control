# Regression history audit

This audit separates the six-month baseline from subsequent reviewed fixes.
The baseline covers the UTC interval from 2026-04-09 through 2026-10-08 at
`120e3b5ae977975aeb9d080b4216d62f793934fa`. The
[integration extension](evidence/regression-extension-2026-10-09.json) records
PR305 and Silver changes through its exact `source_end` revision. These are
dated results. Before a badge assertion, confirm that this source and its mapped
tests have merged and that the final required checks passed.

The [baseline inventory](evidence/regression-audit-2026-10-09.json) screens all
475 first-parent commits. It contains 282 separately identified repair
candidates in 166 work items: 148 have a regression that exercises the described
failure condition and 134 receive no test credit (52.48%). Two independent
reviewers sampled ten mappings each. Their corrections remove tests of already
correct behavior and a performance optimization from the count, separate two
MQTT defects, and strengthen two tests that did not prove the claimed behavior.

The extension adds 15 preexisting defects fixed by PR305, six runtime defects
fixed by the Silver changes, and one correction to the coverage denominator.
Subsequent review adds two generator-output defects and one Trivy file-type
classification defect. The coverage and Trivy fixes receive no regression-test
credit. Together, these inventories contain **307 repair candidates, 171 with
reviewed regressions (55.70%)**. Uncertain legacy repair candidates remain in the
denominator without credit. This is an evidence-based estimate over the recorded
source interval, not a permanent claim about future commits.

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

The original selected mappings passed as 381 pytest cases and eight subtests,
followed by two additional release cases and two subtests. The retrospective
group passed as 23 cases. After independent review, 73 logic/watchdog cases and
15 subtests passed, including the strengthened checks. The extension and changed
mappings passed as 84 cases and 127 subtests; the two real Bandit discovery tests
passed separately with Bandit installed, rather than being credited when skipped.
These are software and mock-boundary results; they do not establish physical
equipment acceptance.

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

## Extension and independent checks

The [PR305 comparisons](evidence/regression-pr305-proof-2026-10-09.json) run
13 current regression cases against the original target module from `120e3b5`.
All reproduce a failure on that module and pass on the reviewed implementation.
They cover release-note parsing, quoted TOML keys, retryable staging and workflow
validation. Other imported dependencies and local fixtures retain their current
implementations, so these are isolated module comparisons.

The [mutation checks](evidence/regression-strengthened-proof-2026-10-09.json)
show that dropping the calculated derivative correction or restoring a setpoint
only after External mode now fails the relevant assertion. The unmodified tests
pass. In this report, `passed: false` describes the deliberately faulty mutant
being detected, not a failure of the reviewed implementation.

New signing, DESTDIR, native build and credential-file features are excluded
from the historical defect count, as are corrections made while developing
those new features. Refactoring, style edits and documentation corrections also
do not create additional fixed bugs in the inventory.


## Subsequent generation and scanner review

At source `1594a724c7f46120569891f5afdb356463c27427`, the extension also records
regeneration that downgraded the reviewed runner pin and removed mandatory
source-bound changelog instructions. These are two independently observed wrong
outputs of the generator, rather than editorial wording changes. The
[bounded old/current comparison](evidence/regression-generation-proof-2026-10-09.json)
confirms both effects using captured generator output shapes, and all 15
regeneration tests pass.

[Trivy before/after evidence](evidence/regression-trivy-proof-2026-10-09.json)
records the erroneous interpretation of `Dockerfile.dockerignore` as Dockerfile
instructions. Correcting the exclusion configuration removes that parse error
without excluding the actual Dockerfile. It receives no regression-test credit.
The initial misplaced exclusion and corrected nesting count as one repair.

The expanded namespace-aware coverage measurement completes the already-counted
coverage-denominator fix; it does not create a second defect entry. The 55 new
native-rejection and change-scope tests protect existing behavior and are not
counted as 55 new bugs. Complete-source CI and the final merged-source binding
remain required before a badge assertion.
