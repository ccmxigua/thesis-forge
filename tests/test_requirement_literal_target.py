"""Regression: requirements evidence is not the target thesis paragraph."""
import sys
import tempfile
import unittest
from pathlib import Path
from docx import Document
from docx.shared import Pt
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_format_spec as formatter


class RequirementLiteralTargetTests(unittest.TestCase):
    def run_application(self, repeats=1):
        doc = Document()
        for _ in range(repeats):
            p = doc.add_paragraph()
            p.add_run('A').font.size = Pt(7.5)
            p.add_run('bstract').font.size = Pt(7.5)
        spec = {'content_instances': [{'id': 'abstract', 'role': 'abstract_title_en',
            'text': 'Abstract', 'style_properties': {'font': {
                'latin': 'Times New Roman', 'size_pt': 16, 'bold': True}}}]}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'thesis.docx'
            doc.save(target)
            binding = {'abstract': [{'text': '英文摘要“Abstract”标题用三号Times New Roman体加粗',
                'source_document': str(Path(directory) / 'requirements.docx'),
                'location': {'part': 'document', 'child_index': 59}}]}
            audit = formatter.apply_content_instance_overrides(
                doc, spec, {}, binding, target_source_document=target)
            doc.save(target)
            serialized = Document(target)
            actual = formatter.audit_serialized_content_instance_targets(serialized, audit)
            return serialized, audit, actual

    def test_requirement_sentence_targets_exact_literal_and_overrides_stale_style(self):
        doc, audit, actual = self.run_application()
        self.assertEqual(audit[0]['status'], 'applied')
        self.assertEqual(audit[0]['match_count'], 1)
        self.assertEqual(doc.paragraphs[0].text, 'Abstract')
        for run in doc.paragraphs[0].runs:
            self.assertEqual(run.font.size.pt, 16)
            self.assertEqual(run.font.name, 'Times New Roman')
            self.assertTrue(run.font.bold)
        self.assertEqual(audit[0]['serialized_target_status'], 'verified')

    def test_requirement_sentence_does_not_disambiguate_repeated_target_literal(self):
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.run_application(repeats=2)
