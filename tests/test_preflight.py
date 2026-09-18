from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight.py"


class PreflightTests(unittest.TestCase):
    def _manifest(self, directory: Path) -> Path:
        path = directory / "template-manifest.json"
        path.write_text(
            json.dumps({
                "schema_version": "1.0",
                "source_tex": str(ROOT / "tests" / "sample-thesis.tex"),
                "cases": [{
                    "id": "fixture",
                    "requirements": str(
                        ROOT / "template_profiles" / "cau-graduate-thesis-2025-winter"
                        / "resources" / "rules.pdf"
                    ),
                    "analysis_mode": "llm_primary",
                }],
            }),
            encoding="utf-8",
        )
        return path

    def test_preflight_validates_without_creating_a_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self._manifest(root)
            result = subprocess.run(
                [sys.executable, str(PREFLIGHT), "--template-manifest", str(manifest), "--json"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["read_only"])
            self.assertTrue(report["template_manifest"]["valid"])
            self.assertEqual(sorted(path.name for path in root.iterdir()), [manifest.name])

    def test_required_host_context_fails_closed_without_starting_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            for name in (
                "THESIS_FORGE_HOST_RUNTIME",
                "THESIS_FORGE_HOST_RUNTIME_VERSION",
                "THESIS_FORGE_HOST_INVOCATION_ID",
                "THESIS_FORGE_PARENT_SESSION_ID",
            ):
                env.pop(name, None)
            result = subprocess.run(
                [sys.executable, str(PREFLIGHT), "--require-host-runtime", "--json"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(any(item.startswith("host_runtime:") for item in report["errors"]))
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_preflight_wrapper_is_read_only_and_batch_start_has_a_distinct_name(self) -> None:
        wrapper = (ROOT / "run_all_preflights.sh").read_text(encoding="utf-8")
        fresh = (ROOT / "run_fresh_batch.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/preflight.py", wrapper)
        self.assertNotIn("batch_rerun_ten_schools.py", wrapper)
        self.assertIn("batch_rerun_ten_schools.py", fresh)


if __name__ == "__main__":
    unittest.main()
