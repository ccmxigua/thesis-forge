from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import batch_rerun_ten_schools as batch  # noqa: E402


class BatchAcceptanceTests(unittest.TestCase):
    def _result(self, root: Path, *, returncode: int = 0, render: bool = True) -> dict:
        work = root / "case" / "work"
        requirements = work / "requirements"
        apply_dir = work / "application"
        requirements.mkdir(parents=True)
        apply_dir.mkdir(parents=True)
        output = root / "case" / "generated.docx"
        output.write_bytes(b"current-docx")
        format_spec = requirements / "format-spec.json"
        format_spec.write_text("{}\n", encoding="utf-8")
        (requirements / "schema-validation.json").write_text(
            json.dumps({"valid": True, "errors": []}), encoding="utf-8"
        )
        capability = work / "capability-preflight.json"
        capability.write_text(json.dumps({"status": "passed", "findings": []}), encoding="utf-8")
        validation = apply_dir / "validation-report.json"
        validation.write_text(json.dumps({
            "valid": True,
            "format_ready": True,
            "serialized_docx_valid": True,
            "render_validation": {"rendered_verified": render},
            "submission_ready": render,
        }), encoding="utf-8")
        comparison = apply_dir / "format-comparison.json"
        comparison.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        manifest = work / "pipeline-manifest.json"
        manifest.write_text(json.dumps({
            "status": "completed",
            "compliance_mode": "full",
            "output": str(output),
            "format_spec": str(format_spec),
            "capability_preflight": str(capability),
            "capability_preflight_status": "passed",
            "validation_report": str(validation),
            "format_comparison": str(comparison),
            "submission_ready": render,
        }), encoding="utf-8")
        return {
            "returncode": returncode,
            "fresh_run": {
                "run_id": "fresh-run",
                "cache_reused": False,
                "pipeline_manifest": str(manifest),
            },
        }

    def test_returncode_three_and_blocked_manifest_are_not_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._result(root, returncode=3)
            manifest = Path(result["fresh_run"]["pipeline_manifest"])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["status"] = "blocked"
            payload["blocking_reasons"] = ["open_questions"]
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            accepted = batch.case_acceptance(result)
            self.assertFalse(accepted["accepted"])
            self.assertIn("pipeline_returncode:3", accepted["blockers"])
            self.assertIn("pipeline_status:blocked", accepted["blockers"])

    def test_missing_trusted_render_is_not_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            accepted = batch.case_acceptance(self._result(Path(td), render=False))
            self.assertFalse(accepted["accepted"])
            self.assertIn("trusted_render_not_verified", accepted["blockers"])

    def test_current_artifacts_can_pass_only_as_one_bound_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            accepted = batch.case_acceptance(self._result(Path(td)))
            self.assertTrue(accepted["accepted"], accepted)
            self.assertEqual(accepted["status"], "accepted")

    def test_host_binding_failure_is_global_batch_stop(self) -> None:
        result = {
            "returncode": 2,
            "stages": {
                "host_agent": {
                    "stderr_tail": "host runtime mismatch: expected codex, observed openclaw",
                },
            },
        }
        self.assertEqual(batch.global_fatal_reason(result), "host runtime mismatch")

    def test_manifest_boundary_and_canonical_selection(self) -> None:
        if not batch.DEFAULT_MANIFEST.is_file():
            self.skipTest("external ten-school manifest is not included in this source checkout")
        _source, cases, _manifest = batch.load_manifest(batch.DEFAULT_MANIFEST)
        by_id = {case["id"]: case for case in cases}
        self.assertIsNone(by_id["ustb"]["style_template"])
        self.assertEqual(by_id["ustb"]["template_boundary"], "requirements_only")
        selected = batch.select_cases(cases, ["ustb", "bsu"])
        self.assertEqual([case["id"] for case in selected], ["bsu", "ustb"])
        command = batch.pipeline_command(
            by_id["ustb"], Path("source.tex"), Path("work"), Path("output.docx"),
            compliance_mode="full", prepare_host_review=True,
        )
        self.assertNotIn("--style-template", command)


if __name__ == "__main__":
    unittest.main()
