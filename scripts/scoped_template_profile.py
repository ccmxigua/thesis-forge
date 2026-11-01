"""Explicit, source-bound formatting scopes; never infer school rules from defaults.

This optional adapter is intentionally separate from the legacy role-wide writer.
The caller declares roles and applicable properties. Byte/source checks establish
identity, not semantic approval, actual fonts, page layout or native acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.shared import Pt
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree

from artifact_io import commit_files, paths_alias, sibling_temp
from font_declarations import word_run_display_issue, script_selection_unresolved
from format_spec_validation import validate_instance
from semantic_contract import strict_json_loads, strict_json_read
from toc_heading_fonts import _check_style_dependencies
from toc_style_contract import _check_formatting_protection, _on_off
from toc_refresh import BLOCKED, collect_field_ranges, flatten_fields

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
M = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
SPECIAL = {W+n for n in ('object', 'drawing', 'pict', 'fldChar', 'fldSimple',
                          'instrText', 'sym', 'sdt', 'customXml', 'txbxContent')}
SPECIAL |= {M+'oMath', M+'oMathPara'}
BOOKS = {W+'bookmarkStart', W+'bookmarkEnd'}


class Unhandled(ValueError):
    """An explicit scope is unresolved; leave that entire rule unchanged."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def text(node):
    return ''.join(n.text or '' for n in node.iter(W+'t'))


def text_hash(node):
    return sha(text(node).encode())


def _path(node):
    return node.getroottree().getpath(node)


def _xml(node):
    return etree.tostring(node, with_tail=False)


def _digest(node):
    # Detached staged subtrees omit unused ancestor xmlns declarations.
    # Exclusive canonicalization compares actual structure, not that context.
    return sha(etree.tostring(node, method='c14n', exclusive=True))


def validate_profile(profile):
    root = Path(__file__).resolve().parents[1] / 'schema'
    schema = strict_json_read(root/'scoped-template-profile.schema.json')
    existing = strict_json_read(root/'format-spec.schema.json')
    # Only this bundled schema's known local references are expanded. No
    # remote schema fetching and no new optional validator dependency.
    prefix = existing['$id']+'#/'
    def local_refs(node):
        if isinstance(node, dict):
            return {k: '#/'+v[len(prefix):] if k == '$ref' and isinstance(v, str) and v.startswith(prefix)
                    else local_refs(v) for k, v in node.items()}
        if isinstance(node, list):
            return [local_refs(v) for v in node]
        return node
    schema = local_refs(schema); schema['$defs'] = existing['$defs']
    errors = validate_instance(profile, schema)
    if errors:
        raise ValueError('invalid scoped profile: '+ '; '.join(errors))
    ids = [r['id'] for r in profile['rules']]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate scoped rule id')
    for rule in profile['rules']:
        if 'header' in rule and any(k in rule for k in ('table','keyword','terminal','spacer','display_prefix')):
            raise ValueError('header action cannot be mixed with body actions')
        if 'table' in rule and (rule['role'] != 'table_caption' or any(
                k in rule for k in ('keyword','terminal','spacer','display_prefix'))):
            raise ValueError('table action requires a caption and cannot carry unrelated text actions')
        if 'keyword' in rule and (rule['role'] not in ('keywords_zh','keywords_en') or
                                  any(k in rule for k in ('terminal','display_prefix','spacer'))):
            raise ValueError('keyword action/role conflict')
        if 'terminal' in rule and rule['role'] != 'bibliography_entry':
            raise ValueError('terminal action requires bibliography entry role')
        if 'display_prefix' in rule and rule['role'] not in ('thesis_subtitle_zh','thesis_subtitle_en'):
            raise ValueError('display prefix requires a subtitle role')
        if 'boundary_edit_policy' in rule and (
                sum(k in rule for k in ('display_prefix','terminal')) != 1 or any(
                    k in rule for k in ('header','table','spacer','keyword'))):
            raise ValueError('boundary policy requires exactly one subtitle/terminal action')
        props = rule.get('paragraph', {})
        for a, b in (('first_line_indent_chars', 'first_line_indent_pt'),
                     ('space_before_lines', 'space_before_pt'), ('space_after_lines', 'space_after_pt')):
            if a in props and b in props:
                raise ValueError('conflicting relative/absolute paragraph properties')
        if any(abs(float(v)*8-round(float(v)*8)) > 1e-8
               for k, v in rule.get('table', {}).items() if k.endswith('_pt')):
            raise ValueError('border widths must be representable in eighth-points')
    def representable(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'table':
                    continue  # Table edges use eighth-points, not twips.
                if key == 'size_pt' and abs(value*2-round(value*2)) > 1e-8:
                    raise ValueError('font size must be representable in half-points')
                if key.endswith('_pt') and isinstance(value, (int,float)) and key != 'size_pt' and abs(value*20-round(value*20)) > 1e-8:
                    raise ValueError('paragraph dimensions must be representable in twips')
                representable(value)
        elif isinstance(node, list):
            for v in node:
                representable(v)
    representable(profile['rules'])
    if profile.get('navigation_target_policy','strict')!='strict' and not profile.get('repair_toc_navigation'):
        raise ValueError('navigation target policy requires explicit navigation repair')


def _select(doc, selector):
    """Exact source-text identity plus optional style/region; never a P index."""
    candidates = list(doc.paragraphs)
    region = selector.get('region')
    if region:
        starts = [i for i, p in enumerate(candidates) if text_hash(p._p) == region['after']]
        ends = [i for i, p in enumerate(candidates) if text_hash(p._p) == region['before']]
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise Unhandled('region boundary absent, repeated or reversed')
        candidates = candidates[starts[0]+1:ends[0]]
    hashes = selector['text_sha256']
    hashes = [hashes] if isinstance(hashes, str) else hashes
    selected = [p for p in candidates if text_hash(p._p) in hashes and
                ('source_style' not in selector or p.style.name == selector['source_style'])]
    if len(selected) != 1:
        raise Unhandled('paragraph source identity absent or ambiguous')
    return selected[0]


def _range_guard(node, *, disruptive=False, insertion=False):
    """Fields and range endpoints can live in other paragraphs of the story."""
    story = next((n for n in node.iterancestors() if n.tag in {W+'body', W+'hdr', W+'ftr'}), None)
    if story is None:
        # A staged detached clone is guarded through its source before cloning.
        return
    order = {n:i for i, n in enumerate(story.iter())}
    local = [order[n] for n in node.iter()]
    lo, hi = min(local), max(local)
    stack = []; intervals = []; ranges = {}; ends = {}
    starts = {'bookmarkStart':'bookmarkEnd', 'commentRangeStart':'commentRangeEnd',
              'permStart':'permEnd', 'moveFromRangeStart':'moveFromRangeEnd', 'moveToRangeStart':'moveToRangeEnd'}
    for n in story.iter():
        if n.tag == W+'fldChar':
            kind = n.get(W+'fldCharType')
            if kind == 'begin':
                stack.append(order[n])
            elif kind == 'end':
                if not stack:
                    raise Unhandled('story has an unmatched field boundary')
                intervals.append((stack.pop(), order[n]))
        for start, end in starts.items():
            key = (start, n.get(W+'id'))
            if n.tag == W+start:
                if key in ranges:
                    raise Unhandled('story range identity duplicated')
                ranges[key] = order[n]
            if n.tag == W+end:
                if key in ends:
                    raise Unhandled('story range endpoint duplicated')
                ends[key] = order[n]
    if stack:
        raise Unhandled('story has an incomplete complex field')
    if any(a <= hi and b >= lo for a, b in intervals):
        raise Unhandled('target belongs to a cross-paragraph field scope')
    if disruptive:
        if set(ranges) != set(ends) or any(ranges[k] >= ends[k] for k in ranges):
            raise Unhandled('story range graph incomplete or reversed')
        position = hi if insertion else None
        if any((a <= position < ends[k] if insertion else a <= hi and ends[k] >= lo)
               for k, a in ranges.items()):
            raise Unhandled('text/insertion would change an existing protected range')


def _guard(paragraph, *, disruptive=False):
    node = paragraph._p
    _range_guard(node, disruptive=disruptive)
    if any(n.tag in BLOCKED or n.tag == W+'sdt' for n in node.iterancestors()):
        raise Unhandled('ancestor revision, compatibility branch or content control')
    if any(n.tag in BLOCKED for n in node.iter()):
        raise Unhandled('unresolved revision/compatibility structure')
    if any(n.tag in SPECIAL for n in node.iter()):
        raise Unhandled('field, math, object or nested display scope retained')
    if node.find(W+'pPr/'+W+'sectPr') is not None:
        raise Unhandled('section-boundary paragraph retained')
    try:
        _check_style_dependencies(paragraph.style)
        for n in node.iter(W+'r'):
            if next(n.iterancestors(W+'p'), None) is not node:
                raise Unhandled('nested paragraph has a separate owner')
            if list(n.iter(W+'t')):
                run = Run(n, paragraph)
                _check_style_dependencies(run.style)
                if word_run_display_issue(n, paragraph):
                    raise Unhandled('hidden/revised/conditional run display unresolved')
                if script_selection_unresolved(run, paragraph, paragraph.part.document):
                    raise Unhandled('complex-script selection requires a separate scope')
    except ValueError as exc:
        raise Unhandled(str(exc)) from exc


def _style_size(style):
    seen = set()
    while style is not None:
        if style.style_id in seen:
            raise Unhandled('font size inheritance cycle')
        seen.add(style.style_id)
        if style.font.size is not None:
            return style.font.size.pt
        style = style.base_style
    return None


def _effective_size(paragraph, doc, font):
    if font.get('size_pt') is not None:
        return float(font['size_pt'])
    values = set()
    for node in paragraph._p.iter(W+'r'):
        if not text(node):
            continue
        run = Run(node, paragraph)
        size = run.font.size.pt if run.font.size is not None else _style_size(run.style)
        size = size if size is not None else _style_size(paragraph.style)
        if size is None:
            default = doc.styles.element.find(W+'docDefaults/'+W+'rPrDefault/'+W+'rPr/'+W+'sz')
            if default is not None:
                size = float(default.get(W+'val'))/2
        if size is None or size <= 0:
            raise Unhandled('effective text font size unresolved for relative units')
        values.add(size)
    if len(values) != 1:
        raise Unhandled('mixed or empty effective text size for relative units')
    return values.pop()


def _font(run, props):
    """Set only authorized slots; do not conflate Latin, CJK and CS faces."""
    if not props:
        return
    rpr = run._element.get_or_add_rPr()
    if props.get('cjk') or props.get('latin'):
        fonts = rpr.get_or_add_rFonts()
        if props.get('cjk'):
            fonts.set(W+'eastAsia', props['cjk'])
            fonts.attrib.pop(W+'eastAsiaTheme', None)
        if props.get('latin'):
            for slot in ('ascii', 'hAnsi'):
                fonts.set(W+slot, props['latin'])
                fonts.attrib.pop(W+slot+'Theme', None)
    for key, attr in (('size_pt', 'size'), ('bold', 'bold'), ('italic', 'italic')):
        if key in props:
            setattr(run.font, attr, Pt(float(props[key])) if key == 'size_pt' else props[key])


def _paragraph(paragraph, props, font, doc):
    if not props:
        return
    relative = ('first_line_indent_chars' in props or ('spacing_line_height_pt' not in props and
                any(k in props for k in ('space_before_lines', 'space_after_lines'))))
    size = _effective_size(paragraph, doc, font) if relative else None
    pf = paragraph.paragraph_format
    ind = paragraph._p.get_or_add_pPr().find(W+'ind')
    if ind is not None and any(k in props for k in ('left_indent_pt', 'right_indent_pt')):
        if any(W+k in ind.attrib for k in ('start','end','startChars','endChars')):
            raise Unhandled('logical/directional indentation requires a separate scope')
    if 'alignment' in props:
        pf.alignment = {'left':WD_ALIGN_PARAGRAPH.LEFT, 'center':WD_ALIGN_PARAGRAPH.CENTER,
                        'right':WD_ALIGN_PARAGRAPH.RIGHT, 'justify':WD_ALIGN_PARAGRAPH.JUSTIFY,
                        'distributed':WD_ALIGN_PARAGRAPH.DISTRIBUTE}[props['alignment']]
    for prop in ('left_indent', 'right_indent', 'first_line_indent', 'space_before', 'space_after'):
        if prop+'_pt' in props:
            setattr(pf, prop, Pt(float(props[prop+'_pt'])))
            if prop in ('left_indent', 'right_indent'):
                paragraph._p.pPr.find(W+'ind').attrib.pop(W+prop.split('_')[0]+'Chars', None)
    if 'first_line_indent_chars' in props:
        chars = float(props['first_line_indent_chars'])
        pf.first_line_indent = Pt(chars*size)
        ind = paragraph._p.get_or_add_pPr().find(W+'ind')
        ind.attrib.pop(W+'hangingChars', None)
        ind.set(W+'firstLineChars', str(round(chars*100)))
    elif 'first_line_indent_pt' in props:
        ind = paragraph._p.get_or_add_pPr().find(W+'ind')
        for name in ('firstLineChars', 'hangingChars'):
            ind.attrib.pop(W+name, None)
    for side in ('before', 'after'):
        if 'space_'+side+'_lines' in props:
            height = float(props.get('spacing_line_height_pt', size))
            setattr(pf, 'space_'+side, Pt(float(props['space_'+side+'_lines'])*height))
        if any('space_'+side+unit in props for unit in ('_pt', '_lines')):
            spacing = paragraph._p.get_or_add_pPr().find(W+'spacing')
            spacing.attrib.pop(W+side+'Lines', None)
            spacing.attrib.pop(W+side+'Autospacing', None)
    if 'line_spacing' in props:
        ls = props['line_spacing']; kind = ls['type']; value = float(ls['value'])
        if kind in {'single', 'one_point_five', 'double'} and value != {
                'single':1, 'one_point_five':1.5, 'double':2}[kind]:
            raise Unhandled('named line-spacing type/value conflict')
        pf.line_spacing = Pt(value) if kind in ('exact', 'at_least') else value
        pf.line_spacing_rule = {'exact':WD_LINE_SPACING.EXACTLY, 'at_least':WD_LINE_SPACING.AT_LEAST,
                               'multiple':WD_LINE_SPACING.MULTIPLE, 'single':WD_LINE_SPACING.SINGLE,
                               'one_point_five':WD_LINE_SPACING.ONE_POINT_FIVE,
                               'double':WD_LINE_SPACING.DOUBLE}[kind]
    for key in ('keep_with_next', 'page_break_before'):
        if key in props:
            setattr(pf, key, props[key])
    if 'outline_level' in props:
        ppr = paragraph._p.get_or_add_pPr()
        outline = ppr.find(W+'outlineLvl')
        if outline is None:
            outline = OxmlElement('w:outlineLvl'); ppr.append(outline)
        outline.set(W+'val', str(props['outline_level']-1))


def _plain_text(paragraph):
    if any(n.tag in BOOKS or n.tag in SPECIAL for n in paragraph._p.iter()):
        raise Unhandled('text change touches bookmarks, fields or objects')
    runs = list(paragraph._p.iter(W+'r'))
    if not runs or any(n.getparent() is not paragraph._p or
                       any(c.tag not in {W+'rPr', W+'t'} for c in n) or
                       len(n.findall(W+'t')) != 1 for n in runs):
        raise Unhandled('text edit needs plain, directly owned text runs')
    return runs


def _range_font(paragraph, label, label_font, value_font):
    if not text(paragraph._p).startswith(label) or not text(paragraph._p)[len(label):].strip():
        raise Unhandled('keyword label/value boundary not proved')
    offset = 0
    for node in list(paragraph._p.iter(W+'r')):
        texts = node.findall(W+'t')
        if not texts:
            continue  # Comment references/empty runs have no label/value text.
        if len(texts) != 1 or any(c.tag not in {W+'rPr', W+'t'} for c in node):
            raise Unhandled('keyword range needs plain text runs')
        value = texts[0].text or ''; split = len(label)-offset
        if 0 < split < len(value):
            right = copy.deepcopy(node)
            texts[0].text = value[:split]; right.find(W+'t').text = value[split:]
            texts[0].set(XML_SPACE, 'preserve'); right.find(W+'t').set(XML_SPACE, 'preserve')
            node.addnext(right)
            _font(Run(node, paragraph), label_font); _font(Run(right, paragraph), value_font)
        else:
            _font(Run(node, paragraph), label_font if offset < len(label) else value_font)
        offset += len(value)


def _copy_contents(target, source):
    # CT_P.text is a computed, read-only view. The paragraph/table root's
    # original raw text/tail is unchanged; replace only staged children.
    target.attrib.clear(); target.attrib.update(source.attrib)
    for c in list(target):
        target.remove(c)
    for c in source:
        target.append(copy.deepcopy(c))


def _format_rule(doc, paragraph, rule, *, boundary_records=None):
    boundary = rule.get('boundary_edit_policy') == 'navigation_only'
    _guard(paragraph, disruptive=not boundary and ('display_prefix' in rule or 'terminal' in rule))
    clone = copy.deepcopy(paragraph._p)
    staged = Paragraph(clone, paragraph._parent)
    font = rule.get('font', {}); props = rule.get('paragraph', {})
    if any(n.tag == W+'numPr' for n in clone.iter()) and rule['role'] == 'appendix_body':
        raise Unhandled('numbered appendix list is not ordinary prose')
    for node in clone.iter(W+'r'):
        if text(node):
            _font(Run(node, staged), font)
    _paragraph(staged, props, font, doc)
    if boundary:
        from word_boundary_edits import stage_boundary_edit
        try:
            record = stage_boundary_edit(doc, paragraph, clone, rule)
        except ValueError as exc:
            raise Unhandled(str(exc)) from exc
        if record is not None and boundary_records is not None:
            boundary_records.append(record)
        return clone
    if 'keyword' in rule:
        if rule['role'] not in ('keywords_zh', 'keywords_en'):
            raise Unhandled('keyword range requires an explicit keyword role')
        k = rule['keyword']; _range_font(staged, k['label'], k.get('label_font', {}), k.get('value_font', {}))
    if 'display_prefix' in rule:
        if rule['role'] not in ('thesis_subtitle_zh', 'thesis_subtitle_en'):
            raise Unhandled('decorative prefix requires an explicit subtitle role')
        runs = _plain_text(staged); prefix = rule['display_prefix']
        if not text(clone).strip():
            raise Unhandled('empty subtitle retained')
        if text(clone).startswith(prefix*2):
            raise Unhandled('duplicate decorative prefix retained for review')
        if not text(clone).startswith(prefix):
            node = copy.deepcopy(runs[0]); node.find(W+'t').text = prefix
            node.find(W+'t').set(XML_SPACE, 'preserve'); runs[0].addprevious(node)
    if 'terminal' in rule:
        if rule['role'] != 'bibliography_entry':
            raise Unhandled('terminal edit requires a bound bibliography entry')
        runs = _plain_text(staged); value = text(clone)
        if re.search(r'https?://|www\.|\bdoi\b|10\.\d{4,9}/', value, re.I):
            raise Unhandled('URL/DOI-bearing reference retained for manual punctuation review')
        config = rule['terminal']; old = next((t for t in config['accepted'] if value.endswith(t)), None)
        if old is None:
            raise Unhandled('reference terminal token absent or ambiguous')
        if old != config['value']:
            tail = next((n.find(W+'t') for n in reversed(runs) if n.find(W+'t').text), None)
            if tail is None or not tail.text.endswith(old):
                raise Unhandled('terminal token crosses run boundary')
            tail.text = tail.text[:-len(old)]+config['value']
    return clone


def _table_rule(doc, caption, rule):
    from table_roles import classify, is_unit_annotation
    _guard(caption)
    node = caption._p.getnext()
    if node is not None and node.tag == W+'p' and is_unit_annotation(text(node)):
        node = node.getnext()
    if node is None or node.tag != W+'tbl':
        raise Unhandled('data table is not adjacent to its declared caption')
    clone = copy.deepcopy(node); table = Table(clone, doc._body)
    _range_guard(node)
    decision = classify(table, caption_bound=True)
    if decision['role'] != 'data':
        raise Unhandled('protected/unknown table: '+', '.join(decision['reasons']))
    if any(n.tag in BLOCKED or n.tag == W+'sdt' for n in node.iter()):
        raise Unhandled('table has revision, MC or controlled scope')
    if node.xpath('.//w:tblPrEx/w:tblBorders'):
        raise Unhandled('row-exception borders require an explicit edge contract')
    grid = clone.find(W+'tblGrid')
    if grid is None or len(grid) != len(table.rows[0].cells):
        raise Unhandled('table grid does not prove physical cell edges')
    cfg = rule['table']; props = clone.find(W+'tblPr')
    borders = props.find(W+'tblBorders')
    if borders is None:
        borders = OxmlElement('w:tblBorders'); props.append(borders)
    for c in list(borders):
        borders.remove(c)
    widths = {'top':cfg['top_border_pt'], 'bottom':cfg['bottom_border_pt']}
    def set_border(parent, side, width=None):
        n = OxmlElement('w:'+side); n.set(W+'val', 'single' if width is not None else 'nil')
        if width is not None:
            n.set(W+'sz', str(round(float(width)*8)))
            if 'color' in cfg:
                n.set(W+'color', cfg['color'])
        parent.append(n)
    for side in ('top', 'bottom', 'left', 'right', 'insideH', 'insideV'):
        set_border(borders, side, widths.get(side))
    for row_index, row in enumerate(table.rows):
        for cell in row.cells:
            tcpr = cell._tc.get_or_add_tcPr(); cb = tcpr.find(W+'tcBorders')
            if cb is None:
                cb = OxmlElement('w:tcBorders'); tcpr.append(cb)
            for c in list(cb):
                cb.remove(c)
            for side in ('top', 'bottom', 'left', 'right', 'start', 'end', 'insideH', 'insideV', 'tl2br', 'tr2bl'):
                set_border(cb, side, cfg['header_border_pt'] if
                           (row_index == 0 and side == 'bottom') or (row_index == 1 and side == 'top') else None)
    return node, clone, decision


def _spacers(paragraph, config, doc):
    _range_guard(paragraph._p, disruptive=True, insertion=True)
    node = paragraph._p.getnext(); count = 0
    while node is not None and node.tag == W+'p' and not text(node).strip():
        _guard(Paragraph(node, paragraph._parent))
        if (any(n.tag in BOOKS or n.tag in {W+'commentReference', W+'commentRangeStart', W+'commentRangeEnd',
                                          W+'br', W+'cr', W+'tab', W+'permStart', W+'permEnd'} for n in node.iter())
                or any(c.tag not in {W+'pPr', W+'r'} for c in node)
                or any(c.tag not in {W+'rPr', W+'t'} for r in node.findall(W+'r') for c in r)
                or any(_on_off(n.get(W+'val'), default=True, label='blank page break')
                       for n in node.findall(W+'pPr/'+W+'pageBreakBefore'))):
            raise Unhandled('blank-looking paragraph carries structural semantics')
        count += 1; node = node.getnext()
    if node is None:
        raise Unhandled('spacer has no following content boundary')
    if count > config['count']:
        raise Unhandled('extra source blank paragraphs retained; deletion not authorized')
    result = []
    for _ in range(config['count']-count):
        n = OxmlElement('w:p'); p = Paragraph(n, paragraph._parent)
        # Empty managed spacers need explicit absolute line-height, not a
        # guessed number of body-font points.
        _paragraph(p, config.get('paragraph', {}), {}, doc)
        result.append(n)
    return result


def _section_nodes(doc):
    result = []; owned = []; index = 0
    for node in doc._element.body:
        if node.tag == W+'sectPr':
            result.append((node, owned)); break
        owned.append(node)
        end = node.find(W+'pPr/'+W+'sectPr') if node.tag == W+'p' else None
        if end is not None:
            result.append((end, owned)); owned = []; index += 1
    return result


def _header_rule(doc, heading, rule):
    _guard(heading)
    cfg = rule['header']; side = cfg['side']; sections = _section_nodes(doc)
    target = [i for i, (_, nodes) in enumerate(sections) if heading._p in nodes]
    if len(target) != 1:
        raise Unhandled('source heading section ownership unresolved')
    refs = []; inherited = {}
    for i, (sect, nodes) in enumerate(sections):
        for ref in sect.findall(W+'headerReference'):
            inherited[ref.get(W+'type')] = ref.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        refs.append(dict(inherited))
    ref = refs[target[0]].get(side)
    if not ref:
        raise Unhandled('header part absent, shared or inherited across sections/sides')
    rel = doc.part.rels.get(ref)
    if rel is None or rel.is_external or not rel.reltype.endswith('/header'):
        raise Unhandled('header relationship unresolved')
    part = rel.target_part; ps = part.element.findall(W+'p')
    # Distinct rIds may resolve to the SAME OPC part. Identity is the part,
    # including inherited effective references, never just the relationship id.
    uses = [doc.part.rels.get(rid) for sides in refs for rid in sides.values()]
    if sum(r is not None and not r.is_external and r.target_part is part for r in uses) != 1:
        raise Unhandled('header part is shared through effective OPC relationships')
    nonempty = [p for p in ps if text(p).strip()]
    if len(nonempty) != 1 or text_hash(nonempty[0]) != cfg['expected_text_sha256']:
        raise Unhandled('source header placeholder identity absent or ambiguous')
    p = Paragraph(nonempty[0], SimpleNamespace(part=doc.part))
    _guard(p, disruptive=cfg.get('synchronize_heading', False))
    clone = copy.deepcopy(p._p); staged = Paragraph(clone, p._parent)
    runs = _plain_text(staged)
    if cfg.get('synchronize_heading', False):
        runs[0].find(W+'t').text = text(heading._p)
        for node in runs[1:]:
            node.find(W+'t').text = ''
    for node in clone.iter(W+'r'):
        _font(Run(node, staged), rule.get('font', {}))
    _paragraph(staged, rule.get('paragraph', {}), rule.get('font', {}), doc)
    return nonempty[0], clone, part


def _evidence(doc):
    frames, malformed = collect_field_ranges(doc)
    return {'status':'native_evidence_required', 'native_verified':False,
            'toc_fields':[{'opcode':f['opcode'], 'instruction':''.join(n.text or '' for n in f['instruction_nodes']),
                           'cached_text':f['cached_text']} for f in flatten_fields(frames)],
            'field_parse_findings':malformed,
            'sections':[{'section':i+1, 'properties_xml':_xml(s).decode()} for i, (s, _) in enumerate(_section_nodes(doc))],
            'tables':[{'header_row_flags':len(t._tbl.xpath('.//w:tblHeader')),
                       'merged':bool(t._tbl.xpath('.//w:gridSpan | .//w:vMerge | .//w:hMerge'))} for t in doc.tables],
            'math_omml':len(list(doc._element.iter(M+'oMath'))),
            'opaque_objects':len(list(doc._element.iter(W+'object'))),
            'font_declarations':[dict(n.attrib) for n in doc._element.iter(W+'rFonts')],
            'pending_categories':['toc_pages_and_native_links', 'page_breaks_and_blank_space',
                                  'printed_page_number_positions', 'header_rule_appearance',
                                  'continued_table_headers', 'figure_caption_page_grouping',
                                  'opaque_equation_display_and_edit', 'actual_font_and_glyphs']}


def _figure_checks(doc, checks):
    out = []
    for cfg in checks:
        item = {'reference_label':cfg['reference_label'], 'status':'unhandled_preserved',
                'visual_order_verified':False, 'reference_semantics_verified':False,
                'automatic_reordering':False}
        try:
            caption = _select(doc, cfg['caption']); ps = doc.paragraphs
            pattern = re.compile(r'(?<![A-Za-z0-9])'+re.escape(cfg['reference_label'])+r'(?![A-Za-z0-9])')
            mentions = [p for p in ps if p._p is not caption._p and pattern.search(text(p._p))]
            # TOC/cache/field paragraphs, captions and hidden/revised branches
            # cannot supply proof of a body-prose reference.
            candidates = []
            for p in mentions:
                try:
                    _guard(p)
                    if p.style.name.lower().startswith(('caption', 'toc', '图题', '题注')):
                        continue
                    candidates.append(p)
                except Unhandled:
                    continue
            if not candidates:
                raise Unhandled('no proved plain-body first mention')
            nodes = list(doc._element.body); first = candidates[0]
            anchor = caption._p.getprevious()
            if anchor is None or anchor.tag != W+'p' or not anchor.xpath('.//w:drawing | .//w:pict'):
                raise Unhandled('figure group/anchor ownership unresolved')
            item.update(status='potential_reference_after_figure' if nodes.index(first._p) > nodes.index(anchor) else 'potential_xml_reference_precedes_figure',
                        caption_locator=_path(caption._p), reference_locator=_path(first._p),
                        suggestion='Review prose/figure ordering; floating layout and narrative intent require verification.')
        except Unhandled as exc:
            item['reason'] = str(exc)
        out.append(item)
    return out


def _protected_signature(doc):
    """Bind math/objects/fields and endpoint identity plus textual range offsets."""
    records = {}
    special = SPECIAL | BOOKS | {W+'commentRangeStart', W+'commentRangeEnd', W+'commentReference',
                                W+'permStart', W+'permEnd'}
    for part in doc.part.package.parts:
        name = str(part.partname)
        if not name.startswith('/word/') or not name.endswith('.xml'):
            continue
        root = getattr(part, 'element', None)
        if root is None:
            root = etree.fromstring(part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
        items = []
        for n in root.iter():
            if n.tag not in special:
                continue
            p = next(n.iterancestors(W+'p'), None)
            offset = None
            if n.tag in BOOKS or n.tag in {W+'commentRangeStart', W+'commentRangeEnd', W+'permStart', W+'permEnd'}:
                if p is not None:
                    # Runs are siblings of endpoints; count descendant text in
                    # the preceding siblings rather than only direct w:t.
                    offset = sum(len(text(s)) for s in n.itersiblings(preceding=True))
            items.append((n.tag, sha(etree.tostring(n, method='c14n', exclusive=True)),
                          text(p) if p is not None and offset is not None else None, offset))
        if items:
            records[name] = items
    return records


def _word_xml_state(doc):
    from word_reference_inventory import roots
    return {name:_digest(root) for name,root in roots(doc)}


def _nonboundary_control(data, stages):
    """Independent source-based formatting control, never copied from live output.

    Use whole-node replacement here rather than the live mutation helper, so
    faults in that helper cannot be repeated into the expected document.
    """
    from word_reference_inventory import roots
    doc = Document(io.BytesIO(data)); bypart = dict(roots(doc)); prepared = []
    for p, operations, spacers, baseline in stages:
        source_root = p._p.getroottree().getroot()
        partname = str(p.part.partname)
        root = bypart[partname]
        ns = {k:v for k,v in source_root.nsmap.items() if k}
        insertion = root.getroottree().xpath(_path(p._p), namespaces=ns)[0]
        replacements = []
        for target, clone, part in operations:
            control_root = bypart[str(part.partname)]
            found = control_root.getroottree().xpath(_path(target), namespaces={k:v for k,v in target.nsmap.items() if k})
            if len(found) != 1:
                raise ValueError('independent formatting-control target unresolved')
            replacements.append((found[0], copy.deepcopy(baseline if baseline is not None and target is p._p else clone)))
        prepared.append((insertion, replacements, spacers))
    for insertion, replacements, spacers in prepared:
        for target, clone in replacements:
            target.getparent().replace(target, clone)
            if target is insertion:
                insertion = clone
        for spacer in spacers:
            n = copy.deepcopy(spacer); insertion.addnext(n); insertion = n
    return doc


def _verify_boundary_inverse(doc, records, control_state, navigation=None):
    from word_boundary_edits import apply_text_records
    memory = io.BytesIO(); doc.save(memory); memory.seek(0)
    restored = Document(memory)
    if navigation and navigation['changes']:
        from toc_navigation import restore_additive_navigation
        restore_additive_navigation(restored, navigation)
    apply_text_records(restored, records, inverse=True)
    state = _word_xml_state(restored)
    if state != control_state:
        raise ValueError('complete candidate boundary inverse differs from independent formatting control')
    return {'valid':True,'scope':'complete Word XML after exact inverse vs source-derived non-text-formatting control',
            'word_xml_sha256':sha(json.dumps(state,sort_keys=True).encode()),'native_verified':False}


def apply_profile(source, profile_path, output, report_path):
    """Publish a non-release candidate and receipt as one rollback-safe batch."""
    source, profile_path, output, report_path = map(Path, (source, profile_path, output, report_path))
    data = source.read_bytes(); profile_bytes = profile_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [i.filename.casefold() for i in archive.infolist()]
        if len(set(names)) != len(names):
            raise ValueError('duplicate/ambiguous OPC member names')
    profile = strict_json_loads(profile_bytes.decode()); validate_profile(profile)
    protected = [source, profile_path]
    # Register ALL sources before any skip or mutation, even unused sources.
    for item in profile.get('sources', []):
        p = (profile_path.parent/item['path']).resolve()
        protected.append(p)
        if sha(p.read_bytes()) != item['sha256']:
            raise ValueError('profile evidence source hash mismatch')
    if any(paths_alias([dest, p]) for dest in (output, report_path) for p in protected) or paths_alias([output, report_path]):
        raise ValueError('output/report aliases a protected input or each other')
    if any(p.exists() and not p.is_file() for p in (output, report_path)):
        raise ValueError('output/report must be regular file destinations')
    if sha(data) != profile['input_sha256']:
        raise ValueError('scoped profile input hash mismatch')
    doc = Document(io.BytesIO(data)); _check_formatting_protection(doc)
    for n in doc.settings.element.findall(W+'documentProtection'):
        if _on_off(n.get(W+'enforcement'), default=False, label='document protection'):
            raise ValueError('enforced document protection')
    before_parts = {str(p.partname).lstrip('/'):p.blob for p in doc.part.package.parts}
    protected_before = _protected_signature(doc)
    report = {'protocol':'scoped_template_profile_v1', 'profile_id':profile['profile_id'],
              'declaration_origin':'caller_declared', 'semantic_authority_verified':False,
              'input_sha256':sha(data), 'profile_sha256':sha(profile_bytes),
              'rules':[], 'native_verified':False, 'submission_ready':False,
              'original_inputs_preserved':True}
    selected = []; claimed = {}
    for rule in profile['rules']:
        try:
            p = _select(doc, rule['selector'])
            previous = claimed.get(p._p, [])
            if previous and not all('header' in r and 'header' in rule and
                                    r['header']['side'] != rule['header']['side'] and
                                    r['role'] == rule['role'] for r in previous):
                raise ValueError('multiple scoped rules claim the same source paragraph')
            claimed.setdefault(p._p, []).append(rule); selected.append((rule, p))
        except Unhandled as exc:
            report['rules'].append({'id':rule['id'], 'role':rule['role'], 'status':'unhandled_preserved', 'reason':str(exc)})
    plan = SimpleNamespace(owners={}, paragraphs={})
    changed_parts = set(); staged_rules = []; effects = set(); boundary_records = []; boundary_sources = []
    for rule, p in selected:
        item = {'id':rule['id'], 'role':rule['role'], 'locator':_path(p._p), 'status':'unhandled_preserved',
                'source_evidence':rule['evidence']}
        try:
            _guard(p)
            baseline = None
            if rule['role'].startswith('heading_') or rule['role'] in ('bibliography_heading', 'abstract_title_zh', 'abstract_title_en'):
                plan.owners[p._p] = rule['role']; plan.paragraphs[p._p] = p
            if 'header' in rule:
                target, clone, part = _header_rule(doc, p, rule)
                operations = [(target, clone, part)]
            elif 'table' in rule:
                target, clone, decision = _table_rule(doc, p, rule); part = doc.part; item['table_role'] = decision['role']
                operations = [(target, clone, part)]
                if rule.get('font') or rule.get('paragraph'):
                    operations.append((p._p, _format_rule(doc, p, rule), doc.part))
            else:
                target = p._p
                pending_text = []
                clone = _format_rule(doc, p, rule, boundary_records=pending_text); part = doc.part
                operations = [(target, clone, part)]
                if pending_text:
                    no_text = {k:v for k,v in rule.items() if k not in {'display_prefix','terminal','boundary_edit_policy'}}
                    baseline = _format_rule(doc, p, no_text)
            spacers = _spacers(p, rule['spacer'], doc) if 'spacer' in rule else []
            keys = {target for target, _, _ in operations}
            if keys & effects:
                raise ValueError('scoped rules overlap an actual effect target')
            effects.update(keys)
            item['before_sha256'] = _digest(target); item['after_sha256'] = _digest(clone)
            changed = any(_digest(t) != _digest(c) for t, c, _ in operations) or bool(spacers)
            item.update(status='applied' if changed else 'already_satisfied', inserted_spacers=len(spacers))
            if rule.get('boundary_edit_policy'):
                item['boundary_text_edits'] = pending_text
                boundary_records.extend(pending_text)
                boundary_sources.extend((p,record) for record in pending_text)
            staged_rules.append((p, operations, spacers, baseline))
        except Unhandled as exc:
            item['reason'] = str(exc)
        report['rules'].append(item)
    # All scopes/effect conflicts are checked before the first live write.
    # Runtime faults abort the in-memory candidate; no output/receipt publishes.
    control_state = _word_xml_state(_nonboundary_control(data, staged_rules)) if boundary_records else None
    for p, operations, spacers, baseline in staged_rules:
        for target, clone, part in operations:
            if _digest(target) != _digest(clone):
                _copy_contents(target, clone)
                changed_parts.add(str(part.partname).lstrip('/'))
        insertion = p._p
        for node in spacers:
            insertion.addnext(node); insertion = node
        if spacers:
            changed_parts.add('word/document.xml')
    for p,record in boundary_sources:
        # Inserting a permitted spacer earlier in the story may shift the
        # paragraph locator. The source-relative w:t identity stays fixed.
        old_paragraph = record['source_paragraph_path']
        suffix = record['text_node_path'][len(old_paragraph):]
        record['final_text_node_path'] = _path(p._p)+suffix
    if boundary_records:
        from word_boundary_edits import apply_text_records
        expected = Document(io.BytesIO(data))
        apply_text_records(expected, boundary_records)
        protected_before = _protected_signature(expected)
    if _protected_signature(doc) != protected_before:
        raise ValueError('formatting altered protected content/ranges; nothing published')
    report['boundary_text_edit_count'] = len(boundary_records)
    if boundary_records:
        report['boundary_inverse'] = _verify_boundary_inverse(doc,boundary_records,control_state)
    navigation = None
    if profile.get('repair_toc_navigation', False):
        from toc_navigation import repair_toc_links, audit_toc_navigation
        frames, _ = collect_field_ranges(doc); toc = set()
        order = {n:i for i, n in enumerate(doc._element.body.iter())}
        for f in flatten_fields(frames):
            if f['opcode'] == 'TOC' and f['separator'] is not None and f['end'] is not None:
                toc.update(p._p for p in doc.paragraphs if any(order[f['separator']] < order[n] < order[f['end']] for n in p._p.iter()))
        navigation = repair_toc_links(doc, {'toc_results':toc}, plan,
                                      target_policy=profile.get('navigation_target_policy','strict'))
        if not audit_toc_navigation(doc, navigation)['valid']:
            raise ValueError('navigation integrity/rollback not verified; nothing published')
        report['navigation'] = navigation
        if navigation['changes']:
            changed_parts.update(('word/document.xml', 'word/settings.xml'))
    report['figure_order'] = _figure_checks(doc, profile.get('figure_checks', []))
    report['pending_native_evidence'] = _evidence(doc)
    after_parts = {str(p.partname).lstrip('/'):p.blob for p in doc.part.package.parts}
    unexpected = [name for name in set(before_parts) | set(after_parts) if before_parts.get(name) != after_parts.get(name) and name not in changed_parts]
    if unexpected:
        raise ValueError('unplanned package mutations: '+str(unexpected))
    staged = None; receipt = None
    try:
        staged = sibling_temp(output); receipt = sibling_temp(report_path, suffix='.json')
        if not changed_parts:
            staged.write_bytes(data)
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(staged, 'w') as dst:
                dst.comment = src.comment
                for info in src.infolist():
                    dst.writestr(info, after_parts[info.filename] if info.filename in changed_parts else src.read(info))
        final = Document(staged)
        if navigation is not None:
            audit = audit_toc_navigation(final, navigation)
            if not audit['valid']:
                raise ValueError('serialized navigation integrity failed')
            report['navigation_audit'] = audit
        if boundary_records:
            report['boundary_inverse'] = _verify_boundary_inverse(final,boundary_records,control_state,navigation)
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(staged) as published:
            if original.namelist() != published.namelist():
                raise ValueError('serialized OPC member set/order changed')
            for info in original.infolist():
                expected_payload = after_parts[info.filename] if info.filename in changed_parts else original.read(info)
                if published.read(info.filename) != expected_payload:
                    raise ValueError('serialized OPC payload differs from verified staged state: '+info.filename)
        report['output_sha256'] = sha(staged.read_bytes())
        if navigation is not None:
            report['navigation_audit']['output_sha256'] = report['output_sha256']
        report['changed_parts'] = sorted(changed_parts)
        unresolved = (any(r['status'] == 'unhandled_preserved' for r in report['rules'])
                      or any(r['status'] == 'unhandled_preserved' for r in report['figure_order'])
                      or bool(navigation and navigation['findings']))
        report['status'] = 'review_candidate_with_unhandled' if unresolved else 'review_candidate'
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        if source.read_bytes() != data or profile_path.read_bytes() != profile_bytes:
            raise ValueError('input/profile changed during application')
        for item, p in zip(profile.get('sources', []), protected[2:]):
            if sha(p.read_bytes()) != item['sha256']:
                raise ValueError('evidence source changed during application')
        commit_files([(staged, output), (receipt, report_path)])
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        if receipt is not None:
            receipt.unlink(missing_ok=True)
    return report
