from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import requirements_engine as engine  # noqa: E402
from semantic_contract import HOST_AGENT_ORIGIN, attach_request_provenance  # noqa: E402


class HostAgentReviewTests(unittest.TestCase):
    def _request(self, clauses: list[dict], evidence: dict) -> dict:
        request = engine.build_llm_request([], clauses, evidence, {}, "full")
        return attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
        )

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


if __name__ == "__main__":
    unittest.main()
