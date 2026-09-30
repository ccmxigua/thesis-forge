from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import attach_request_provenance


class RepeatedLiteralOccurrenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clauses = []
        self.evidence = {"evidence": []}
        for cid, eid, text, child, order in (
            ("C301", "E301", "学  位  论  文", 3, 2),
            ("C902", "E902", "学 位 论 文", 17, 11),
        ):
            location = {"part": "document", "child_index": child, "order": order}
            self.evidence["evidence"].append({
                "id": eid, "text": text, "kind": "paragraph", "location": location,
            })
            self.clauses.append({
                "id": cid, "text": "学 位 论 文", "evidence_ids": [eid],
                "source_kind": "paragraph", "location": location, "part_index": 0,
                "source_evidence_text": text,
                "source_span": {
                    "evidence_id": eid, "text": text, "start_offset": 0,
                    "end_offset": len(text), "location": location,
                    "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            })
        request = engine.build_llm_request(
            [], self.clauses, self.evidence, {}, "full", contract_version="3.0",
            runtime_context={"code_fingerprint_sha256": "f" * 64},
        )
        request["case_id"] = "standalone"
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=self.evidence,
            clauses=self.clauses, run_id="run-occurrences-test",
        )
        engine.prepare_host_agent_review_packets(
            request, self.clauses, self.evidence, "a" * 64, self.directory, chunk_size=10,
        )
        self.chunk = json.loads((self.directory / "llm-request-chunks.json").read_text())[0]
        manifest = json.loads((self.directory / "host-agent-review-manifest.json").read_text())
        engine.validate_host_review_chunk_source_projection(request, [self.chunk], manifest)
        self.response = {
            "contract_version": "3.0", "provenance": copy.deepcopy(self.chunk["provenance"]),
            "requirements": [{
                "role": "thesis_type_zh", "properties": {"text": "学 位 论 文"},
                "field_key": "thesis_type", "clause_ids": ["C301", "C902"],
                "evidence_ids": ["E301", "E902"],
                "source_fragment_clause_ids": ["C301", "C902"],
                "confidence": 0.98, "reason": "Two independently cited fixed title occurrences.",
                "verification": {"mode": "static_docx", "checks": ["Check each literal."]},
            }],
            "clause_reviews": [{
                "clause_id": c["id"], "classification": "executable",
                "normative_basis": "fixed_statement", "reason": "Fixed source text.",
                "obligations": [{"id": "literal", "status": "covered", "reason": "Retain this occurrence."}],
            } for c in self.clauses],
            "unsupported_items": [], "reported_conflicts": [],
        }

    def prepare(self, response=None, chunk=None):
        chunk = self.chunk if chunk is None else chunk
        return bridge.prepare_native_response_candidate(
            self.response if response is None else response, chunk,
            source_projection_validation_sha256=bridge._response_sha256(chunk),
        )

    def test_separate_occurrences_preserve_raw_reviews_edges_and_exact_text(self) -> None:
        for selector in (["C301", "C902"], ["C301"]):
            with self.subTest(selector=selector):
                raw = copy.deepcopy(self.response)
                raw["requirements"][0]["source_fragment_clause_ids"] = selector
                before = copy.deepcopy(raw)
                candidate, audit = self.prepare(raw)
                self.assertEqual(raw, before)
                self.assertEqual(candidate["clause_reviews"], raw["clause_reviews"])
                self.assertEqual([r["clause_ids"] for r in candidate["requirements"]], [["C301"], ["C902"]])
                self.assertEqual([r["evidence_ids"] for r in candidate["requirements"]], [["E301"], ["E902"]])
                self.assertEqual([r["properties"]["text"] for r in candidate["requirements"]], ["学  位  论  文", "学 位 论 文"])
                self.assertEqual(bridge.validate_host_agent_response(candidate, self.chunk), [])
                receipt = audit["source_literal_occurrence_projections"][0]
                self.assertEqual(receipt["input_requirement"], raw["requirements"][0])
                self.assertEqual([f["location"]["child_index"] for f in receipt["source_fragments"]], [3, 17])
                self.assertNotEqual(receipt["response_before_sha256"], receipt["response_after_sha256"])

    def test_projection_is_idempotent_and_merge_keeps_two_content_instances(self) -> None:
        candidate, _ = self.prepare()
        repeated, audit = self.prepare(candidate)
        self.assertEqual(repeated, candidate)
        self.assertEqual(audit["source_literal_occurrence_projections"], [])
        spec, conflicts, _ = engine.merge_llm_primary(
            Path("source.docx"), {"schema_version": "1.0", "roles": {}, "page": {}, "requirements": []},
            self.clauses, candidate, {"E301", "E902"},
            expected_provenance=self.chunk["provenance"], require_provenance=True,
        )
        self.assertEqual(conflicts, [])
        instances = spec["content_instances"]
        self.assertEqual(len(instances), 2)
        self.assertNotEqual(instances[0]["id"], instances[1]["id"])
        self.assertEqual([i["source_fragments"][0]["location"]["child_index"] for i in instances], [3, 17])

    def test_same_wording_at_different_locations_stays_separate(self) -> None:
        chunk = copy.deepcopy(self.chunk)
        source = chunk["evidence_context"]["E301"]["text"]
        clause = chunk["clauses"][1]
        clause["source_evidence_text"] = source
        clause["source_span"].update(text=source, end_offset=len(source), source_sha256=hashlib.sha256(source.encode()).hexdigest())
        chunk["evidence_context"]["E902"]["text"] = source
        candidate, _ = self.prepare(chunk=chunk)
        self.assertEqual(len(candidate["requirements"]), 2)
        self.assertEqual(candidate["requirements"][0]["properties"]["text"], candidate["requirements"][1]["properties"]["text"])
        self.assertNotEqual(candidate["requirements"][0]["evidence_ids"], candidate["requirements"][1]["evidence_ids"])

    def test_conditional_styled_existing_and_pending_relations_are_not_partitioned(self) -> None:
        cases = []
        for mutation in (
            {"properties": {"text": "学 位 论 文", "font": {"size_pt": 22}}},
            {"applicability": {"status": "conditional", "conditions": ["academic"]}},
            {"existing_requirement_id": "R301"},
            {"evidence_ids": ["E301", "E902", "E-FOREIGN"]},
            {"properties": {"text": "学 位 论 文（学术学位）"}},
        ):
            raw = copy.deepcopy(self.response)
            raw["requirements"][0].update(mutation)
            cases.append(raw)
        raw = copy.deepcopy(self.response)
        raw["clause_reviews"][1]["classification"] = "unresolved"
        cases.append(raw)
        raw = copy.deepcopy(self.response)
        raw["clause_reviews"][1]["obligations"] = []
        cases.append(raw)
        raw = copy.deepcopy(self.response)
        raw["reported_conflicts"] = [{"clause_ids": ["C301", "C902"]}]
        cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw):
                candidate, receipts = bridge._project_repeated_literal_occurrences(
                    raw, self.chunk, source_projection_validation_sha256=bridge._response_sha256(self.chunk),
                )
                self.assertEqual(candidate, raw)
                self.assertEqual(receipts, [])

    def test_stale_hash_wrong_location_adjacent_unknown_and_duplicate_sources_fail_closed(self) -> None:
        for mutation in ("hash", "location", "adjacent", "unknown", "duplicate", "evidence"):
            with self.subTest(mutation=mutation):
                chunk, raw = copy.deepcopy(self.chunk), copy.deepcopy(self.response)
                if mutation == "hash":
                    chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
                elif mutation == "location":
                    chunk["clauses"][0]["source_span"]["location"]["child_index"] = 99
                elif mutation == "adjacent":
                    for location in (chunk["clauses"][1]["location"], chunk["clauses"][1]["source_span"]["location"], chunk["evidence_context"]["E902"]["location"]):
                        location.update(child_index=4, order=3)
                elif mutation == "unknown":
                    raw["requirements"][0]["source_fragment_clause_ids"] = ["C404"]
                elif mutation == "duplicate":
                    raw["requirements"][0]["clause_ids"] = ["C301", "C301"]
                else:
                    chunk["evidence_context"]["E301"]["id"] = "E-FOREIGN"
                candidate, receipts = bridge._project_repeated_literal_occurrences(
                    raw, chunk, source_projection_validation_sha256=bridge._response_sha256(chunk),
                )
                self.assertEqual(candidate, raw)
                self.assertEqual(receipts, [])

    def test_unverified_chunk_and_legacy_responses_are_not_partitioned(self) -> None:
        for digest in (None, "0" * 64):
            candidate, audit = bridge._project_repeated_literal_occurrences(
                self.response, self.chunk, source_projection_validation_sha256=digest,
            )
            self.assertEqual(candidate, self.response)
            self.assertEqual(audit, [])
        legacy = copy.deepcopy(self.response)
        legacy["contract_version"] = "2.1"
        candidate, audit = bridge._project_repeated_literal_occurrences(
            legacy, self.chunk, source_projection_validation_sha256=bridge._response_sha256(self.chunk),
        )
        self.assertEqual(candidate, legacy)
        self.assertEqual(audit, [])

    def test_explicit_single_occurrence_whitespace_uses_only_selected_source(self) -> None:
        raw = copy.deepcopy(self.response)
        raw["requirements"] = []
        for clause in self.clauses:
            requirement = copy.deepcopy(self.response["requirements"][0])
            requirement.update(clause_ids=[clause["id"]], evidence_ids=clause["evidence_ids"], source_fragment_clause_ids=[clause["id"]])
            raw["requirements"].append(requirement)
        candidate, audit = self.prepare(raw)
        self.assertEqual([r["properties"]["text"] for r in candidate["requirements"]], ["学  位  论  文", "学 位 论 文"])
        self.assertEqual(audit["source_literal_occurrence_projections"], [])
        self.assertEqual(len(audit["source_literal_whitespace_projections"]), 1)
        self.assertEqual(audit["source_literal_whitespace_projections"][0]["source_fragments"][0]["clause_id"], "C301")

    def test_single_selector_never_repairs_changed_punctuation_or_degree_qualifier(self) -> None:
        for text in ("学 位 论 文：", "学 位 论 文（学术学位）", "学 位 论 文和"):
            with self.subTest(text=text):
                raw = copy.deepcopy(self.response)
                raw["requirements"][0].update(clause_ids=["C301"], evidence_ids=["E301"], source_fragment_clause_ids=["C301"])
                raw["requirements"][0]["properties"]["text"] = text
                raw["clause_reviews"] = raw["clause_reviews"][:1]
                chunk = copy.deepcopy(self.chunk)
                chunk["clauses"] = chunk["clauses"][:1]
                with self.assertRaises(ValueError):
                    self.prepare(raw, chunk)

    def test_unknown_semantic_fields_and_object_refs_cannot_bypass_full_schema(self) -> None:
        for mutation in ("style", "condition", "obligation_ref"):
            with self.subTest(mutation=mutation):
                raw = copy.deepcopy(self.response)
                if mutation == "obligation_ref":
                    raw["clause_reviews"][0]["obligations"][0]["requirement_index"] = 0
                else:
                    raw["requirements"][0][mutation] = "source-only extra value"
                with self.assertRaises(ValueError):
                    self.prepare(raw)

    def test_rejected_candidate_still_retains_occurrence_projection_receipt(self) -> None:
        raw = copy.deepcopy(self.response)
        # An independent enum error may not erase evidence of the title split.
        raw["clause_reviews"][0]["confidence"] = 2
        with self.assertRaises(ValueError) as caught:
            self.prepare(raw)
        receipt = caught.exception.source_literal_occurrence_projections[0]
        self.assertEqual(receipt["input_requirement"], raw["requirements"][0])
        self.assertEqual(len(receipt["source_fragments"]), 2)
        self.assertEqual(receipt["input_fingerprints"]["run_id"], self.chunk["provenance"]["run_id"])
        parent = caught.exception.repair_base_candidate
        self.assertEqual(len(parent["requirements"]), 2)
        self.assertTrue(caught.exception.retry_authorizing_error_records)
        self.assertTrue(all(
            record["response_sha256"] == bridge._response_sha256(parent)
            for record in caught.exception.retry_authorizing_error_records
        ))


if __name__ == "__main__":
    unittest.main()
