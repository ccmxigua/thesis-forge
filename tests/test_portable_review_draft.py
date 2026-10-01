from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import requirements_engine as engine  # noqa: E402
import offline_review_receipt  # noqa: E402
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
        self.assertIs(result["independent_review_verified"], False)
        self.assertIs(result["provider_model_verified"], False)

    def test_portable_cli_works_without_codex_or_other_native_cli(self) -> None:
        extraction_path = self.work / "review" / "requirements" / "extraction-manifest.json"
        _write(extraction_path, self.extraction)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "offline_review_receipt.py"),
             "--work-dir", str(self.work), "--response", str(self.response_path),
             "--receipt", str(self.receipt_path),
             "--extraction-manifest", str(extraction_path)],
            env={"PATH": ""}, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["submission_ready"])

    def test_portable_cli_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        for raw in ('{"run_id":"fresh-run","run_id":"stale"}',
                    '{"run_id":NaN}'):
            with self.subTest(raw=raw):
                extraction_path = self.work / "review" / "requirements" / "extraction-manifest.json"
                extraction_path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    offline_review_receipt._object(extraction_path, label="offline extraction manifest")

    def test_non_object_receipt_and_response_fail_closed(self) -> None:
        for path in (self.receipt_path, self.response_path):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_text("[]", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                    self._check()
                path.write_bytes(original)

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

    def test_real_wrapper_prepare_merge_and_continue_without_native_cli(self) -> None:
        self._real_wrapper_flow(defaults=False)

    def test_ordinary_conversation_defaults_work_without_native_cli(self) -> None:
        self._real_wrapper_flow(defaults=True)

    def _real_wrapper_flow(self, *, defaults: bool) -> None:
        root = Path(self.temporary.name) / "cli-smoke"
        root.mkdir()
        requirements = root / "requirements.docx"
        source = root / "source.docx"
        output = root / "review.docx"
        work = root / "work"
        for path, text in ((requirements, "本节为说明性标题。"),
                           (source, "测试论文正文。")):
            document = Document()
            document.add_paragraph(text)
            document.save(path)
        # Relative caller-owned paths outside the checkout and no installed
        # native CLI: the current agent supplies JSON, not a provider override.
        env = dict(os.environ, PATH="", THESIS_FORGE_HOST_RUNTIME="ordinary-agent")
        base = [sys.executable, str(ROOT / "scripts" / "thesis_format.py"),
                requirements.name, source.name, output.name,
                "--work-dir", work.name]
        preparation = base[:-2] if defaults else base + ["--prepare-agent-review"]
        prepared = subprocess.run(preparation,
                                  cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(prepared.returncode, 0, prepared.stderr[-1200:] + prepared.stdout[-1200:])
        if defaults:
            context = json.loads(prepared.stdout.splitlines()[0])
            self.assertEqual(context["model_selection"], "unchanged_by_skill")
            work = Path(context["work_dir"])
            self.assertEqual(work.parent, (root / "build").resolve())
            base[-1] = str(work)
        review_dir = work / "review" / "requirements"
        chunks = json.loads((review_dir / "llm-request-chunks.json").read_text(encoding="utf-8"))
        self.assertTrue(chunks)
        for chunk in chunks:
            response = {
                "contract_version": "3.0", "provenance": chunk["provenance"],
                "requirements": [],
                "clause_reviews": [
                    {"clause_id": clause["id"], "classification": "informational",
                     "reason": "The cited text is a section label, not a formatting instruction."}
                    for clause in chunk["clauses"]
                ],
                "unsupported_items": [], "reported_conflicts": [],
            }
            _write(review_dir / chunk["batch"]["response_filename"], response)
        merged_path = work / "review" / "host-agent-response.json"
        merged = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "merge_host_agent_review.py"),
             str(review_dir), "--response-out", str(merged_path)],
            cwd=root, env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(merged.returncode, 0, merged.stderr[-1200:] + merged.stdout[-1200:])
        continued = subprocess.run(
            base + ["--llm-response", str(merged_path)]
            + ([] if defaults else ["--offline-review-draft"]),
            cwd=root, env=env, capture_output=True, text=True, check=False,
        )
        manifest = json.loads((work / "pipeline-manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("refusing to reuse a non-empty work directory", continued.stderr)
        self.assertEqual(manifest["offline_review_receipt"]["status"],
                         "offline_merged_without_independent_review")
        self.assertFalse(manifest.get("submission_ready", False))
        self.assertEqual(continued.returncode, 0,
                         continued.stderr[-1200:] + continued.stdout[-1200:])
        self.assertTrue(output.is_file())
        self.assertEqual(manifest["output_policy"], "review_draft")


if __name__ == "__main__":
    unittest.main()
