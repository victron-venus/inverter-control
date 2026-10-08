"""The security gate must block findings and never publish an incomplete scan."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate(monkeypatch, tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("security_gate", scripts / "run_bandit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts" / "run_bandit.py"))
    monkeypatch.setattr(module, "version", lambda _: "1.9.4")
    monkeypatch.setattr(module, "verify_discovery", lambda _: None)
    return module


@pytest.mark.parametrize("severity", ["LOW", "MEDIUM", "HIGH"])
def test_every_severity_blocks_but_preserves_sarif(gate, monkeypatch, tmp_path, severity):
    def scan(command, **kwargs):
        assert command[1:3] == ["-m", "bandit"]
        assert "-lll" not in command
        assert kwargs["timeout"] == 300
        report = {
            "errors": [],
            "metrics": {"_totals": {"loc": 2}},
            "results": [
                {
                    "test_id": "B105",
                    "test_name": "hardcoded_password_string",
                    "more_info": "https://bandit.readthedocs.io/",
                    "issue_text": "fixture finding",
                    "issue_severity": severity,
                    "filename": "src/module.py",
                    "line_number": 1,
                }
            ],
        }
        (tmp_path / "bandit-results.json").write_text(json.dumps(report))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(gate.subprocess, "run", scan)
    assert gate.main() == 1
    report = json.loads((tmp_path / "bandit-results.sarif").read_text())
    assert len(report["runs"][0]["results"]) == 1


def test_parser_error_removes_previous_success(gate, monkeypatch, tmp_path):
    (tmp_path / "bandit-results.sarif").write_text("stale successful scan")

    def scan(*args, **kwargs):
        report = {
            "errors": [{"filename": "broken.py"}],
            "results": [],
            "metrics": {"_totals": {"loc": 2}},
        }
        (tmp_path / "bandit-results.json").write_text(json.dumps(report))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, "run", scan)
    with pytest.raises(ValueError, match="incomplete"):
        gate.main()
    assert not (tmp_path / "bandit-results.sarif").exists()


def test_failed_scanner_cannot_publish_a_clean_report(gate, monkeypatch, tmp_path):
    def scan(*args, **kwargs):
        report = {"errors": [], "results": [], "metrics": {"_totals": {"loc": 2}}}
        (tmp_path / "bandit-results.json").write_text(json.dumps(report))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(gate.subprocess, "run", scan)
    with pytest.raises(RuntimeError, match="contradicts"):
        gate.main()
    assert not (tmp_path / "bandit-results.sarif").exists()
