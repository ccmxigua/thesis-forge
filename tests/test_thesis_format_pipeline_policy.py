from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from thesis_format_pipeline import enforce_obligation_review_output_policy  # noqa: E402


class ThesisFormatPipelinePolicyTests(unittest.TestCase):
    def test_irreducible_ambiguity_is_allowed_only_in_review_draft(self) -> None:
        results = [{"check_id": "C00076", "verdict": "manual_review_required"}]

        self.assertEqual(
            enforce_obligation_review_output_policy(results, output_policy="review_draft"),
            ["C00076"],
        )
        with self.assertRaisesRegex(ValueError, "C00076.*submission output is blocked"):
            enforce_obligation_review_output_policy(results, output_policy="submission")

    def test_no_manual_review_result_preserves_both_output_policies(self) -> None:
        self.assertEqual(
            enforce_obligation_review_output_policy(
                [{"check_id": "C00066", "verdict": "consistent"}],
                output_policy="submission",
            ),
            [],
        )

    def test_unknown_output_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported output policy"):
            enforce_obligation_review_output_policy([], output_policy="preview")


if __name__ == "__main__":
    unittest.main()
