from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import requirements_engine as engine  # noqa: E402
import thesis_format_pipeline as pipeline  # noqa: E402
from semantic_contract import (  # noqa: E402
    attach_request_provenance,
    request_body_sha256,
    request_envelope_sha256,
    sha256_file,
    sha256_json,
)
from native_semantic_review import (  # noqa: E402
    OBLIGATION_COVERAGE_PROTOCOL,
    build_obligation_coverage_request,
    validate_obligation_coverage_response,
)


class HostReviewCommitMarkerTests(unittest.TestCase):
    def _make_committed_merge(self, work: Path) -> tuple[Path, Path, Path, dict]:
        clauses = [{
            "id": "C1", "text": "本节为说明性标题。", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {"part": "document", "order": 1},
        }]
        evidence = {"evidence": [{
            "id": "E1", "text": clauses[0]["text"], "kind": "paragraph",
        }]}
        request = engine.build_llm_request([], clauses, evidence, {}, "full")
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="commit-marker-test-run",
        )
        review_dir = work / "requirements"
        engine.prepare_host_agent_review_packets(
            request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
        )
        chunk = json.loads(
            (review_dir / "llm-request-chunks.json").read_text(encoding="utf-8")
        )[0]
        response = {
            "contract_version": "2.1",
            "provenance": chunk["provenance"],
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "informational",
                "requirement_indexes": [], "reason": "该段为说明性内容。",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        (review_dir / chunk["batch"]["response_filename"]).write_text(
            json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        response_path = work / "llm-response.json"
        _merged, metadata = engine.merge_host_agent_review_packets(
            review_dir, response_out=response_path,
        )
        chunk_response_path = review_dir / chunk["batch"]["response_filename"]
        chunk_response_sha = sha256_json(response)
        independent_dir = review_dir / "independent-review-chunk-0001-attempt-01"
        independent_dir.mkdir()
        independent_request = build_obligation_coverage_request(
            response, chunk, run_id="commit-marker-test-run", chunk_index=1,
        )
        independent_request["attempt"] = 1
        reviewer_response = {"results": [{
            "check_id": "C1",
            "verdict": "consistent",
            "rationale": "来源是说明性标题，没有遗漏可执行义务。",
            "evidence_quotes": [clauses[0]["text"]],
            "identified_obligations": [],
            "machine_obligation_ids": [],
        }]}
        normalized_results = validate_obligation_coverage_response(
            reviewer_response, independent_request["checks"],
        )
        request_path = independent_dir / "request.json"
        reviewer_response_path = independent_dir / "response.json"
        request_path.write_text(json.dumps(independent_request, ensure_ascii=False), encoding="utf-8")
        reviewer_response_path.write_text(json.dumps(reviewer_response, ensure_ascii=False), encoding="utf-8")
        request_sha = sha256_json(independent_request)
        reviewer_response_sha = sha256_file(reviewer_response_path)
        review_audit = {
            "status": "completed",
            "protocol": OBLIGATION_COVERAGE_PROTOCOL,
            "adapter_id": "codex",
            "host_runtime": "codex",
            "request_sha256": request_sha,
            "request_path": str(request_path.resolve()),
            "response_sha256": reviewer_response_sha,
            "response_path": str(reviewer_response_path.resolve()),
            "results": normalized_results,
        }
        coverage_envelope = {
            "protocol": OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed",
            "run_id": "commit-marker-test-run",
            "chunk_index": 1,
            "candidate_response_sha256": chunk_response_sha,
            "provenance": chunk["provenance"],
            "review_request_sha256": request_sha,
            "review_response_sha256": reviewer_response_sha,
            "results": normalized_results,
            "review_audit": review_audit,
        }
        coverage_path = independent_dir / "coverage-audit.json"
        coverage_path.write_text(json.dumps(coverage_envelope, ensure_ascii=False), encoding="utf-8")
        independent_pointer = {
            "status": "completed",
            "protocol": OBLIGATION_COVERAGE_PROTOCOL,
            "audit_path": coverage_path.relative_to(review_dir).as_posix(),
            "audit_sha256": sha256_file(coverage_path),
            "run_id": "commit-marker-test-run",
            "chunk_index": 1,
            "candidate_response_sha256": chunk_response_sha,
            "review_request_sha256": request_sha,
            "review_response_sha256": reviewer_response_sha,
        }
        audit_path = review_dir / "host-agent-run.json"
        audit_path.write_text(json.dumps({
            "status": "merged",
            "run_id": "commit-marker-test-run",
            "response_contract_version": "2.1",
            "adapter_id": "codex",
            "structured_output_mode": "native_schema",
            "host_runtime": "codex",
            "response_path": str(response_path.resolve()),
            "chunk_count": 1,
            "chunk_lifecycle": [{
                "chunk_index": 1,
                "status": "completed",
                "remote_operation_state": "completed",
            }],
            "chunk_runs": [{
                "chunk_index": 1,
                "response_path": str(chunk_response_path.resolve()),
                "accepted_response_sha256": chunk_response_sha,
                "independent_obligation_review": independent_pointer,
            }],
            "merge": metadata,
        }, ensure_ascii=False), encoding="utf-8")
        extraction_manifest = {
            "run_id": "commit-marker-test-run",
            "llm_request_body_sha256": request_body_sha256(request),
            "llm_request_envelope_sha256": request_envelope_sha256(request),
            "llm_request_file_sha256": sha256_file(review_dir / "llm-request.json"),
        }
        return response_path, audit_path, review_dir / "merge-receipt.json", extraction_manifest

    def _reseal_independent_request_for_test(
        self, audit_path: Path, mutate,
    ) -> dict:
        """Model an attacker who can rewrite local audit files and their hashes."""
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        chunk_audit = audit["chunk_runs"][0]
        pointer = chunk_audit["independent_obligation_review"]
        envelope_path = audit_path.parent / pointer["audit_path"]
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        review_audit = envelope["review_audit"]
        request_path = Path(review_audit["request_path"])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        mutate(request)
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        request_sha = sha256_json(request)
        review_audit["request_sha256"] = request_sha
        envelope["review_request_sha256"] = request_sha
        pointer["review_request_sha256"] = request_sha
        envelope_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        pointer["audit_sha256"] = sha256_file(envelope_path)
        return audit

    def test_pipeline_accepts_only_complete_byte_bound_merge_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            response, audit, receipt, extraction = self._make_committed_merge(work)
            result = pipeline.validate_host_review_receipts(
                response_path=response, audit_path=audit, receipt_path=receipt,
                extraction_manifest=extraction, work=work,
            )
            self.assertEqual(result["run_id"], "commit-marker-test-run")
            self.assertEqual(result["merge_commit_marker"]["path"], str((receipt.parent / "merge-commit.json").resolve()))

    def test_pipeline_rejects_response_bytes_changed_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            response, audit, receipt, extraction = self._make_committed_merge(work)
            response.write_text(response.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bytes do not match the commit marker"):
                pipeline.validate_host_review_receipts(
                    response_path=response, audit_path=audit, receipt_path=receipt,
                    extraction_manifest=extraction, work=work,
                )

    def test_pipeline_rejects_missing_final_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            response, audit, receipt, extraction = self._make_committed_merge(work)
            (receipt.parent / "merge-commit.json").unlink()
            with self.assertRaisesRegex(ValueError, "final merge commit marker is missing"):
                pipeline.validate_host_review_receipts(
                    response_path=response, audit_path=audit, receipt_path=receipt,
                    extraction_manifest=extraction, work=work,
                )

    def test_pipeline_rejects_mutated_independent_review_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            response, audit, receipt, extraction = self._make_committed_merge(work)
            host_audit = json.loads(audit.read_text(encoding="utf-8"))
            review_pointer = host_audit["chunk_runs"][0]["independent_obligation_review"]
            envelope = json.loads(
                (audit.parent / review_pointer["audit_path"]).read_text(encoding="utf-8")
            )
            reviewer_response_path = Path(envelope["review_audit"]["response_path"])
            reviewer_response_path.write_text(
                reviewer_response_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "request/response .* byte or identity validation"):
                pipeline.validate_host_review_receipts(
                    response_path=response, audit_path=audit, receipt_path=receipt,
                    extraction_manifest=extraction, work=work,
                )

    def test_pipeline_rejects_resealed_noncanonical_independent_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            _response, audit_path, _receipt, _extraction = self._make_committed_merge(work)
            audit = self._reseal_independent_request_for_test(
                audit_path,
                lambda request: request["checks"][0].update(
                    document_text="伪造的条款原文",
                ),
            )
            with self.assertRaisesRegex(ValueError, "not reconstructed from the canonical source packet"):
                pipeline._validate_independent_obligation_receipts(
                    audit=audit,
                    review_root=audit_path.parent,
                    expected_run_id="commit-marker-test-run",
                    expected_request_body_sha=request_body_sha256(
                        json.loads((audit_path.parent / "llm-request.json").read_text(encoding="utf-8"))
                    ),
                    expected_request_envelope_sha=None,
                    expected_request_file_sha=None,
                )

    def test_pipeline_rejects_chunk_packet_not_projected_from_fresh_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            response, audit_path, receipt, extraction = self._make_committed_merge(work)
            chunks_path = audit_path.parent / "llm-request-chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks[0]["clauses"][0]["text"] = "替换后的来源条款"
            chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match the deterministic source projection"):
                pipeline.validate_host_review_receipts(
                    response_path=response, audit_path=audit_path, receipt_path=receipt,
                    extraction_manifest=extraction, work=work,
                )

    def test_pipeline_rejects_duplicate_independent_clause_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td).resolve()
            _response, audit_path, _receipt, _extraction = self._make_committed_merge(work)

            def duplicate_check(request: dict) -> None:
                request["checks"].append(dict(request["checks"][0]))

            audit = self._reseal_independent_request_for_test(audit_path, duplicate_check)
            with self.assertRaisesRegex(ValueError, "does not cover every accepted clause"):
                pipeline._validate_independent_obligation_receipts(
                    audit=audit,
                    review_root=audit_path.parent,
                    expected_run_id="commit-marker-test-run",
                    expected_request_body_sha=request_body_sha256(
                        json.loads((audit_path.parent / "llm-request.json").read_text(encoding="utf-8"))
                    ),
                    expected_request_envelope_sha=None,
                    expected_request_file_sha=None,
                )

    def test_failed_marker_publication_rolls_back_only_this_artifact_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            data_path = root / "merged.json"
            marker_path = root / "merge-commit.json"
            original_replace = Path.replace

            def fail_marker_replace(path: Path, target: Path) -> Path:
                if Path(target).name == "merge-commit.json":
                    raise OSError("injected marker publication failure")
                return original_replace(path, target)

            with patch.object(Path, "replace", autospec=True, side_effect=fail_marker_replace):
                with self.assertRaisesRegex(OSError, "injected marker publication failure"):
                    engine._write_json_artifacts_atomic(
                        [(data_path, {"ok": True})],
                        commit_marker_path=marker_path,
                        commit_root=root,
                        commit_metadata={"status": "committed"},
                    )

            self.assertFalse(data_path.exists())
            self.assertFalse(marker_path.exists())
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
