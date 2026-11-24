from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from apply_format_spec import audit_table_rules, apply_table_rules
import continued_table_acceptance
import submission_audit

CONTINUATION_SPEC = {"schema_version":"1.0","source_document":"synthetic.docx",
                     "roles":{},"requirements":[],"status":"rule_resolved",
                     "tables":{"continuation":{"caption_suffix":"（续）","repeat_header_row":True,
                                               "verification":"word_render"}}}
HOLD = "continued_table_native_scope_pending"

class TestContinuationGate(unittest.TestCase):
    def test_unbound_verified_boolean_cannot_clear_candidate_generation_finding(self):
        doc = Document()
        doc.add_table(rows=2, cols=2)
        findings = audit_table_rules(doc, {"continuation": {"caption_suffix": "（续）"}},
                                     {"table_continuation": {"verified": True}})
        self.assertEqual(findings, [])
        diagnostic = getattr(continued_table_acceptance, "continuation_geometry_diagnostic", lambda _x: {} )(
            {"continuation":{"caption_suffix":"（续）"}})
        self.assertEqual(diagnostic.get("status"), "pending_native_geometry")
        self.assertFalse(diagnostic["blocking"])
        self.assertFalse(diagnostic["native_observation_verified"])

    def test_explicit_false_repeat_flag_does_not_prove_structure(self):
        doc = Document()
        table = doc.add_table(rows=2, cols=2)
        flag = OxmlElement("w:tblHeader")
        flag.set(qn("w:val"), "0")
        table.rows[0]._tr.get_or_add_trPr().append(flag)
        findings = audit_table_rules(doc, {"repeat_header_row": True})
        self.assertTrue(any(f["property"]=="tables[1].repeat_header" for f in findings))

    def test_explicit_automatic_false_flags_become_enabled_on_reopen(self):
        for value in ("0", "off", "false"):
            with self.subTest(value=value):
                doc = Document()
                table = doc.add_table(rows=2, cols=2)
                flag = OxmlElement("w:tblHeader")
                flag.set(qn("w:val"), value)
                table.rows[0]._tr.get_or_add_trPr().append(flag)
                result = apply_table_rules(doc, {"repeat_header_row": True})
                b = BytesIO()
                doc.save(b)
                reopened = Document(b)
                self.assertEqual(result["header_rows_repeated"], 1)
                self.assertEqual(audit_table_rules(reopened, {"repeat_header_row": True}), [])
                self.assertEqual(reopened.tables[0].rows[0]._tr.trPr.find(qn("w:tblHeader")).get(qn("w:val")), "1")

    def test_nested_semantic_repeat_does_not_add_automatic_flags(self):
        doc = Document()
        table = doc.add_table(rows=2, cols=2)
        result = apply_table_rules(doc, {"continuation":{"repeat_header_row":True}})
        b = BytesIO()
        doc.save(b)
        reopened = Document(b)
        self.assertEqual(result["header_rows_repeated"], 0)
        self.assertFalse(reopened.tables[0]._tbl.xpath(".//w:tblHeader"))

    def test_duplicate_header_declarations_are_not_overwritten(self):
        doc = Document()
        table = doc.add_table(rows=2, cols=2)
        trpr = table.rows[0]._tr.get_or_add_trPr()
        for value in ("0","1"):
            flag = OxmlElement("w:tblHeader")
            flag.set(qn("w:val"), value)
            trpr.append(flag)
        result = apply_table_rules(doc, {"repeat_header_row":True})
        b = BytesIO()
        doc.save(b)
        reopened = Document(b)
        self.assertEqual(result["header_rows_repeated"],0)
        self.assertEqual([t.get(qn("w:val")) for t in reopened.tables[0]._tbl.xpath(".//w:tblHeader")],["0","1"])
        self.assertTrue(audit_table_rules(reopened, {"repeat_header_row":True}))

    def test_trusted_generic_render_and_visual_do_not_prove_table_scope(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "synthetic.docx"
            Document().save(path)
            accepted_generic_visual = {"valid": True, "status": "passed"}
            # This is a contract double for already accepted generic Word
            # render, not synthetic evidence claimed as native.
            with patch.object(submission_audit, "_render_summary", return_value={"rendered_verified":True}):
                untargeted = submission_audit.audit_docx(path, {}, visual_review_validation=accepted_generic_visual)
                self.assertTrue(untargeted["submission_ready"])
                for raw in (None, {"table_continuation":{"verified":True}},
                            {"table_continuation":{"status":"host_evidence_bound_unverified"}}):
                    result = submission_audit.audit_docx(path, CONTINUATION_SPEC, raw,
                                                         visual_review_validation=accepted_generic_visual)
                    self.assertTrue(result["serialized_docx_valid"])
                    self.assertFalse(result["submission_ready"])
                    self.assertIn(HOLD,{x["code"] for x in result["submission_blockers"]})

    def test_same_recomputed_audit_cli_used_after_word_emits_scoped_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f=root/"final.docx"; Document().save(f)
            spec=root/"spec.json"; spec.write_text(json.dumps(CONTINUATION_SPEC))
            claimed=root/"render.json"; claimed.write_text('{"table_continuation":{"verified":true}}')
            report=root/"audit.json"
            run=subprocess.run([sys.executable,"-B",str(submission_audit.__file__),str(f),
                                "--format-spec",str(spec),"--render-report",str(claimed),
                                "--out",str(report)],text=True,capture_output=True)
            self.assertEqual(run.returncode,2,run.stderr)
            audit=json.loads(report.read_bytes())
            self.assertFalse(audit["submission_ready"])
            self.assertIn(HOLD,{x["code"] for x in audit["submission_blockers"]})

if __name__ == "__main__":
    unittest.main()
