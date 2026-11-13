"""Visible controls outside a TOC result cannot grant whole-paragraph writes."""
import sys
from pathlib import Path
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from toc_style_contract import bound_complex_toc_scope
from tests.test_toc_refresh_pipeline import field

CONTROLS = ('w:tab', 'w:br', 'w:cr', 'w:ptab', 'w:sym',
            'w:noBreakHyphen', 'w:softHyphen', 'w:footnoteReference',
            'w:endnoteReference', 'm:oMath')

def visible_content(tag):
    node = OxmlElement(tag)
    if tag == 'w:sym':
        node.set(qn('w:font'), 'Symbol'); node.set(qn('w:char'), 'F061')
    elif tag == 'w:ptab':
        for key, value in [('alignment', 'left'), ('relativeTo', 'margin'), ('leader', 'none')]:
            node.set(qn('w:' + key), value)
    elif tag in ('w:footnoteReference', 'w:endnoteReference'):
        node.set(qn('w:id'), '1')
    elif tag == 'm:oMath':
        run = OxmlElement('m:r'); text = OxmlElement('m:t'); text.text = 'x'
        run.append(text); node.append(run)
        return node  # OMML is a paragraph child, not a w:r child.
    run = OxmlElement('w:r'); run.append(node)
    return run

@pytest.mark.parametrize('tag', CONTROLS)
@pytest.mark.parametrize('position', ('before', 'after'))
def test_external_visible_controls_make_toc_paragraph_mixed(tag, position):
    doc = Document()
    p = doc.add_paragraph()
    if position == 'before':
        p._p.append(visible_content(tag))
    field(p, ' TOC \\o "1-3" ', 'Heading\t1')
    if position == 'after':
        p._p.append(visible_content(tag))
    before = doc._element.xml
    scope, malformed = bound_complex_toc_scope(doc, {'TOC'})
    assert p._p in scope
    assert malformed
    assert doc._element.xml == before

@pytest.mark.parametrize('tag', CONTROLS)
def test_same_control_inside_result_remains_in_scope(tag):
    doc = Document()
    p = doc.add_paragraph()
    field(p, ' TOC \\o "1-3" ', 'Heading\t1')
    end_run = p._p.xpath('.//w:fldChar[@w:fldCharType="end"]')[0].getparent()
    end_run.addprevious(visible_content(tag))
    scope, malformed = bound_complex_toc_scope(doc, {'TOC'})
    assert p._p in scope
    assert not malformed


@pytest.mark.parametrize('tag', ('w:tab', 'w:br', 'm:oMath'))
def test_cli_does_not_write_styles_for_mixed_result(tmp_path, tag):
    import json
    from docx.enum.style import WD_STYLE_TYPE
    from tests import toc_pipeline_fixture as fixture
    doc = Document(); doc.add_heading('Chapter', 1); doc.add_paragraph('Body prose')
    style = doc.styles.add_style('toc 1', WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles['Normal']
    p = doc.add_paragraph(style=style)
    field(p, ' TOC ', 'Chapter\t1')
    p._p.append(visible_content(tag))
    template = Document(); title = template.add_paragraph('目录')
    template.add_comment(title.runs, text='一级目录项：中文字体为宋体，西文字体为Times New Roman，四号。', author='Fixture')
    source = tmp_path / 'template.docx'; template.save(source)
    result, _, audit = fixture.PropertyAuthorityTests().execute(
        tmp_path, doc, {'body_text': {'font': {'size_pt': 12}}},
        {'body_text': 'Normal'}, source)
    report = json.loads((audit / 'toc-style-application.json').read_text())
    assert report['status'] == 'unhandled_preserved'
    assert report['changes'] == []
    assert result.styles['toc 1'].font.size is None
