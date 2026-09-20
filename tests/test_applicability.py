from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from applicability import evaluate_applicability  # noqa: E402


class ApplicabilityTests(unittest.TestCase):
    def test_missing_declaration_is_unconditionally_applicable(self) -> None:
        result = evaluate_applicability(None)
        self.assertEqual(result["status"], "always")
        self.assertEqual(result["result"], "true")

    def test_excluded_declaration_is_false_without_guessing(self) -> None:
        result = evaluate_applicability({"status": "excluded"})
        self.assertEqual(result["result"], "false")
        self.assertEqual(result["evaluated"], [])

    def test_missing_conditional_fact_is_unknown_not_false(self) -> None:
        result = evaluate_applicability({
            "status": "conditional",
            "conditions": [{
                "fact": "source_inventory.figures",
                "operator": "present",
            }],
        })
        self.assertEqual(result["result"], "unknown")
        self.assertEqual(result["reason"], "condition_0_fact_missing")

    def test_conditions_are_all_evaluated_deterministically(self) -> None:
        declaration = {
            "status": "conditional",
            "conditions": [
                {"fact": "thesis_profile.degree_level", "operator": "equals", "value": "doctor"},
                {"fact": "source_inventory.figures", "operator": "in", "value": [0, 2]},
            ],
        }
        result = evaluate_applicability(
            declaration,
            thesis_profile={"degree_level": "doctor"},
            source_inventory={"figures": 2},
        )
        self.assertEqual(result["result"], "true")
        self.assertEqual(len(result["evaluated"]), 2)
        self.assertEqual(result["evaluated"][1]["actual"], 2)

    def test_present_preserves_explicit_false_and_zero(self) -> None:
        false_result = evaluate_applicability(
            {
                "status": "conditional",
                "conditions": [{"fact": "thesis_profile.has_appendices", "operator": "present"}],
            },
            thesis_profile={"has_appendices": False},
        )
        zero_result = evaluate_applicability(
            {
                "status": "conditional",
                "conditions": [{"fact": "source_inventory.figures", "operator": "present"}],
            },
            source_inventory={"figures": 0},
        )
        self.assertEqual(false_result["result"], "true")
        self.assertEqual(zero_result["result"], "true")

    def test_registered_english_text_fact_is_evaluated_from_source_inventory(self) -> None:
        result = evaluate_applicability(
            {
                "status": "conditional",
                "conditions": [{
                    "fact": "source_inventory.english_text",
                    "operator": "present",
                    "value": None,
                }],
            },
            source_inventory={"english_text": True},
        )
        self.assertEqual(result["result"], "true")
        self.assertEqual(result["evaluated"][0]["actual"], True)

    def test_false_condition_is_recorded_with_observed_value(self) -> None:
        result = evaluate_applicability(
            {
                "status": "conditional",
                "conditions": [{
                    "fact": "runtime.host",
                    "operator": "equals",
                    "value": "codex",
                }],
            },
            runtime={"host": "openclaw"},
        )
        self.assertEqual(result["result"], "false")
        self.assertEqual(result["evaluated"][0]["actual"], "openclaw")

    def test_unknown_status_or_operator_fails_closed(self) -> None:
        status = evaluate_applicability({"status": "maybe"})
        operator = evaluate_applicability({
            "status": "conditional",
            "conditions": [{"fact": "runtime.host", "operator": "contains", "value": "codex"}],
        }, runtime={"host": "codex"})
        self.assertEqual(status["result"], "unknown")
        self.assertEqual(operator["result"], "unknown")


if __name__ == "__main__":
    unittest.main()
