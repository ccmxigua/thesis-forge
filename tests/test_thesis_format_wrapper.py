from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import thesis_format as wrapper  # noqa: E402


class ThesisFormatWrapperTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            requirements=Path("requirements.docx"),
            input=Path("thesis.tex"),
            output=Path("output.docx"),
            work_dir=Path("build/run"),
            style_template=None,
            thesis_profile=Path("profile.json"),
            template_profile=Path("template-profile.json"),
            render_report=Path("render-report.json"),
            host_review_chunk_size=20,
            require_submission_ready=True,
            strict_release=True,
        )

    def test_pipeline_command_forwards_template_profile_to_strict_pipeline(self) -> None:
        command = wrapper.pipeline_command(self._args())
        self.assertIn("--template-profile", command)
        self.assertEqual(
            command[command.index("--template-profile") + 1],
            "template-profile.json",
        )

    def test_portable_offline_draft_does_not_request_native_host(self) -> None:
        args = self._args()
        args.require_submission_ready = False
        args.strict_release = False
        command = wrapper.pipeline_command(
            args, llm_response=Path("review/response.json"),
            offline_merge_receipt=Path("review/merge-receipt.json"),
        )
        self.assertIn("--allow-offline-review", command)
        self.assertIn("--offline-merge-receipt", command)
        self.assertIn("--allow-existing-work", command)
        self.assertNotIn("--host-agent-audit", command)
        self.assertNotIn("--strict-release", command)

    def test_packet_preparation_does_not_select_native_adapter(self) -> None:
        with patch.object(wrapper, "run_stage", return_value=0) as run_stage:
            code = wrapper.main([
                "requirements.docx", "thesis.tex", "--work-dir", "build/run",
                "--prepare-agent-review",
            ])
        self.assertEqual(code, 0)
        command = run_stage.call_args.args[0]
        self.assertIn("--prepare-host-review", command)
        self.assertNotIn("--host-runtime", command)
        self.assertNotIn("--auto-host-agent", command)
        self.assertNotIn("--allow-existing-work", command)


if __name__ == "__main__":
    unittest.main()
