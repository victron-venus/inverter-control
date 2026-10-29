"""Keep device installation and local hooks aligned with the reviewed CI environment."""

import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def test_device_requirements_match_declared_runtime_and_device_extras():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    requirements = {
        line.strip()
        for line in (REPO / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    # The test extra contains optional device D-Bus and metrics integrations.
    expected = set(project["dependencies"]) | set(project["optional-dependencies"]["test"])
    assert requirements == expected


def test_precommit_ruff_uses_the_same_version_as_locked_ci():
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    version = next(package["version"] for package in lock["package"] if package["name"] == "ruff")
    hooks = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    ruff = next(
        repo
        for repo in hooks["repos"]
        if repo["repo"] == "https://github.com/astral-sh/ruff-pre-commit"
    )
    assert ruff["rev"] == f"v{version}"
