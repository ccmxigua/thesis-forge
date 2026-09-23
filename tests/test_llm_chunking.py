from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import requirements_engine as engine  # noqa: E402
from host_review_contract import HOST_REVIEW_CONTRACT_V3  # noqa: E402
from host_review_contract import validate_response as validate_host_review_response  # noqa: E402
from semantic_contract import HOST_AGENT_ORIGIN, attach_request_provenance  # noqa: E402


class HostAgentReviewTests(unittest.TestCase):
    def _request(self, clauses: list[dict], evidence: dict) -> dict:
        request = engine.build_llm_request([], clauses, evidence, {}, "full")
        return attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
        )

    def _response_for_chunk(self, chunk: dict, *, requirement_indexes: list[object] | None = None) -> dict:
        clauses = chunk["clauses"]
        requirements = []
        reviews = []
        for clause in clauses:
            requirements.append({
                "role": "body_text", "properties": {"font": {"size_pt": 12}},
                "clause_ids": [clause["id"]], "evidence_ids": clause["evidence_ids"],
                "confidence": 1, "reason": "The supplied clause states this requirement.",
            })
            reviews.append({
                "clause_id": clause["id"], "classification": "executable",
                "requirement_indexes": [0] if requirement_indexes is None else requirement_indexes,
                "reason": "The clause is executable in DOCX.",
            })
        return {
            "contract_version": "2.1",
            "provenance": chunk["provenance"],
            "requirements": requirements,
            "clause_reviews": reviews,
            "unsupported_items": [], "reported_conflicts": [],
        }

    def test_packets_are_offline_and_expose_only_matching_existing_rules(self) -> None:
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        rule_spec = {
            "requirements": [
                {"id": "R1", "role": "body_text", "source_text": "正文使用宋体", "evidence_ids": ["E1"]},
                {"id": "R2", "role": "body_text", "source_text": "正文使用宋体", "evidence_ids": ["E9"]},
                {"id": "R3", "role": "body_text", "source_text": "标题居中", "evidence_ids": ["E1"]},
            ],
        }
        request = engine.build_llm_request(
            [], clauses, evidence, engine._narrow_rule_spec_for_chunk(rule_spec, clauses), "full"
        )
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
        )
        with tempfile.TemporaryDirectory() as td:
            manifest = engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, Path(td), chunk_size=1,
            )
            self.assertEqual(manifest["protocol"], "host_agent_semantic_review")
            self.assertEqual(manifest["origin"], HOST_AGENT_ORIGIN)
            chunks = json.loads((Path(td) / "llm-request-chunks.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in chunks[0]["clauses"]], ["C1"])
            self.assertEqual(
                [item["id"] for item in chunks[0]["rule_spec"]["requirements"]],
                ["R1"],
            )
            self.assertFalse((Path(td) / "llm-response-chunk-0001.json").exists())

    def test_host_responses_are_bound_and_local_indexes_are_shifted(self) -> None:
        clauses = [
            {"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
             "source_kind": "paragraph", "location": {}, "part_index": 0},
            {"id": "C2", "text": "标题居中", "evidence_ids": ["E2"],
             "source_kind": "paragraph", "location": {}, "part_index": 0},
        ]
        evidence = {
            "evidence": [
                {"id": "E1", "text": "正文使用宋体", "kind": "paragraph"},
                {"id": "E2", "text": "标题居中", "kind": "paragraph"},
            ],
            "structure_evidence": {}, "page_evidence": {},
        }
        request = self._request(clauses, evidence)
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            manifest = engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunks = json.loads((review_dir / "llm-request-chunks.json").read_text(encoding="utf-8"))
            for chunk in chunks:
                clause = chunk["clauses"][0]
                response = {
                    "contract_version": "2.1",
                    "provenance": chunk["provenance"],
                    "requirements": [{
                        "role": "body_text", "properties": {"font": {"size_pt": 12}},
                        "clause_ids": [clause["id"]], "evidence_ids": clause["evidence_ids"],
                        "confidence": 1, "reason": "The supplied clause states this requirement.",
                    }],
                    "clause_reviews": [{
                        "clause_id": clause["id"], "classification": "executable",
                        "requirement_indexes": [0], "reason": "The clause is executable in DOCX.",
                    }],
                    "unsupported_items": [], "reported_conflicts": [],
                }
                (review_dir / chunk["batch"]["response_filename"]).write_text(
                    json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                )
            merged, metadata = engine.merge_host_agent_review_packets(
                review_dir, response_out=review_dir / "llm-response.json",
            )
            self.assertTrue(metadata["chunked"])
            self.assertEqual(metadata["chunk_count"], 2)
            self.assertEqual(len(merged["requirements"]), 2)
            self.assertEqual(merged["clause_reviews"][1]["requirement_indexes"], [1])
            self.assertEqual(merged["provenance"], request["provenance"])
            self.assertEqual(merged["provenance"]["origin"], HOST_AGENT_ORIGIN)
            self.assertEqual(metadata["request_body_sha256"], request["provenance"]["request_sha256"])
            self.assertEqual(metadata["request_envelope_sha256"], engine.request_envelope_sha256(request))
            self.assertEqual(
                json.loads((review_dir / "merge-receipt.json").read_text())["request_body_sha256"],
                request["provenance"]["request_sha256"],
            )
            receipt = json.loads((review_dir / "merge-receipt.json").read_text())
            ledger = json.loads((review_dir / "semantic-review-ledger.json").read_text())
            self.assertEqual(receipt["aggregate_sha256"], engine.sha256_json(merged))
            self.assertEqual(receipt["semantic_review_ledger_sha256"], engine.sha256_json(ledger))
            self.assertEqual(ledger["response_sha256"], receipt["aggregate_sha256"])
            self.assertEqual(metadata["semantic_review_ledger_sha256"], receipt["semantic_review_ledger_sha256"])

    def test_merge_preserves_typed_conflicts_from_every_chunk(self) -> None:
        clauses = [
            {"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
             "source_kind": "paragraph", "location": {}, "part_index": 0},
            {"id": "C2", "text": "正文使用黑体", "evidence_ids": ["E2"],
             "source_kind": "paragraph", "location": {}, "part_index": 0},
        ]
        evidence = {"evidence": [
            {"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"},
            {"id": "E2", "text": clauses[1]["text"], "kind": "paragraph"},
        ]}
        request = engine.build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
            run_id="typed-conflict-merge-test",
        )
        expected_conflicts = []
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td) / "requirements"
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunks = json.loads(
                (review_dir / "llm-request-chunks.json").read_text(encoding="utf-8")
            )
            for chunk in chunks:
                clause = chunk["clauses"][0]
                evidence_id = clause["evidence_ids"][0]
                conflict = {
                    "type": "source_conflict",
                    "reason": f"Sources for {clause['id']} disagree about body font.",
                    "clause_ids": [clause["id"]],
                    "evidence_ids": [evidence_id],
                    "target": {"role": "body_text", "property": "font"},
                    "candidates": [{
                        "evidence_id": evidence_id,
                        "value_type": "object",
                        "value_json": json.dumps({"cjk": "SimSun", "size_pt": 12}),
                    }],
                    "status": "requires_human_review",
                }
                expected_conflicts.append(conflict)
                response = {
                    "contract_version": HOST_REVIEW_CONTRACT_V3,
                    "provenance": chunk["provenance"],
                    "requirements": [],
                    "clause_reviews": [{
                        "clause_id": clause["id"],
                        "classification": "informational",
                        "reason": "The source conflict is preserved for review.",
                    }],
                    "unsupported_items": [],
                    "reported_conflicts": [conflict],
                }
                (review_dir / chunk["batch"]["response_filename"]).write_text(
                    json.dumps(response, ensure_ascii=False), encoding="utf-8",
                )

            merged, _metadata = engine.merge_host_agent_review_packets(
                review_dir, response_out=review_dir / "merged.json",
            )

            self.assertEqual(merged["reported_conflicts"], expected_conflicts)
            persisted = json.loads((review_dir / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["reported_conflicts"], expected_conflicts)

    def test_merge_rejects_chunk_provenance_hash_domain_mismatch(self) -> None:
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        request = self._request(clauses, evidence)
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunks_path = review_dir / "llm-request-chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks[0]["provenance"]["evidence_sha256"] = "0" * 64
            chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            response = self._response_for_chunk(chunks[0])
            (review_dir / chunks[0]["batch"]["response_filename"]).write_text(
                json.dumps(response, ensure_ascii=False), encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "provenance evidence_sha256"):
                engine.merge_host_agent_review_packets(review_dir)
            self.assertFalse((review_dir / "merge-receipt.json").exists())

    def test_merge_rejects_chunk_text_tampering_even_when_ids_are_preserved(self) -> None:
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        request = self._request(clauses, evidence)
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunks_path = review_dir / "llm-request-chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            # Keep the IDs and even the old provenance block untouched.  The
            # source-derived projection must still reject changed text before
            # any response can be merged.
            chunks[0]["clauses"][0]["text"] = "正文使用黑体"
            chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source projection"):
                engine.merge_host_agent_review_packets(review_dir)
            self.assertFalse((review_dir / "merge-receipt.json").exists())

    def test_merge_rejects_a_response_from_another_model_protocol(self) -> None:
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        request = self._request(clauses, evidence)
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunk = json.loads((review_dir / "llm-request-chunks.json").read_text(encoding="utf-8"))[0]
            response = {
                "contract_version": "2.1",
                "provenance": {**chunk["provenance"], "origin": "fresh_llm"},
                "requirements": [],
                "clause_reviews": [{
                    "clause_id": "C1", "classification": "informational",
                    "requirement_indexes": [], "reason": "The clause was reviewed.",
                }],
                "unsupported_items": [], "reported_conflicts": [],
            }
            (review_dir / chunk["batch"]["response_filename"]).write_text(
                json.dumps(response, ensure_ascii=False), encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "provenance_origin_mismatch"):
                engine.merge_host_agent_review_packets(review_dir)

    def test_shared_validator_rejects_local_indexes_before_global_offset(self) -> None:
        clauses = [
            {"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
             "source_kind": "paragraph", "location": {}, "part_index": 0},
            {"id": "C2", "text": "标题居中", "evidence_ids": ["E2"],
             "source_kind": "paragraph", "location": {}, "part_index": 0},
        ]
        evidence = {
            "evidence": [
                {"id": "E1", "text": "正文使用宋体", "kind": "paragraph"},
                {"id": "E2", "text": "标题居中", "kind": "paragraph"},
            ],
        }
        request = self._request(clauses, evidence)
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunks = json.loads((review_dir / "llm-request-chunks.json").read_text(encoding="utf-8"))
            for index, chunk in enumerate(chunks):
                bad_index = [1] if index == 1 else None
                response = self._response_for_chunk(chunk, requirement_indexes=bad_index)
                path = review_dir / chunk["batch"]["response_filename"]
                path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
                if bad_index is not None:
                    errors = validate_host_review_response(response, chunk)
                    self.assertTrue(any("invalid_integer" in item for item in errors))
            merged_path = review_dir.parent / "merged.json"
            with self.assertRaisesRegex(ValueError, "contract failed"):
                engine.merge_host_agent_review_packets(review_dir, response_out=merged_path)
            self.assertFalse(merged_path.exists())
            self.assertFalse((review_dir / "merge-receipt.json").exists())

    def test_merge_rejects_response_path_outside_run_directory(self) -> None:
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        request = self._request(clauses, evidence)
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td) / "requirements"
            review_dir.mkdir()
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            manifest_path = review_dir / "host-agent-review-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["response_files"] = ["../outside.json"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes its run directory"):
                engine.merge_host_agent_review_packets(review_dir)
            self.assertFalse((review_dir / "merge-receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
