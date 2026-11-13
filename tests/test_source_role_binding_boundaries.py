"""Direct safety-boundary regressions for the pre-mutation role snapshot."""
import sys
from pathlib import Path
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from source_role_binding import build_source_role_bindings
from tests.test_toc_refresh_pipeline import field


def test_frontmatter_and_backmatter_do_not_reopen_as_body():
    doc = Document()
    front = doc.add_paragraph('Unclassified front matter')
    heading = doc.add_heading('Chapter one', 1)
    prose = doc.add_paragraph('Ordinary body prose')
    refs = doc.add_paragraph('References')
    refs.style = doc.styles.add_style('Bibliography Heading', WD_STYLE_TYPE.PARAGRAPH)
    entry = doc.add_paragraph('[1] Smith, 2024. A title.')
    late = doc.add_heading('Late heading', 1)
    tail = doc.add_paragraph('Must not reopen body')
    b = build_source_role_bindings(doc)
    assert not b.body_allowed(front)
    assert b.bound('heading_1', heading)
    assert b.body_allowed(prose)
    assert b.bound('bibliography_entry', entry)
    assert not b.body_allowed(tail)
    assert next(r for r in b.records if r['scope'] == 'backmatter')


def test_cached_toc_heading_is_not_a_body_start():
    doc = Document()
    cached = doc.add_paragraph(style='Heading 1')
    field(cached, ' TOC \\o "1-3" ', 'Chapter one\t2')
    after = doc.add_paragraph('Front matter after cached heading')
    real = doc.add_heading('Actual chapter', 1)
    prose = doc.add_paragraph('Real body')
    b = build_source_role_bindings(doc)
    assert not b.bound('heading_1', cached)
    assert not b.body_allowed(after)
    assert b.bound('heading_1', real)
    assert b.body_allowed(prose)


def test_snapshot_keys_are_xml_elements_not_mutated_styles():
    doc = Document(); p = doc.add_paragraph('Ordinary prose')
    before = doc._element.xml
    b = build_source_role_bindings(doc)
    assert doc._element.xml == before
    assert b.body_allowed(p)
    p.style = 'Heading 1'
    assert b.body_allowed(doc.paragraphs[0])
    assert not b.bound('heading_1', p)
    new = doc.add_paragraph('Created later')
    assert not b.body_allowed(new)
    assert b.reason(new) == ('not_in_source_inventory',)


def test_unknown_normal_derived_style_and_objects_are_preserved():
    doc = Document()
    custom = doc.styles.add_style('Unknown semantic role', WD_STYLE_TYPE.PARAGRAPH)
    custom.base_style = doc.styles['Normal']
    p = doc.add_paragraph('Specialized content', custom)
    math = doc.add_paragraph('Equation prose')
    math._p.append(OxmlElement('m:oMath'))
    drawing = doc.add_paragraph('Figure content')
    drawing.add_run()._r.append(OxmlElement('w:drawing'))
    body = doc.add_paragraph('Ordinary prose')
    b = build_source_role_bindings(doc)
    assert not b.body_allowed(p)
    assert not b.body_allowed(math)
    assert not b.body_allowed(drawing)
    assert b.body_allowed(body)


def test_direct_outline_body_override_prevents_heading_promotion():
    doc = Document()
    p = doc.add_heading('Named heading but explicit body outline', 1)
    level = OxmlElement('w:outlineLvl'); level.set(qn('w:val'), '9')
    p._p.get_or_add_pPr().append(level)
    b = build_source_role_bindings(doc)
    assert not b.bound('heading_1', p)


def test_table_cell_heading_cannot_start_body_region():
    doc = Document()
    doc.add_paragraph('Abstract', 'Title')
    table = doc.add_table(rows=1, cols=2)
    cell = table.cell(0, 0).paragraphs[0]; cell.text = 'Fake heading'; cell.style = 'Heading 1'
    table.cell(0, 1).text = 'Value'
    actual = doc.add_heading('Actual chapter', 1)
    b = build_source_role_bindings(doc)
    assert not b.bound('heading_1', cell)
    assert not b.table_allowed(cell)
    assert b.bound('heading_1', actual)


def test_explicit_body_mapping_cannot_override_toc_scope():
    doc = Document()
    custom = doc.styles.add_style('Mapped Body', WD_STYLE_TYPE.PARAGRAPH)
    p = doc.add_paragraph(style=custom); field(p, ' TOC ', 'Heading\t2')
    b = build_source_role_bindings(doc, explicit_mappings={'body_text': 'Mapped Body'})
    assert not b.body_allowed(p)
    assert b.bound('toc', p)


def test_annotation_adjoining_table_is_not_body_prose():
    doc = Document(); doc.add_heading('Chapter', 1)
    table = doc.add_table(rows=2, cols=2)
    for row in table.rows:
        for cell in row.cells:
            cell.text = 'Data'
    annotation = doc.add_paragraph('Source: synthetic data')
    prose = doc.add_paragraph('Independent body prose')
    b = build_source_role_bindings(doc)
    assert not b.body_allowed(annotation)
    assert 'table_annotation_role_unresolved' in b.reason(annotation)
    assert b.body_allowed(prose)
