from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from thesis_format_pipeline import (  # noqa: E402
    _scope_unresolved_release_gates,
    _source_content_pending_release_gates,
    enforce_obligation_review_output_policy,
)
from manual_review import build_manual_review_ledger  # noqa: E402
from semantic_contract import sha256_json  # noqa: E402


class ThesisFormatPipelinePolicyTests(unittest.TestCase):
    @staticmethod
    def _canonical_scope_source() -> tuple[list[dict], dict, dict]:
        quote = "at least 3 groups"
        raw_source = f"prefix {quote} suffix"
        start = len("prefix ")
        source_sha256 = hashlib.sha256(raw_source.encode("utf-8")).hexdigest()
        clause = {
            "id": "C1", "text": quote, "evidence_ids": ["E1"],
            "source_span": {
                "evidence_id": "E1", "start_offset": start,
                "end_offset": start + len(quote), "text": quote,
                "source_sha256": source_sha256,
            },
        }
        location = {
            "evidence_id": "E1", "start_offset": start,
            "end_offset": start + len(quote), "source_sha256": source_sha256,
        }
        item_base = {
            "clause_id": "C1", "source_quote": quote,
            "source_start": 0, "source_end": len(quote),
            "source_text_sha256": sha256_json(quote),
            "source_location": location,
            "evidence_ids": ["E1"], "execution_authorized": False,
        }
        return [clause], {"evidence": [{"id": "E1", "text": raw_source}]}, item_base

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

    def test_genuine_author_content_pending_is_draft_only(self) -> None:
        results = [{"check_id": "C00102", "verdict": "source_content_pending"}]
        self.assertEqual(
            enforce_obligation_review_output_policy(results, output_policy="review_draft"),
            [],
        )
        with self.assertRaisesRegex(ValueError, "C00102.*submission output is blocked"):
            enforce_obligation_review_output_policy(results, output_policy="submission")

    def test_source_content_pending_generates_an_evidence_bound_author_input_gate(self) -> None:
        quote = "这些内容是示例，请作者自行撰写真实内容。"
        source_sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        gates = _source_content_pending_release_gates([{
            "source_content_pending_items": [{
                "clause_id": "C00102",
                "source_quotes": [quote],
                "evidence_ids": ["E00009"],
                "reason": "原文明确要求作者补写真实研究内容。",
            }],
        }], clauses=[{
            "id": "C00102", "text": quote, "evidence_ids": ["E00009"],
            "source_span": {
                "evidence_id": "E00009", "start_offset": 0,
                "end_offset": len(quote), "text": quote,
                "source_sha256": source_sha,
            },
        }], evidence_doc={"evidence": [{"id": "E00009", "text": quote}]})
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["category"], "input_prerequisite")
        self.assertEqual(gates[0]["clause_ids"], ["C00102"])
        self.assertEqual(gates[0]["evidence_ids"], ["E00009"])
        self.assertEqual(gates[0]["source_location"], {
            "evidence_id": "E00009", "start_offset": 0,
            "end_offset": len(quote), "source_sha256": source_sha,
        })
        self.assertIn("本人真实研究内容", gates[0]["action"])
        self.assertIn("【待补写真实论文内容：C00102】", gates[0]["placeholder_text"])
        self.assertNotIn("generated content", gates[0]["action"])

    def test_source_content_pending_rejects_missing_evidence_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires non-empty evidence IDs"):
            _source_content_pending_release_gates([{
                "source_content_pending_items": [{
                    "clause_id": "C00102",
                    "source_quotes": ["这些示例内容请作者自行撰写真实内容。"],
                    "evidence_ids": [],
                    "reason": "待作者提供真实内容。",
                }],
            }], clauses=[], evidence_doc={"evidence": []})

    def test_source_content_pending_rejects_unbound_quotes_ids_hash_ranges_and_duplicates(self) -> None:
        quote = "请作者撰写真实研究内容"
        raw_source = f"前文：{quote}。后文"
        start = raw_source.index(quote)
        source_sha = hashlib.sha256(raw_source.encode("utf-8")).hexdigest()
        clause = {
            "id": "C00102", "text": quote, "evidence_ids": ["E00009"],
            "source_span": {
                "evidence_id": "E00009", "start_offset": start,
                "end_offset": start + len(quote), "text": quote,
                "source_sha256": source_sha,
            },
        }
        evidence_doc = {"evidence": [{"id": "E00009", "text": raw_source}]}
        item = {
            "clause_id": "C00102", "source_quotes": [quote],
            "evidence_ids": ["E00009"], "reason": "待作者提供真实内容。",
        }
        mutations = [
            ({**item, "source_quotes": ["伪造的作者指令"]}, [clause], evidence_doc),
            ({**item, "evidence_ids": ["E99999"]}, [clause], evidence_doc),
            (item, [{**clause, "id": "C99999"}], evidence_doc),
            (item, [{**clause, "source_span": {**clause["source_span"], "start_offset": 999}}], evidence_doc),
            (item, [{**clause, "source_span": {**clause["source_span"], "source_sha256": "a" * 64}}], evidence_doc),
        ]
        for changed_item, changed_clauses, changed_evidence in mutations:
            with self.subTest(item=changed_item, clauses=changed_clauses):
                with self.assertRaisesRegex(ValueError, "not bound to current source"):
                    _source_content_pending_release_gates(
                        [{"source_content_pending_items": [changed_item]}],
                        clauses=changed_clauses, evidence_doc=changed_evidence,
                    )
        with self.assertRaisesRegex(ValueError, "duplicate source-content pending clause"):
            _source_content_pending_release_gates(
                [{"source_content_pending_items": [item, item]}],
                clauses=[clause], evidence_doc=evidence_doc,
            )

    def test_source_content_pending_rejects_malformed_review_container(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an array"):
            _source_content_pending_release_gates(  # type: ignore[arg-type]
                {}, clauses=[], evidence_doc={"evidence": []},
            )

    def test_each_scope_unresolved_obligation_gets_a_distinct_non_executable_marker(self) -> None:
        clauses, evidence_doc, item_base = self._canonical_scope_source()
        review = {
            "scope_unresolved_items": [
                {
                    "analysis_obligation_id": "AO-" + "1" * 24,
                    "clause_id": "C1", "source_ref": "Q-source-1",
                    **item_base,
                    "obligation_summary": "The lower bound uses groups.",
                    "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
                    "scope_dependency_dimensions": ["metric"],
                },
                {
                    "analysis_obligation_id": "AO-" + "2" * 24,
                    "clause_id": "C1", "source_ref": "Q-source-2",
                    **item_base,
                    "obligation_summary": "A separate obligation has a different unresolved target.",
                    "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
                    "scope_dependency_dimensions": ["target"],
                },
            ],
        }
        gates = _scope_unresolved_release_gates(
            [review], clauses=clauses, evidence_doc=evidence_doc,
        )
        self.assertEqual(len(gates), 2)
        self.assertNotEqual(gates[0]["analysis_obligation_id"], gates[1]["analysis_obligation_id"])
        self.assertTrue(all(not gate["execution_authorized"] for gate in gates))

        ledger = build_manual_review_ledger({}, [], release_gates=gates)
        self.assertEqual(len(ledger["items"]), 2)
        self.assertFalse(ledger["submission_ready"])
        self.assertEqual(
            {item["analysis_obligation_id"] for item in ledger["items"]},
            {"AO-" + "1" * 24, "AO-" + "2" * 24},
        )
        self.assertTrue(all(
            item["marker_required"] and "待确认适用范围" in item["placeholder_text"]
            for item in ledger["items"]
        ))

    def test_scope_unresolved_release_gate_rejects_authorized_or_duplicate_records(self) -> None:
        clauses, evidence_doc, item_base = self._canonical_scope_source()
        item = {
            "analysis_obligation_id": "AO-" + "1" * 24,
            "clause_id": "C1", "source_ref": "Q-source-1",
            **item_base,
            "obligation_summary": "scope unclear",
            "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
            "scope_dependency_dimensions": ["metric"],
            "execution_authorized": True,
        }
        with self.assertRaisesRegex(ValueError, "malformed.*executable"):
            _scope_unresolved_release_gates(
                [{"scope_unresolved_items": [item]}], clauses=clauses,
                evidence_doc=evidence_doc,
            )
        item["execution_authorized"] = False
        with self.assertRaisesRegex(ValueError, "malformed.*executable"):
            _scope_unresolved_release_gates(
                [{"scope_unresolved_items": [item, item]}], clauses=clauses,
                evidence_doc=evidence_doc,
            )

    def test_scope_unresolved_gate_rejects_unbound_quote_range_hash_or_location(self) -> None:
        clauses, evidence_doc, item_base = self._canonical_scope_source()
        base = {
            "analysis_obligation_id": "AO-" + "1" * 24,
            "clause_id": "C1", "source_ref": "Q-source-1", **item_base,
            "obligation_summary": "scope unclear",
            "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
            "scope_dependency_dimensions": ["metric"],
        }
        mutations = [
            {"source_start": 999, "source_end": 1000},
            {"source_quote": "forged quote"},
            {"source_text_sha256": "a" * 64},
            {"source_location": None},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                item = {**base, **mutation}
                with self.assertRaisesRegex(ValueError, "unbound to exact source"):
                    _scope_unresolved_release_gates(
                        [{"scope_unresolved_items": [item]}], clauses=clauses,
                        evidence_doc=evidence_doc,
                    )

    def test_unknown_output_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported output policy"):
            enforce_obligation_review_output_policy([], output_policy="preview")


if __name__ == "__main__":
    unittest.main()
