"""Keep human release notes bound to the source used to build the package."""

import base64
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "notes_release_control", Path(__file__).parents[1] / "scripts/release_control.py"
)
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)
SOURCE = "a" * 40


class StrictGitHub:
    """Permit only the expected source read and reject all remote mutation."""

    def __init__(self, response: dict | None = None):
        self.responses = [] if response is None else [response]
        self.calls = []
        self.writes = []

    def api(self, path: str, method: str = "GET", body: dict | None = None):
        self.calls.append((method, path, body))
        if method != "GET":
            self.writes.append((method, path, body))
            message = f"Unexpected remote write: {method} {path}"
            raise AssertionError(message)
        if path != f"contents/CHANGELOG.md?ref={SOURCE}" or not self.responses:
            message = f"Unexpected remote read: {path}"
            raise AssertionError(message)
        return self.responses.pop(0)

    def upload(self, tag: str, path: Path):
        self.writes.append(("upload", tag, path))
        message = f"Unexpected remote upload: {tag} {path}"
        raise AssertionError(message)


def contents(text):
    raw = text.encode()
    return {
        "type": "file",
        "path": "CHANGELOG.md",
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
        "size": len(raw),
        "sha": release.hashlib.sha1(
            b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
        ).hexdigest(),
    }


NOTES = """# Changelog
## [1.2.4] - Unreleased
Do not publish this later change.
## [1.2.3] - 2026-10-07
### Fixed
Preserve unavailable telemetry instead of reporting zero.
### Upgrade
Review optional site settings before enabling the feature.
### Security
Reject malformed requests before issuing hardware commands.
## [1.2.2] - 2026-10-01
Do not copy older notes either.
"""


def render(text=NOTES, tag="v1.2.3-beta.8", response_change=None):
    response = contents(text)
    response.update(response_change or {})
    github = StrictGitHub(response)
    with patch.object(
        release, "source_policy_snapshot", return_value={"data": {"release_notes": "CHANGELOG.md"}}
    ):
        body = release.release_notes(github, tag, SOURCE, "Original source and validation links.")
    assert github.calls == [("GET", f"contents/CHANGELOG.md?ref={SOURCE}", None)]
    assert not github.responses
    assert not github.writes
    return body


@pytest.mark.parametrize("tag", ["v1.2.3", "v1.2.3-rc.1", "v1.2.3-beta.8"])
def test_notes_use_only_exact_base_and_retain_provenance(tag):
    body = render(tag=tag)
    assert "Preserve unavailable telemetry" in body
    assert "Do not publish" not in body
    assert "Do not copy" not in body
    assert body.endswith("Original source and validation links.")
    assert "## Build provenance" in body


def test_crlf_notes_preserve_source_byte_verification():
    crlf = NOTES.replace("\n", "\r\n")
    assert render(crlf) == render(NOTES)
    lf_contents = contents(NOTES)
    with pytest.raises(release.ReleaseError, match="size mismatch"):
        render(crlf, response_change={"size": lf_contents["size"]})
    with pytest.raises(release.ReleaseError, match="blob identity"):
        render(crlf, response_change={"sha": lf_contents["sha"]})


@pytest.mark.parametrize(
    "text",
    [
        NOTES.replace("[1.2.3]", "[2.0.0]"),
        NOTES + "\n## [1.2.3]\nDuplicate version.\n",
        NOTES.replace("### Upgrade", "### Other"),
        NOTES.replace("### Security", "### Other"),
        "## [1.2.3]\n### Upgrade\n\n### Security\nNo vulnerabilities fixed.\n",
    ],
)
def test_missing_ambiguous_or_incomplete_notes_fail(text):
    with pytest.raises(release.ReleaseError):
        render(text)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "symlink"},
        {"path": "other.md"},
        {"size": 1},
        {"sha": "b" * 40},
        {"content": "!invalid!"},
        {"content": "a" * 400_001},
    ],
)
def test_corrupt_or_wrong_source_is_rejected(changes):
    with pytest.raises(release.ReleaseError):
        render(response_change=changes)


def test_notes_policy_is_opt_in_for_other_toolkit_consumers():
    github = StrictGitHub()
    with patch.object(release, "source_policy_snapshot", return_value={"data": {}}):
        assert release.release_notes(github, "v1.2.3", SOURCE, "provenance") == "provenance"
    assert not github.calls
    assert not github.writes


def test_missing_notes_abort_before_remote_release_mutation(tmp_path):
    github = StrictGitHub()
    with (
        patch.object(release, "release_notes", side_effect=release.ReleaseError("missing notes")),
        pytest.raises(release.ReleaseError, match="missing notes"),
    ):
        release.publish(github, "v1.2.3", SOURCE, tmp_path, False, "provenance")
    assert not github.calls
    assert not github.writes


def test_source_changelog_route_is_pinned_to_a_commit():
    import re

    allowed = release.API_PATHS["GET"]
    assert any(re.fullmatch(pattern, f"contents/CHANGELOG.md?ref={SOURCE}") for pattern in allowed)
    for ref in ("main", "v1.2.3", "a" * 39, "../main"):
        assert not any(
            re.fullmatch(pattern, f"contents/CHANGELOG.md?ref={ref}") for pattern in allowed
        )
