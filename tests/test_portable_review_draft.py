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

import requirements_engine as engine  # noqa: E402
from semantic_contract import attach_request_provenance, sha256_file, sha256_json  # noqa: E402
from thesis_format_pipeline import validate_offline_merge_receipt  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


class PortableReviewDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / "run"
        review = self.work / "review"
        requirements = review / "requirements"
        self.response_path = review / "host-agent-response.json"
        self.receipt_path = requirements / "merge-receipt.json"
        self.ledger_path = requirements / "semantic-review-ledger.json"
        self.marker_path = requirements / "merge-commit.json"
        self.response = {"contract_version": "3.0", "requirements": [], "clause_reviews": []}
        _write(self.response_path, self.response)
        ledger = {"response_sha256": sha256_json(self.response)}
        _write(self.ledger_path, ledger)
        self.extraction = {
            "run_id": "fresh-run", "llm_request_body_sha256": "a" * 64,
            "llm_request_envelope_sha256": "b" * 64,
            "llm_request_file_sha256": "c" * 64,
            "runtime_context": None,
        }
        self.receipt = {
            "status": "merged", "protocol": "host_agent_semantic_review",
            "run_id": "fresh-run", "request_body_sha256": "a" * 64,
            "request_envelope_sha256": "b" * 64,
            "request_file_sha256": "c" * 64, "runtime_context": None,
            "aggregate_sha256": sha256_json(self.response),
            "merged_response_path": str(self.response_path),
            "semantic_review_ledger_path": str(self.ledger_path),
            "semantic_review_ledger_sha256": sha256_json(ledger),
            "merge_commit_path": str(self.marker_path),
        }
        _write(self.receipt_path, self.receipt)
        self._write_marker()

    def _write_marker(self) -> None:
        marker = {
            "status": "committed", "protocol": "host_agent_semantic_review_merge",
            "run_id": "fresh-run", "aggregate_sha256": sha256_json(self.response),
            "artifacts": [
                {"path": path.relative_to(self.work / "review").as_posix(),
                 "sha256": sha256_file(path)}
                for path in (self.response_path, self.receipt_path, self.ledger_path)
            ],
        }
        _write(self.marker_path, marker)

    def _check(self) -> dict:
        return validate_offline_merge_receipt(
            response_path=self.response_path,
            receipt_path=self.receipt_path,
            extraction_manifest=self.extraction,
            work=self.work,
        )

    def test_current_run_merge_is_analysis_only(self) -> None:
        result = self._check()
        self.assertEqual(result["status"], "offline_merged_without_independent_review")
        self.assertIs(result["submission_ready"], False)

    def test_old_run_and_byte_changes_are_rejected(self) -> None:
        self.extraction["run_id"] = "different-run"
        with self.assertRaisesRegex(ValueError, "not bound"):
            self._check()
        self.extraction["run_id"] = "fresh-run"
        self.response["requirements"].append({"role": "cover"})
        _write(self.response_path, self.response)
        with self.assertRaisesRegex(ValueError, "not bound"):
            self._check()

    def test_modified_ledger_or_receipt_cannot_be_resealed_without_marker(self) -> None:
        _write(self.ledger_path, {"response_sha256": "0" * 64})
        with self.assertRaisesRegex(ValueError, "ledger"):
            self._check()
        _write(self.ledger_path, {"response_sha256": sha256_json(self.response)})
        changed = copy.deepcopy(self.receipt)
        changed["semantic_review_ledger_path"] = str(self.work / "other.json")
        _write(self.receipt_path, changed)
        with self.assertRaises(ValueError):
            self._check()

    def test_marker_path_traversal_is_rejected(self) -> None:
        marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        marker["artifacts"][0]["path"] = "../../outside.json"
        _write(self.marker_path, marker)
        with self.assertRaises(ValueError):
            self._check()

    def test_real_packet_merge_and_receipt_need_no_native_cli(self) -> None:
        portable_work = Path(self.temporary.name) / "portable-run"
        review_dir = portable_work / "review" / "requirements"
        response_path = portable_work / "review" / "host-agent-response.json"
        receipt_path = review_dir / "merge-receipt.json"
        source = "本节为说明性标题。"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        clauses = [{
            "id": "C1", "text": source, "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {"part": "document", "order": 1},
            "source_span": {
                "evidence_id": "E1", "start_offset": 0, "end_offset": len(source),
                "text": source, "source_sha256": digest,
            },
        }]
        evidence = {"evidence": [{"id": "E1", "text": source, "kind": "paragraph"}]}
        request = engine.build_llm_request(
            [], clauses, evidence, {}, "full", contract_version="3.0",
        )
        request = attach_request_provenance(
            request, source_sha256=digest, evidence_doc=evidence,
            clauses=clauses, run_id="portable-agent-run",
        )
        engine.prepare_host_agent_review_packets(
            request, clauses, evidence, digest, review_dir, chunk_size=1,
        )
        chunk = json.loads(
            (review_dir / "llm-request-chunks.json").read_text(encoding="utf-8")
        )[0]
        response = {
            "contract_version": "3.0", "provenance": chunk["provenance"],
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "informational",
                "reason": "The cited paragraph is a section label, not a format instruction.",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        _write(review_dir / chunk["batch"]["response_filename"], response)
        engine.merge_host_agent_review_packets(
            review_dir, response_out=response_path,
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        extraction = {
            "run_id": "portable-agent-run",
            "llm_request_body_sha256": receipt["request_body_sha256"],
            "llm_request_envelope_sha256": receipt["request_envelope_sha256"],
            "llm_request_file_sha256": receipt["request_file_sha256"],
            "runtime_context": receipt["runtime_context"],
        }
        result = validate_offline_merge_receipt(
            response_path=response_path, receipt_path=receipt_path,
            extraction_manifest=extraction, work=portable_work,
        )
        self.assertEqual(result["run_id"], "portable-agent-run")
        self.assertFalse(result["submission_ready"])


if __name__ == "__main__":
    unittest.main()
