from pathlib import Path
import sys
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import current_session as cs


def toc(path,mode=None):
    d=Document();p=d.add_paragraph();field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),'TOC \\o "1-3" \\h');p._p.append(field)
    d.add_paragraph('第1章 正文',style='Heading 1')
    if mode=='roman':
        num=OxmlElement('w:pgNumType');num.set(qn('w:fmt'),'upperRoman');num.set(qn('w:start'),'1');d.sections[0]._sectPr.append(num)
    if mode=='ole':p.add_run()._r.append(OxmlElement('w:object'))
    d.save(path)


def test_roman_restart_and_complex_objects_require_native_without_render(tmp_path):
    assert hasattr(cs,'toc_routing'),'Native TOC routing is not implemented'
    for mode in ('roman','ole'):
        path=tmp_path/f'{mode}.docx';toc(path,mode);before=path.read_bytes()
        assert cs.toc_routing(path)['toc_action']=='native_refresh_required'
        assert path.read_bytes()==before


def test_plain_decimal_toc_does_not_expand_materializer_claims(tmp_path):
    assert hasattr(cs,'toc_routing')
    path=tmp_path/'plain.docx';toc(path)
    assert cs.toc_routing(path)['toc_action']=='safe_subset_eligible'
    d=Document();d.add_paragraph('No TOC');d.save(path)
    assert cs.toc_routing(path)['toc_action']=='not_present'
