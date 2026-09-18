#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import requirements_engine  # noqa: E402
import requirements_input as adapter  # noqa: E402


def make_docx(path: Path, text: str = "正文中文使用小四号宋体，行距固定值20磅。") -> None:
    document = Document()
    document.add_paragraph(text)
    document.save(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mock_converter() -> adapter.Converter:
    return adapter.Converter(
        tool="mock_libreoffice",
        kind="soffice",
        path=Path("/mock/bin/soffice"),
        version="Mock LibreOffice 1.0",
    )


def successful_conversion(template: Path):
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        out_dir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        shutil.copyfile(template, out_dir / f"{source.stem}.docx")
        return subprocess.CompletedProcess(command, 0, "converted\n", "")

    return run


class RequirementsInputAdapterTest(unittest.TestCase):
    def test_docx_pass_through_is_validated_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "requirements.docx"
            manifest_path = root / "input-manifest.json"
            make_docx(source)

            result = adapter.normalize_requirements_input(
                source, root / "work", manifest_path,
            )

            self.assertEqual(result.normalized_path, source.resolve())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["conversion_status"], "not_required")
            self.assertFalse(manifest["artifact_reused"])
            self.assertIsNone(manifest["converter"])
            self.assertEqual(manifest["original"]["path"], str(source.resolve()))
            self.assertEqual(manifest["original"]["suffix"], ".docx")
            self.assertEqual(manifest["original"]["kind"], "docx")
            self.assertEqual(manifest["normalized"], manifest["original"])
            self.assertEqual(manifest["validation"]["status"], "valid")

    def test_doc_conversion_is_fresh_audited_and_never_overwrites_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "requirements.doc"
            source_bytes = b"legacy-binary-word-sentinel"
            source.write_bytes(source_bytes)
            template = root / "converter-template.docx"
            make_docx(template)

            with (
                patch.object(adapter, "_discover_converters", return_value=[mock_converter()]),
                patch.object(adapter.subprocess, "run", side_effect=successful_conversion(template)) as run_mock,
            ):
                first = adapter.normalize_requirements_input(
                    source, root / "work", root / "manifest-1.json",
                )
                second = adapter.normalize_requirements_input(
                    source, root / "work", root / "manifest-2.json",
                )

            self.assertEqual(run_mock.call_count, 2)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse(source.with_suffix(".docx").exists())
            self.assertNotEqual(first.normalized_path, second.normalized_path)
            self.assertTrue(first.normalized_path.is_relative_to((root / "work").resolve()))
            manifest = first.manifest
            self.assertEqual(manifest["conversion_status"], "converted")
            self.assertFalse(manifest["artifact_reused"])
            self.assertEqual(manifest["original"]["bytes"], len(source_bytes))
            self.assertEqual(manifest["original"]["sha256"], hashlib.sha256(source_bytes).hexdigest())
            self.assertEqual(manifest["normalized"]["bytes"], first.normalized_path.stat().st_size)
            self.assertEqual(manifest["normalized"]["sha256"], sha256(first.normalized_path))
            self.assertEqual(manifest["converter"]["tool"], "mock_libreoffice")
            self.assertEqual(manifest["converter"]["version"], "Mock LibreOffice 1.0")
            self.assertIsInstance(manifest["converter"]["command"], list)
            self.assertIn(str(source.resolve()), manifest["converter"]["command"])

    def test_converter_unavailable_fails_closed_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "requirements.doc"
            source.write_bytes(b"legacy")
            manifest_path = root / "manifest.json"
            with patch.object(adapter, "_discover_converters", return_value=[]):
                with self.assertRaisesRegex(adapter.RequirementsInputError, "no supported local converter"):
                    adapter.normalize_requirements_input(source, root / "work", manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["conversion_status"], "failed")
            self.assertIn("original was not modified", manifest["error"])
            self.assertEqual(source.read_bytes(), b"legacy")

    def test_converter_failure_fails_closed_and_records_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "requirements.doc"
            source.write_bytes(b"legacy")
            manifest_path = root / "manifest.json"
            failed = subprocess.CompletedProcess([], 7, "", "conversion exploded")
            with (
                patch.object(adapter, "_discover_converters", return_value=[mock_converter()]),
                patch.object(adapter.subprocess, "run", return_value=failed),
            ):
                with self.assertRaisesRegex(adapter.RequirementsInputError, "failed closed"):
                    adapter.normalize_requirements_input(source, root / "work", manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["conversion_attempts"][0]["returncode"], 7)
            self.assertEqual(manifest["conversion_attempts"][0]["version"], "Mock LibreOffice 1.0")
            self.assertIn("conversion exploded", manifest["error"])

    def test_invalid_converted_output_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "requirements.doc"
            source.write_bytes(b"legacy")
            manifest_path = root / "manifest.json"

            def invalid_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                out_dir = Path(command[command.index("--outdir") + 1])
                Path(out_dir / f"{source.stem}.docx").write_bytes(b"not-a-docx")
                return subprocess.CompletedProcess(command, 0, "converted", "")

            with (
                patch.object(adapter, "_discover_converters", return_value=[mock_converter()]),
                patch.object(adapter.subprocess, "run", side_effect=invalid_run),
            ):
                with self.assertRaisesRegex(adapter.RequirementsInputError, "invalid DOCX"):
                    adapter.normalize_requirements_input(source, root / "work", manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("not a ZIP-based DOCX", manifest["conversion_attempts"][0]["validation_error"])
            self.assertIsNone(manifest["normalized"])

    def test_low_level_engine_normalizes_before_fresh_llm_primary_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "requirements.doc"
            source.write_bytes(b"legacy-requirements")
            template = root / "converter-template.docx"
            make_docx(template)
            out = root / "review"
            args = requirements_engine.parse_args([
                str(source), "--out", str(out),
                "--analysis-mode", "llm_primary",
                "--prepare-host-review",
                "--host-review-chunk-size", "1",
            ])

            with (
                patch.object(adapter, "_discover_converters", return_value=[mock_converter()]),
                patch.object(adapter.subprocess, "run", side_effect=successful_conversion(template)),
            ):
                code = requirements_engine.analyse(args)

            self.assertEqual(code, 0)
            extraction = json.loads((out / "extraction-manifest.json").read_text(encoding="utf-8"))
            evidence = json.loads((out / "document-evidence.json").read_text(encoding="utf-8"))
            request = json.loads((out / "llm-request.json").read_text(encoding="utf-8"))
            review = json.loads((out / "host-agent-review-manifest.json").read_text(encoding="utf-8"))
            spec = json.loads((out / "format-spec.json").read_text(encoding="utf-8"))

            self.assertEqual(extraction["source_document"], str(source.resolve()))
            self.assertEqual(extraction["source_sha256"], sha256(source))
            self.assertEqual(extraction["requirements_input_normalization"]["conversion_status"], "converted")
            self.assertEqual(evidence["source_document"], str(source.resolve()))
            self.assertEqual(evidence["source_input_kind"], "doc")
            self.assertEqual(request["provenance"]["source_sha256"], sha256(source))
            self.assertEqual(review["contract_version"], "2.1")
            self.assertEqual(review["clause_count"], extraction["clause_count"])
            self.assertEqual(spec["analysis_mode"], "llm_primary")
            self.assertEqual(spec["status"], "needs_clarification")
            self.assertFalse(spec["semantic_review_provenance_valid"])

    def test_formal_pipeline_records_docx_pass_through_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            requirements = root / "requirements.docx"
            thesis = root / "thesis.docx"
            make_docx(requirements)
            make_docx(thesis, "第一章 绪论")
            work = root / "work"
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "thesis_format_pipeline.py"),
                    str(requirements), str(thesis), str(root / "unused.docx"),
                    "--work-dir", str(work), "--prepare-host-review",
                ],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            manifest = json.loads((work / "pipeline-manifest.json").read_text(encoding="utf-8"))
            normalization = manifest["requirements_input_normalization"]
            self.assertEqual(manifest["status"], "host_review_required")
            self.assertEqual(manifest["inputs"]["requirements_kind"], "docx")
            self.assertEqual(normalization["conversion_status"], "not_required")
            self.assertEqual(normalization["original"]["sha256"], sha256(requirements))
            self.assertEqual(normalization["normalized"]["sha256"], sha256(requirements))
            self.assertEqual(manifest["requirements_normalization_errors"] if "requirements_normalization_errors" in manifest else [], [])

    def test_user_facing_help_advertises_automatic_legacy_doc_support(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "thesis_format.py"), "--help"],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = re.sub(r"\s+", " ", result.stdout)
        self.assertIn(".doc is normalized automatically", help_text)
        self.assertIn(".docx passes through", help_text)


if __name__ == "__main__":
    unittest.main()
