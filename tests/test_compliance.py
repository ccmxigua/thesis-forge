from __future__ import annotations

import unittest

from scripts.compliance import annotate_satisfied_inputs, summarize


class SatisfiedInputComplianceTests(unittest.TestCase):
    def test_supplied_metadata_is_not_reported_as_missing(self) -> None:
        records = [{
            "clause_id": "C00001",
            "scope": "docx",
            "status": "requires_metadata",
            "reason": "metadata required",
            "requirement_ids": [],
            "evidence_ids": ["E1"],
        }]
        capability = [{
            "clause_id": "C00001",
            "category": "supported",
            "metadata_fields": ["classification_number"],
            "reason": "模板字段已由输入元数据合同提供：classification_number",
        }]

        annotated = annotate_satisfied_inputs(records, capability)

        self.assertEqual(annotated[0]["status"], "input_provided_unverified")
        self.assertEqual(annotated[0]["source_prerequisite_status"], "requires_metadata")
        self.assertEqual(annotated[0]["metadata_fields"], ["classification_number"])
        summary = summarize(annotated, "supported_subset", "validation")
        self.assertEqual(summary["docx_compliance"]["counts"], {"input_provided_unverified": 1})
        self.assertEqual(summary["docx_compliance"]["blocking_clause_ids"], ["C00001"])

    def test_unsupplied_metadata_remains_requires_metadata(self) -> None:
        records = [{
            "clause_id": "C00044",
            "scope": "docx",
            "status": "requires_metadata",
            "reason": "metadata required",
            "requirement_ids": [],
            "evidence_ids": ["E44"],
        }]

        annotated = annotate_satisfied_inputs(records, [])

        self.assertEqual(annotated[0]["status"], "requires_metadata")


if __name__ == "__main__":
    unittest.main()
