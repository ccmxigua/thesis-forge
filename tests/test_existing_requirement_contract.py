"""Regression for BSU chunk 0015, run cd06ae1d-b4a0-44c4-9c4f-03df8ac30c64.

The five ID/role/clause/evidence/text tuples below are reduced from the actual
failure, not model-generated semantic truth. Original attempt-01.raw.json
SHA256: 9a0340aabf7fd455fa3550fb469deee68b75db25029cdd7334bf2ed08bc485b0.
Production code must never special-case these IDs, this school or this run.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from existing_requirement_contract import existing_reference_errors, project_authoritative_existing_payloads
from host_review_contract import validate_response, contract_error_records
from host_review_schema import native_output_schema, native_schema_support_errors, normalize_native_response
from host_agent_bridge import _structured_contract_repair_guidance
from requirements_engine import build_llm_request, merge_llm_primary, _build_host_review_chunks
from semantic_contract import attach_request_provenance


class ExistingRequirementContractTests(unittest.TestCase):
    def setUp(self):
        self.clauses = [{"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"]}]
        self.evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体"}]}
        self.baseline = {"schema_version": "1.0", "roles": {}, "page": {}, "requirements": [{
            "id": "R00012", "role": "body_text", "properties": {"font": {"cjk": "SimSun"}},
            "clause_ids": ["C1"], "evidence_ids": ["E1"], "source_text": "正文使用宋体",
            "resolved_by": "rule", "confidence": 1,
        }]}
        self.response = {"contract_version": "3.0", "requirements": [{
            "existing_requirement_id": "R00012", "role": "body_text",
            "properties": {"font": {"cjk": "SimSun"}}, "clause_ids": ["C1"],
            "evidence_ids": ["E1"], "reason": "当前证据指定正文宋体。", "confidence": 1,
        }], "clause_reviews": [{"clause_id": "C1", "classification": "verify_existing",
                                "reason": "当前证据与确定性条款一致。"}],
            "unsupported_items": [], "reported_conflicts": []}

    def request(self, baseline=None):
        return build_llm_request([], self.clauses, self.evidence,
                                 self.baseline if baseline is None else baseline,
                                 "full", contract_version="3.0")

    def merge(self, response):
        return merge_llm_primary(Path("synthetic-source"), self.baseline, self.clauses,
                                 response, {e["id"] for e in self.evidence["evidence"]})

    def test_valid_reference_and_canonical_payload_agree_at_both_gates(self):
        self.response["requirements"][0]["properties"]["font"]["size_pt"] = 12
        before = copy.deepcopy(self.response)
        self.assertEqual(validate_response(self.response, self.request()), [])
        spec, conflicts, audit = self.merge(self.response)
        self.assertFalse([x for x in conflicts if x.get("type") == "llm_contract"], conflicts)
        self.assertEqual(spec["requirements"][0]["id"], "R00012")
        self.assertEqual(spec["requirements"][0]["properties"], self.baseline["requirements"][0]["properties"])
        self.assertTrue(any(x.get("type") == "existing_requirement_payload_projection" for x in audit))
        self.assertEqual(self.response, before)

    def test_bad_identity_rejected_early_and_late_without_repair(self):
        for field, value, code in [
            ("existing_requirement_id", "R99999", "unknown_existing_requirement_id"),
            ("existing_requirement_id", ["R00012"], "invalid_existing_requirement_id"),
            ("role", "heading_1", "existing_requirement_payload_mismatch"),
            ("clause_ids", ["C2"], "existing_requirement_clause_mismatch"),
            ("evidence_ids", ["E2"], "existing_requirement_evidence_mismatch"),
        ]:
            with self.subTest(field=field, value=value):
                response = copy.deepcopy(self.response)
                response["requirements"][0][field] = value
                before = copy.deepcopy(response)
                self.assertTrue(any(code in x for x in validate_response(response, self.request())))
                _, conflicts, _ = self.merge(response)
                self.assertIn(code, str(conflicts))
                projected, repairs = project_authoritative_existing_payloads(
                    response, {"R00012": self.baseline["requirements"][0]}, {"C1": self.clauses[0]},
                )
                self.assertEqual(repairs, [])
                self.assertEqual(projected, before)

    def test_same_text_wrong_occurrence_and_changed_source_are_rejected(self):
        self.clauses.append({"id": "C2", "text": "正文使用宋体", "evidence_ids": ["E1"]})
        item = copy.deepcopy(self.response["requirements"][0])
        item["clause_ids"] = ["C2"]
        mapping = {c["id"]: c for c in self.clauses}
        self.assertIn("existing_requirement_clause_mismatch", existing_reference_errors(
            item, {"R00012": self.baseline["requirements"][0]}, mapping))
        self.clauses[0]["text"] = "正文使用黑体"
        self.assertIn("existing_requirement_source_text_mismatch", str(validate_response(self.response, self.request())))
        self.assertIn("existing_requirement_source_text_mismatch", str(self.merge(self.response)[1]))

    def test_duplicate_ids_and_empty_properties_cannot_be_projected(self):
        for field, value in [("clause_ids", ["C1", "C1"]), ("evidence_ids", ["E1", "E1"]),
                             ("properties", {}), ("properties", None)]:
            with self.subTest(field=field, value=value):
                response = copy.deepcopy(self.response)
                response["requirements"][0][field] = value
                projected, repairs = project_authoritative_existing_payloads(
                    response, {"R00012": self.baseline["requirements"][0]}, {"C1": self.clauses[0]})
                self.assertEqual(repairs, [])
                self.assertEqual(projected, response)
                self.assertTrue(validate_response(response, self.request()))

    def test_multi_clause_exact_existing_requirement_is_valid(self):
        self.clauses.append({"id": "C2", "text": "正文两端对齐", "evidence_ids": ["E2"]})
        self.evidence["evidence"].append({"id": "E2", "text": "正文两端对齐"})
        existing = self.baseline["requirements"][0]
        existing.update(clause_ids=["C1", "C2"], evidence_ids=["E1", "E2"],
                        source_text="正文使用宋体 | 正文两端对齐")
        self.response["requirements"][0].update(clause_ids=["C2", "C1"], evidence_ids=["E2", "E1"])
        self.response["clause_reviews"].append({"clause_id": "C2", "classification": "verify_existing", "reason": "复用组合要求。"})
        self.assertEqual(validate_response(self.response, self.request()), [])
        self.assertNotIn("existing_requirement_", str(self.merge(self.response)[1]))

    def test_new_requirement_has_code_allocated_id_and_does_not_mutate_response(self):
        response = copy.deepcopy(self.response)
        del response["requirements"][0]["existing_requirement_id"]
        response["clause_reviews"][0]["classification"] = "executable"
        self.assertEqual(validate_response(response, self.request()), [])
        spec, conflicts, _ = self.merge(response)
        self.assertTrue(spec["requirements"])
        self.assertTrue(all(x["id"] != "R00012" for x in spec["requirements"]))
        self.assertNotIn("id", response["requirements"][0])
        self.assertFalse([x for x in conflicts if x.get("type") == "llm_contract"], conflicts)

    def test_scoped_native_schema_and_null_new_requirement(self):
        for baseline, expected in [(self.baseline, ["R00012"]), ({}, None)]:
            with self.subTest(expected=expected):
                request = self.request(baseline)
                field = request["response_schema"]["properties"]["requirements"]["items"]["properties"]["existing_requirement_id"]
                self.assertEqual(field.get("enum"), expected)
                native = native_output_schema(request["response_schema"])
                self.assertEqual(native_schema_support_errors(native), [])
                response = copy.deepcopy(self.response)
                response["requirements"][0]["existing_requirement_id"] = None
                normalized = normalize_native_response(response, request["response_schema"])
                self.assertNotIn("existing_requirement_id", normalized["requirements"][0])
                self.assertEqual(validate_response(normalized, request), [])

    def test_chunk_schema_is_scoped_before_provenance_is_computed(self):
        self.clauses.append({"id": "C2", "text": "另一段", "evidence_ids": ["E2"]})
        self.evidence["evidence"].append({"id": "E2", "text": "另一段"})
        request = attach_request_provenance(self.request(), source_sha256="a"*64,
                                            evidence_doc=self.evidence, clauses=self.clauses, run_id="test")
        chunks = _build_host_review_chunks(request, self.clauses, self.evidence, "a"*64, 1)
        fields = [x["response_schema"]["properties"]["requirements"]["items"]["properties"]["existing_requirement_id"] for x in chunks]
        self.assertEqual(fields, [{"type": "string", "enum": ["R00012"]}, {"type": "null"}])
        from semantic_contract import request_body_sha256
        for chunk in chunks:
            self.assertEqual(chunk["provenance"]["request_sha256"], request_body_sha256(chunk))

    def test_real_bsu_five_guessed_references_fail_before_merge(self):
        rows = [
            ("R00567", "heading_1", "C00288", "E00202", "5 研究结论与建议", "thesis_title_zh"),
            ("R00568", "heading_2", "C00289", "E00203", "5.1 研究结论", "thesis_title_en"),
            ("R00569", "heading_3", "C00290", "E00204", "5.1.1 研究结论（根据实际情况填写）", "heading_1"),
            ("R00570", "heading_2", "C00299", "E00209", "5.2 研究建议", "heading_2"),
            ("R00571", "heading_3", "C00300", "E00210", "5.2.1 研究建议（根据实际情况填写）", "heading_3"),
        ]
        for rid, role, cid, eid, text, baseline_role in rows:
            with self.subTest(reference=rid):
                self.clauses = [{"id": cid, "text": text, "evidence_ids": [eid]}]
                self.evidence = {"evidence": [{"id": eid, "text": text}]}
                response = copy.deepcopy(self.response)
                response["requirements"][0].update(existing_requirement_id=rid, role=role,
                    properties={"text": text}, clause_ids=[cid], evidence_ids=[eid])
                response["clause_reviews"][0]["clause_id"] = cid
                request = self.request({})  # The five references were NOT in chunk 15.
                errors = validate_response(response, request)
                self.assertIn("unknown_existing_requirement_id", str(errors))
                records = contract_error_records(errors, response=response, chunk=request)
                self.assertTrue(all(x["identity_repair_allowed"] is False for x in records))
                guidance = _structured_contract_repair_guidance(records, contract_version="3.0")
                self.assertIn("fail closed", guidance)
                self.baseline["requirements"] = [{"id": rid, "role": baseline_role,
                    "properties": {"font": {"latin": "Times New Roman"}}, "clause_ids": ["C00303"],
                    "evidence_ids": ["E00212"], "source_text": "论文中出现英文时需要使用Times New Roman字体"}]
                self.assertIn("existing_requirement_payload_mismatch", str(self.merge(response)[1]))


if __name__ == "__main__":
    unittest.main()
