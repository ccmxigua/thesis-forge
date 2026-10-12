from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import thesis_format_pipeline as pipeline  # noqa: E402
import apply_format_spec  # noqa: E402
from format_spec_validation import load_and_validate  # noqa: E402
from manual_review import build_manual_review_ledger  # noqa: E402


class TemplateConflictBindingTests(unittest.TestCase):
    template_sha = "a" * 64
    artifact_sha = "d" * 64
    template_path = "/inputs/official-template.docx"
    artifact_path = "/run/requirements/template-evidence.json"
    source_text = "图题应使用黑体五号"

    def setUp(self) -> None:
        self.clauses = [{
            "id": "C00001", "text": self.source_text, "evidence_ids": ["E00001"],
        }]
        self.evidence_doc = {"evidence": [{"id": "E00001", "text": self.source_text}]}
        self.template_item = {
            "id": "TE00001", "category": "semantic_style", "semantic_role": "figure_caption",
            "property": "style_properties", "value": {"font": {"cjk": "宋体"}},
            "direct_semantic": True,
            "provenance": {
                "source_kind": "official_template_docx", "source_path": self.template_path,
                "source_sha256": self.template_sha,
            },
        }
        self.template_evidence = {
            "schema_version": "1.0", "status": "extracted",
            "source": {
                "kind": "official_template_docx", "path": self.template_path,
                "bytes": 123, "sha256": self.template_sha,
            },
            "items": [copy.deepcopy(self.template_item)],
        }
        self.official_template_record = {
            "path": self.template_path, "bytes": 123, "sha256": self.template_sha,
        }
        self.artifact_record = {"path": self.artifact_path, "bytes": 456, "sha256": self.artifact_sha}
        self.question = {
            "id": "TQ0001", "source_type": "template_reconciliation_conflict",
            "clause_id": "C00001", "requirement_id": "R00001",
            "source_text": self.source_text,
            "question": "书面要求与官方模板冲突，请人工确认。",
            "candidate_roles": ["figure_caption"], "property": "font.cjk",
            "written_value": "黑体", "official_template_values": ["宋体"],
            "evidence_ids": ["E00001"], "written_evidence_ids": ["E00001"],
            "template_evidence_ids": ["TE00001"],
        }

    @staticmethod
    def binding() -> dict:
        return {
            "case_id": "tju-test", "run_id": "run-1",
            "source_sha256": "1" * 64, "clause_sha256": "2" * 64,
            "evidence_sha256": "3" * 64, "request_sha256": None,
            "requirements_sha256": "4" * 64, "input_source_sha256": "5" * 64,
            "format_spec_sha256": "6" * 64,
            "official_template_sha256": "a" * 64,
            "official_template_source": "style_template",
        }

    def build(self, question=None, *, template_evidence=None, official=None):
        return pipeline._template_conflict_release_gates(
            [copy.deepcopy(question or self.question)],
            clauses=self.clauses,
            evidence_doc=self.evidence_doc,
            template_evidence=copy.deepcopy(
                self.template_evidence if template_evidence is None else template_evidence
            ),
            official_template_record=copy.deepcopy(
                self.official_template_record if official is None else official
            ),
            template_evidence_artifact_record=copy.deepcopy(self.artifact_record),
        )

    def test_cross_source_conflict_becomes_review_gate_with_typed_evidence(self) -> None:
        remaining, gates = self.build()
        self.assertEqual(remaining, [])
        self.assertEqual(len(gates), 1)
        gate = gates[0]
        self.assertEqual(gate["category"], "semantic_content_review")
        self.assertEqual(gate["clause_ids"], ["C00001"])
        self.assertEqual(gate["evidence_ids"], ["E00001"])
        self.assertEqual(gate["template_evidence_ids"], ["TE00001"])
        self.assertEqual(gate["template_source_sha256"], self.template_sha)
        self.assertEqual(gate["template_evidence_artifact_sha256"], self.artifact_sha)

        ledger = build_manual_review_ledger(
            {}, [], binding=self.binding(), release_gates=gates,
        )
        self.assertFalse(ledger["submission_ready"])
        self.assertEqual(ledger["items"][0]["evidence_ids"], ["E00001"])
        self.assertEqual(ledger["items"][0]["template_evidence_ids"], ["TE00001"])
        self.assertEqual(ledger["obligation_crosswalk"]["entries"][0]["template_evidence_ids"], ["TE00001"])
        self.assertEqual(
            load_and_validate(ledger, ROOT / "schema" / "manual-review-ledger.schema.json"),
            [],
        )
        conflict = {
            "type": "official_template_conflict", "clause_ids": ["C00001"],
            "requirement_id": "R00001", "role": "figure_caption",
            "property": "font.cjk", "written_value": "黑体",
            "official_template_values": ["宋体"], "official_template_evidence_ids": ["TE00001"],
        }
        spec = {"run_id": "run-1", "blocking_errors": [conflict]}
        self.assertNotIn("blocking_errors", pipeline.requirement_blockers(
            spec, [], "supported_subset", review_draft_manual_ledger=ledger,
            allow_source_bound_review_draft_conflicts=True,
        ))
        tampered = copy.deepcopy(ledger)
        tampered["items"][0]["template_source_sha256"] = "b" * 64
        self.assertIn("blocking_errors", pipeline.requirement_blockers(
            spec, [], "supported_subset", review_draft_manual_ledger=tampered,
            allow_source_bound_review_draft_conflicts=True,
        ))

    def test_stale_source_mixed_ids_wrong_values_and_missing_evidence_fail_closed(self) -> None:
        changes = [
            ("written evidence not bound", {"evidence_ids": ["E-foreign"], "written_evidence_ids": ["E-foreign"]}, None, None),
            ("wrong template role", {"candidate_roles": ["body"]}, None, None),
            ("wrong template value", {"official_template_values": ["黑体"]}, None, None),
            ("missing template evidence", {"template_evidence_ids": ["TE-missing"]}, None, None),
            ("stale template bytes", None, None, {**self.official_template_record, "sha256": "b" * 64}),
        ]
        for label, change, template_evidence, official in changes:
            with self.subTest(label=label), self.assertRaises(ValueError):
                question = copy.deepcopy(self.question)
                if change:
                    question.update(change)
                self.build(question, template_evidence=template_evidence, official=official)

    def test_untyped_template_ids_and_non_exact_source_clause_are_rejected(self) -> None:
        untyped = copy.deepcopy(self.question)
        untyped.pop("source_type")
        with self.assertRaisesRegex(ValueError, "untyped"):
            self.build(untyped)
        wrong_clause_text = copy.deepcopy(self.question)
        wrong_clause_text["source_text"] = "不同的书面条款"
        with self.assertRaisesRegex(ValueError, "source_text differs"):
            self.build(wrong_clause_text)

    def test_untyped_questions_pass_through_without_external_evidence_claim(self) -> None:
        ordinary = {"id": "Q1", "clause_id": "C00001", "source_text": self.source_text,
                    "evidence_ids": ["E00001"]}
        remaining, gates = pipeline._template_conflict_release_gates(
            [ordinary], clauses=self.clauses, evidence_doc=self.evidence_doc,
            template_evidence=None, official_template_record=None,
            template_evidence_artifact_record=None,
        )
        self.assertEqual(remaining, [ordinary])
        self.assertEqual(gates, [])

    def test_rule_only_manual_binding_uses_current_input_sha_as_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            requirements = root / "requirements.docx"
            source = root / "source.docx"
            clauses = root / "clauses.json"
            evidence = root / "evidence.json"
            context = root / "evidence-context.json"
            extraction = root / "extraction-manifest.json"
            spec_path = root / "format-spec.json"
            requirements.write_bytes(b"requirements")
            source.write_bytes(b"source document")
            clauses.write_text('[{"id":"C1","text":"x","evidence_ids":["E1"]}]', encoding="utf-8")
            evidence.write_text('{"evidence":[{"id":"E1","text":"x"}]}', encoding="utf-8")
            context.write_text('{"evidence_context":[]}', encoding="utf-8")
            spec_path.write_text('{"run_id":"run-1"}', encoding="utf-8")

            def record(path: Path) -> dict:
                payload = path.read_bytes()
                return {"path": str(path.resolve()), "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}

            req_record = record(requirements)
            source_record = record(source)
            clauses_record = record(clauses)
            evidence_record = record(evidence)
            context_record = record(context)
            extraction.write_text(json.dumps({
                "status": "completed", "cache_reused": False, "run_id": "run-1",
                "source_sha256": req_record["sha256"],
                "requirement_clauses_sha256": clauses_record["sha256"],
                "document_evidence_sha256": evidence_record["sha256"],
                "evidence_sha256": context_record["sha256"],
                "semantic_review_provenance": None,
            }), encoding="utf-8")
            manifest = {
                "case_id": "case-1", "run_id": "run-1",
                "inputs": {"requirements": req_record, "source": source_record,
                           "official_template_evidence": None,
                           "official_template_source": "not_supplied"},
            }
            args = Namespace(
                source_clauses=clauses, source_evidence=evidence,
                source_evidence_context=context, source_extraction_manifest=extraction,
                official_template=None, case_id="case-1", format_spec=spec_path,
            )
            binding, errors = apply_format_spec._current_manual_review_binding(
                args, {"run_id": "run-1"}, manifest,
            )
            self.assertEqual(errors, [])
            self.assertIsNotNone(binding)
            self.assertEqual(binding["source_sha256"], source_record["sha256"])
            self.assertEqual(binding["requirements_sha256"], req_record["sha256"])


if __name__ == "__main__":
    unittest.main()
