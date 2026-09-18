from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from profile_bootstrap import build_profile  # noqa: E402
from region_graph import compile_region_graph  # noqa: E402


ROLE_ORDER = [
    "cover", "cover_en", "declarations", "defense_committee", "abstract_zh",
    "abstract_en", "toc_zh", "toc_en", "body", "acknowledgments",
    "references", "appendices", "academic_outputs",
]
TEXTS = [
    "封面", "COVER", "声明", "答辩委员会", "摘要", "Abstract", "目录", "CONTENTS",
    "1 引言", "致 谢", "参考文献", "附录A 标题", "攻读硕士学位期间的学术成果",
]


class ProfileBootstrapTests(unittest.TestCase):
    def _inputs(self, temp: Path):
        official = temp / "official.docx"
        document = Document()
        candidates = []
        for index, text in enumerate(TEXTS):
            document.add_paragraph(text)
            candidates.append({
                "candidate_index": index,
                "text": text,
                "body_child_index": index,
                "section_index": 0,
                "style_id": None,
            })
        document.save(official)
        packet = temp / "candidates.json"
        packet.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
        roles = temp / "roles.json"
        roles.write_text(json.dumps({
            "role_order": ROLE_ORDER,
            "candidate_indexes": list(range(len(ROLE_ORDER))),
        }), encoding="utf-8")
        required = temp / "required.json"
        required.write_text(json.dumps({"regions": [
            {"role": "body", "source_required": True},
            {"role": "acknowledgments", "source_required": True},
            {"role": "references", "source_required": True},
            {"role": "appendices", "source_required": False},
            {"role": "academic_outputs", "source_required": True},
        ]}), encoding="utf-8")
        return official, packet, roles, required

    def _build(self, temp: Path, roles: Path | None = None):
        official, packet, default_roles, required = self._inputs(temp)
        output = temp / "profile"
        audit = build_profile(
            official_docx=official, candidates_path=packet,
            role_selection_path=roles or default_roles,
            required_selection_path=required, output_dir=output,
            profile_id="test-clean-profile", title="Test", organization="Test Org",
            source_url="https://example.test/template", effective_version="1",
        )
        return output, audit

    def test_build_separates_stable_audit_selectors_from_precise_region_selectors(self):
        with tempfile.TemporaryDirectory() as raw:
            output, audit = self._build(Path(raw))
            profile = json.loads((output / "profile.json").read_text(encoding="utf-8"))
            compiled = compile_region_graph(
                profile, template_path=output / "resources" / "official-template.docx"
            )

        acknowledgment = next(
            item for item in profile["structure"]["ordered_roles"]
            if item["role"] == "acknowledgments"
        )
        appendix = next(
            item for item in profile["structure"]["ordered_roles"]
            if item["role"] == "appendices"
        )
        region = next(item for item in profile["regions"]["nodes"] if item["id"] == "acknowledgments")
        self.assertNotIn("body_child_index", acknowledgment["selector"])
        self.assertNotIn("section_index", acknowledgment["selector"])
        self.assertIn("body_child_index", region["start_selector"])
        self.assertFalse(appendix["required"])
        self.assertEqual(appendix["conditional"], "source_role.appendices.present")
        self.assertEqual(compiled["status"], "compiled")
        self.assertEqual(compiled["findings"], [])
        self.assertEqual(audit["status"], "generated")

    def test_build_rejects_invented_candidate_index(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            official, packet, roles, required = self._inputs(temp)
            payload = json.loads(roles.read_text(encoding="utf-8"))
            payload["candidate_indexes"][-1] = 999
            roles.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invented candidate indexes"):
                build_profile(
                    official_docx=official, candidates_path=packet,
                    role_selection_path=roles, required_selection_path=required,
                    output_dir=temp / "out", profile_id="test-clean-profile",
                    title="Test", organization="Test Org",
                    source_url="https://example.test/template", effective_version="1",
                )

    def test_build_can_exclude_non_dynamic_template_role_from_output_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            official, packet, roles, required = self._inputs(temp)
            payload = json.loads(roles.read_text(encoding="utf-8"))
            payload["excluded_output_roles"] = ["toc_en"]
            roles.write_text(json.dumps(payload), encoding="utf-8")
            output = temp / "profile"
            audit = build_profile(
                official_docx=official, candidates_path=packet,
                role_selection_path=roles, required_selection_path=required,
                output_dir=output, profile_id="test-clean-profile", title="Test",
                organization="Test Org", source_url="https://example.test/template",
                effective_version="1",
            )
            profile = json.loads((output / "profile.json").read_text(encoding="utf-8"))

        self.assertNotIn("toc_en", [item["role"] for item in profile["structure"]["ordered_roles"]])
        self.assertEqual(audit["excluded_output_roles"], ["toc_en"])

    def test_build_rejects_excluding_dynamic_assembly_role(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            official, packet, roles, required = self._inputs(temp)
            payload = json.loads(roles.read_text(encoding="utf-8"))
            payload["excluded_output_roles"] = ["references"]
            roles.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dynamic assembly roles cannot be excluded"):
                build_profile(
                    official_docx=official, candidates_path=packet,
                    role_selection_path=roles, required_selection_path=required,
                    output_dir=temp / "out", profile_id="test-clean-profile",
                    title="Test", organization="Test Org",
                    source_url="https://example.test/template", effective_version="1",
                )


if __name__ == "__main__":
    unittest.main()
