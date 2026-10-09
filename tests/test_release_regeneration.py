"""Regeneration must retain local security pins, release notes and policy."""

import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

from scripts import release_generation_overlay as local

REPO = Path(__file__).resolve().parents[1]
OLD_PIN = "step-security/harden-runner@351661ca32ac09a36dc5ee2d536e3128f2a3c8ed # v2.22.0"


@pytest.fixture
def repository(tmp_path):
    for name in (
        ".release-policy.json",
        ".github/workflows/release-build.yml",
        ".github/workflows/release-pipeline.yml",
        ".github/workflows/quality-gate.yml",
        "RELEASING.md",
        "docs/release-notes-policy.md",
        "scripts/regenerate_release.py",
        "scripts/release_generation_overlay.py",
        "scripts/release_signing_overlay.py",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, target)
    return tmp_path


def downgrade_templates(root):
    """Reproduce the older toolkit's pin and absent local requirements block."""
    for name in local.GENERATED:
        path = root / ".github/workflows" / name
        text = local.PIN_LINE.sub(lambda m: m["prefix"] + OLD_PIN, path.read_text())
        path.write_text(text.replace("      id-token: write\n", ""))
    path = root / "RELEASING.md"
    before, rest = path.read_text().split(local.BEGIN)
    _, after = rest.split(local.END)
    path.write_text(before + after.lstrip("\n"))


def test_overlay_repairs_real_template_drift_and_is_idempotent(repository):
    pin = local.reviewed_runner_pin(repository)
    downgrade_templates(repository)
    with pytest.raises(ValueError, match="harden-runner differs"):
        local.overlay(repository, check=True)
    local.overlay(repository)
    first = {p: p.read_bytes() for p in repository.rglob("*") if p.is_file()}
    local.overlay(repository)
    local.overlay(repository, check=True)
    assert first == {p: p.read_bytes() for p in first}
    for name in local.GENERATED:
        text = (repository / ".github/workflows" / name).read_text()
        assert OLD_PIN not in text
        assert pin in text
    assert local.BEGIN in (repository / "RELEASING.md").read_text()


@pytest.mark.parametrize("change", ["mutable", "conflicting", "missing"])
def test_ambiguous_or_mutable_pin_authority_is_rejected(repository, change):
    path = repository / ".github/workflows/release-build.yml"
    text = path.read_text()
    pin = local.reviewed_runner_pin(repository)
    if change == "mutable":
        text = text.replace(pin, local.ACTION + "@main")
    elif change == "conflicting":
        text = text.replace(pin, OLD_PIN, 1)
    else:
        text = text.replace(local.ACTION, "other-action")
    path.write_text(text)
    with pytest.raises(ValueError, match="unambiguous immutable"):
        local.overlay(repository)


@pytest.mark.parametrize("change", ["mutable", "missing", "not_generated"])
def test_changed_generated_workflow_shape_is_rejected(repository, change):
    path = repository / ".github/workflows/quality-gate.yml"
    text = path.read_text()
    pin = local.reviewed_runner_pin(repository)
    if change == "mutable":
        text = text.replace(pin, local.ACTION + "@main", 1)
    elif change == "missing":
        text = text.replace(local.ACTION, "other-action")
    else:
        text = text.replace(local.GENERATED_HEADER, "# Not a generated file", 1)
    path.write_text(text)
    with pytest.raises(ValueError, match="structure changed"):
        local.overlay(repository)


@pytest.mark.parametrize("change", ["missing", "modified", "duplicate", "anchor"])
def test_release_requirements_cannot_silently_drift(repository, change):
    path = repository / "RELEASING.md"
    text = path.read_text()
    if change == "missing":
        before, rest = text.split(local.BEGIN)
        _, after = rest.split(local.END)
        text = before + after
    elif change == "modified":
        text = text.replace("### Security", "### Overview")
    elif change == "duplicate":
        text += "\n" + local.BEGIN
    else:
        text = text.replace(local.NOTES_ANCHOR, "## Changed template\n")
    path.write_text(text)
    with pytest.raises(ValueError):
        local.overlay(repository, check=True)


def test_wrapper_restores_templates_on_every_generation(repository, tmp_path):
    policy = (repository / ".release-policy.json").read_bytes()
    downgrade_templates(repository)
    toolkit = tmp_path / "toolkit"
    (toolkit / "scripts").mkdir(parents=True)
    outputs = {
        str(p.relative_to(repository)): p.read_text()
        for p in [
            repository / "RELEASING.md",
            *(repository / ".github/workflows" / name for name in local.GENERATED),
        ]
    }
    generator = toolkit / "scripts/install_release.py"
    generator.write_text(
        "from pathlib import Path\nimport sys\n"
        + f"outputs = {outputs!r}\n"
        + "for name, text in outputs.items():\n"
        + "    (Path(sys.argv[1]) / name).write_text(text)\n"
    )
    command = [
        sys.executable,
        str(repository / "scripts/regenerate_release.py"),
        str(toolkit),
        "--root",
        str(repository),
    ]
    # Execute only the local interpreter and generated test fixture, without a shell.
    subprocess.run(command, check=True)  # nosec B603
    first = {name: (repository / name).read_bytes() for name in outputs}
    subprocess.run(command, check=True)  # nosec B603
    assert first == {name: (repository / name).read_bytes() for name in outputs}
    assert (repository / ".release-policy.json").read_bytes() == policy
    local.overlay(repository, check=True)
    pipeline = (repository / ".github/workflows/release-pipeline.yml").read_text()
    assert "      id-token: write\n" in pipeline


@pytest.mark.parametrize("field", ["release_notes", "validation_workflows"])
def test_wrapper_rejects_generator_policy_mutation(repository, tmp_path, field):
    toolkit = tmp_path / "toolkit"
    (toolkit / "scripts").mkdir(parents=True)
    policy = json.loads((repository / ".release-policy.json").read_text())
    policy[field] = "altered-policy"
    (toolkit / "scripts/install_release.py").write_text(
        "from pathlib import Path\nimport sys\n"
        + f"(Path(sys.argv[1]) / '.release-policy.json').write_text({json.dumps(policy)!r})\n"
    )
    # Execute only the local interpreter and generated test fixture, without a shell.
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            str(repository / "scripts/regenerate_release.py"),
            str(toolkit),
            "--root",
            str(repository),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Generator changed local release policy" in result.stderr


def test_current_repository_retains_local_requirements():
    local.overlay(REPO, check=True)
