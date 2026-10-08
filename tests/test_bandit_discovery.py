"""Exercise real Bandit discovery with this repository's command and policy."""

import importlib.util
import json

# Fixed scanner argv against isolated fixtures, with no shell or external input.
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("bandit"), "run in the Bandit gate environment")
class BanditDiscoveryTests(unittest.TestCase):
    """The gate must cover CI helpers in both clones and Git worktrees."""

    def test_directory_exclusions_preserve_ci_and_source_prefixes(self):
        """Real findings in .github survive Git directory and Git file metadata."""
        repository = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repository / "scripts"))
        try:
            import run_bandit
        finally:
            sys.path.pop(0)
        for git_is_file in (False, True):
            with self.subTest(git_is_file=git_is_file), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in [
                    ".github/release-tests/probe.py",
                    "build_helpers.py",
                    "dist_helpers.py",
                    "build/ignored.py",
                    "dist/ignored.py",
                ]:
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("eval(input())\n", encoding="utf-8")
                if git_is_file:
                    (root / ".git").write_text("gitdir: unused-fixture\n", encoding="utf-8")
                else:
                    (root / ".git").mkdir()
                    (root / ".git" / "ignored.py").write_text("eval(input())\n", encoding="utf-8")
                (root / ".github" / "bandit.yml").write_bytes(
                    (repository / ".github" / "bandit.yml").read_bytes()
                )
                report_path = root / "report.json"
                # Use the production argv builder so CLI-default regressions fail this test.
                result = subprocess.run(  # nosec B603
                    run_bandit.bandit_command(report_path),
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                expected = {
                    "./.github/release-tests/probe.py",
                    "./build_helpers.py",
                    "./dist_helpers.py",
                }
                self.assertEqual(set(report["metrics"]) - {"_totals"}, expected)
                self.assertEqual({item["filename"] for item in report["results"]}, expected)
                self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
