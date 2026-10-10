from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import visual_page_review as visual  # noqa: E402


def _response(page_number: int, *, issues: list[dict] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "page_number": page_number,
        "review_state": "visually_reviewed",
        "visual_summary": f"Page {page_number} is legible with no visible layout defect.",
        "issues": issues or [],
        "uncertainty_notes": [],
    }


def _issue(**changes) -> dict:
    return {
        "category": "visible_requirement_mismatch",
        "severity": "medium",
        "finding": "A visible label differs from its source requirement.",
        "bbox_norm": [0.1, 0.2, 0.3, 0.4],
        "confidence": 0.8,
        "basis": "source_requirement",
        "requirement_ids": ["R1"],
        "source_clause_ids": ["C1"],
        "needs_human_review": False,
        **changes,
    }


class VisualPageReviewTests(unittest.TestCase):
    def _fixture(self, root: Path, *, page_count: int = 2) -> tuple[Path, Path, Path, Path, Path]:
        docx = root / "final.docx"
        docx.write_bytes(b"test DOCX bytes; these are only hash-bound fixture bytes")
        pdf = root / "final.pdf"
        document = fitz.open()
        for number in range(1, page_count + 1):
            page = document.new_page(width=300, height=400)
            page.insert_text((40, 50), f"Page {number} fixture")
        document.save(pdf)
        document.close()
        clauses_path = root / "requirement-clauses.json"
        clauses_path.write_text(json.dumps([
            {"id": "C1", "text": "Show a visible label."},
            {"id": "C2", "text": "Keep the page number centered."},
        ]), encoding="utf-8")
        spec_path = root / "format-spec.json"
        spec_path.write_text(json.dumps({
            "schema_version": "1.0",
            "source_document": "visual-review-fixture.docx",
            "compliance_mode": "full",
            "requirements": [
                {"id": "R1", "role": "visible label", "properties": {"text": "Visible label"},
                 "evidence_ids": ["E1"], "resolved_by": "rule", "confidence": 0.9, "clause_ids": ["C1"]},
                {"id": "R2", "role": "page number", "properties": {"text": "Centered page number"},
                 "evidence_ids": ["E2"], "resolved_by": "rule", "confidence": 0.9, "clause_ids": ["C2"]},
            ],
            "clause_compliance": [
                {"clause_id": "C1", "evidence_ids": ["E1"], "scope": "docx",
                 "status": "generated_and_verified", "requirement_ids": ["R1"], "reason": "Generated."},
                {"clause_id": "C2", "evidence_ids": ["E2"], "scope": "docx",
                 "status": "generated_and_verified", "requirement_ids": ["R2"], "reason": "Generated."},
            ],
            "roles": {}, "status": "semantic_resolved",
        }), encoding="utf-8")
        render_path = root / "word-render-report.json"
        render_path.write_text(json.dumps({
            "source_docx": {"path": str(docx.resolve()), "sha256": visual.sha256_file(docx)},
            "rendered_pdf": {"path": str(pdf.resolve()), "sha256": visual.sha256_file(pdf)},
        }), encoding="utf-8")
        return docx, pdf, render_path, spec_path, clauses_path

    def _prepare(self, root: Path, *, page_count: int = 2):
        docx, pdf, render_path, spec_path, clauses_path = self._fixture(root, page_count=page_count)
        work = root / "visual-review"
        manifest = visual.prepare_review(
            final_docx=docx, pdf=pdf, render_report=render_path,
            format_spec_path=spec_path, source_clauses_path=clauses_path,
            work_dir=work,
        )
        return manifest, work, docx, pdf, render_path, spec_path, clauses_path

    def _execute_valid(self, manifest: dict, work: Path) -> tuple[dict, list[list[str]]]:
        invocations: list[list[str]] = []

        def runner(command, *, cwd, timeout, input_text, env=None):
            self.assertEqual(env["THESIS_FORGE_HOST_RUNTIME"], "codex")
            invocations.append(command)
            image_arg = command[command.index("--image") + 1]
            page_number = int(Path(image_arg).stem.split("-")[-1])
            response = _response(page_number)
            last_message_path = Path(command[command.index("--output-last-message") + 1])
            last_message_path.write_text(json.dumps(response), encoding="utf-8")
            events = [
                {"type": "thread.started", "thread_id": f"fixture-{page_number}"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}},
                {"type": "turn.completed"},
            ]
            return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "")

        with patch.object(visual.codex, "probe_capabilities", return_value={
            "binary": "/usr/bin/codex", "version_returncode": 0, "exec_help_returncode": 0,
            "image_input_supported": True, "output_schema_supported": True,
            "model_image_capability_status": "unverified_without_live_model_request",
        }), patch.object(visual, "run_process", side_effect=runner):
            result = visual.execute_review(manifest, work_dir=work, codex_binary="/usr/bin/codex",
                                           host_runtime="codex")
        return result, invocations

    def test_prepare_binds_every_physical_page_without_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest, work, docx, pdf, render, spec, clauses = self._prepare(Path(td))
            self.assertEqual(manifest["page_count"], 2)
            self.assertEqual([page["page_number"] for page in manifest["pages"]], [1, 2])
            self.assertFalse(manifest["coverage_complete"])
            self.assertTrue(manifest["raster_coverage_complete"])
            self.assertEqual(manifest["unreviewed_page_numbers"], [1, 2])
            self.assertFalse(manifest["model_request_attempted"])
            self.assertFalse(manifest["model_request_made"])
            self.assertFalse(manifest["submission_ready"])
            for page in manifest["pages"]:
                self.assertEqual(page["rendered_from_pdf_sha256"], visual.sha256_file(pdf))
                self.assertTrue(Path(page["image"]["path"]).is_file())
                prompt = Path(page["prompt"]["path"]).read_text(encoding="utf-8")
                self.assertIn('"id":"R1"', prompt)
                self.assertIn('"id":"C1"', prompt)
            self.assertEqual(manifest["final_docx"]["sha256"], visual.sha256_file(docx))
            self.assertEqual(manifest["pdf"]["sha256"], visual.sha256_file(pdf))
            self.assertEqual(manifest["render_report"]["sha256"], visual.sha256_file(render))
            self.assertEqual(manifest["format_spec"]["sha256"], visual.sha256_file(spec))
            self.assertEqual(manifest["source_clauses"]["sha256"], visual.sha256_file(clauses))
            self.assertTrue((work / "page-images" / "page-0002.png").is_file())

    def test_valid_native_image_calls_cover_every_page_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest, work, docx, pdf, render, spec, clauses = self._prepare(Path(td))
            result, invocations = self._execute_valid(manifest, work)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(invocations), 2)
            self.assertTrue(all(command.count("--image") == 1 for command in invocations))
            self.assertTrue(all(command[-1] == "-" for command in invocations))
            self.assertEqual(result["requested_model"], "gpt-6-luna")
            self.assertEqual(result["host_runtime"], "codex")
            self.assertFalse(result["model_image_capability_verified"])
            self.assertTrue(result["model_request_made"])
            self.assertFalse(result["submission_ready"])
            report = work / "visual-review-manifest.json"
            verified = visual.verify_visual_review_report(
                report, final_docx=docx, pdf=pdf, render_report=render,
                format_spec_path=spec, source_clauses_path=clauses,
            )
            self.assertTrue(verified["valid"], verified["blockers"])

    def test_human_review_state_cannot_pass_visual_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest, work, docx, pdf, render, spec, clauses = self._prepare(Path(td), page_count=1)

            def runner(command, *, cwd, timeout, input_text, env=None):
                self.assertEqual(env["THESIS_FORGE_HOST_RUNTIME"], "codex")
                response = _response(1)
                response.update(review_state="needs_human_review", uncertainty_notes=["The footer text is too small to read confidently."])
                last_message_path = Path(command[command.index("--output-last-message") + 1])
                last_message_path.write_text(json.dumps(response), encoding="utf-8")
                events = [
                    {"type": "thread.started", "thread_id": "human-review-fixture"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}},
                    {"type": "turn.completed"},
                ]
                return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "")

            with patch.object(visual.codex, "probe_capabilities", return_value={
                "binary": "/usr/bin/codex", "version_returncode": 0, "exec_help_returncode": 0,
                "image_input_supported": True, "output_schema_supported": True,
            }), patch.object(visual, "run_process", side_effect=runner):
                result = visual.execute_review(manifest, work_dir=work, codex_binary="/usr/bin/codex",
                                               host_runtime="codex")
            self.assertEqual(result["status"], "human_review_required")
            verification = visual.verify_visual_review_report(
                work / "visual-review-manifest.json", final_docx=docx, pdf=pdf,
                render_report=render, format_spec_path=spec, source_clauses_path=clauses,
            )
            self.assertFalse(verification["valid"])
            self.assertIn("visual_review_has_findings_or_uncertainty", verification["blockers"])

    def test_source_clause_edges_and_region_contract_fail_closed(self) -> None:
        catalog = visual.build_source_catalog(
            {"compliance_mode": "full", "requirements": [
                {"id": "R1", "clause_ids": ["C1"]}, {"id": "R2", "clause_ids": ["C2"]},
            ], "clause_compliance": [
                {"clause_id": "C1", "status": "generated_and_verified", "requirement_ids": ["R1"]},
                {"clause_id": "C2", "status": "generated_and_verified", "requirement_ids": ["R2"]},
            ]},
            [{"id": "C1", "text": "First source."}, {"id": "C2", "text": "Second source."}],
        )
        good = _response(1, issues=[_issue()])
        visual.validate_page_response(good, page_number=1, catalog=catalog)
        invalid = [
            {**good, "page_number": True},
            {**good, "review_state": "unknown"},
            {**good, "issues": [_issue(source_clause_ids=["C2"])]},
            {**good, "issues": [_issue(requirement_ids=["R1", "R2"])]},
            {**good, "issues": [_issue(bbox_norm=[0, 0, 1.1, 1])]},
            {**good, "issues": [_issue(bbox_norm=[0.2, 0.2, 0.2, 0.4])]},
            {**good, "issues": [_issue(bbox_norm=[0, 0, float("nan"), 1])]},
            {**good, "issues": [_issue(requirement_ids=[["R1"]])]},
            {**good, "issues": [_issue(source_clause_ids=[["C1"]])]},
        ]
        for response in invalid:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    visual.validate_page_response(response, page_number=1, catalog=catalog)

    def test_model_runner_failure_is_a_durable_no_retry_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest, work, *_ = self._prepare(Path(td), page_count=2)
            calls: list[list[str]] = []

            def failed(command, *, cwd, timeout, input_text, env=None):
                self.assertEqual(env["THESIS_FORGE_HOST_RUNTIME"], "codex")
                calls.append(command)
                return subprocess.CompletedProcess(command, 124, "", "timeout")

            with patch.object(visual.codex, "probe_capabilities", return_value={
                "binary": "/usr/bin/codex", "version_returncode": 0, "exec_help_returncode": 0,
                "image_input_supported": True, "output_schema_supported": True,
            }), patch.object(visual, "run_process", side_effect=failed):
                result = visual.execute_review(manifest, work_dir=work, codex_binary="/usr/bin/codex",
                                               host_runtime="codex")
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(len(calls), 1)
            self.assertIsNone(result["model_request_made"])
            self.assertTrue(result["model_request_attempted"])
            self.assertEqual(result["unreviewed_page_numbers"], [1, 2])
            saved = json.loads((work / "visual-review-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["blocked_reason"], "native_codex_timeout")
            with self.assertRaisesRegex(ValueError, "cannot resume"):
                visual.execute_review(result, work_dir=work, codex_binary="/usr/bin/codex",
                                      host_runtime="codex")

    def test_source_changes_after_preparation_stop_before_any_model_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest, work, _docx, _pdf, _render, _spec, clauses = self._prepare(Path(td))
            clauses.write_text(clauses.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with patch.object(visual.codex, "probe_capabilities") as probe:
                with self.assertRaisesRegex(ValueError, "source_clauses changed after preparation"):
                    visual.execute_review(manifest, work_dir=work, codex_binary="/usr/bin/codex",
                                          host_runtime="codex")
            probe.assert_not_called()

    def test_execution_requires_an_explicit_codex_host_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manifest, work, *_ = self._prepare(Path(td))
            with patch.object(visual.codex, "probe_capabilities") as probe:
                with self.assertRaisesRegex(ValueError, "explicitly declared Codex host runtime"):
                    visual.execute_review(manifest, work_dir=work, codex_binary="/usr/bin/codex",
                                          host_runtime="openclaw")
            probe.assert_not_called()

    def test_verifier_rejects_projected_response_and_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest, work, docx, pdf, render, spec, clauses = self._prepare(root)
            result, _ = self._execute_valid(manifest, work)
            report_path = work / "visual-review-manifest.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["pages"][0]["raw_response"]["visual_summary"] = "forged projection"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            tampered = visual.verify_visual_review_report(
                report_path, final_docx=docx, pdf=pdf, render_report=render,
                format_spec_path=spec, source_clauses_path=clauses,
            )
            self.assertFalse(tampered["valid"])
            self.assertTrue(any("response_projection_mismatch" in item for item in tampered["blockers"]))

            report["pages"][0]["raw_response"]["visual_summary"] = _response(1)["visual_summary"]
            report["code_fingerprint_sha256"] = "0" * 64
            report_path.write_text(json.dumps(report), encoding="utf-8")
            stale_code = visual.verify_visual_review_report(
                report_path, final_docx=docx, pdf=pdf, render_report=render,
                format_spec_path=spec, source_clauses_path=clauses,
            )
            self.assertFalse(stale_code["valid"])
            self.assertIn("visual_code_fingerprint_mismatch", stale_code["blockers"])

            # Keep the original current report as evidence, then verify that
            # changing any bound final input invalidates acceptance.
            report["code_fingerprint_sha256"] = visual.code_fingerprint_sha256()
            report_path.write_text(json.dumps(report), encoding="utf-8")
            docx.write_bytes(b"changed final document")
            stale = visual.verify_visual_review_report(
                report_path, final_docx=docx, pdf=pdf, render_report=render,
                format_spec_path=spec, source_clauses_path=clauses,
            )
            self.assertFalse(stale["valid"])
            self.assertTrue(any("final_docx_hash_mismatch" in item for item in stale["blockers"]))

    def test_prepare_refuses_existing_work_dir_and_docx_pdf_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx, pdf, render, spec, clauses = self._fixture(root)
            work = root / "visual-review"
            work.mkdir()
            with self.assertRaisesRegex(ValueError, "already exists"):
                visual.prepare_review(final_docx=docx, pdf=pdf, render_report=render,
                                      format_spec_path=spec, source_clauses_path=clauses, work_dir=work)
            render_data = json.loads(render.read_text(encoding="utf-8"))
            render_data["rendered_pdf"]["sha256"] = "0" * 64
            render.write_text(json.dumps(render_data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PDF hash"):
                visual.prepare_review(final_docx=docx, pdf=pdf, render_report=render,
                                      format_spec_path=spec, source_clauses_path=clauses,
                                      work_dir=root / "new-review")


if __name__ == "__main__":
    unittest.main()
