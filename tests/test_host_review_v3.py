from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge  # noqa: E402
from format_spec_validation import schema_support_errors, validate_instance  # noqa: E402
from host_review_contract import (  # noqa: E402
    HOST_REVIEW_CONTRACT_V3,
    contract_error_records,
    derived_requirement_indexes,
    validate_response,
)
from requirements_engine import build_llm_request  # noqa: E402
from semantic_contract import attach_request_provenance  # noqa: E402
from semantic_review_ledger import build_semantic_review_ledger  # noqa: E402


class HostReviewV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clauses = [{
            "id": "C1",
            "text": "正文使用宋体",
            "evidence_ids": ["E1"],
            "source_kind": "paragraph",
            "location": {},
            "part_index": 0,
        }]
        self.evidence = {
            "evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]
        }
        self.request = build_llm_request(
            [], self.clauses, self.evidence, {}, "full",
            contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        self.request = attach_request_provenance(
            self.request,
            source_sha256="a" * 64,
            evidence_doc=self.evidence,
            clauses=self.clauses,
            run_id="run-v3-test",
        )

    def _informational_response(self) -> dict:
        return {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "provenance": self.request["provenance"],
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "informational",
                "reason": "The current evidence was reviewed.",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def _executable_response(self) -> dict:
        return {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "provenance": self.request["provenance"],
            "requirements": [{
                "role": "body_text",
                "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
                "confidence": 0.9,
                "reason": "The clause specifies the body-text font.",
                "verification": {"mode": "word_render", "checks": ["Check the body-text font."]},
            }],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "executable",
                "reason": "The clause is executable in DOCX.",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def test_v3_schema_has_one_model_authoritative_relation(self) -> None:
        reviews_schema = self.request["response_schema"]["properties"]["clause_reviews"]["items"]
        self.assertNotIn("requirement_indexes", reviews_schema["properties"])
        self.assertNotIn("Maintain requirement_indexes exactly", str(self.request["instructions"]))
        # The schema itself, rather than prose, is the enforcement boundary.
        self.assertEqual(validate_response(self._informational_response(), self.request), [])

    def test_v3_derives_reverse_relation_and_rejects_model_duplicate(self) -> None:
        response = self._executable_response()
        self.assertEqual(derived_requirement_indexes(response, self.clauses), {"C1": [0]})
        self.assertEqual(validate_response(response, self.request), [])

        response["clause_reviews"][0]["requirement_indexes"] = [0]
        errors = validate_response(response, self.request)
        self.assertTrue(any("forbidden_in_contract_3.0" in error for error in errors))

    def test_retry_prompt_does_not_reintroduce_v2_relation_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            chunk_path = Path(td) / "chunk.json"
            chunk_path.write_text(json.dumps(self.request), encoding="utf-8")
            prompt = bridge._host_prompt(
                request_path=Path(td) / "request.json",
                chunk_path=chunk_path,
                response_path=Path(td) / "response.json",
                run_id="run-v3-test",
                chunk_index=1,
                chunk_count=1,
                attempt=2,
                retry_hint="requirement_index_not_backed_by_clause",
                retry_parent_response_sha256="b" * 64,
            )
        self.assertIn("Do not emit clause_reviews.requirement_indexes", prompt)
        self.assertNotIn("Maintain requirement_indexes exactly", prompt)
        self.assertNotIn("requirement_indexes: []", prompt)
        self.assertIn("rejected parent response sha256", prompt)

    def test_ledger_is_deterministic_and_does_not_infer_obligations(self) -> None:
        response = self._executable_response()
        ledger = build_semantic_review_ledger(response, self.clauses)
        self.assertEqual(ledger["relationship_policy"]["authoritative_edge"], "requirements[].clause_ids")
        self.assertEqual(ledger["edges"][0]["requirement_index"], 0)
        self.assertRegex(ledger["edges"][0]["requirement_id"], r"^R[0-9a-f]{16}$")
        self.assertEqual(ledger["requirements"][0]["requirement_id"], ledger["edges"][0]["requirement_id"])
        self.assertEqual(ledger["clauses"][0]["obligations"], [])
        self.assertEqual(ledger["clauses"][0]["obligation_decomposition"], "not_supplied")
        self.assertEqual(
            ledger["response_sha256"],
            build_semantic_review_ledger(response, self.clauses)["response_sha256"],
        )

    def test_ledger_requirement_ids_survive_response_reordering(self) -> None:
        first = self._executable_response()
        second = json.loads(json.dumps(first))
        second["requirements"].append({
            "role": "body_text",
            "properties": {"font": {"cjk": "SimHei", "size_pt": 11}},
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "confidence": 0.8,
            "reason": "A second independent supported property.",
            "verification": {"mode": "word_render", "checks": ["Check the second property."]},
        })
        reordered = json.loads(json.dumps(second))
        reordered["requirements"] = list(reversed(reordered["requirements"]))
        first_ledger = build_semantic_review_ledger(second, self.clauses)
        second_ledger = build_semantic_review_ledger(reordered, self.clauses)
        self.assertEqual(
            {item["requirement_id"] for item in first_ledger["requirements"]},
            {item["requirement_id"] for item in second_ledger["requirements"]},
        )
        self.assertEqual(
            {(item["clause_id"], item["requirement_id"]) for item in first_ledger["edges"]},
            {(item["clause_id"], item["requirement_id"]) for item in second_ledger["edges"]},
        )

    def test_compact_packet_exposes_only_bounded_runtime_context(self) -> None:
        chunk = json.loads(json.dumps(self.request))
        chunk["runtime_context"] = {
            "confirmed_thesis_profile": {
                "schema_version": "1.0",
                "profile_id": "profile-test",
                "degree_category": "master",
                "security_level": "public",
                "cover_metadata": {"degree_category": "master"},
                "source_sha256": "should-not-be-exposed",
            },
            "runtime_inventory": {
                "status": "complete",
                "declaration_anchor_status": "selected",
                "anchor_inventory": {"selected": "abstract_title_zh"},
                "source_sha256": "should-not-be-exposed",
            },
            "case_id": "BSU",
            "run_id": "trusted-run-id-must-not-be-exposed",
            "code_fingerprint_sha256": "trusted-hash-must-not-be-exposed",
        }
        packet = bridge.compact_model_packet(chunk)
        context = packet["runtime_context"]
        self.assertEqual(context["confirmed_thesis_profile"]["degree_category"], "master")
        self.assertEqual(context["runtime_inventory"]["status"], "complete")
        self.assertEqual(context["case_id"], "BSU")
        self.assertNotIn("source_sha256", json.dumps(context, ensure_ascii=False))
        self.assertNotIn("trusted-run-id", json.dumps(context, ensure_ascii=False))
        self.assertIn("trusted hashes and provenance omitted", context["policy"])

    def test_contract_errors_are_structured_without_repairing_response(self) -> None:
        response = self._executable_response()
        response["clause_reviews"][0]["normative_basis"] = "informational"
        errors = validate_response(response, self.request)
        records = contract_error_records(errors, response=response, chunk=self.request)
        self.assertTrue(any(record["code"] == "normative_basis_invalid" for record in records))
        self.assertEqual(response["clause_reviews"][0]["normative_basis"], "informational")
        self.assertEqual(len(next(record for record in records if record["code"] == "normative_basis_invalid")["response_sha256"]), 64)

    def test_equation_role_rejects_unsupported_numbering_without_normalizing(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["role"] = "equations"
        response["requirements"][0]["properties"] = {
            "numbering": {"format": "chapter.decimal", "style": "decimal", "depth": 2},
        }
        errors = validate_response(response, self.request)
        self.assertTrue(any("unknown property 'numbering'" in error for error in errors), errors)
        self.assertEqual(response["requirements"][0]["properties"]["numbering"]["depth"], 2)

    def test_unsupported_schema_keyword_is_not_silently_ignored(self) -> None:
        schema = {"type": "array", "contains": {"const": "required"}}
        self.assertTrue(any("unsupported_schema_keyword:contains" in error for error in schema_support_errors(schema)))
        self.assertTrue(any("unsupported_schema_keyword:contains" in error for error in validate_instance([], schema)))


if __name__ == "__main__":
    unittest.main()
