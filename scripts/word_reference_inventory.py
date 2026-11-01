"""Conservative, read-only Word range consumers across all package stories.

This proves only the supplied package. Unknown/dynamic field syntax cannot
authorize a boundary edit or additive TOC target migration.
"""
from __future__ import annotations

import re
from lxml import etree
from toc_refresh import BLOCKED

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
MC_TAGS = {MC+k for k in ('AlternateContent','Choice','Fallback')}


def _branches(node):
    return tuple(_path(a) for a in node.iterancestors() if a.tag in {MC+'Choice',MC+'Fallback'})


def _selection_tags(node):
    return {a.tag for a in [node]+list(node.iterancestors()) if a.tag in BLOCKED or a.tag == W+'sdt'}


def _story(node):
    owner = next((a for a in [node]+list(node.iterancestors()) if a.tag in {
        W+k for k in ('body','hdr','ftr','footnote','endnote','comment','txbxContent')}), None)
    return _path(owner)


def _owner(node):
    return next(node.iterancestors(W+'p'), None)


def _path(node):
    return node.getroottree().getpath(node) if node is not None else None


def roots(doc):
    for part in doc.part.package.parts:
        name = str(part.partname)
        if not name.startswith('/word/') or not name.endswith('.xml'):
            continue
        root = getattr(part, 'element', None)
        if root is None:
            root = etree.fromstring(part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
        yield name, root


def _parse(instruction):
    op = re.match(r'\s*([A-Z]+)\b', instruction, re.I)
    if not op:
        raise ValueError('field opcode absent')
    op = op[1].upper()
    if op in {'HYPERLINK','PAGEREF','REF','NOTEREF'}:
        prefix = r'HYPERLINK\s+\\l' if op == 'HYPERLINK' else op
        m = re.fullmatch(r'\s*'+prefix+r'\s+(?:"([^"\r\n]+)"|([^\s"\\]+))'
                         r'(?:\s+\\[hpw]\s*)*\s*', instruction, re.I)
        if not m:
            raise ValueError('unsupported reference field grammar')
        return op, m[1] or m[2]
    if op in {'PAGE','NUMPAGES','SECTION','SECTIONPAGES'}:
        if not re.fullmatch(r'\s*'+op+r'(?:\s+\\\*\s+(?:Arabic|ROMAN|roman|MERGEFORMAT))*\s*', instruction):
            raise ValueError('unsupported page field grammar')
        return op, None
    if op == 'TOC':
        if not re.fullmatch(r'\s*TOC(?:\s+\\(?:[hzu]|o\s+"[1-9]-[1-9]"))*\s*', instruction, re.I):
            raise ValueError('unresolved TOC selection or bookmark scope')
        return op, None
    raise ValueError('unsupported/dynamic field opcode: '+op)


def inspect_references(doc):
    records = []; bookmarks = []; findings = []
    parts = {str(p.partname):p for p in doc.part.package.parts}
    for name, root in roots(doc):
        stack = []; starts = {}; ends = {}; positions = {n:i for i,n in enumerate(root.iter())}
        for n in root.iter():
            if n.tag in {W+'bookmarkStart',W+'bookmarkEnd'}:
                sid = n.get(W+'id'); dest = starts if n.tag == W+'bookmarkStart' else ends
                if not sid or sid in dest:
                    findings.append('missing/duplicate bookmark id in '+name)
                dest[sid] = n
                if any(a.tag in BLOCKED or a.tag == W+'sdt' for a in n.iterancestors()):
                    findings.append('conditional/revised bookmark scope in '+name)
            if n.tag == W+'fldSimple':
                try:
                    if _story(n) is None:raise ValueError('field has no supported logical story')
                    op, target = _parse(n.get(W+'instr',''))
                    if op == 'TOC' or stack or any(c.tag in {W+'fldSimple',W+'fldChar'} for c in list(n.iter())[1:]):
                        raise ValueError('nested/simple field interpretation unresolved')
                    if any(a.tag in BLOCKED or a.tag == W+'sdt' for a in list(n.iter())+list(n.iterancestors())):
                        raise ValueError('conditional/revised field selection')
                    records.append({'part':name,'kind':op,'target':target,'node':_path(n),
                                    'owner':_path(_owner(n)),'in_toc_result':False,'instruction':n.get(W+'instr','')})
                except ValueError as exc:
                    findings.append(name+': '+str(exc))
            elif n.tag == W+'fldChar':
                kind = n.get(W+'fldCharType')
                if kind == 'begin':
                    if any(not f['separate'] for f in stack):
                        findings.append(name+': dynamic instruction-side nested field')
                    if _story(n) is None or any(f['story']!=_story(n)for f in stack):
                        findings.append(name+': field crosses logical stories')
                    in_toc = any(f['separate'] and re.match(r'\s*TOC\b',''.join(f['instruction']),re.I) for f in stack)
                    stack.append({'begin':n,'instruction':[],'separate':False,'in_toc':in_toc,
                                  'selection_tags':_selection_tags(n),'branches':_branches(n),'cross_branch':False,
                                  'story':_story(n),'cross_story':False})
                elif kind == 'separate':
                    if not stack or stack[-1]['separate']:
                        findings.append(name+': invalid field separator')
                    else:
                        stack[-1]['separate'] = True
                elif kind == 'end':
                    if not stack:
                        findings.append(name+': unmatched field end'); continue
                    f = stack.pop()
                    try:
                        instruction = ''.join(f['instruction']); op,target = _parse(instruction)
                        tags = f['selection_tags'] | _selection_tags(n)
                        if not f['separate'] or f['cross_branch'] or _branches(n) != f['branches'] or f['cross_story'] or _story(n)!=f['story']:
                            raise ValueError('incomplete/cross-branch/cross-story field')
                        # A complete literal PAGE field reads no bookmark in
                        # ANY branch. Keep native selection unresolved; this
                        # is only a conservative union-of-consumers proof.
                        selection_unresolved = bool(tags)
                        if tags and not (tags <= MC_TAGS and op in {'PAGE','NUMPAGES','SECTION','SECTIONPAGES'}):
                            raise ValueError('conditional/revised consumer selection unresolved')
                        records.append({'part':name,'kind':op,'target':target,'node':_path(f['begin']),
                                        'owner':_path(_owner(f['begin'])),'in_toc_result':f['in_toc'],
                                        'instruction':instruction,'display_selection_unresolved':selection_unresolved,
                                        'consumer_class':'non_text_page' if op in {'PAGE','NUMPAGES','SECTION','SECTIONPAGES'} else
                                        'text_copy' if op in {'REF','NOTEREF'} else 'navigation' if target else 'toc_selection'})
                    except ValueError as exc:
                        findings.append(name+': '+str(exc))
                else:
                    findings.append(name+': unknown field delimiter')
            elif n.tag == W+'instrText':
                if not stack or stack[-1]['separate']:
                    findings.append(name+': orphan/result-side field instruction')
                else:
                    stack[-1]['instruction'].append(n.text or '')
            elif n.tag == W+'hyperlink' and n.get(W+'anchor'):
                rid=n.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                rel=parts[name].rels.get(rid)if rid else None
                if rid and (rel is None or not rel.reltype.endswith('/hyperlink')):
                    findings.append(name+': hyperlink relationship unresolved')
                records.append({'part':name,'kind':'EXTERNAL_ANCHOR'if rel is not None and rel.is_external else'ANCHOR',
                                'target':n.get(W+'anchor'),'relationship_target':str(rel.target_ref)if rel is not None else None,
                                'node':_path(n),'owner':_path(_owner(n)),'in_toc_result':False,'instruction':None})
                if any(a.tag in BLOCKED or a.tag == W+'sdt' for a in list(n.iter())+list(n.iterancestors())):
                    findings.append(name+': conditional/revised hyperlink selection')
            if stack:
                for f in stack:
                    f['selection_tags'].update(_selection_tags(n))
                    if n.tag in {W+'fldChar',W+'instrText'} and _branches(n) != f['branches']:
                        f['cross_branch'] = True
                    if n.tag in {W+'fldChar',W+'instrText'} and _story(n)!=f['story']:
                        f['cross_story'] = True
        if stack:
            findings.append(name+': unterminated field')
        if set(starts) != set(ends):
            findings.append(name+': incomplete bookmark graph')
        for sid,start in starts.items():
            end = ends.get(sid)
            if end is None:
                continue
            if positions[start] >= positions[end] or not start.get(W+'name'):
                findings.append(name+': reversed/unnamed bookmark')
            if _story(start) is None or _story(start)!=_story(end):
                findings.append(name+': bookmark crosses logical stories')
            bookmarks.append({'part':name,'id':sid,'name':start.get(W+'name'),
                              'start':_path(start),'end':_path(end)})
    names = [b['name'].casefold() for b in bookmarks if b['name']]
    if len(names) != len(set(names)):
        findings.append('bookmark names ambiguous across stories')
    return {'protocol':'word_reference_inventory_v1','valid':not findings,
            'references':records,'bookmarks':bookmarks,'findings':findings,
            'scope':'supplied_package_only; external incoming references are not observable'}


def boundary_range_proof(doc, paragraph, text_node):
    inventory = inspect_references(doc)
    if not inventory['valid']:
        raise ValueError('reference inventory unresolved: '+str(inventory['findings']))
    story = next((n for n in paragraph._p.iterancestors() if n.tag in {W+'body',W+'hdr',W+'ftr'}), None)
    if story is None:
        raise ValueError('source story unresolved')
    positions = {n:i for i,n in enumerate(story.iter())}; position = positions[text_node]
    active = []; starts = {}; ends = {}
    kinds = ('bookmark','commentRange','perm','moveFromRange','moveToRange')
    for n in story.iter():
        for kind in kinds:
            for suffix, dest in (('Start',starts),('End',ends)):
                if n.tag == W+kind+suffix:
                    key = (kind,n.get(W+'id'))
                    if not key[1] or key in dest:
                        raise ValueError('missing/duplicate source range endpoint')
                    dest[key] = n
    if set(starts) != set(ends) or any(positions[a] >= positions[ends[k]] for k,a in starts.items()):
        raise ValueError('source story range graph incomplete/reversed')
    for key,start in starts.items():
        if positions[start] < position < positions[ends[key]]:
            kind,sid = key
            if kind in ('commentRange','perm','moveFromRange','moveToRange'):
                raise ValueError('comment/permission/revision range cannot authorize text edits')
            name = start.get(W+'name') if kind == 'bookmark' else None
            users = [r for r in inventory['references'] if name and r['target'] and r['target'].casefold() == name.casefold()]
            if any(r['kind'] not in {'HYPERLINK','PAGEREF','ANCHOR'} for r in users):
                raise ValueError('protected range has a text-consuming reference: '+str(name))
            active.append({'kind':kind,'id':sid,'name':name,'consumers':users})
    return {'active_ranges':active,'reference_inventory':inventory,
            'policy':'navigation_only; marker nodes and authored text outside boundary token retained'}


def reject_control_range_intersection(node, *, outside_inline_comments=False):
    """A new target/field edit must not cross comment/permission/move ranges."""
    story = next((n for n in node.iterancestors() if n.tag in {W+'body',W+'hdr',W+'ftr'}), None)
    if story is None:raise ValueError('navigation control-range story unresolved')
    order = {n:i for i,n in enumerate(story.iter())}; lo,hi = min(order[n] for n in node.iter()),max(order[n] for n in node.iter())
    starts={};ends={}
    for n in story.iter():
        for kind in ('commentRange','perm','moveFromRange','moveToRange'):
            for suffix,dest in (('Start',starts),('End',ends)):
                if n.tag==W+kind+suffix:
                    key=(kind,n.get(W+'id'))
                    if not key[1] or key in dest:raise ValueError('navigation control-range identity unresolved')
                    dest[key]=n
    if set(starts)!=set(ends)or any(order[s]>=order[ends[k]]for k,s in starts.items()):
        raise ValueError('navigation control-range graph incomplete/reversed')
    for key,start in starts.items():
        if not (order[start]<=hi and order[ends[key]]>=lo):continue
        if outside_inline_comments and key[0]=='commentRange':
            # New whole-heading markers go immediately after pPr and after
            # the last child. Neither write may occur inside a comment span.
            ppr=node.find(W+'pPr')
            first=max(order[n]for n in ppr.iter())+.5 if ppr is not None else lo+.5
            last=hi+.5
            if not any(order[start]<point<order[ends[key]]for point in (first,last)):
                continue
        raise ValueError('navigation write intersects a comment/permission/revision range')
