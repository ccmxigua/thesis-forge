from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from profile_confirmation_migration import (
    CONFIRMATION_SCOPE,
    POLICY,
    file_record,
    profile_semantic_projection,
    validate_profile_confirmation_migration,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ProfileConfirmationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.parent_work = self.root / "parent-run"
        self.parent_review = self.parent_work / "review" / "requirements"
        self.parent_review.mkdir(parents=True)
        self.source_tex = self.root / "sample.tex"
        self.source_tex.write_text("\\title{Confirmed sample}\n", encoding="utf-8")
        self.source_docx = self.root / "fixed-source.docx"
        self.source_docx.write_bytes(b"fixed docx bytes")
        self.requirements = self.root / "bsu-requirements.docx"
        self.requirements.write_bytes(b"fixed requirements bytes")
        self.run_id = "parent-run-001"
        self.parent_profile_path = self.parent_work / "thesis-profile.json"
        self.parent_profile = {
            "schema_version": "1.0",
            "degree_level": "master",
            "degree_category": "academic",
            "writing_language": "zh",
            "student_id": "EXAMPLE-1",
            "cover_metadata": {
                "trust": {"source": "source_document", "confirmed": True, "note": "from source"},
                "title_zh": "示例论文",
            },
            "provenance": {
                "source_kind": "semantic_metadata",
                "source_document": str(self.source_tex.resolve()),
                "normalizer": "extract_semantic_metadata.normalize_semantic_metadata",
                "trust": {"source": "source_document", "confirmed": True, "note": "from source"},
                "field_sources": {"degree_level": "degree_level"},
                "source_sha256": hashlib.sha256(self.source_tex.read_bytes()).hexdigest(),
            },
        }
        write_json(self.parent_profile_path, self.parent_profile)
        self.confirmed_profile = copy.deepcopy(self.parent_profile)
        self.confirmed_profile["provenance"].update({
            "source_document": str(self.source_docx.resolve()),
            "source_sha256": hashlib.sha256(self.source_docx.read_bytes()).hexdigest(),
            "trust": {
                "source": "user_confirmed", "confirmed": True,
                "note": "User confirmed values apply to fixed DOCX.",
            },
        })
        self.confirmed_profile["cover_metadata"]["trust"] = {
            "source": "user_confirmed", "confirmed": True,
            "note": "User confirmed values apply to fixed DOCX.",
        }
        self.confirmed_path = self.root / "confirmed-profile.json"
        write_json(self.confirmed_path, self.confirmed_profile)
        self.parent_request_path = self.parent_review / "llm-request.json"
        runtime = {
            "confirmed_thesis_profile": self.parent_profile,
            "thesis_profile_sha256": hashlib.sha256(self.parent_profile_path.read_bytes()).hexdigest(),
            "runtime_inventory": {"anchor_inventory": {"source": file_record(self.source_docx)}},
        }
        write_json(self.parent_request_path, {
            "provenance": {"run_id": self.run_id}, "runtime_context": runtime,
        })
        self.parent_receipt_path = self.parent_review / "merge-receipt.json"
        self.parent_receipt_path.write_text("{}\n", encoding="utf-8")
        write_json(self.parent_work / "pipeline-manifest.json", {
            "run_id": self.run_id,
            "pipeline_level": "latex_end_to_end",
            "inputs": {
                "source": file_record(self.source_tex),
                "requirements": file_record(self.requirements),
                "thesis_profile": file_record(self.parent_profile_path),
            },
            "intermediate_docx": file_record(self.source_docx),
            "steps": [{
                "name": "latex_to_docx_reuse", "returncode": 0, "reused": True,
                "artifact": file_record(self.source_docx),
            }],
        })
        write_json(self.parent_review / "extraction-manifest.json", {
            "sources": {
                "structure_source": file_record(self.source_docx),
                "requirements_source": file_record(self.requirements),
            },
        })
        self.confirmation_path = self.root / "confirmation.json"
        self.write_confirmation()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_confirmation(self) -> None:
        write_json(self.confirmation_path, {
            "policy": POLICY,
            "confirmation_scope": CONFIRMATION_SCOPE,
            "approval_reference": {"statement": "approved"},
            "parent_profile": file_record(self.parent_profile_path),
            "parent_pipeline_manifest": file_record(self.parent_work / "pipeline-manifest.json"),
            "source_tex": file_record(self.source_tex),
            "confirmed_profile": file_record(self.confirmed_path),
            "source_docx": file_record(self.source_docx),
            "requirements_docx": file_record(self.requirements),
            "semantic_values_sha256": digest(profile_semantic_projection(self.parent_profile)),
        })

    def validate(self) -> dict:
        return validate_profile_confirmation_migration(
            parent_receipt_path=self.parent_receipt_path,
            current_source_path=self.source_docx,
            requirements_path=self.requirements,
            confirmed_profile_path=self.confirmed_path,
            confirmation_record_path=self.confirmation_path,
            expected_run_id=self.run_id,
        )

    def test_accepts_exactly_provenance_only_confirmation_projection(self) -> None:
        result = self.validate()
        self.assertTrue(result["semantic_values_identical"])
        self.assertFalse(result["model_request_made"])
        self.assertFalse(result["submission_ready"])
        self.assertEqual(result["review_request_profile"], "verified_parent_profile_unchanged")
        self.assertEqual(result["generation_profile"], "user_confirmed_profile")

    def test_rejects_semantic_profile_field_change(self) -> None:
        self.confirmed_profile["degree_level"] = "doctor"
        write_json(self.confirmed_path, self.confirmed_profile)
        self.write_confirmation()
        with self.assertRaisesRegex(ValueError, "changes semantic profile fields"):
            self.validate()

    def test_rejects_source_field_map_change(self) -> None:
        self.confirmed_profile["provenance"]["field_sources"]["degree_level"] = "other_field"
        write_json(self.confirmed_path, self.confirmed_profile)
        self.write_confirmation()
        with self.assertRaisesRegex(ValueError, "changes semantic profile fields"):
            self.validate()

    def test_rejects_confirmed_profile_bound_to_another_source(self) -> None:
        self.confirmed_profile["provenance"]["source_sha256"] = "0" * 64
        write_json(self.confirmed_path, self.confirmed_profile)
        self.write_confirmation()
        with self.assertRaisesRegex(ValueError, "not user-confirmed and bound"):
            self.validate()

    def test_rejects_unconfirmed_trust_source(self) -> None:
        self.confirmed_profile["provenance"]["trust"]["source"] = "source_document"
        write_json(self.confirmed_path, self.confirmed_profile)
        self.write_confirmation()
        with self.assertRaisesRegex(ValueError, "not user-confirmed and bound"):
            self.validate()

    def test_rejects_profile_hash_replacement_in_confirmation_record(self) -> None:
        value = json.loads(self.confirmation_path.read_text())
        value["parent_profile"]["sha256"] = "f" * 64
        write_json(self.confirmation_path, value)
        with self.assertRaisesRegex(ValueError, "confirmation parent profile record mismatch"):
            self.validate()

    def test_rejects_source_docx_replacement_in_confirmation_record(self) -> None:
        value = json.loads(self.confirmation_path.read_text())
        value["source_docx"]["sha256"] = "f" * 64
        write_json(self.confirmation_path, value)
        with self.assertRaisesRegex(ValueError, "confirmation fixed source DOCX record mismatch"):
            self.validate()

    def test_rejects_sample_tex_replacement_in_confirmation_record(self) -> None:
        value = json.loads(self.confirmation_path.read_text())
        value["source_tex"]["sha256"] = "f" * 64
        write_json(self.confirmation_path, value)
        with self.assertRaisesRegex(ValueError, "confirmation sample TEX record mismatch"):
            self.validate()

    def test_rejects_parent_pipeline_manifest_replacement_in_confirmation_record(self) -> None:
        value = json.loads(self.confirmation_path.read_text())
        value["parent_pipeline_manifest"]["sha256"] = "f" * 64
        write_json(self.confirmation_path, value)
        with self.assertRaisesRegex(ValueError, "confirmation parent pipeline manifest record mismatch"):
            self.validate()

    def test_rejects_unbound_conversion_step(self) -> None:
        path = self.parent_work / "pipeline-manifest.json"
        pipeline = json.loads(path.read_text())
        pipeline["steps"][0]["artifact"] = file_record(self.requirements)
        write_json(path, pipeline)
        self.write_confirmation()
        with self.assertRaisesRegex(ValueError, "parent conversion-step DOCX binding mismatch"):
            self.validate()

    def test_rejects_missing_conversion_step(self) -> None:
        path = self.parent_work / "pipeline-manifest.json"
        pipeline = json.loads(path.read_text())
        pipeline["steps"] = []
        write_json(path, pipeline)
        self.write_confirmation()
        with self.assertRaisesRegex(ValueError, "exactly one source TEX-to-DOCX step"):
            self.validate()

    def test_rejects_confirmation_requirements_hash_replacement(self) -> None:
        value = json.loads(self.confirmation_path.read_text())
        value["requirements_docx"]["sha256"] = "f" * 64
        write_json(self.confirmation_path, value)
        with self.assertRaisesRegex(ValueError, "confirmation requirements DOCX record mismatch"):
            self.validate()

    def test_rejects_parent_request_profile_mismatch(self) -> None:
        request = json.loads(self.parent_request_path.read_text())
        request["runtime_context"]["confirmed_thesis_profile"]["degree_level"] = "doctor"
        write_json(self.parent_request_path, request)
        with self.assertRaisesRegex(ValueError, "differs from the profile embedded"):
            self.validate()

    def test_rejects_parent_structure_docx_mismatch(self) -> None:
        other = self.root / "other.docx"
        other.write_bytes(b"other docx")
        extraction_path = self.parent_review / "extraction-manifest.json"
        extraction = json.loads(extraction_path.read_text())
        extraction["sources"]["structure_source"] = file_record(other)
        write_json(extraction_path, extraction)
        with self.assertRaisesRegex(ValueError, "parent structure DOCX binding mismatch"):
            self.validate()

    def test_rejects_confirmation_semantic_digest_mismatch(self) -> None:
        value = json.loads(self.confirmation_path.read_text())
        value["semantic_values_sha256"] = "0" * 64
        write_json(self.confirmation_path, value)
        with self.assertRaisesRegex(ValueError, "semantic-values digest mismatch"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
