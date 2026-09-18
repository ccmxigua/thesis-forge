from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from format_spec_validation import load_and_validate
from resource_registry import fixed_text_sha256, materialize_declaration_resources


class RunScopedResourceRegistryTest(unittest.TestCase):
    def _spec(self) -> dict:
        return {
            "schema_version": "1.0",
            "source_document": "current-requirements.docx",
            "status": "semantic_resolved",
            "roles": {},
            "requirements": [],
            "declarations": {
                "before_role": "document_start",
                "items": [{
                    "id": "custom_statement",
                    "heading": "本次输入的声明标题",
                    "body_parts": ["本次输入的第一段固定正文。", "本次输入的第二段固定正文。"],
                    "source_evidence_ids": ["E-current-1", "E-current-2"],
                    "signature_placeholders": [{
                        "role": "author", "label": "作者签名",
                        "attestation_scope": "placeholder_presence_only",
                    }],
                }],
            },
        }

    def test_same_source_text_gets_a_new_binding_for_each_run(self) -> None:
        first = materialize_declaration_resources(self._spec(), "run-one")
        second = materialize_declaration_resources(self._spec(), "run-two")
        first_item = first["declarations"]["items"][0]
        second_item = second["declarations"]["items"][0]
        self.assertNotEqual(first_item["resource_id"], second_item["resource_id"])
        self.assertNotEqual(first_item["version"], second_item["version"])
        self.assertEqual(first["resource_registry"]["run_id"], "run-one")
        self.assertEqual(second["resource_registry"]["run_id"], "run-two")
        self.assertEqual(
            first["resource_registry"]["items"][first_item["resource_id"]]["body_parts"],
            second["resource_registry"]["items"][second_item["resource_id"]]["body_parts"],
        )

    def test_arbitrary_current_run_resource_is_schema_valid(self) -> None:
        spec = materialize_declaration_resources(self._spec(), "run-schema")
        errors = load_and_validate(spec, ROOT / "schema" / "format-spec.schema.json")
        self.assertEqual(errors, [])

    def test_stale_registry_cannot_cross_run(self) -> None:
        spec = materialize_declaration_resources(self._spec(), "run-old")
        with self.assertRaisesRegex(ValueError, "does not match the current extraction run"):
            materialize_declaration_resources(copy.deepcopy(spec), "run-new")

    def test_same_run_reuse_revalidates_binding(self) -> None:
        spec = materialize_declaration_resources(self._spec(), "run-recheck")
        broken = copy.deepcopy(spec)
        resource_id = broken["declarations"]["items"][0]["resource_id"]
        broken["resource_registry"]["items"][resource_id]["body_parts"] = ["被篡改的固定正文"]
        with self.assertRaisesRegex(ValueError, "sha256 does not match"):
            materialize_declaration_resources(broken, "run-recheck")

    def test_fixed_text_digest_preserves_spaces_and_paragraph_boundaries(self) -> None:
        self.assertNotEqual(fixed_text_sha256("研究 成果"), fixed_text_sha256("研究成果"))
        self.assertNotEqual(fixed_text_sha256("第一段\n第二段"), fixed_text_sha256("第一段第二段"))


if __name__ == "__main__":
    unittest.main()
