"""Synthetic public-CLI regressions; never use a user's thesis or a renderer."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(os.environ.get("THESIS_TEST_SCRIPT_ROOT", ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from manual_review import build_manual_review_ledger



def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path


def record(path):
    return {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}


def review_args(root, doc, roles, mappings, template=None, unsupported=False, spec_overrides=None):
    """Build actual, hash-bound inputs, not mocks of the validation boundary."""
    source = root / "synthetic-source.docx"
    doc.save(source)
    req = root / "synthetic-requirements.docx"
    requirements_doc = Document()
    requirements_doc.add_paragraph("Synthetic regression requirement fixture only.")
    requirements_doc.save(req)
    spec = {"schema_version": "1.0", "source_document": str(req), "run_id": "synthetic-properties",
            "status": "rule_resolved", "compliance_mode": "supported_subset", "roles": roles,
            "requirements": [
                {"id": "R-%d" % i, "role": role, "properties": properties,
                 "evidence_ids": ["E-%d" % i], "resolved_by": "rule", "confidence": 1,
                 "source_text": "Synthetic explicitly requested properties."}
                for i, (role, properties) in enumerate(roles.items()) if properties
            ]}
    spec.update(spec_overrides or {})
    if unsupported:
        spec["clause_compliance"] = [
            {"clause_id": "C00159", "evidence_ids": ["E-atomic"], "scope": "docx",
             "status": "unsupported_backend", "requirement_ids": [],
             "reason": "Synthetic indivisible 12pt font and outline obligation is unsupported."}]
    spec_path = write_json(root / "format-spec.json", spec)
    mapping_path = write_json(root / "mappings.json", mappings)
    clauses = write_json(root / "clauses.json", [])
    evidence = write_json(root / "evidence.json", {})
    context = write_json(root / "context.json", {})
    extraction = write_json(root / "extraction.json", {
        "status": "completed", "cache_reused": False, "run_id": spec["run_id"],
        "source_sha256": digest(req), "requirement_clauses_sha256": digest(clauses),
        "document_evidence_sha256": digest(evidence), "evidence_sha256": digest(context)})
    binding = {"case_id": "standalone", "run_id": spec["run_id"], "source_sha256": digest(source),
               "clause_sha256": digest(clauses), "evidence_sha256": digest(context),
               "request_sha256": None, "requirements_sha256": digest(req),
               "input_source_sha256": digest(source), "format_spec_sha256": digest(spec_path),
               "official_template_sha256": digest(template) if template else None,
               "official_template_source": "style_template" if template else "not_supplied"}
    ledger = write_json(root / "manual.json", build_manual_review_ledger(None, [], binding=binding))
    inputs = {"requirements": record(req), "source": record(source),
              "official_template_source": binding["official_template_source"]}
    if template:
        inputs["official_template_evidence"] = record(template)
    pipeline = write_json(root / "pipeline.json", {
        "case_id": "standalone", "inputs": inputs, "manual_review_ledger_input_sha256": digest(ledger)})
    output = root / "synthetic-output.docx"
    audits = root / "audits"
    args = [sys.executable, "-B", str(SCRIPT_ROOT / "scripts/apply_format_spec.py"), str(source),
            str(spec_path), str(output), "--out-dir", str(audits), "--style-map", str(mapping_path),
            "--output-policy", "review_draft", "--require-coverage", "--manual-review-items", str(ledger),
            "--pipeline-manifest", str(pipeline), "--source-clauses", str(clauses),
            "--source-evidence", str(evidence), "--source-evidence-context", str(context),
            "--source-extraction-manifest", str(extraction)]
    if template:
        args += ["--official-template", str(template)]
    return args, source, spec_path, output, audits


class PropertyAuthorityTests(unittest.TestCase):
    def execute(self, root, doc, roles, mappings, template=None, unsupported=False, spec_overrides=None, tamper_scorecard=False):
        args, source, spec, output, audits = review_args(root, doc, roles, mappings, template, unsupported, spec_overrides)
        before = (source.read_bytes(), spec.read_bytes())
        if tamper_scorecard:
            script = "import sys;sys.path.insert(0,sys.argv.pop(1));import apply_format_spec as a;original=a.append_scorecard;\ndef tamper(doc, card):\n original(doc,card)\n p=next(p for p in doc.paragraphs if p.text=='第一章 合成')\n p.add_run('unexpected heading mutation')\na.append_scorecard=tamper\na.main(sys.argv[1:])"
            args = [sys.executable, '-B', '-c', script, str(SCRIPT_ROOT/'scripts')] + args[3:]
        result = subprocess.run(args, capture_output=True, text=True, timeout=90,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                     "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-6000:])
        self.assertEqual((source.read_bytes(), spec.read_bytes()), before)
        report = json.loads((audits / "validation-report.json").read_text())
        self.assertFalse(report["submission_ready"])
        return Document(output), report, audits
