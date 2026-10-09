### Required release notes

The release source commit must update `CHANGELOG.md` under one `## [X.Y.Z]`
base-version heading, with nonempty `### Upgrade` and `### Security` sections.
Describe compatibility impact, upgrade/recovery steps and security changes;
state explicitly when no special action or security change applies. Publication
reads this file from the release's source commit, not the current working copy.

Release section headings use ATX syntax (`#`, `##`, `###`), with up to three
leading spaces. Each section ends at the next heading of the same or a higher
level. Underlined Setext headings inside the selected version section are
rejected; use ATX headings or put a blank line before a thematic `---` separator.
Comments and code examples do not define release sections.

`docs/release-notes-policy.md` is the local source for this requirements block.
Use `scripts/regenerate_release.py` when updating shared release templates; its
overlay retains these requirements and checks the reviewed runner action pin.
