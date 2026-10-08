"""Exercise the complete validator when optional generator pins are absent."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "pin_contracts", ROOT / "scripts/workflow_contracts.py"
)
contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contracts)


class ActionPinsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / ".github/workflows", self.root / ".github/workflows")
        shutil.copyfile(ROOT / ".release-policy.json", self.root / ".release-policy.json")

    def change_reference(self, reference, *, reusable=False):
        path = self.root / ".github/workflows/ci.yml"
        # BaseLoader creates only scalar strings, lists and dictionaries.
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)  # nosec B506
        if reusable:
            workflow["jobs"]["fuzz"]["uses"] = reference
        else:
            workflow["jobs"]["ci"]["steps"][0]["uses"] = reference
        path.write_text(yaml.safe_dump(workflow))

    def test_actual_workflows_pass_without_optional_manifest(self):
        self.assertFalse((self.root / ".github/action-pins.json").exists())
        contracts.validate(self.root)

    def test_mutable_step_action_is_rejected_without_manifest(self):
        self.change_reference("actions/checkout@main")
        with self.assertRaisesRegex(ValueError, "immutable SHA"):
            contracts.validate(self.root)

    def test_mutable_reusable_workflow_is_rejected_without_manifest(self):
        self.change_reference("owner/repo/.github/workflows/check.yml@v1", reusable=True)
        with self.assertRaisesRegex(ValueError, "immutable SHA"):
            contracts.validate(self.root)

    def test_docker_action_requires_content_digest(self):
        self.change_reference("docker://alpine:latest")
        with self.assertRaisesRegex(ValueError, "immutable SHA"):
            contracts.validate(self.root)
        self.change_reference("docker://alpine@sha256:" + "a" * 64)
        contracts.validate(self.root)

    def test_generator_marker_is_required_without_manifest(self):
        path = self.root / ".github/workflows/quality-gate.yml"
        path.write_text(path.read_text().split("\n", 1)[1])
        with self.assertRaisesRegex(ValueError, "missing generator marker"):
            contracts.validate(self.root)

    def test_manifest_mismatch_remains_rejected(self):
        (self.root / ".github/action-pins.json").write_text(
            json.dumps([{"packageName": "actions/checkout", "digest": "b" * 40}])
        )
        with self.assertRaisesRegex(ValueError, "differs from generator pins"):
            contracts.validate(self.root)
