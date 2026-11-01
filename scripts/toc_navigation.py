"""Guarded static TOC reference repair. Native pages/navigation remain unverified."""
from __future__ import annotations
import copy,hashlib,re
from collections import Counter
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph
from lxml import etree
from toc_refresh import collect_field_ranges,flatten_fields,request_toc_refresh,BLOCKED
from template_role_policy import compact,text,_cached_title,_source_structure_issues
from toc_heading_fonts import _check_style_dependencies
from toc_style_contract import _check_formatting_protection,_on_off,_style_level
from font_declarations import word_run_display_issue

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
M='{http://schemas.openxmlformats.org/officeDocument/2006/math}'
BOOK={W+'bookmarkStart',W+'bookmarkEnd'}
PROTOCOL='guarded_toc_navigation_v1'

def _title_key(value):
    # Preserve word boundaries and case. A B/AB or US/us are not inferred equal.
    return re.sub(r'\s+',' ',value).strip()

def _path(node):return node.getroottree().getpath(node)
def _hash(node):return hashlib.sha256(etree.tostring(node,method='c14n',exclusive=True)).hexdigest()
def _owner(node):return next(node.iterancestors(W+'p'),None)
def _at(doc,path):
    nodes=doc._element.getroottree().xpath(path,namespaces={k:v for k,v in doc._element.nsmap.items()if k})
    if len(nodes)!=1:raise ValueError('navigation locator absent or ambiguous')
    return nodes[0]
def _tokens(p):
    return [(n.tag,n.text or '')for n in p.iter()if n.tag in {W+'t',M+'t',W+'tab',W+'br'}]

def _body_content(doc):
    # Review-only pages may later be appended. Bind each original paragraph,
    # including text-box/table paragraphs, rather than trusting global counts.
    return [{'locator':_path(p),'tokens':_tokens(p),
             'numbering':[_hash(n)for n in p.iter(W+'numPr')]}
            for p in doc._element.body.iter(W+'p')]

def _entry_title(p,page):
    output=[]
    for node in p.iter():
        if node is page['begin']:break
        if node.tag==W+'tab'and node.getparent().tag==W+'r':break
        if node.tag==W+'t':output.append(node.text or '')
    return ''.join(output).strip()
def _book_records(doc):
    return [{'tag':n.tag,'attributes':dict(n.attrib),'parent':_path(n.getparent()),
             'nonbookmark_sibling_offset':sum(q.tag not in BOOK for q in n.itersiblings(preceding=True))}
            for n in doc._element.body.iter()if n.tag in BOOK]
def _objects(doc):
    return [(n.tag,_hash(n))for n in doc._element.body.iter()if n.tag in {M+'oMath',W+'object'}]
def _media(doc):
    return {str(p.partname):hashlib.sha256(p.blob).hexdigest()for p in doc.part.package.parts
            if str(p.partname).startswith(('/word/media/','/word/embeddings/'))}

def _control_markers(doc):
    controls={W+k for k in ('commentRangeStart','commentRangeEnd','commentReference','permStart','permEnd',
                           'moveFromRangeStart','moveFromRangeEnd','moveToRangeStart','moveToRangeEnd')}
    return [{'tag':n.tag,'attributes':dict(n.attrib),'parent':_path(n.getparent()),
             'nonbookmark_sibling_offset':sum(s.tag not in BOOK for s in n.itersiblings(preceding=True))}
            for n in doc._element.body.iter()if n.tag in controls]

def _registry(doc):
    starts=list(doc._element.body.iter(W+'bookmarkStart'));ends=list(doc._element.body.iter(W+'bookmarkEnd'))
    byid={};byname={};positions={n:i for i,n in enumerate(doc._element.body.iter())}
    for n in starts:
        sid=n.get(W+'id');name=n.get(W+'name')
        if not sid or not name or sid in byid or name.casefold()in byname:
            raise ValueError('duplicate/missing bookmark identity')
        byid[sid]=[n,None];byname[name.casefold()]=n
    for n in ends:
        sid=n.get(W+'id')
        if sid not in byid or byid[sid][1]is not None:raise ValueError('unpaired/duplicate bookmark end')
        byid[sid][1]=n
    if any(end is None or positions[start]>=positions[end]for start,end in byid.values()):
        raise ValueError('incomplete/reversed bookmark range')
    # Other Word stories are read only; a competing name cannot be guessed away.
    names=Counter(n.get(W+'name','').casefold()for n in starts)
    for part in doc.part.package.parts:
        name=str(part.partname)
        if part is doc.part or not name.startswith('/word/')or not name.endswith('.xml'):continue
        root=getattr(part,'element',None)
        if root is None:root=etree.fromstring(part.blob,etree.XMLParser(resolve_entities=False,no_network=True))
        names.update(n.get(W+'name','').casefold()for n in root.iter(W+'bookmarkStart'))
    if any(count>1 for name,count in names.items()if name):raise ValueError('bookmark name ambiguous across stories')
    return {n.get(W+'name'):(n,byid[n.get(W+'id')][1])for n in starts},names

def _owned_range(pair,p):
    """Accept a complete whole-heading range or a valid point bookmark.

    The latter identifies the managed paragraph, not a verified printed page.
    Nonempty partial heading ranges remain unresolved.
    """
    start,end=pair
    if start.getparent()is not p or end.getparent()is not p:return False
    children=list(p);a,b=children.index(start),children.index(end)
    if a>=b:return False
    outside=''.join(text(n)for n in children[:a]+children[b+1:])
    inside=''.join(text(n)for n in children[a+1:b])
    return not inside or(not outside.strip()and inside==text(p)and bool(inside.strip()))

def _protect_document(doc):
    _check_formatting_protection(doc)
    rels=[r for r in doc.part.rels.values()if r.reltype.endswith('/settings')]
    if rels:
        if len(rels)!=1 or rels[0].is_external:raise ValueError('settings scope unresolved')
        root=rels[0].target_part.element
        for n in root.findall(W+'documentProtection'):
            if _on_off(n.get(W+'enforcement'),default=False,label='document protection'):
                raise ValueError('enforced document protection')

def _visible_paragraph(p,doc,*,target=False):
    if p.getparent()is not doc._element.body:raise ValueError('non-body/nested paragraph scope')
    if any(n.tag in BLOCKED for n in p.iter())or any(n.tag in BLOCKED for n in p.iterancestors()):
        raise ValueError('revision/compatibility display scope unresolved')
    paragraph=Paragraph(p,doc._body);_check_style_dependencies(paragraph.style)
    for node in p.iter(W+'r'):
        if list(node.iter(W+'t'))and word_run_display_issue(node,paragraph):raise ValueError('run display scope unresolved')
    if target:
        if _source_structure_issues(p)or any(n.tag in {W+'drawing',W+'object',W+'pict',M+'oMath',M+'oMathPara',W+'numPr',W+'sym',W+'tab',W+'br',W+'cr'}for n in p.iter()):
            raise ValueError('source heading contains unresolved field/object/numbering')
        st=paragraph.style;seen=set()
        while st is not None:
            if st.style_id in seen:raise ValueError('source style cycle')
            seen.add(st.style_id)
            if list(st.element.iter(W+'numPr')):raise ValueError('source heading numbering text unresolved')
            st=st.base_style

def _reference(frame):
    instruction=''.join(n.text or ''for n in frame['instruction_nodes']);kind=frame['opcode']
    prefix=r'\s*HYPERLINK\s+\\l\s+'if kind=='HYPERLINK'else r'\s*PAGEREF\s+'
    m=re.match(prefix+r'(?:"([^"\r\n]+)"|([^\s"\\]+))',instruction,re.I)
    if not m or (instruction[m.end():].strip()and not re.fullmatch(r'(?:\s+\\[hpw]\s*)+',instruction[m.end():])):
        raise ValueError('unsupported reference grammar/switches')
    group=1 if m[1]is not None else 2
    return {'frame':frame,'kind':kind,'anchor':m[group],'span':m.span(group),'instruction':instruction}

def _row_fields(frames,p):
    found=[]
    for f in frames:
        if f['opcode']=='TOC'or _owner(f['begin'])is not p:continue
        if f['end']is None or _owner(f['end'])is not p:raise ValueError('row field spans multiple paragraphs')
        if f['opcode']not in {'HYPERLINK','PAGEREF'}or f['simple_fields']or f['unsafe']:
            raise ValueError('unsupported row field/display scope')
        if _on_off(f['begin'].get(W+'fldLock'),default=False,label='field lock'):raise ValueError('locked row field')
        found.append(_reference(f))
    return found

def _instruction_state(frames):
    return [{'locator':_path(f['begin']),'opcode':f['opcode'],
             'end_locator':_path(f['end'])if f['end']is not None else None,
             'separator_locator':_path(f['separator'])if f['separator']is not None else None,
             'parent_locator':_path(f['parent']['begin'])if f['parent']is not None else None,
             'parent_result':f['parent_result'],
             'instruction':''.join(n.text or ''for n in f['instruction_nodes'])}for f in frames]

def _write_reference(ref,new):
    """Apply only a precomputed instruction string; transaction caller owns rollback."""
    offset=0;nodes=ref['frame']['instruction_nodes']
    for i,n in enumerate(nodes):
        size=len(n.text or '')if i<len(nodes)-1 else len(new)-offset
        n.text=new[offset:offset+size];offset+=size
        n.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
    ref['frame']['begin'].set(W+'dirty','true')

def _set_refresh_request(node):
    node.set(W+'val','true')

def _whole_heading_range(pair,p):
    if not _owned_range(pair,p):return False
    children=list(p);a,b=(children.index(n)for n in pair)
    return bool(text(p).strip())and ''.join(text(n)for n in children[a+1:b])==text(p)

def _bound_additive_proof(doc,scopes,target,paragraph,refs,partial,anchors):
    from word_reference_inventory import inspect_references,reject_control_range_intersection
    for node in (target,paragraph):
        ppr=node.findall(W+'pPr')
        if len(ppr)>1 or(ppr and node[0]is not ppr[0]):raise ValueError('paragraph insertion boundary malformed')
    reject_control_range_intersection(target,outside_inline_comments=True)
    reject_control_range_intersection(paragraph)
    if len([p for p in doc._element.body.iter(W+'p') if p not in scopes.get('toc_results',set())
            and _title_key(text(p))==_title_key(text(target))])!=1:
        raise ValueError('bound source title is not unique across the document')
    old={r['anchor']for r in refs}
    if len(refs)!=2 or {r['kind']for r in refs}!={'HYPERLINK','PAGEREF'}or len(old)!=1:
        raise ValueError('additive migration requires one consistent TOC field pair')
    if not old<=set(anchors):raise ValueError('original reference target identity unresolved')
    inventory=inspect_references(doc)
    if not inventory['valid']:raise ValueError('complete reference inventory unresolved: '+str(inventory['findings']))
    folded={name.casefold()for name in old}
    consumers=[r for r in inventory['references']if r['target']and r['target'].casefold()in folded]
    if len(consumers)!=2 or any(r['kind']not in {'HYPERLINK','PAGEREF'}or r['part']!='/word/document.xml'
        or r['owner']!=_path(paragraph)or not r['in_toc_result']for r in consumers):
        raise ValueError('original anchor has an additional/non-TOC consumer')
    involved={name.casefold()for name in partial}
    if any(r['target']and r['target'].casefold()in involved and r['kind']not in {'HYPERLINK','PAGEREF','ANCHOR'}
           for r in inventory['references']):
        raise ValueError('partial source range has a text-consuming reference')
    return {'policy':'bound_heading_additive','old_pair_targets':sorted(old),
            'partial_ranges_preserved':list(partial),'selected_pair_consumers':consumers,
            'reference_inventory':inventory,'author_bookmark_ownership_inferred':False}

def repair_toc_links(doc,scopes,plan,*,target_policy='strict'):
    if target_policy not in {'strict','bound_heading_additive'}:raise ValueError('unknown TOC target policy')
    report={'protocol':PROTOCOL,'status':'not_applicable','changes':[],'entries':[],
            'findings':[],'native_navigation_verified':False,'native_page_numbers_verified':False,
            'cached_page_recomputed':False,'bookmarks_before':_book_records(doc),
            'bookmarks_expected':_book_records(doc),
            'body_content_before':_body_content(doc),
            'objects_before':_objects(doc),'media_before':_media(doc),'target_policy':target_policy,
            'control_markers_before':_control_markers(doc)}
    roots,malformed=collect_field_ranges(doc);frames=list(flatten_fields(roots))
    report['field_instructions_expected']=_instruction_state(frames)
    report['field_instructions_before']=copy.deepcopy(report['field_instructions_expected'])
    report['transaction_status']='not_started'
    try:
        _protect_document(doc)
        preflight=request_toc_refresh(doc,apply=False)
        if malformed or preflight['status']=='unhandled_preserved':raise ValueError('complete field scope protected/unresolved: '+str(preflight['findings']))
        anchors,names=_registry(doc)
    except (ValueError,TypeError,AttributeError,etree.XMLSyntaxError)as exc:
        report['status']='blocked_preserved';report['findings'].append({'code':'toc_navigation_scope_blocked','reason':str(exc),'status':'unhandled_preserved'})
        return report
    candidates={}
    for node,role in plan.owners.items():
        if role.startswith('heading_')or role in {'bibliography_heading','abstract_title_zh','abstract_title_en'}:
            candidates.setdefault(_title_key(text(node)),[]).append((node,role))
    planned=[];new_bookmarks={};used={n.get(W+'id')for n in doc._element.iter()if n.tag in BOOK}
    inverse_paragraphs={}
    next_id=max([int(v)for v in used if str(v).isdigit()]+[-1])+1
    for paragraph in doc.paragraphs:
        p=paragraph._p
        if p not in scopes.get('toc_results',set()):continue
        entry={'paragraph_locator':_path(p),'title':_cached_title(p),'status':'unhandled_preserved',
               'cache_tokens':_tokens(p),'field_instructions_expected':[n.text or ''for n in p.iter(W+'instrText')],
               'numbering':[_hash(n)for n in p.iter(W+'numPr')]}
        try:
            _visible_paragraph(p,doc)
            if list(p.iter(W+'fldSimple')):raise ValueError('simple cached field unsupported')
            if list(p.iter(W+'hyperlink')):raise ValueError('markup hyperlink cache is not a proved complex-field pair')
            refs=_row_fields(frames,p)
            if not refs:
                # Outer TOC delimiter paragraphs are not entries.
                if not text(p).strip():continue
                raise ValueError('cache text has no supported reference field')
            links=[f for f in refs if f['kind']=='HYPERLINK'];pages=[f for f in refs if f['kind']=='PAGEREF']
            if len(pages)!=1 or len(links)>1:raise ValueError('ambiguous reference count')
            entry['title']=_entry_title(p,pages[0]['frame'])
            enclosing=[];parent=pages[0]['frame']['parent']
            while parent is not None:
                enclosing.append(parent);parent=parent['parent']
            in_toc=any(f['opcode']=='TOC'for f in enclosing)
            if not in_toc and len(links)!=1:raise ValueError('PAGEREF-only cache lacks a complete outer TOC')
            targets=candidates.get(_title_key(entry['title']),[])
            if len(targets)!=1:raise ValueError('source heading absent or ambiguous')
            target,role=targets[0]
            if target in scopes.get('toc_results',set()):raise ValueError('source heading is inside cached TOC scope')
            _visible_paragraph(target,doc,target=True)
            level=_style_level(paragraph.style);m=re.fullmatch(r'heading_([1-9])',role)
            if level and m and level!=int(m[1]):raise ValueError('source/TOC level mismatch')
            valid=[name for name,pair in anchors.items()if _owned_range(pair,target)]
            if target_policy=='bound_heading_additive':
                if any(_owner(pair[0])is target and _owner(pair[1])is target and
                       not _whole_heading_range(pair,target)for pair in anchors.values()):
                    raise ValueError('point/partial bookmark cannot establish a complete bound heading')
                valid=[name for name in valid if _whole_heading_range(anchors[name],target)]
            partial=[name for name,pair in anchors.items()if
                     (_owner(pair[0])is target or _owner(pair[1])is target)and name not in valid]
            if partial and target_policy=='strict':raise ValueError('source bookmark covers an unproved partial heading range')
            already=all(f['anchor']in valid for f in refs)and len(set(f['anchor']for f in refs))==1
            if target_policy=='bound_heading_additive'and not already:
                entry['bound_migration_proof']=_bound_additive_proof(doc,scopes,target,p,refs,partial,anchors)
            own=[f['anchor']for f in refs if f['anchor']in valid]
            if already:
                name=refs[0]['anchor'];state='already_correct'
            else:
                if len(set(own))>1:raise ValueError('paired reference ownership ambiguous')
                if own:name=own[0]
                elif len(valid)==1:name=valid[0]
                elif len(valid)>1:raise ValueError('source bookmark range ambiguous')
                elif target in new_bookmarks:name=new_bookmarks[target]['name']
                else:
                    if not links:raise ValueError('PAGEREF-only target lacks an existing complete bookmark pair')
                    # Retain the existing additive bookmark policy, only for a
                    # unique safe managed heading. No original range is moved.
                    name='TF_TOC_'+hashlib.sha256((compact(entry['title'])+'|'+_path(target)).encode()).hexdigest()[:24]
                    if name.casefold()in names:raise ValueError('private bookmark name collision')
                    begin=OxmlElement('w:bookmarkStart');begin.set(W+'id',str(next_id));begin.set(W+'name',name)
                    end=OxmlElement('w:bookmarkEnd');end.set(W+'id',str(next_id));next_id+=1
                    new_bookmarks[target]={'name':name,'begin':begin,'end':end};names[name.casefold()]=1
                state='repaired'
            if not name or any(c.isspace()or c in '"\\\r\n'or ord(c)<32 for c in name):raise ValueError('bookmark cannot be represented safely in field syntax')
            entry.update(status=state,target_bookmark=name,target_locator=_path(target),
                         target_text_sha256=hashlib.sha256(text(target).encode()).hexdigest(),source_role=role,
                         representation='paired'if links else'PAGEREF_only',fields=[])
            operations=[]
            for ref in refs:
                a,b=ref['span'];new=ref['instruction'][:a]+name+ref['instruction'][b:]
                # Validate all text assignments on clones before touching live XML.
                for node in ref['frame']['instruction_nodes']:copy.deepcopy(node).text=new
                entry['fields'].append({'locator':_path(ref['frame']['begin']),'kind':ref['kind'],'anchor':name,'instruction':new})
                if new!=ref['instruction']:operations.append((ref,new))
            if operations:
                for node in (p,target):inverse_paragraphs.setdefault(_path(node),etree.tostring(node,encoding='unicode'))
            planned.append((entry,operations,target));report['entries'].append(entry)
        except (ValueError,TypeError,KeyError,AttributeError,etree.C14NError)as exc:
            entry['reason']=str(exc);report['entries'].append(entry)
            report['findings'].append({'code':'toc_navigation_entry_unhandled','title':entry['title'],'reason':str(exc),'status':'unhandled_preserved'})
    # No live writes occurred during planning. A commit exception rolls back
    # every reference and every new pair, including earlier successful rows.
    backups=[];inserted=[];settings_node=None;settings_before=None
    body_before=etree.tostring(doc._element.body)
    row_instructions_before={entry['paragraph_locator']:list(entry['field_instructions_expected'])for entry,ops,target in planned}
    settings_part=next((rel.target_part for rel in doc.part.rels.values()if rel.reltype.endswith('/settings')),None)
    settings_xml_before=etree.tostring(settings_part.element)if settings_part else None
    inverse_body_hash=_hash(doc._element.body)
    rollback_paragraphs={_path(p):_hash(p)for p in doc._element.body.iter(W+'p')}
    try:
        for entry,ops,target in planned:
            if not ops:continue
            bookmark=new_bookmarks.get(target)
            if bookmark and bookmark['begin'].getparent()is None:
                # Register before each call: an XML operation can mutate and
                # then raise. A later endpoint must not orphan an earlier one.
                inserted.append(bookmark['begin'])
                target.insert(1 if target.find(W+'pPr')is not None else 0,bookmark['begin'])
                inserted.append(bookmark['end'])
                target.append(bookmark['end'])
            for ref,new in ops:
                backups.extend((n,n.text,dict(n.attrib))for n in ref['frame']['instruction_nodes']+[ref['frame']['begin']])
                _write_reference(ref,new)
            entry['field_instructions_expected']=[n.text or ''for n in _at(doc,entry['paragraph_locator']).iter(W+'instrText')]
            report['changes'].append({'title':entry['title'],'new_anchor':entry['target_bookmark'],
                                      'instruction_nodes_changed':sum(len(f['frame']['instruction_nodes'])for f,_ in ops),
                                      'old_bookmark_preserved':True,'cached_page_recomputed':False})
        if report['changes']:
            rels=[r for r in doc.part.rels.values()if r.reltype.endswith('/settings')]
            if rels:
                settings=rels[0].target_part.element
                existing=settings.findall(W+'updateFields')
                if len(existing)>1:raise ValueError('ambiguous refresh setting')
                settings_node=existing[0]if existing else OxmlElement('w:updateFields')
                settings_before=(dict(settings_node.attrib),settings_node.getparent())
                if not existing:settings.append(settings_node)
                _set_refresh_request(settings_node)
        _registry(doc)
        report['transaction_status']='committed'
    except Exception as exc:
        restore_errors=[]
        for node,old,attrs in reversed(backups):
            try:node.text=old;node.attrib.clear();node.attrib.update(attrs)
            except Exception as error:restore_errors.append(str(error))
        for node in reversed(inserted):
            try:
                if node.getparent()is not None:node.getparent().remove(node)
            except Exception as error:restore_errors.append(str(error))
        if settings_node is not None and settings_before is not None:
            try:
                attrs,parent=settings_before
                if parent is None and settings_node.getparent()is not None:settings_node.getparent().remove(settings_node)
                else:settings_node.attrib.clear();settings_node.attrib.update(attrs)
            except Exception as error:restore_errors.append(str(error))
        restored=(not restore_errors and etree.tostring(doc._element.body)==body_before and
                  (etree.tostring(settings_part.element)if settings_part else None)==settings_xml_before)
        report['transaction_status']='rolled_back'if restored else'rollback_failed'
        report['rollback_verification']={'verified':restored,'paragraphs_before':rollback_paragraphs,
            'settings_before_sha256':hashlib.sha256(settings_xml_before).hexdigest()if settings_xml_before else None,
            'restore_errors':restore_errors}
        report['changes']=[]
        for entry,ops,target in planned:
            if ops:
                entry['status']='unhandled_preserved'
                entry['reason']='transaction rolled back'if restored else'transaction rollback incomplete'
                # Keep the immutable pre-write row expectations. Do not adopt
                # a failed restore as the expected serialized document.
                entry['field_instructions_expected']=row_instructions_before[entry['paragraph_locator']]
        report['findings'].append({'code':'toc_navigation_transaction_rolled_back'if restored else'toc_navigation_rollback_incomplete',
            'reason':str(exc),'status':'unhandled_preserved'})
    if report['transaction_status']=='committed':
        roots,_=collect_field_ranges(doc);report['field_instructions_expected']=_instruction_state(list(flatten_fields(roots)))
        report['bookmarks_expected']=_book_records(doc)
        if report['changes']:
            report['additive_inverse']={'protocol':'bound_navigation_inverse_v1','paragraphs':inverse_paragraphs,
                'body_before_sha256':inverse_body_hash,'body_after_sha256':_hash(doc._element.body),
                'settings_before_xml':settings_xml_before.decode()if settings_xml_before else None,
                'settings_after_sha256':_hash(settings_part.element)if settings_part else None}
    else:
        report['field_instructions_expected']=copy.deepcopy(report['field_instructions_before'])
        report['bookmarks_expected']=copy.deepcopy(report['bookmarks_before'])
    report['status']=('processed_with_unhandled'if report['findings']and report['changes']else
                      'unhandled_preserved'if report['findings']else'references_repaired'if report['changes']else
                      'references_verified'if report['entries']else'not_applicable')
    return report

def restore_additive_navigation(doc,report):
    """Exact in-memory inverse for a still-identical managed candidate only."""
    from docx.oxml import parse_xml
    inverse=report.get('additive_inverse')
    if not inverse or inverse.get('protocol')!='bound_navigation_inverse_v1' or not audit_toc_navigation(doc,report)['valid']:
        raise ValueError('navigation inverse receipt invalid')
    settings_part=next((r.target_part for r in doc.part.rels.values()if r.reltype.endswith('/settings')),None)
    if(_hash(doc._element.body)!=inverse['body_after_sha256']or settings_part is None or
       _hash(settings_part.element)!=inverse['settings_after_sha256']or inverse['settings_before_xml']is None):
        raise ValueError('navigation candidate changed or settings identity unresolved')
    before_body=copy.deepcopy(doc._element.body);before_settings=copy.deepcopy(settings_part.element)
    def replace_contents(target,source):
        target.attrib.clear();target.attrib.update(source.attrib)
        for n in list(target):target.remove(n)
        for n in source:target.append(copy.deepcopy(n))
    try:
        staged=[(_at(doc,path),parse_xml(xml.encode()))for path,xml in inverse['paragraphs'].items()]
        for target,source in staged:replace_contents(target,source)
        replace_contents(settings_part.element,parse_xml(inverse['settings_before_xml'].encode()))
        if _hash(doc._element.body)!=inverse['body_before_sha256']:
            raise ValueError('navigation inverse did not exactly restore source body')
        _registry(doc)
    except Exception:
        replace_contents(doc._element.body,before_body);replace_contents(settings_part.element,before_settings)
        raise
    return {'restored':True,'native_navigation_verified':False}

def audit_toc_navigation(doc,report):
    """Verify the navigation receipt independently on a serialized artifact."""
    errors=[];rows=[]
    if report.get('protocol')!=PROTOCOL:
        return {'valid':False,'status':'unhandled_preserved','findings':[{'code':'navigation_receipt_protocol_unresolved'}],
                'native_navigation_verified':False,'native_page_numbers_verified':False}
    old=report['bookmarks_before'];current=_book_records(doc)
    for item in old:
        if current.count(item)!=1:errors.append('original bookmark endpoint/range changed')
    if current!=report['bookmarks_expected']:errors.append('bookmark graph differs from the committed navigation receipt')
    if 'control_markers_before'in report:
        if _control_markers(doc)!=report['control_markers_before']:errors.append('comment/permission/revision markers changed')
    elif report.get('target_policy')=='bound_heading_additive':errors.append('bound navigation control-marker proof missing')
    if not report['changes']and current!=old:errors.append('uncommitted transaction altered the original bookmark graph')
    if report['changes']:
        try:_registry(doc)
        except (ValueError,TypeError,AttributeError,etree.XMLSyntaxError)as exc:errors.append('final bookmark graph invalid: '+str(exc))
    rollback=report.get('rollback_verification')
    if rollback is not None:
        if report.get('transaction_status')!='rolled_back'or not rollback.get('verified'):
            errors.append('navigation rollback was not verified')
        for locator,expected in rollback['paragraphs_before'].items():
            try:
                if _hash(_at(doc,locator))!=expected:raise ValueError('rolled-back paragraph differs from pre-write snapshot')
            except (ValueError,TypeError,KeyError,AttributeError,etree.C14NError)as exc:errors.append(str(exc))
        settings_part=next((rel.target_part for rel in doc.part.rels.values()if rel.reltype.endswith('/settings')),None)
        current_settings=hashlib.sha256(etree.tostring(settings_part.element)).hexdigest()if settings_part else None
        if current_settings!=rollback['settings_before_sha256']:errors.append('rolled-back settings differ from pre-write snapshot')
    if _objects(doc)!=[tuple(v)for v in report['objects_before']]:errors.append('math/OLE subtree changed')
    if _media(doc)!=report['media_before']:errors.append('media/embedding bytes changed')
    for item in report['body_content_before']:
        try:
            p=_at(doc,item['locator'])
            if _tokens(p)!=[tuple(v)for v in item['tokens']]:raise ValueError('original body text/cache tokens changed')
            if [_hash(n)for n in p.iter(W+'numPr')]!=item['numbering']:raise ValueError('original body numbering changed')
        except (ValueError,TypeError,KeyError,AttributeError,etree.C14NError)as exc:errors.append(str(exc))
    roots,malformed=collect_field_ranges(doc);frames=list(flatten_fields(roots))
    if _instruction_state(frames)!=report['field_instructions_expected']:errors.append('field instructions changed after navigation processing')
    for item in report['entries']:
        row_errors=[]
        try:
            p=_at(doc,item['paragraph_locator'])
            if _tokens(p)!=[tuple(v)for v in item['cache_tokens']]:raise ValueError('cache text/tab/page value changed')
            if [_hash(n)for n in p.iter(W+'numPr')]!=item['numbering']:raise ValueError('cached numbering changed')
            if [n.text or ''for n in p.iter(W+'instrText')]!=item['field_instructions_expected']:raise ValueError('row field instructions changed')
            if item['status']in {'repaired','already_correct'}:
                if malformed:raise ValueError('final field range incomplete')
                anchors,_=_registry(doc);pair=anchors.get(item['target_bookmark']);target=_at(doc,item['target_locator'])
                if pair is None or not _owned_range(pair,target):raise ValueError('final target bookmark/range unresolved')
                _visible_paragraph(target,doc,target=True)
                if hashlib.sha256(text(target).encode()).hexdigest()!=item['target_text_sha256']:raise ValueError('source heading text changed')
                byloc={_path(f['begin']):f for f in frames}
                page=None
                for ref in item['fields']:
                    frame=byloc.get(ref['locator'])
                    if frame is None:raise ValueError('final reference field absent')
                    parsed=_reference(frame)
                    if parsed['kind']=='PAGEREF':page=frame
                    if parsed['anchor']!=item['target_bookmark']or parsed['instruction']!=ref['instruction']:raise ValueError('final reference target mismatch')
                if page is None or _title_key(_entry_title(p,page))!=_title_key(text(target)):
                    raise ValueError('final cache/source title binding differs')
        except (ValueError,TypeError,KeyError,AttributeError,etree.C14NError)as exc:row_errors.append(str(exc))
        rows.append({'paragraph_locator':item['paragraph_locator'],'status':item['status'],'consistent':not row_errors,'errors':row_errors})
        errors.extend(row_errors)
    findings=copy.deepcopy(report['findings'])+[{'code':'toc_navigation_integrity_failed','reason':v}for v in errors]
    return {'protocol':PROTOCOL,'status':report['status'],'valid':not errors,'entries':rows,'findings':findings,
            'navigation_references_verified':bool(rows)and not findings and all(q['status']in {'repaired','already_correct'}for q in rows),
            'native_navigation_verified':False,'native_page_numbers_verified':False,'cached_pages_trusted':False}
