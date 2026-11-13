"""Request native TOC repagination after formatting; never invent cached pages.

Word/WPS fields contain saved display values, not a layout-independent page map.
Dirty markers request native refresh; they are explicitly not refresh evidence.
"""
from __future__ import annotations

import re
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
BLOCKED = {W+n for n in ('ins','del','moveFrom','moveTo','delText','pPrChange','rPrChange',
                       'sectPrChange','tblPrChange','trPrChange','tcPrChange','tblGridChange','numberingChange')}
BLOCKED |= {MC+n for n in ('AlternateContent','Choice','Fallback')}
SUPPORTED_TOC_CHILD_FIELDS = frozenset({'HYPERLINK', 'PAGEREF'})


def collect_field_ranges(doc):
    """Collect complete complex field ranges across paragraphs before mutation.

    Arbitrary PAGEREFs outside a dynamic TOC are left alone. Malformed, locked
    or conditional TOCs are retained and reported without a partial update.
    """
    stack = []
    roots = []
    malformed = []
    for node in doc._element.body.iter():
        if node.tag == W+'fldSimple':
            if re.match(r'\s*TOC\b', node.get(W+'instr', ''), re.I):
                malformed.append('simple_toc_field_not_supported')
            if stack:
                stack[-1]['simple_fields'].append(node)
        if node.tag == W+'fldChar':
            kind = node.get(W+'fldCharType')
            if kind == 'begin':
                paragraph = next(node.iterancestors(W+'p'), None)
                frame = {'begin':node, 'end':None, 'separator':None, 'instruction':[], 'instruction_nodes':[], 'separate':False,
                         'children':[], 'simple_fields':[], 'parent':stack[-1] if stack else None,
                         'parent_result':stack[-1]['separate'] if stack else None,
                         'unsafe':paragraph is not None and any(n.tag in BLOCKED for n in paragraph.iter()),
                         'cached_text':[]}
                stack.append(frame)
            elif kind == 'separate':
                if not stack or stack[-1]['separate']:
                    malformed.append('unexpected_field_separator')
                else:
                    stack[-1]['separate'] = True
                    stack[-1]['separator'] = node
            elif kind == 'end':
                if not stack:
                    malformed.append('unmatched_field_end')
                else:
                    frame = stack.pop()
                    frame['end'] = node
                    if not frame['separate'] or not ''.join(frame['instruction']).strip():
                        malformed.append('incomplete_field')
                    frame['opcode'] = re.split(r'\s+', ''.join(frame['instruction']).strip())[0].upper() if frame['instruction'] else ''
                    if stack:
                        stack[-1]['children'].append(frame)
                    else:
                        roots.append(frame)
        elif node.tag == W+'instrText':
            if not stack:
                malformed.append('orphan_field_instruction')
            elif not stack[-1]['separate']:
                stack[-1]['instruction'].append(node.text or '')
                stack[-1]['instruction_nodes'].append(node)
        elif node.tag == W+'t' and stack and stack[-1]['separate']:
            stack[-1]['cached_text'].append(node.text or '')
        if stack and (node.tag in BLOCKED or any(a.tag in BLOCKED for a in node.iterancestors())):
            for frame in stack:
                frame['unsafe'] = True
    if stack:
        malformed.append('unterminated_field')

    return roots, malformed


def flatten_fields(frames):
    for frame in frames:
        yield frame
        yield from flatten_fields(frame['children'])


def toc_result_paragraphs(doc):
    """Identify cached TOC paragraphs before role assignment or text mutation.

    Complex fields span paragraphs and may split their instruction across runs.
    Reuse the field parser; simple fields and explicit TOC styles also protect
    saved results even when a producer has omitted the complex-field wrapper.
    This is cache identification, not a claim that cached page numbers are valid.
    """
    from docx_semantics import all_body_paragraphs
    paragraphs = list(all_body_paragraphs(doc))
    protected = set()
    order = {node: index for index, node in enumerate(doc._element.body.iter())}
    frames, _ = collect_field_ranges(doc)
    intervals = [(order[f['separator']], order[f['end']])
                 for f in flatten_fields(frames)
                 if f['opcode'] == 'TOC' and f['separator'] is not None and f['end'] is not None]
    for paragraph in paragraphs:
        nodes = list(paragraph._p.iter())
        style = paragraph.style
        if any(start < order[node] < end for start, end in intervals for node in nodes):
            protected.add(paragraph._p)
        if any(node.tag == W+'fldSimple' and re.match(r'\s*TOC\b', node.get(W+'instr', ''), re.I)
               for node in nodes):
            protected.add(paragraph._p)
        if style is not None and any(re.fullmatch(r'(?:TOC|目录)\s*[1-9]', value or '', re.I)
                                     for value in (style.name, style.style_id)):
            protected.add(paragraph._p)
    return protected


def request_toc_refresh(doc, *, apply=True, request_document_update=True):
    """Request refresh only after the shared full-story field preflight."""
    roots, malformed = collect_field_ranges(doc)
    tocs = [f for f in flatten_fields(roots) if f['opcode'] == 'TOC']
    report = {'protocol':'native_toc_refresh_request_v1', 'toc_count':len(tocs),
              'status':'not_applicable', 'field_results_recomputed':False,
              'native_page_numbers_verified':False, 'cached_pages_trusted':False,
              'refresh_flags_written':False,
              'refresh_requests':[], 'findings':[]}
    if malformed:
        report.update(status='unhandled_preserved')
        report['findings'] = [{'code':'toc_refresh_structure_unresolved', 'reasons':sorted(set(malformed))}]
        return report
    selected = []
    for toc in tocs:
        frames = list(flatten_fields([toc]))
        for child in frames[1:]:
            # The supported cache representation contains only internal
            # link/page-reference field types, at any nesting depth. A known
            # outer TOC never authorizes evaluation of arbitrary child code.
            if child['opcode'] not in SUPPORTED_TOC_CHILD_FIELDS:
                report['findings'].append({'code':'toc_refresh_unsupported_child_field',
                    'opcode':child['opcode'],
                    'locator':child['begin'].getroottree().getpath(child['begin'])})
            if not child['parent_result']:
                report['findings'].append({'code':'toc_refresh_child_instruction_scope_unhandled',
                    'opcode':child['opcode'],
                    'locator':child['begin'].getroottree().getpath(child['begin'])})
        ancestors = []
        parent = toc['parent']
        while parent is not None:
            ancestors.append(parent)
            parent = parent['parent']
        boundary = frames + ancestors
        if ancestors:
            report['findings'].append({'code':'toc_refresh_enclosing_field_scope_unhandled'})
        if any(f['unsafe'] for f in boundary):
            report['findings'].append({'code':'toc_refresh_display_scope_unresolved'})
        locks = [f['begin'].get(W+'fldLock', '').casefold() for f in boundary]
        if any(v in ('1','true','on') for v in locks):
            report['findings'].append({'code':'toc_refresh_locked_field'})
        if any(v not in ('','0','false','off','1','true','on') for v in locks):
            report['findings'].append({'code':'toc_refresh_lock_state_unresolved'})
        if any(f['simple_fields'] for f in frames) or next(toc['begin'].iterancestors(W+'fldSimple'), None) is not None:
            # Simple cached subfields and enclosing simple fields are not
            # proven refresh boundaries. Preserve them even when unlocked.
            report['findings'].append({'code':'toc_refresh_simple_field_scope_unhandled'})
        selected.extend(f for f in frames if f['opcode'] in ('TOC','PAGEREF'))
    if report['findings']:
        report['status'] = 'unhandled_preserved'
        return report
    for frame in selected:
        if apply:
            frame['begin'].set(qn('w:dirty'), 'true')
        report['refresh_requests'].append({
            'opcode':frame['opcode'], 'instruction':''.join(frame['instruction']),
            'locator':frame['begin'].getroottree().getpath(frame['begin']),
            'previous_cached_text':''.join(frame['cached_text']),
            'status':'requires_native_update_and_verification'})
    if selected:
        if apply and request_document_update:
            setting = doc.settings.element.find(W+'updateFields')
            if setting is None:
                setting = OxmlElement('w:updateFields')
                doc.settings.element.append(setting)
            setting.set(qn('w:val'), 'true')
        if apply:
            report['refresh_flags_written'] = True
        report['document_wide_update_requested'] = bool(apply and request_document_update)
        report['status'] = 'native_update_required'
        report['required_action'] = 'Update the entire TOC in the target native engine, save/reopen, and compare each cached page with its actual destination/footer.'
    return report
