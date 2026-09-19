from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
