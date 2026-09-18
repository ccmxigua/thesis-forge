from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_PIPELINE_FILES = (
    "scripts/thesis_format_pipeline.py",
    "scripts/requirements_engine.py",
    "scripts/thesis_format.py",
    "scripts/merge_host_agent_review.py",
    "scripts/profile_copilot.py",
    "scripts/batch_rerun_ten_schools.py",
    "scripts/run_golden_e2e.py",
    "scripts/compliance.py",
    "scripts/apply_format_spec.py",
    "scripts/submission_audit.py",
    "scripts/template_profile.py",
    "scripts/template_builder.py",
    "scripts/resource_registry.py",
    "scripts/template_reconciliation.py",
    "scripts/format_spec_validation.py",
    "scripts/docx_semantics.py",
)
FORBIDDEN_SCHOOL_TOKENS = {
    "szu", "xzhmu", "tju", "dlut", "ujs", "cauc", "bsu", "ustb", "btbu", "neau",
    "深圳大学", "徐州医科大学", "天津大学", "大连理工大学", "江苏大学", "中国民航大学",
    "北京体育大学", "北京科技大学", "北京工商大学", "东北农业大学",
}


class ArchitectureGuardTest(unittest.TestCase):
    def test_project_does_not_own_an_independent_llm_provider_client(self) -> None:
        forbidden = (
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "THESIS_FORMAT_LLM",
            "call_openai_compatible", "urllib.request", "--use-llm",
        )
        violations = []
        for relative in CORE_PIPELINE_FILES:
            text = (ROOT / relative).read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    violations.append(f"{relative}: {token}")
        self.assertEqual(violations, [], "independent provider wiring is forbidden:\n" + "\n".join(violations))

    def test_core_pipeline_has_no_school_specific_control_flow(self) -> None:
        violations: list[str] = []
        for relative in CORE_PIPELINE_FILES:
            path = ROOT / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.IfExp, ast.Match, ast.comprehension)):
                    continue
                test = getattr(node, "test", None)
                if test is None and isinstance(node, ast.Match):
                    candidates = [node.subject, *[case.pattern for case in node.cases]]
                else:
                    candidates = [test]
                rendered = " ".join(ast.dump(candidate, include_attributes=False) for candidate in candidates if candidate is not None).lower()
                matched = sorted(token for token in FORBIDDEN_SCHOOL_TOKENS if token.lower() in rendered)
                if matched:
                    violations.append(f"{relative}:{getattr(node, 'lineno', '?')}: {', '.join(matched)}")
        self.assertEqual(violations, [], "school-specific control flow is forbidden:\n" + "\n".join(violations))

    def test_run_scoped_resource_core_has_no_institution_resource_ids(self) -> None:
        paths = [ROOT / relative for relative in (
            "scripts/apply_format_spec.py",
            "scripts/format_spec_validation.py",
            "scripts/requirements_engine.py",
            "scripts/resource_registry.py",
            "scripts/docx_semantics.py",
        )]
        paths.append(ROOT / "schema" / "role-registry.json")
        forbidden_patterns = (r"\bCAU\b", r"\bTJU\b", r"\bcau[_-]", r"\btju[_-]")
        violations = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                import re
                if re.search(pattern, text, re.IGNORECASE):
                    violations.append(f"{path.relative_to(ROOT)}: {pattern}")
        self.assertEqual(violations, [], "run-scoped core must not contain institution resource ids:\n" + "\n".join(violations))

    def test_fresh_batch_driver_has_no_legacy_reuse_path(self) -> None:
        text = (ROOT / "scripts" / "batch_rerun_ten_schools.py").read_text(encoding="utf-8")
        forbidden = ("previous_build", "rebase-response", "rebuild_clause_review", "reviewed-responses")
        violations = [token for token in forbidden if token in text]
        self.assertEqual(
            violations, [],
            "fresh batch driver must not consume previous outputs or migration responses: "
            + ", ".join(violations),
        )

    def test_fresh_batch_manifest_is_external_and_complete(self) -> None:
        manifest_path = ROOT / "inputs" / "ten-school-template-manifest.json"
        if not manifest_path.is_file():
            self.skipTest("external ten-school manifest is not included in this source checkout")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = manifest.get("cases")
        self.assertIsInstance(cases, list)
        self.assertEqual(len(cases), 10)
        self.assertEqual(len({item["id"] for item in cases}), 10)
        for item in cases:
            self.assertEqual(item.get("analysis_mode"), "llm_primary", item["id"])
            self.assertTrue((ROOT / item["requirements"]).is_file(), item["id"])
            if item.get("style_template"):
                self.assertTrue((ROOT / item["style_template"]).is_file(), item["id"])
            if item.get("template_profile"):
                self.assertTrue((ROOT / item["template_profile"]).is_file(), item["id"])

    def test_requirements_engine_defaults_to_full_llm_review(self) -> None:
        text = (ROOT / "scripts" / "requirements_engine.py").read_text(encoding="utf-8")
        self.assertIn('choices=["llm_primary", "rule_only", "known_template"], default="llm_primary"', text)
        self.assertIn("--allow-supported-subset", (ROOT / "scripts" / "batch_rerun_ten_schools.py").read_text(encoding="utf-8"))

    def test_regression_baseline_capture_is_schema_valid_and_stable(self) -> None:
        source = ROOT / "build" / "ten-school-examples-20260719"
        if not source.exists():
            self.skipTest("ten-school regression artifacts are not available")
        with tempfile.TemporaryDirectory() as td:
            first = Path(td) / "first.json"
            second = Path(td) / "second.json"
            for output in (first, second):
                subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "capture_regression_baseline.py"), str(source), str(output)],
                    cwd=ROOT, check=True, text=True, capture_output=True,
                )
            one = json.loads(first.read_text(encoding="utf-8"))
            two = json.loads(second.read_text(encoding="utf-8"))
            one.pop("generated_at", None); two.pop("generated_at", None)
            self.assertEqual(one, two)
            self.assertEqual(len(one["cases"]), 10)
            by_id = {item["case_id"]: item for item in one["cases"]}
            self.assertFalse(by_id["tju"]["serialized_docx_valid"])
            self.assertEqual(by_id["ujs"]["status"], "failed")
            self.assertEqual(by_id["neau"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
