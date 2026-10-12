from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import visual_review_protocol as protocol  # noqa: E402


class VisualPageReviewRegressionTests(unittest.TestCase):
    def test_synthetic_case_preserves_false_positive_limits(self) -> None:
        case = json.loads((ROOT / "tests/fixtures/visual-review/synthetic-protocol-regression.json").read_text())
        self.assertEqual(case["page_count"], 3)
        self.assertEqual(case["renderer_scope"], "lo_preview_only")
        self.assertEqual(case["render_validation"], "not_run")
        observations = {row["physical_page"]: row for row in case["observed_candidates"]}
        self.assertEqual(observations[1]["certainty"], "synthetic")
        self.assertEqual(observations[3]["certainty"], "cause_unknown")
        self.assertIn("not automatically a defect", case["non_defect_or_unproven_candidates"][0]["rule"])
        self.assertIn("no evidence about any real document", case["coverage_limit"])

    def test_unresolved_source_map_blocks_even_when_other_checks_look_clear(self) -> None:
        page = {
            "page_number": 1,
            "page_status": "observations_recorded",
            "page_map": {"source_mapping_status": "unknown"},
            "request_image_receipt": {"status": "provider_confirmed_exact_images"},
            "issues": [],
            "unresolved_checklist_items": [],
        }
        status, gate, blockers = protocol.review_status(
            page_count=1, pages=[page],
            scope={"scope": "word_delivery", "word_attestation_present": True},
        )
        self.assertEqual((status, gate), ("unknown", "blocked"))
        self.assertIn("page_source_mapping_unknown:1", blockers)

    def test_structured_provider_receipt_contract_and_renderer_scope_are_separate(self) -> None:
        page = {
            "page_number": 1,
            "page_status": "observations_recorded",
            "page_map": {"source_mapping_status": "resolved"},
            "request_image_receipt": {"status": "provider_confirmed_exact_images"},
            "issues": [],
            "unresolved_checklist_items": [],
        }
        status, gate, blockers = protocol.review_status(
            page_count=1, pages=[page],
            scope={"scope": "word_delivery", "word_attestation_present": True},
        )
        self.assertEqual((status, gate, blockers), ("passed", "passed", []))
        lo_scope = {"scope": "lo_preview", "word_attestation_present": False}
        self.assertEqual(protocol.renderer_status_projection(lo_scope, "passed"), {
            "lo_preview_visual_status": "passed",
            "word_delivery_visual_status": "not_run",
        })
        self.assertEqual(protocol.renderer_status_projection(
            {"scope": "word_delivery"}, "passed"
        )["lo_preview_visual_status"], "not_run")

    def test_page_map_rejects_parent_hash_substitution_and_unordered_pages(self) -> None:
        base = {
            "schema_version": "1.0", "source_docx_sha256": "a" * 64, "pdf_sha256": "b" * 64,
            "pages": [{
                "physical_pdf_page": 1, "section_id": "source-section-1",
                "chapter_or_appendix_anchor": "heading-anchor-1", "page_type": "body",
                "source_clause_ids": ["C1"],
                "applicability": {"contents_entries_and_targets": False},
            }],
        }
        rows = protocol.validate_page_map(
            base, docx_sha256="a" * 64, pdf_sha256="b" * 64,
            page_count=1, allowed_clause_ids={"C1"},
        )
        self.assertEqual(rows[0]["source_mapping_status"], "resolved")
        with self.assertRaisesRegex(ValueError, "hash binding"):
            protocol.validate_page_map(
                base, docx_sha256="c" * 64, pdf_sha256="b" * 64,
                page_count=1, allowed_clause_ids={"C1"},
            )
        base["pages"][0]["physical_pdf_page"] = 2
        with self.assertRaisesRegex(ValueError, "1..N"):
            protocol.validate_page_map(
                base, docx_sha256="a" * 64, pdf_sha256="b" * 64,
                page_count=1, allowed_clause_ids={"C1"},
            )

    def test_checklist_requires_all_items_reason_and_bound_image(self) -> None:
        valid = [{
            "item_id": item_id,
            "status": "not_applicable" if item_id == "contents_entries_and_targets" else "pass",
            "observation": "Checked this page item.",
            "reason": "No contents page by source map." if item_id == "contents_entries_and_targets" else None,
            "evidence_image_ids": ["p0001-full"],
        } for item_id in protocol.CHECKLIST_ITEMS]
        protocol.validate_checklist(valid, allowed_image_ids={"p0001-full"})
        with self.assertRaisesRegex(ValueError, "every required"):
            protocol.validate_checklist(valid[:-1], allowed_image_ids={"p0001-full"})
        missing_reason = [dict(row) for row in valid]
        missing_reason[-1]["reason"] = None
        with self.assertRaisesRegex(ValueError, "requires a reason"):
            protocol.validate_checklist(missing_reason, allowed_image_ids={"p0001-full"})
        unbound = [dict(row) for row in valid]
        unbound[0]["evidence_image_ids"] = ["other-page-image"]
        with self.assertRaisesRegex(ValueError, "attached full-page or crop"):
            protocol.validate_checklist(unbound, allowed_image_ids={"p0001-full"})

    def test_issue_cannot_close_on_same_pdf_or_without_full_rerender_review(self) -> None:
        finding = {
            "finding_id": "VRF-test-0001", "physical_pdf_page": 1,
            "evidence_image_ids": ["p0001-full"], "status": "closed_after_rerender",
            "finding": "A visible table rule was corrected.", "severity": "medium",
            "reviewer_claim": "reviewer", "disposition_reason": "Confirmed after rerender.",
            "source_clause_ids": [],
            "closure": {
                "prior_pdf_sha256": "a" * 64, "rerender_pdf_sha256": "a" * 64,
                "new_review_run_id": "run-new", "new_full_page_coverage": True,
                "source_finding_id": "VRF-test-0001",
                "source_report": {"path": "/tmp/old-report.json", "bytes": 1, "sha256": "c" * 64},
            },
        }
        ledger = {
            "schema_version": "1.0", "run_id": "run-new",
            "reviewed_pdf_sha256": "a" * 64, "findings": [finding],
        }
        with self.assertRaisesRegex(ValueError, "new fully reviewed rerender"):
            protocol.validate_issue_ledger(
                ledger, run_id="run-new", current_pdf_sha256="a" * 64,
                page_count=1, source_clause_ids=set(), current_run_full_coverage=True,
            )
        finding["closure"]["rerender_pdf_sha256"] = "b" * 64
        ledger["reviewed_pdf_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "new fully reviewed rerender"):
            protocol.validate_issue_ledger(
                ledger, run_id="run-new", current_pdf_sha256="b" * 64,
                page_count=1, source_clause_ids=set(), current_run_full_coverage=False,
            )

    def test_medium_intentional_disposition_requires_clause_and_human_reason(self) -> None:
        base = {
            "finding_id": "VRF-test-0001", "physical_pdf_page": 1,
            "evidence_image_ids": ["p0001-full"], "status": "intentional_with_source_basis",
            "finding": "A mild, visible layout variation.", "severity": "medium",
            "reviewer_claim": "reviewer claim", "disposition_reason": "The cited source clause permits this presentation.",
            "source_clause_ids": ["C1"], "closure": None,
        }
        ledger = {"schema_version": "1.0", "run_id": "run-1",
                  "reviewed_pdf_sha256": "a" * 64, "findings": [base]}
        protocol.validate_issue_ledger(
            ledger, run_id="run-1", current_pdf_sha256="a" * 64,
            page_count=1, source_clause_ids={"C1"}, current_run_full_coverage=True,
            allowed_image_ids_by_page={1: {"p0001-full"}},
        )
        high = json.loads(json.dumps(ledger))
        high["findings"][0]["severity"] = "high"
        with self.assertRaisesRegex(ValueError, "critical/high"):
            protocol.validate_issue_ledger(
                high, run_id="run-1", current_pdf_sha256="a" * 64,
                page_count=1, source_clause_ids={"C1"}, current_run_full_coverage=True,
                allowed_image_ids_by_page={1: {"p0001-full"}},
            )

    def test_closed_finding_projection_is_bound_to_prior_report_and_code(self) -> None:
        fingerprint = {"visual_page_review.py": "code-hash"}
        prior_pdf = "a" * 64
        source_clause_hash = "c" * 64
        issue = {
            "finding_id": "VRF-old-0001", "finding": "Header overlaps the running title.",
            "severity": "medium", "status": "open", "evidence_image_ids": ["p0001-full"],
            "source_clause_ids": ["C1"],
        }
        source = {
            "protocol": protocol.PROTOCOL, "run_id": "run-old",
            "code_fingerprint": fingerprint, "code_fingerprint_sha256": "b" * 64,
            "source_clauses": {"sha256": source_clause_hash}, "pdf": {"sha256": prior_pdf},
            "pages": [{"page_number": 1, "issues": [issue]}],
        }
        carried = {
            "finding_id": "VRF-old-0001", "physical_pdf_page": 1,
            "evidence_image_ids": ["p0001-full"], "finding": issue["finding"],
            "severity": "medium", "source_clause_ids": ["C1"],
            "closure": {"prior_pdf_sha256": prior_pdf},
        }
        kwargs = {
            "current_run_id": "run-new", "current_code_fingerprint": fingerprint,
            "current_code_fingerprint_sha256": "b" * 64,
            "current_source_clauses_sha256": source_clause_hash,
        }
        protocol.validate_closed_finding_source(carried, source, **kwargs)
        for mutation in ("finding", "page", "code", "clauses", "duplicate"):
            damaged = json.loads(json.dumps(source))
            row = json.loads(json.dumps(carried))
            if mutation == "finding":
                row["finding"] = "different text"
            elif mutation == "page":
                row["physical_pdf_page"] = 2
            elif mutation == "code":
                damaged["code_fingerprint_sha256"] = "d" * 64
            elif mutation == "clauses":
                damaged["source_clauses"]["sha256"] = "e" * 64
            elif mutation == "duplicate":
                damaged["pages"].append({"page_number": 2, "issues": [dict(issue)]})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                protocol.validate_closed_finding_source(row, damaged, **kwargs)


if __name__ == "__main__":
    unittest.main()
