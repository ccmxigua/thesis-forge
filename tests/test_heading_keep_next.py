from __future__ import annotations
import hashlib
from pathlib import Path
import sys
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import apply_format_spec as afs


def digest(p):return hashlib.sha256(etree.tostring(p._p,method='c14n')).hexdigest()


def fixture():
    d=Document();p=d.add_paragraph('1.2 来源确认的二级标题');p.paragraph_format.keep_with_next=False
    body=d.add_paragraph('正文不应改变。');body.add_run().add_break()
    ole=OxmlElement('w:object');body.runs[0]._r.append(ole)
    binding={'paragraph_index':0,'expected_xml_sha256':digest(p),'role':'heading_2',
             'rule_source':{'requirement_id':'R1','clause_ids':['C1'],'evidence_ids':['E1'],'source_text':'二级标题与下一段同页。','keep_with_next':True}}
    return d,p,body,binding


def test_existing_role_format_already_overrides_direct_false():
    d,p,body,b=fixture();afs.apply_direct_format(p,{'paragraph':{'keep_with_next':True}})
    assert p.paragraph_format.keep_with_next is True


def test_bound_normal_heading_changes_only_keep_next():
    assert hasattr(afs,'apply_heading_keep_with_next'),'Source-bound heading pagination is not implemented'
    d,p,body,b=fixture();before=etree.tostring(body._p)
    receipt=afs.apply_heading_keep_with_next(d,[b],enabled=True)
    assert p.style.name=='Normal'
    assert p.paragraph_format.keep_with_next is True
    assert etree.tostring(body._p)==before
    assert receipt[0]['role']=='heading_2'
    assert receipt[0]['before'] is False and receipt[0]['after'] is True


@pytest.mark.parametrize('mutation',['role','hash','rule'])
def test_unbound_heading_is_not_guessed(mutation):
    assert hasattr(afs,'apply_heading_keep_with_next')
    d,p,body,b=fixture();before=etree.tostring(d._element)
    if mutation=='role':b['role']='body_text'
    if mutation=='hash':b['expected_xml_sha256']='0'*64
    if mutation=='rule':b['rule_source']['evidence_ids']=[]
    with pytest.raises(ValueError):afs.apply_heading_keep_with_next(d,[b],enabled=True)
    assert etree.tostring(d._element)==before


def test_disabled_and_explicit_false_are_respected():
    assert hasattr(afs,'apply_heading_keep_with_next')
    d,p,body,b=fixture();before=etree.tostring(d._element)
    assert afs.apply_heading_keep_with_next(d,[b],enabled=False)==[]
    assert etree.tostring(d._element)==before
    p.paragraph_format.keep_with_next=True;b['expected_xml_sha256']=digest(p);b['rule_source']['keep_with_next']=False
    afs.apply_heading_keep_with_next(d,[b],enabled=True)
    assert p.paragraph_format.keep_with_next is False


def test_all_bindings_are_validated_before_any_mutation():
    assert hasattr(afs,'apply_heading_keep_with_next')
    d,p,body,b=fixture();before=etree.tostring(d._element)
    bad=dict(b,paragraph_index=99)
    with pytest.raises(ValueError):afs.apply_heading_keep_with_next(d,[b,bad],enabled=True)
    assert etree.tostring(d._element)==before
