"""TOC cached entries are not body boundaries; unknown duplicates stay blocked."""
import sys
import tempfile
import unittest
from pathlib import Path
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.section import WD_SECTION
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from page_field_ops import inspect_docx
from section_model import _selector_section

class TocSelectorTests(unittest.TestCase):
    def evidence(self, toc_style=None, derived=False, duplicate=False):
        with tempfile.TemporaryDirectory() as tmp:
            doc=Document()
            if toc_style:
                style=doc.styles.add_style(toc_style, WD_STYLE_TYPE.PARAGRAPH)
                if derived:
                    child=doc.styles.add_style('University contents', WD_STYLE_TYPE.PARAGRAPH)
                    child.base_style=style
                    style=child
                doc.add_paragraph('第1章 绪论1', style=style)
            else:
                doc.add_paragraph('第1章 绪论1')
            doc.add_section(WD_SECTION.NEW_PAGE)
            doc.add_heading('第1章 绪论', 1)
            if duplicate: doc.add_heading('第1章 另一标题', 1)
            path=Path(tmp)/'test.docx'; doc.save(path)
            return inspect_docx(path)
    def select(self, evidence):
        return _selector_section({'strategy':'heading_text','heading_text_pattern':r'^第\s*1\s*章'},evidence)
    def test_toc_style_excluded(self):
        ev=self.evidence('toc 1')
        self.assertTrue(ev['paragraphs'][0].get('is_toc_entry'))
        self.assertEqual(self.select(ev),(2,[]))
    def test_derived_toc_style_excluded(self):
        self.assertEqual(self.select(self.evidence('toc 1',derived=True)),(2,[]))
    def test_unknown_duplicate_remains_ambiguous(self):
        self.assertIsNone(self.select(self.evidence())[0])
    def test_similar_style_name_not_evidence(self):
        self.assertIsNone(self.select(self.evidence('toc chapter'))[0])
    def test_two_real_headings_remain_ambiguous(self):
        self.assertIsNone(self.select(self.evidence('toc 1',duplicate=True))[0])
    def test_only_toc_is_no_match(self):
        ev=self.evidence('toc 1'); ev['paragraphs']=ev['paragraphs'][:1]
        self.assertIsNone(self.select(ev)[0])
    def test_first_heading_excludes_marked_toc(self):
        ev=self.evidence('toc 1');ev['paragraphs'][0]['is_heading_1']=True
        self.assertEqual(_selector_section({'strategy':'first_heading_1'},ev),(2,[]))
if __name__=='__main__': unittest.main()
