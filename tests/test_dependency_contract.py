"""Keep device installation and local hooks aligned with the reviewed CI environment."""

import re
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


def test_installer_preflight_enforces_the_declared_dependency_floors():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    floors = {}
    for requirement in project["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)>=([0-9.]+)", requirement)
        if match:
            floors[match.group(1).lower()] = match.group(2)
    installer = (REPO / "update.sh").read_text()
    checks = dict(re.findall(r'require_stable\("([a-z0-9-]+)", "([0-9.]+)"\)', installer))
    assert set(checks) <= set(floors)
    for name in ("cryptography", "certifi", "requests", "idna"):
        assert checks.get(name) == floors[name], name
