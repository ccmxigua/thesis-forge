from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import batch_rerun_ten_schools as batch  # noqa: E402
import post_render_acceptance as post_render  # noqa: E402
from thesis_format_pipeline import runtime_code_fingerprint  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_valid_pdf(path: Path, text: str = "post-word") -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(str(path))
    document.close()


class PostRenderAcceptanceTests(unittest.TestCase):
    def test_evidence_loader_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "evidence.json"
            path.write_text('{"valid":true,"valid":false}', encoding="utf-8")
            self.assertIsNone(post_render.read_object(path))

    def _pre_render_fixture(self, root: Path) -> tuple[Path, Path, str]:
        source = root / "generated.docx"
        Document().save(source)
        digest = _sha256(source)
        validation = root / "validation-report.json"
        validation.write_text(json.dumps({
            "valid": True,
            "format_ready": True,
            "serialized_docx_valid": True,
            "output_policy": "submission",
            "compliance_mode": "full",
            "docx_fully_compliant": True,
            "output_docx": str(source),
            "property_receipt_audit": {
                "valid": True,
                "receipts": [{"serialized_docx_sha256": digest}],
            },
        }), encoding="utf-8")
        return source, validation, digest

    def _batch_fixture(self, root: Path) -> dict:
        case_dir = root / "case"
        work = case_dir / "work"
        requirements = work / "requirements"
        apply_dir = work / "application"
        requirements.mkdir(parents=True)
        apply_dir.mkdir(parents=True)
        output = case_dir / "generated.docx"
        Document().save(output)
        digest = _sha256(output)
        format_spec = requirements / "format-spec.json"
        format_spec.write_text("{}\n", encoding="utf-8")
        (requirements / "schema-validation.json").write_text(
            json.dumps({"valid": True, "errors": []}), encoding="utf-8"
        )
        capability = work / "capability-preflight.json"
        capability.write_text(json.dumps({"status": "passed", "findings": []}), encoding="utf-8")
        validation = apply_dir / "validation-report.json"
        validation.write_text(json.dumps({
            "valid": True,
            "format_ready": True,
            "serialized_docx_valid": True,
            "output_policy": "submission",
            "compliance_mode": "full",
            "docx_fully_compliant": True,
            "output_docx": str(output),
            "property_receipt_audit": {
                "valid": True,
                "receipts": [{"serialized_docx_sha256": digest}],
            },
        }), encoding="utf-8")
        comparison = apply_dir / "format-comparison.json"
        comparison.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        manifest = work / "pipeline-manifest.json"
        manifest.write_text(json.dumps({
            "status": "completed",
            "compliance_mode": "full",
            "case_id": "fixture-case",
            "inputs": {"baseline_authority": "fallback_input_not_official"},
            "requirements_extraction": {"run_id": "fresh-run"},
            "code_fingerprint": runtime_code_fingerprint(),
            "output": str(output),
            "format_spec": str(format_spec),
            "capability_preflight": str(capability),
            "capability_preflight_status": "passed",
            "validation_report": str(validation),
            "format_comparison": str(comparison),
        }), encoding="utf-8")
        return {
            "returncode": 0,
            "fresh_run": {
                "run_id": "fresh-run",
                "cache_reused": False,
                "pipeline_manifest": str(manifest),
            },
        }

    def test_pre_render_receipts_are_bound_to_the_pre_render_docx(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source, validation, digest = self._pre_render_fixture(Path(td))
            pending_word = json.loads(validation.read_text(encoding="utf-8"))
            pending_word.update(submission_ready=False, submission_status="not_submission_ready")
            validation.write_text(json.dumps(pending_word), encoding="utf-8")
            checks, blockers = post_render._pre_render_checks(source, validation)
            self.assertEqual(blockers, [])
            self.assertEqual(checks["pre_render_artifact"]["sha256"], digest)

            validation.write_text(validation.read_text(encoding="utf-8").replace(digest, "0" * 64), encoding="utf-8")
            _checks, blockers = post_render._pre_render_checks(source, validation)
            self.assertIn("pre_render_property_receipt_artifact_hash_mismatch", blockers)

    def test_pre_render_rejects_draft_and_unresolved_inputs_even_if_format_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source, validation, _ = self._pre_render_fixture(Path(td))
            base = json.loads(validation.read_text(encoding="utf-8"))
            variants = (
                ({"output_policy": "review_draft"}, "pre_render_not_full_submission"),
                ({"compliance_mode": "supported_subset"}, "pre_render_not_full_submission"),
                ({"manual_review_findings": [{"id": "MR-1"}]}, "pre_render_manual_review_pending"),
                ({"manual_review_markers": [{"id": "MR-1"}]}, "pre_render_manual_review_pending"),
                ({"semantic_issue_confirmations": [{"clause_id": "C1"}]}, "pre_render_semantic_issues_pending"),
                ({"pending_content": [{"id": "C1"}]}, "pre_render_content_pending"),
                ({"docx_fully_compliant": False}, "pre_render_docx_not_fully_compliant"),
                ({"preview_placeholders": True}, "pre_render_preview_bypass_present"),
                ({"submission_ready": False, "submission_status": "manual_review_required"},
                 "pre_render_submission_blocked"),
                ({"submission_status": {"status": "passed"}},
                 "pre_render_submission_status_malformed"),
                ({"property_receipt_audit": {"valid": True, "receipts": [None]}},
                 "pre_render_property_receipts_malformed"),
            )
            for change, expected in variants:
                with self.subTest(change=change):
                    validation.write_text(json.dumps({**base, **change}), encoding="utf-8")
                    _, blockers = post_render._pre_render_checks(source, validation)
                    self.assertIn(expected, blockers)

    def test_post_render_outputs_cannot_alias_inputs_or_escape_case(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = root / "case"
            case.mkdir()
            source, validation, _ = self._pre_render_fixture(case)
            inputs = [case / name for name in ("spec.json", "official.docx", "official.json", "generated.json")]
            for path in inputs:
                path.write_text("{}", encoding="utf-8")
            base = [str(source), str(case / "final.docx"), str(case / "final.pdf"),
                    "--render-report", str(case / "render.json"),
                    "--visual-audit", str(case / "visual.json"),
                    "--format-spec", str(inputs[0]), "--pre-validation", str(validation),
                    "--submission-audit", str(case / "audit.json"),
                    "--format-comparison", str(case / "comparison.json"),
                    "--acceptance-out", str(case / "acceptance.json"),
                    "--official-template", str(inputs[1]),
                    "--official-style-map", str(inputs[2]),
                    "--generated-style-map", str(inputs[3])]
            for option, replacement in (
                ("--visual-audit", str(validation)),
                ("--format-comparison-markdown", str(root / "outside.md")),
            ):
                with self.subTest(option=option):
                    args = base.copy()
                    if option in args:
                        args[args.index(option) + 1] = replacement
                    else:
                        args.extend([option, replacement])
                    with self.assertRaises(SystemExit):
                        post_render.main(args)

    def test_pre_render_block_writes_explicit_stop_and_never_starts_word(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, validation, _ = self._pre_render_fixture(root)
            report = json.loads(validation.read_text(encoding="utf-8"))
            report["output_policy"] = "review_draft"
            validation.write_text(json.dumps(report), encoding="utf-8")
            inputs = [root / name for name in ("spec.json", "official.docx", "official.json", "generated.json")]
            for path in inputs:
                path.write_text("{}", encoding="utf-8")
            acceptance = root / "acceptance.json"
            args = [str(source), str(root / "final.docx"), str(root / "final.pdf"),
                    "--render-report", str(root / "render.json"),
                    "--visual-audit", str(root / "visual.json"),
                    "--format-spec", str(inputs[0]), "--pre-validation", str(validation),
                    "--submission-audit", str(root / "audit.json"),
                    "--format-comparison", str(root / "comparison.json"),
                    "--acceptance-out", str(acceptance),
                    "--official-template", str(inputs[1]),
                    "--official-style-map", str(inputs[2]),
                    "--generated-style-map", str(inputs[3])]
            with patch.object(post_render, "run_step") as run_step:
                self.assertEqual(post_render.main(args), 2)
            run_step.assert_not_called()
            result = json.loads(acceptance.read_text(encoding="utf-8"))
            self.assertFalse(result["submission_ready"])
            self.assertFalse(result["word_render_executed"])
            self.assertEqual(result["stopped_at"], "pre_render_validation")

    def test_batch_acceptance_allows_distinct_post_word_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case_result = self._batch_fixture(root)
            manifest_path = Path(case_result["fresh_run"]["pipeline_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pre_render = Path(manifest["output"])
            format_spec = Path(manifest["format_spec"])
            final = pre_render.with_name("final-word.docx")
            final_doc = Document()
            final_doc.add_paragraph("post-word")
            final_doc.save(final)
            final_digest = _sha256(final)
            acceptance = final.parent / "post-render-acceptance.json"
            comparison = final.parent / "final-format-comparison.json"
            pdf = final.parent / "final.pdf"
            _write_valid_pdf(pdf)
            render_report = final.parent / "word-render-report.json"
            render_report.write_text(json.dumps({
                "source_docx": {"path": str(final), "sha256": final_digest},
                "rendered_pdf": {"path": str(pdf), "sha256": _sha256(pdf)},
                "case_id": "fixture-case",
                "run_id": "fresh-run",
            }), encoding="utf-8")
            submission_audit = final.parent / "final-submission-audit.json"
            submission_audit.write_text(json.dumps({
                "submission_ready": True,
                "artifact": str(final),
                "render_validation": {
                    "rendered_verified": True,
                    "evidence": {
                        "source_docx_sha256": final_digest,
                        "rendered_pdf": {"sha256": _sha256(pdf)},
                    },
                },
            }), encoding="utf-8")
            markdown = final.parent / "FINAL-FORMAT-COMPARISON.md"
            markdown.write_text("passed\n", encoding="utf-8")
            visual_audit = final.parent / "pdf-visual-audit.json"
            visual_audit.write_text(json.dumps({
                "status": "passed",
                "pdf": {"path": str(pdf), "sha256": _sha256(pdf)},
            }), encoding="utf-8")
            comparison.write_text(json.dumps({
                "status": "passed",
                "inputs": {
                    "generated_docx": str(final),
                    "generated_docx_sha256": final_digest,
                    "format_spec": str(format_spec),
                    "official_template": None,
                    "official_template_sha256": None,
                },
            }), encoding="utf-8")
            acceptance.write_text(json.dumps({
                "status": "accepted",
                "blockers": [],
                "case_id": "fixture-case",
                "run_id": "fresh-run",
                "pre_render_docx_sha256": _sha256(pre_render),
                "post_render_docx": str(final),
                "post_render_docx_sha256": final_digest,
                "rendered_pdf": str(pdf),
                "rendered_pdf_sha256": _sha256(pdf),
                "render_report": str(render_report),
                "visual_audit": str(visual_audit),
                "submission_audit": str(submission_audit),
                "format_comparison": str(comparison),
            }), encoding="utf-8")
            manifest.update({
                "pre_render_output": str(pre_render),
                "post_render_status": "accepted",
                "post_render_acceptance": str(acceptance),
                "post_word_render": {
                    "pre_render_docx": str(pre_render),
                    "final_docx": str(final),
                    "pdf": str(pdf),
                    "render_report": str(render_report),
                    "visual_audit": str(visual_audit),
                    "submission_audit": str(submission_audit),
                    "format_comparison": str(comparison),
                    "format_comparison_markdown": str(markdown),
                    "requirements_only": True,
                },
                "output": str(final),
                "format_comparison": str(comparison),
            })
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            accepted = batch.case_acceptance(case_result, root=root)
            self.assertTrue(accepted["accepted"], accepted)
            self.assertNotEqual(_sha256(pre_render), final_digest)

    def test_batch_acceptance_rejects_final_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case_result = self._batch_fixture(root)
            manifest_path = Path(case_result["fresh_run"]["pipeline_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pre_render = Path(manifest["output"])
            final = pre_render.with_name("final-word.docx")
            final_doc = Document()
            final_doc.add_paragraph("post-word")
            final_doc.save(final)
            acceptance = final.parent / "post-render-acceptance.json"
            comparison = final.parent / "final-format-comparison.json"
            pdf = final.parent / "final.pdf"
            _write_valid_pdf(pdf)
            render_report = final.parent / "word-render-report.json"
            render_report.write_text(json.dumps({
                "source_docx": {"path": str(final), "sha256": _sha256(final)},
                "rendered_pdf": {"path": str(pdf), "sha256": _sha256(pdf)},
            }), encoding="utf-8")
            submission_audit = final.parent / "final-submission-audit.json"
            submission_audit.write_text(json.dumps({
                "submission_ready": True,
                "render_validation": {"rendered_verified": True},
            }), encoding="utf-8")
            markdown = final.parent / "FINAL-FORMAT-COMPARISON.md"
            markdown.write_text("passed\n", encoding="utf-8")
            visual_audit = final.parent / "pdf-visual-audit.json"
            visual_audit.write_text(json.dumps({
                "status": "passed",
                "pdf": {"path": str(pdf), "sha256": _sha256(pdf)},
            }), encoding="utf-8")
            comparison.write_text(json.dumps({
                "status": "passed",
                "inputs": {"generated_docx_sha256": _sha256(final)},
            }), encoding="utf-8")
            acceptance.write_text(json.dumps({
                "status": "accepted",
                "blockers": [],
                "pre_render_docx_sha256": _sha256(pre_render),
                "post_render_docx": str(final),
                "post_render_docx_sha256": "0" * 64,
                "rendered_pdf": str(pdf),
                "rendered_pdf_sha256": _sha256(pdf),
                "render_report": str(render_report),
                "visual_audit": str(visual_audit),
                "submission_audit": str(submission_audit),
                "format_comparison": str(comparison),
            }), encoding="utf-8")
            manifest.update({
                "pre_render_output": str(pre_render),
                "post_render_status": "accepted",
                "post_render_acceptance": str(acceptance),
                "post_word_render": {
                    "pre_render_docx": str(pre_render),
                    "final_docx": str(final),
                    "pdf": str(pdf),
                    "render_report": str(render_report),
                    "visual_audit": str(visual_audit),
                    "submission_audit": str(submission_audit),
                    "format_comparison": str(comparison),
                    "format_comparison_markdown": str(markdown),
                },
                "output": str(final),
                "format_comparison": str(comparison),
            })
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            accepted = batch.case_acceptance(case_result, root=root)
            self.assertFalse(accepted["accepted"])
            self.assertIn("post_render_acceptance_artifact_hash_mismatch", accepted["blockers"])


if __name__ == "__main__":
    unittest.main()
