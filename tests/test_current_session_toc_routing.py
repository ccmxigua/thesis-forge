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


def complex_field_document(path, fragments):
    d=Document();p=d.add_paragraph()
    begin=OxmlElement('w:fldChar');begin.set(qn('w:fldCharType'),'begin');p.add_run()._r.append(begin)
    for fragment in fragments:
        instruction=OxmlElement('w:instrText');instruction.text=fragment;p.add_run()._r.append(instruction)
    separate=OxmlElement('w:fldChar');separate.set(qn('w:fldCharType'),'separate');p.add_run()._r.append(separate)
    p.add_run('Cached field result')
    end=OxmlElement('w:fldChar');end.set(qn('w:fldCharType'),'end');p.add_run()._r.append(end)
    d.add_paragraph('Chapter One',style='Heading 1');d.save(path)


def test_split_complex_toc_opcode_is_detected_without_modification(tmp_path):
    path=tmp_path/'split.docx';complex_field_document(path,[' T','O','C \\o "1-3" \\h ']);before=path.read_bytes()
    assert cs.toc_routing(path)['toc_action']=='safe_subset_eligible'
    assert path.read_bytes()==before


def test_toc_in_unrelated_field_argument_is_not_a_toc(tmp_path):
    path=tmp_path/'reference.docx';complex_field_document(path,[' REF TOC '])
    assert cs.toc_routing(path)['toc_action']=='not_present'


def test_unterminated_split_field_cannot_claim_directory_absent(tmp_path):
    path=tmp_path/'unterminated.docx';complex_field_document(path,[' T','OC \\h '])
    d=Document(path)
    for node in d._element.body.iter(qn('w:fldChar')):
        if node.get(qn('w:fldCharType'))=='end':node.getparent().remove(node)
    d.save(path)
    result=cs.toc_routing(path)
    assert result['toc_action']=='native_refresh_required'
    assert result['reasons']


def test_whitespace_only_field_instruction_is_unresolved_not_exception(tmp_path):
    path=tmp_path/'empty-opcode.docx';complex_field_document(path,[' ', '\t '])
    result=cs.toc_routing(path)
    assert result['toc_action']=='native_refresh_required'
    assert result['reasons']


def test_uncached_non_toc_fields_do_not_request_toc_refresh(tmp_path):
    for instruction in (' PAGE ', ' REF myBookmark '):
        path=tmp_path/'uncached.docx';d=Document();p=d.add_paragraph()
        begin=OxmlElement('w:fldChar');begin.set(qn('w:fldCharType'),'begin');p.add_run()._r.append(begin)
        code=OxmlElement('w:instrText');code.text=instruction;p.add_run()._r.append(code)
        end=OxmlElement('w:fldChar');end.set(qn('w:fldCharType'),'end');p.add_run()._r.append(end)
        d.save(path);before=path.read_bytes()
        assert cs.toc_routing(path)['toc_action']=='not_present'
        assert path.read_bytes()==before
