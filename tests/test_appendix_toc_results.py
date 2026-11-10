"""TOC caches must never become/consume real appendix headings."""
import contextlib
import io
import json
import sys
from pathlib import Path
import unittest
import tempfile
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_format_spec as afs

RULES = {'label_style': 'alpha_upper', 'page_break_each': True}

def marker(p, kind):
    n=OxmlElement('w:fldChar'); n.set(qn('w:fldCharType'),kind); p.add_run()._r.append(n)

def fixture(kind):
    d=Document()
    if kind == 'complex':
        p=d.add_paragraph();marker(p,'begin')
        for text in (' T','OC \\o "1-3"'):
            n=OxmlElement('w:instrText');n.text=text;p.add_run()._r.append(n)
        marker(p,'separate'); entry=d.add_paragraph('附录A 模拟变量字典\t11')
        marker(d.add_paragraph(),'end')
    elif kind == 'simple':
        entry=d.add_paragraph();f=OxmlElement('w:fldSimple');f.set(qn('w:instr'),'TOC \\o "1-3"')
        r=OxmlElement('w:r');t=OxmlElement('w:t');t.text='附录A 模拟变量字典\t11';r.append(t);f.append(r);entry._p.append(f)
    else:
        d.styles.add_style(kind, WD_STYLE_TYPE.PARAGRAPH)
        entry=d.add_paragraph('附录A 模拟变量字典\t11',kind)
    real=d.add_paragraph('附录A  模拟变量字典')
    return d,entry,real

def check_numbering_and_audit_exclude_toc_cache(kind):
    d,entry,real=fixture(kind); before=entry._p.xml
    counts=afs.apply_appendix_rules(d,RULES)
    assert counts['headings']==1
    assert entry._p.xml==before
    assert real.text=='附录 A  模拟变量字典'
    assert afs.audit_appendix_rules(d,RULES,{})==[]
    d._element.body.remove(real._p)
    assert afs.audit_appendix_rules(d,{'required_when_profile_has_appendices':True},{'has_appendices':True})

def check_role_assignment_preserves_toc_cache(tmp_path,kind):
    d,entry,real=fixture(kind); before=entry._p.xml
    src=tmp_path/'input.docx';out=tmp_path/'output.docx';spec=tmp_path/'spec.json';d.save(src)
    spec.write_text(json.dumps({'schema_version':'1.0','source_document':'test','status':'semantic_resolved',
        'roles':{'heading_appendix':{'font':{'size_pt':16}}},'requirements':[], 'appendices':RULES}))
    with contextlib.redirect_stdout(io.StringIO()):
        afs.main([str(src),str(spec),str(out),'--out-dir',str(tmp_path/'audit'),'--neutral-reference'])
    result=Document(out)
    # Locate by original XML paragraph index (python-docx wrappers are ephemeral).
    idx=list(d._element.body).index(entry._p)
    assert list(result._element.body)[idx].xml==before
    assert result.paragraphs[-1].text=='附录 A  模拟变量字典'
    assert result.paragraphs[-1].style.name=='Thesis Heading Appendix'

class AppendixTocTests(unittest.TestCase):
    def test_numbering_and_audit(self):
        for kind in ('complex', 'simple', 'TOC 1', '目录 1'):
            with self.subTest(kind=kind):
                check_numbering_and_audit_exclude_toc_cache(kind)
    def test_role_assignment(self):
        for kind in ('complex', 'simple', 'TOC 1', '目录 1'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                check_role_assignment_preserves_toc_cache(Path(tmp), kind)

    def test_simple_cache_is_identified_even_when_paragraph_text_omits_field(self):
        from toc_refresh import toc_result_paragraphs
        d, entry, real = fixture('simple')
        self.assertIn(entry._p, toc_result_paragraphs(d))
        self.assertNotIn(real._p, toc_result_paragraphs(d))

    def test_ref_argument_toc_does_not_hide_real_appendix(self):
        from toc_refresh import toc_result_paragraphs
        d, entry, real = fixture('complex')
        instructions = list(d._element.body.iter(qn('w:instrText')))
        instructions[0].text = ' REF '
        instructions[1].text = 'TOC '
        self.assertNotIn(entry._p, toc_result_paragraphs(d))
        self.assertEqual(afs.apply_appendix_rules(d, RULES)['headings'], 2)

    def test_complete_field_in_one_paragraph_preserves_cache_xml(self):
        d=Document();entry=d.add_paragraph();marker(entry,'begin')
        instr=OxmlElement('w:instrText');instr.text='TOC';entry.add_run()._r.append(instr)
        marker(entry,'separate');entry.add_run('附录A 目录缓存\t11');marker(entry,'end')
        real=d.add_paragraph('附录A  正文附录');before=entry._p.xml
        self.assertEqual(afs.apply_appendix_rules(d,RULES)['headings'],1)
        self.assertEqual(entry._p.xml,before)
        self.assertEqual(real.text,'附录 A  正文附录')

    def test_toc_cache_in_table_is_protected_like_body_cache(self):
        from toc_refresh import toc_result_paragraphs
        d=Document();p=d.add_table(rows=1,cols=1).cell(0,0).paragraphs[0]
        marker(p,'begin');n=OxmlElement('w:instrText');n.text='TOC';p.add_run()._r.append(n)
        marker(p,'separate');p.add_run('附录A 缓存\t1');marker(p,'end')
        self.assertIn(p._p,toc_result_paragraphs(d))
