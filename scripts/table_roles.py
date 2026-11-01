"""Conservative table roles and read-only border preservation evidence.

A table-shaped OOXML object is not evidence of a data table. Only an
unambiguous, mapped caption can grant the ordinary data-table rules. Forms,
layout containers, merges and unknown structures never gain that permission
from their position, school name, cell contents or existing border widths.
"""
from __future__ import annotations

import hashlib
import json
import re
from docx.oxml.ns import qn
from lxml import etree


def locator(table):
    return table._tbl.getroottree().getpath(table._tbl)


def is_unit_annotation(text):
    """A narrow standalone unit note, never arbitrary intervening prose."""
    unit = (r'(?:mm|cm|m|km|mg|g|kg|t|ms|s|min|h|kPa|MPa|Pa|Hz|kHz|MHz|'
            r'W|kW|MW|J|kJ|mol|L|mL|%|％|℃|°C|元|万元|人|天|无量纲|dimensionless|unitless)')
    dimensionless_label = r'(?:[A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff0-9_ ]{0,19})\s*[:：]?\s*(?:无量纲|dimensionless|unitless)'
    item = rf'(?:{unit}|{dimensionless_label})'
    return bool(re.fullmatch(rf'(?:单位|units?)\s*[:：]\s*{item}(?:\s*[、,，;；]\s*{item})*', text.strip(), re.I))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _tree(node):
    if node is None:
        return None
    return [node.tag, sorted(node.attrib.items()), node.text or '', [_tree(c) for c in node]]


def _style_chain(table, node=None):
    styles = table.part.document.styles.element
    by_id = {s.get(qn('w:styleId')): s for s in styles.findall(qn('w:style'))}
    props = (node if node is not None else table._tbl).find(qn('w:tblPr'))
    ref = props.find(qn('w:tblStyle')) if props is not None else None
    style_id = ref.get(qn('w:val')) if ref is not None else next(
        (s.get(qn('w:styleId')) for s in by_id.values()
         if s.get(qn('w:type')) == 'table' and s.get(qn('w:default')) in ('1', 'true')), None)
    seen = set(); chain = []; error = None
    while style_id:
        if style_id in seen or style_id not in by_id:
            error = 'table_style_inheritance_unresolved'; break
        seen.add(style_id); style = by_id[style_id]
        if style.get(qn('w:type')) != 'table':
            error = 'table_style_type_unresolved'; break
        chain.append(style)
        base = style.find(qn('w:basedOn'))
        style_id = base.get(qn('w:val')) if base is not None else None
    return chain, error


def border_state(table):
    """Bind borders to their inheritance, row/cell and conditional context."""
    chain, error = _style_chain(table)
    tbl = table._tbl
    direct = _tree(tbl.tblPr.find(qn('w:tblBorders'))) if tbl.tblPr is not None else None
    cells = [_tree(tc.find(qn('w:tcPr'))) for tc in tbl.iter(qn('w:tc'))]
    inherited = [{'style_id': s.get(qn('w:styleId')), 'definition': _tree(s)}
                 for s in chain]
    def context(node):
        return {'table_properties': _tree(node.find(qn('w:tblPr'))),
                'grid': _tree(node.find(qn('w:tblGrid'))),
                'rows': [{'properties': _tree(row.find(qn('w:trPr'))),
                          'table_property_exceptions': _tree(row.find(qn('w:tblPrEx'))),
                          'cells': [{'properties': _tree(cell.find(qn('w:tcPr'))),
                                     'nested_tables': [context(n) for n in cell.findall(qn('w:tbl'))]}
                                    for cell in row.findall(qn('w:tc'))]}
                         for row in node.findall(qn('w:tr'))]}
    nested_styles = []
    for nested in tbl.iter(qn('w:tbl')):
        if nested is tbl:
            continue
        nested_chain, nested_error = _style_chain(table, nested)
        nested_styles.append({'styles': [_tree(s) for s in nested_chain], 'error': nested_error})
    state = {'protocol': 'protected_table_border_context_v2',
             'direct_table_borders': direct, 'cell_properties': cells,
             'style_chain': inherited, 'inheritance_error': error,
             'table_context': context(tbl), 'nested_style_contexts': nested_styles,
             'structural_property_contexts': [
                 {'locator':node.getroottree().getpath(node), 'properties':_tree(node)}
                 for node in tbl.iter() if node.tag in {
                     qn('w:tblPr'), qn('w:tblPrEx'), qn('w:tblGrid'), qn('w:trPr'), qn('w:tcPr')} ]}
    return {'sha256': _hash(state), 'declarations': state}


def classify(table, *, caption_bound=False):
    """Protect positive non-data evidence before considering a data selector."""
    tbl = table._tbl
    rows = tbl.findall(qn('w:tr'))
    cells = [row.findall(qn('w:tc')) for row in rows]
    texts = [[''.join(t.text or '' for t in tc.iter(qn('w:t'))).strip() for tc in row] for row in cells]
    reasons = []
    # These are structural field delimiters, not a school/identity vocabulary.
    plain_field = re.compile(r'(?:姓名|学号|作者|论文作者|指导教师|导师|专业|专业名称|'
        r'学科专业|申请学位|培养单位|研究方向|name|student\s*(?:id|number)|author|'
        r'supervisor|advisor|department|degree|签名|签字|作者签名|作者签字|'
        r'导师签名|导师签字|盖章|signature)', re.I)
    def field_label(text):
        return bool(re.fullmatch(r'[^:：\r\n]{1,80}[:：]', text) or plain_field.fullmatch(text))
    paired_fields = bool(texts) and all(
        len(row) >= 2 and len(row) % 2 == 0
        and all(field_label(row[i]) and not field_label(row[i+1]) for i in range(0, len(row), 2))
        for row in texts)
    if paired_fields:
        role = 'form'; reasons.append('label_value_fields')
    elif tbl.xpath('.//w:ffData|.//w:sdt'):
        role = 'unknown'; reasons.append('form_or_data_controls_require_source_role')
    elif tbl.xpath('.//w:tbl|.//w:gridSpan|.//w:vMerge|.//w:hMerge'):
        role = 'unknown'; reasons.append('nested_or_merged_table_requires_cell_edge_contract')
    elif tbl.tblPr is None or not rows or len(cells[0]) < 2 or any(len(row) != len(cells[0]) for row in cells):
        role = 'unknown'; reasons.append('tabular_structure_unresolved')
    else:
        role = 'data' if caption_bound else 'unknown'
        reasons.append('unique_source_side_mapped_caption' if caption_bound else 'no_bound_data_table_role')
    chain, error = _style_chain(table)
    conditional_cell_borders = any(
        n.tag == qn('w:tcBorders') and any(c.get(qn('w:val')) not in ('nil','none') for c in n)
        for style in chain for n in style.iter())
    if role == 'data' and (error or conditional_cell_borders):
        role = 'unknown'; reasons = [error or 'inherited_cell_borders_require_explicit_override_contract']
    return {'locator': locator(table), 'role': role, 'reasons': reasons,
            'caption_bound': caption_bound, 'row_count': len(rows),
            'embedded_drawing_count': len(tbl.xpath('.//w:drawing|.//w:pict')),
            'physical_cell_counts': [len(row) for row in cells],
            'table_xml_sha256': hashlib.sha256(etree.tostring(tbl, with_tail=False)).hexdigest(),
            'border_state': border_state(table)}


def protected_snapshot(doc, decisions):
    by_path = {locator(t): t for t in doc.tables}
    return {d['locator']: border_state(by_path[d['locator']])
            for d in decisions if d['role'] != 'data'}


def audit_protected(doc, snapshot):
    by_path = {locator(t): t for t in doc.tables}; findings = []
    for path, expected in snapshot.items():
        current = border_state(by_path[path]) if path in by_path else None
        if current != expected:
            findings.append({'role':'table', 'property':'tables.protected_template_borders',
                'failure_type':'protected_table_borders_changed', 'locator':path,
                'template_value':current, 'required_value':expected,
                'reason':'a non-data table lost its exact direct or inherited border declarations'})
    return findings
