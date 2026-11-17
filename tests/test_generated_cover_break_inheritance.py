from io import BytesIO
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import unittest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_BREAK
from apply_format_spec import apply_cover, compile_cover_contract

COVER={'institution':'Synthetic University','fields':[{'id':'title_zh','label':'Title','order':1,'display_policy':'required','value_from':'title_zh'}]}
PROFILE={'cover_metadata':{'trust':{'confirmed':True,'source':'user_confirmed'},'title_zh':'Synthetic Title'}}

def assert_generated_internal_cover_paragraph_does_not_inherit_page_break(inherited):
    d=Document();body=d.add_paragraph('Unchanged body');body.paragraph_format.page_break_before=True
    before=body._p.xml;mappings={}
    if inherited=='normal': d.styles['Normal'].paragraph_format.page_break_before=True
    else:
        style=d.styles.add_style('Existing Cover Title',WD_STYLE_TYPE.PARAGRAPH)
        style.base_style=d.styles['Normal'];style.paragraph_format.page_break_before=True
        mappings={'thesis_title_zh':{'style_name':style.name}}
    apply_cover(d,COVER,PROFILE,compile_cover_contract(COVER,PROFILE),mappings)
    title=d.paragraphs[1]
    assert title.paragraph_format.page_break_before is False
    assert body._p.xml==before
    assert d.styles['Normal'].paragraph_format.page_break_before is (True if inherited=='normal' else None)
    if inherited=='title_style': assert d.styles['Existing Cover Title'].paragraph_format.page_break_before is True
    assert len(title._p.xpath('.//w:br[@w:type="page"]'))==1
    stream=BytesIO();d.save(stream);stream.seek(0);out=Document(stream)
    assert out.paragraphs[1].paragraph_format.page_break_before is False
    assert len(out.paragraphs[1]._p.xpath('.//w:br[@w:type="page"]'))==1
    assert out.paragraphs[2]._p.xml==body._p.xml


def assert_regeneration_keeps_one_cover_end_break_and_original_body():
    d=Document();body=d.add_paragraph('Unchanged body');before=body._p.xml
    d.styles['Normal'].paragraph_format.page_break_before=True
    for _ in range(2): apply_cover(d,COVER,PROFILE,compile_cover_contract(COVER,PROFILE),{})
    assert len(d.paragraphs)==3
    assert d.paragraphs[1].paragraph_format.page_break_before is False
    assert len(d.element.xpath('.//w:br[@w:type="page"]'))==1
    assert body._p.xml==before

class CoverBreakTests(unittest.TestCase):
    def test_multiple_fields_and_explicit_body_break(self):
        d=Document();d.styles["Normal"].paragraph_format.page_break_before=True
        body=d.add_paragraph("Unchanged body");body.add_run().add_break(WD_BREAK.PAGE);before=body._p.xml
        cover={"institution":"Synthetic University","fields":[{"id":k,"label":k,"order":i,"display_policy":"required","value_from":k,"label_display_policy":"always"} for i,k in enumerate(["title_zh","title_en","author"])]}
        apply_cover(d,cover,PROFILE,compile_cover_contract(cover,PROFILE),{})
        assert d.paragraphs[0].paragraph_format.page_break_before is None
        assert all(p.paragraph_format.page_break_before is False for p in d.paragraphs[1:4])
        assert body._p.xml==before
        assert len(d.paragraphs[3]._p.xpath('.//w:br[@w:type="page"]'))==1
        assert len(d.element.xpath('.//w:br[@w:type="page"]'))==2
    def test_normal(self): assert_generated_internal_cover_paragraph_does_not_inherit_page_break("normal")
    def test_title_style(self): assert_generated_internal_cover_paragraph_does_not_inherit_page_break("title_style")
    def test_regeneration(self): assert_regeneration_keeps_one_cover_end_break_and_original_body()

if __name__=="__main__": unittest.main()
