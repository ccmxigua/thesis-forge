"""Synthetic TOC contract boundary tests; no native refresh/acceptance claim."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from apply_format_spec import set_font, set_paragraph
from font_declarations import style_slot
from toc_style_contract import apply_toc_style_rules, audit_toc_style_rules

class TocThemeSlotsTests(unittest.TestCase):
    def apply(self, faces):
        doc = Document()
        st = doc.styles.add_style('toc 1', WD_STYLE_TYPE.PARAGRAPH)
        st.base_style = doc.styles['Normal']
        fonts = st.element.get_or_add_rPr().get_or_add_rFonts()
        for slot, theme in [('eastAsiaTheme','minorEastAsia'), ('asciiTheme','minorHAnsi'),
                            ('hAnsiTheme','minorHAnsi'), ('cstheme','minorBidi')]:
            fonts.set(qn('w:' + slot), theme)
        p = doc.add_paragraph(style=st)
        for kind, value in [('begin',None), (None,' TOC \\o "1-1" '),
                            ('separate',None), (None,'合成目录\t1'), ('end',None)]:
            r = p.add_run()._r
            if kind:
                n = OxmlElement('w:fldChar'); n.set(qn('w:fldCharType'), kind)
            else:
                n = OxmlElement('w:instrText' if 'TOC' in value else 'w:t'); n.text = value
            r.append(n)
        # The public boundary takes its caller's resolved field scope. This
        # fixture supplies its single fully formed cache, not a style-name guess.
        def scope(current, opcodes):
            self.assertIs(current, doc); self.assertEqual(opcodes, {'TOC'})
            return {p._p}, False
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'bound-template.docx'; Document().save(source)
            before = source.read_bytes()
            contract = {'status':'ready','source_path':str(source),
                'source_sha256':hashlib.sha256(before).hexdigest(),
                'rules':{'1':{'font':faces}},'findings':[]}
            report = apply_toc_style_rules(doc, contract, scope_resolver=scope,
                                           set_font=set_font, set_paragraph=set_paragraph)
            self.assertEqual(report['status'], 'style_definitions_applied', report)
            self.assertEqual(source.read_bytes(), before)
            out = Path(temp) / 'synthetic.docx'; doc.save(out); doc = Document(out)
            self.assertTrue(audit_toc_style_rules(doc, report)['valid'])
            self.assertFalse(report['native_verified'])
        return doc.styles['toc 1']

    def test_explicit_both_faces_replace_competing_themes(self):
        st = self.apply({'cjk':'SimSun','latin':'Times New Roman'})
        self.assertEqual(style_slot(st, 'eastAsia'), (True, 'SimSun'))
        for slot in ('ascii','hAnsi','cs'):
            self.assertEqual(style_slot(st, slot), (True, 'Times New Roman'))

    def test_cjk_only_preserves_latin_theme(self):
        st = self.apply({'cjk':'SimSun'})
        self.assertEqual(style_slot(st, 'eastAsia'), (True, 'SimSun'))
        fonts = st.element.get_or_add_rPr().get_or_add_rFonts()
        for slot in ('asciiTheme','hAnsiTheme'):
            self.assertEqual(fonts.get(qn('w:'+slot)), 'minorHAnsi')
        self.assertEqual(fonts.get(qn('w:cstheme')), 'minorBidi')

    def test_latin_only_preserves_cjk_theme(self):
        st = self.apply({'latin':'Times New Roman'})
        self.assertEqual(st.element.get_or_add_rPr().get_or_add_rFonts().get(qn('w:eastAsiaTheme')), 'minorEastAsia')
        for slot in ('ascii','hAnsi','cs'):
            self.assertEqual(style_slot(st, slot), (True, 'Times New Roman'))
