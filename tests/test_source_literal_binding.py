from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_literal_binding import (  # noqa: E402
    SourceFragmentBindingError,
    compose_source_fragments,
    materialize_source_fragment_literals,
)


def _source_clause(
    clause_id: str, evidence_id: str, source: str, text: str,
    start: int, end: int, *, kind: str, location: dict,
) -> dict:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return {
        "id": clause_id,
        "text": text,
        "evidence_ids": [evidence_id],
        "source_kind": kind,
        "location": copy.deepcopy(location),
        "source_evidence_text": source,
        "source_span": {
            "evidence_id": evidence_id,
            "start_offset": start,
            "end_offset": end,
            "text": source[start:end],
            "source_sha256": digest,
            "location": copy.deepcopy(location),
        },
    }


def _adjacent_paragraphs() -> tuple[list[dict], dict[str, dict]]:
    title = "硕 士 学 位 论 文"
    degree = "（学术学位）"
    left_location = {"part": "document", "child_index": 23, "order": 19}
    right_location = {"part": "document", "child_index": 24, "order": 20}
    clauses = [
        _source_clause("C19", "E20", title, title, 0, len(title),
                       kind="paragraph", location=left_location),
        _source_clause("C20", "E21", degree, degree, 0, len(degree),
                       kind="paragraph", location=right_location),
    ]
    evidence = {
        "E20": {"id": "E20", "kind": "paragraph", "text": title,
                "location": left_location},
        "E21": {"id": "E21", "kind": "paragraph", "text": degree,
                "location": right_location},
    }
    return clauses, evidence


class SourceLiteralBindingTests(unittest.TestCase):
    def test_adjacent_paragraphs_materialize_with_explicit_paragraph_boundary(self) -> None:
        clauses, evidence = _adjacent_paragraphs()
        binding = compose_source_fragments(
            ["C19", "C20"], {item["id"]: item for item in clauses}, evidence,
            requirement_clause_ids=["C19", "C20"],
            requirement_evidence_ids=["E20", "E21"],
        )
        self.assertEqual(binding["text"], "硕 士 学 位 论 文\n（学术学位）")
        self.assertEqual(
            [item["separator_before"] for item in binding["source_fragments"]],
            ["", "\n"],
        )
        self.assertEqual(
            [item["clause_id"] for item in binding["source_fragments"]],
            ["C19", "C20"],
        )

    def test_cross_evidence_fragments_require_source_order_and_adjacency(self) -> None:
        clauses, evidence = _adjacent_paragraphs()
        clause_map = {item["id"]: item for item in clauses}
        with self.assertRaisesRegex(SourceFragmentBindingError, "fragments_not_in_source_order"):
            compose_source_fragments(["C20", "C19"], clause_map, evidence)

        clause_map["C20"]["source_span"]["location"]["child_index"] = 27
        evidence["E21"]["location"]["child_index"] = 27
        clause_map["C20"]["location"]["child_index"] = 27
        with self.assertRaisesRegex(SourceFragmentBindingError, "cross_evidence_boundary_unproven"):
            compose_source_fragments(["C19", "C20"], clause_map, evidence)

    def test_lexical_conjunction_is_not_discarded_when_checking_clause_text(self) -> None:
        clauses, evidence = _adjacent_paragraphs()
        clauses[0]["text"] = "硕 士 学 位 论 文和"
        with self.assertRaisesRegex(SourceFragmentBindingError, "clause_text_mismatch:C19"):
            compose_source_fragments(["C19"], {item["id"]: item for item in clauses}, evidence)

    def test_same_evidence_only_preserves_punctuation_or_whitespace_gap(self) -> None:
        source = "标题；限定语"
        location = {"part": "document", "child_index": 8, "order": 5}
        clauses = [
            _source_clause("C1", "E1", source, "标题", 0, 2,
                           kind="paragraph", location=location),
            _source_clause("C2", "E1", source, "限定语", 3, 6,
                           kind="paragraph", location=location),
        ]
        evidence = {"E1": {"id": "E1", "kind": "paragraph", "text": source,
                           "location": location}}
        binding = compose_source_fragments(
            ["C1", "C2"], {item["id"]: item for item in clauses}, evidence,
        )
        self.assertEqual(binding["text"], source)
        self.assertEqual(binding["source_fragments"][1]["separator_before"], "；")

        conflicting_source = "标题；其它内容；限定语"
        conflicting_hash = hashlib.sha256(conflicting_source.encode()).hexdigest()
        conflicting_clauses = [
            _source_clause("C1", "E1", conflicting_source, "标题", 0, 2,
                           kind="paragraph", location=location),
            _source_clause("C2", "E1", conflicting_source, "限定语", 8, 11,
                           kind="paragraph", location=location),
        ]
        conflicting_evidence = {
            "E1": {"id": "E1", "kind": "paragraph", "text": conflicting_source,
                   "location": location, "source_sha256": conflicting_hash},
        }
        with self.assertRaisesRegex(SourceFragmentBindingError, "unselected_source_text_between_fragments"):
            compose_source_fragments(
                ["C1", "C2"],
                {item["id"]: item for item in conflicting_clauses},
                conflicting_evidence,
            )

    def test_wrong_hash_unknown_or_uncited_fragment_is_rejected(self) -> None:
        clauses, evidence = _adjacent_paragraphs()
        clause_map = {item["id"]: item for item in clauses}
        with self.assertRaisesRegex(SourceFragmentBindingError, "unknown_clause:C404"):
            compose_source_fragments(["C404"], clause_map, evidence)
        with self.assertRaisesRegex(SourceFragmentBindingError, "primary_evidence_not_cited:C20"):
            compose_source_fragments(
                ["C19", "C20"], clause_map, evidence,
                requirement_clause_ids=["C19", "C20"],
                requirement_evidence_ids=["E20"],
            )
        clause_map["C20"]["source_span"]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(SourceFragmentBindingError, "source_hash_mismatch:C20"):
            compose_source_fragments(["C19", "C20"], clause_map, evidence)

    def test_materializer_owns_text_and_rejects_model_text_changes(self) -> None:
        clauses, evidence = _adjacent_paragraphs()
        response = {"requirements": [{
            "role": "thesis_type_zh",
            "properties": {"text": None},
            "clause_ids": ["C19", "C20"],
            "evidence_ids": ["E20", "E21"],
            "source_fragment_clause_ids": ["C19", "C20"],
        }]}
        output, audits, errors = materialize_source_fragment_literals(
            response, clauses, evidence,
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            output["requirements"][0]["properties"]["text"],
            "硕 士 学 位 论 文\n（学术学位）",
        )
        self.assertEqual(audits[0]["action"], "materialized_from_current_bound_source_fragments")
        self.assertIsNone(response["requirements"][0]["properties"]["text"])

        response["requirements"][0]["properties"]["text"] = "硕 士 学 位 论 文"
        unchanged, _, errors = materialize_source_fragment_literals(response, clauses, evidence)
        self.assertTrue(any("source_fragment_literal_conflict" in error for error in errors))
        self.assertEqual(unchanged["requirements"][0]["properties"]["text"], "硕 士 学 位 论 文")


if __name__ == "__main__":
    unittest.main()
