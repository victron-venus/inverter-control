## Problem and change

Describe the trigger, previous behavior, and resulting behavior. Link the issue if applicable.

## Validation

List the exact commands and results. Distinguish automated unit/mock tests from device checks.
For hardware checks, identify the source/release, device, firmware, settings and observed results.
If hardware was not tested, say so.

## Review checklist

- [ ] Tests cover new behavior or the regression; explain any not-applicable case.
- [ ] Lint, formatting and applicable CI/security checks pass.
- [ ] Public interfaces, configuration and upgrade/recovery notes are updated when affected.
- [ ] Security boundaries and failure/recovery behavior were considered.
- [ ] The diff contains no credentials, private configuration or unsanitized device logs.

For a suspected vulnerability, use the private reporting process in SECURITY.md before posting details publicly.
