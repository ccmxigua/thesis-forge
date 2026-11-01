"""Source-bound TOC level styles, independent of disposable field caches.

Only complete supported template-comment declarations grant style writes.
This does not update fields, evaluate instructions, or certify native rendering.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from template_body_contract import parse_body_declaration
from template_role_policy import _source_structure_issues, compact
from toc_refresh import request_toc_refresh

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
LEVEL = re.compile(r'([一二三四五六七八九1-9])级目录(?:项|条目)\s*[:：]')
NUMBERS = {c:i for i,c in enumerate('一二三四五六七八九',1)}


def tokens(value):
    return [s.strip() for s in re.split(r'[，,；;。\n]',value.strip().removesuffix('.')) if s.strip()]


def _number(value):
    n=float(value)
    if not math.isfinite(n) or n<0:
        raise ValueError('invalid TOC paragraph unit')
    return n


def parse_toc_properties(value):
    result={}
    for token in tokens(value):
        m=re.fullmatch(r'(左|右)缩进\s*(\d+(?:\.\d+)?)\s*字符',token)
        multiple=re.fullmatch(r'(\d+(?:\.\d+)?)倍行距',token)
        if m:
            props={'paragraph':{('left' if m[1]=='左' else 'right')+'_indent_chars':_number(m[2])}}
        elif multiple:
            n=_number(multiple[1])
            if not n:raise ValueError('zero TOC line spacing')
            props={'paragraph':{'line_spacing':{'type':'multiple','value':n,'unit':'multiple'}}}
        elif token=='首行无缩进':
            props={'paragraph':{'first_line_indent_chars':0}}
        elif token=='无缩进':
            props={'paragraph':{'first_line_indent_chars':0,'left_indent_chars':0,'right_indent_chars':0}}
        else:
            props=parse_body_declaration(token)
        for category,values in props.items():
            for key,val in values.items():
                previous=result.setdefault(category,{})
                if key in previous and previous[key]!=val:raise ValueError('conflicting TOC property: '+key)
                previous[key]=val
    if not any(result.values()):raise ValueError('empty TOC style declaration')
    return result


def _layout_clause(token):
    patterns=(r'另起一页',r'目录之间空[一二两三四五六七八九十\d]+个半角空格',
              r'目录标题下方空[一二两三四五六七八九十\d]+行空白行',r'目录项显示到[1-9]级',
              r'(?:注意)?(?:保持)?页码右对齐',r'(?:有|使用)前导符',
              r'目录(?:的)?(?:标题|条目|前导符|及|、|和|与)+(?:须|应|需|必须)?整体撑满版心(?:区域)?',
              r'(?:正文|内容|论文)(?:修改|变动)后(?:应|需|注意)?(?:刷新|更新)目录(?:保持一致)?')
    return any(re.fullmatch(p,token)for p in patterns)


def parse_toc_comment(raw):
    labels=list(LEVEL.finditer(raw))
    if not labels:raise ValueError('no explicit TOC level declaration')
    title_context=False;nonstyle=[]
    for token in tokens(raw[:labels[0].start()]):
        if token.startswith('目录标题为'):
            parse_toc_properties('中文字体为'+token.removeprefix('目录标题为'));title_context=True
        elif _layout_clause(token):
            nonstyle.append(token)
        elif title_context:
            parse_toc_properties(token)  # recorded context, not a level permission
        else:
            raise ValueError('unknown TOC source context: '+token)
    rules={}
    for i,label in enumerate(labels):
        level=int(label[1]) if label[1].isdigit()else NUMBERS[label[1]]
        segment=raw[label.end():labels[i+1].start() if i+1<len(labels)else len(raw)]
        statements=[];closed=False
        for token in tokens(segment):
            if _layout_clause(token):
                closed=True;nonstyle.append(token)
            elif closed:
                raise ValueError('unresolved continuation after TOC layout context: '+token)
            else:statements.append(token)
        props=parse_toc_properties('，'.join(statements))
        if level in rules and rules[level]!=props:raise ValueError('conflicting TOC level declarations')
        rules[level]=props
    return rules,nonstyle


def bound_toc_style_rules(template,expected_sha256=None):
    record={'protocol':'source_bound_toc_style_levels_v1','rules':{},'contexts':[],
            'findings':[],'status':'not_required','native_verified':False}
    if template is None:return record
    path=Path(template);data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest!=expected_sha256:raise ValueError('TOC template hash mismatch')
    record.update(source_path=str(path.resolve()),source_sha256=digest)
    source=Document(path);rels=[r for r in source.part.rels.values()if r.reltype.endswith('/comments')]
    if not rels:return record
    if len(rels)!=1 or rels[0].is_external:
        record['findings'].append({'code':'toc_comment_source_unresolved'});record['status']='unhandled_preserved';return record
    root=etree.fromstring(rels[0].target_part.blob,etree.XMLParser(resolve_entities=False,no_network=True))
    comments={};duplicate=set()
    for c in root:
        cid=c.get(W+'id')
        if cid in comments:duplicate.add(cid)
        comments[cid]=c
    starts={};ends={};refs={}
    for p in source.paragraphs:
        for tag,mapping in [('commentRangeStart',starts),('commentRangeEnd',ends),('commentReference',refs)]:
            for n in p._p.iter(W+tag):
                if next(n.iterancestors(W+'p'),None)is p._p:mapping.setdefault(n.get(W+'id'),[]).append(p)
    def comment_text(c):
        return '\n'.join(''.join(n.text or ''for n in p.iter(W+'t'))for p in c.iter(W+'p'))
    relevant=[cid for cid,c in comments.items()if re.search(r'级目录|(?:TOC|contents).{0,20}level|level\s*[1-9].{0,20}(?:TOC|contents)',comment_text(c),re.I)]
    groups={}
    for cid in relevant:
        ps=starts.get(cid,[])
        if len(ps)!=1:
            record['findings'].append({'code':'toc_comment_anchor_ambiguous','comment_id':cid});continue
        groups[ps[0]._p]=ps[0]
    for node,p in groups.items():
        cids=[cid for cid,ps in starts.items()if any(q._p is node for q in ps)]
        try:
            if compact(p.text)not in {'目录','contents','tableofcontents'}:raise ValueError('source anchor is not a plain TOC title')
            if _source_structure_issues(node)or list(node.iterdescendants(W+'p')):raise ValueError('source title structure unresolved')
            pieces=[]
            for cid in cids:
                if cid in duplicate or cid not in comments or len(starts[cid])!=1 or [q._p for q in ends.get(cid,[])]!=[node]or[q._p for q in refs.get(cid,[])]!=[node]:
                    raise ValueError('comment range/identity unresolved')
                c=comments[cid]
                if _source_structure_issues(c)or any(n.tag in {W+'drawing',W+'object',W+'pict',W+'tbl'}for n in c.iter()):raise ValueError('comment structure unresolved')
                pieces.append(comment_text(c))
            raw='\n'.join(pieces);rules,nonstyle=parse_toc_comment(raw)
            for level,props in rules.items():
                key=str(level)
                if key in record['rules']and record['rules'][key]!=props:raise ValueError('conflicting source TOC rules')
                record['rules'][key]=props
            record['contexts'].append({'comment_ids':cids,'text':raw,'text_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                                       'anchor_locator':node.getroottree().getpath(node),'nonstyle_clauses_not_executed':nonstyle})
        except ValueError as exc:record['findings'].append({'code':'toc_style_source_unresolved','reason':str(exc),'comment_ids':cids})
    if path.read_bytes()!=data:raise ValueError('TOC template changed during read')
    if record['findings']:record['rules']={};record['status']='unhandled_preserved'
    elif record['rules']:record['status']='ready'
    return record


def _style_level(style):
    levels={int(m[1])for value in (style.style_id,style.name)if(m:=re.fullmatch(r'(?:toc|目录)\s*([1-9])',value,re.I))}
    if len(levels)>1:raise ValueError('conflicting native TOC style identity')
    return next(iter(levels),None)


def _chain(style):
    ids=[]
    while style is not None:
        if style.style_id in ids:raise ValueError('cyclic TOC style inheritance')
        ids.append(style.style_id)
        parent=style.base_style
        if style.element.find(W+'basedOn')is not None and parent is None:raise ValueError('broken TOC style inheritance')
        style=parent
    return ids


def style_digest(node):
    return hashlib.sha256(etree.tostring(node,method='c14n',exclusive=True)).hexdigest()


def _on_off(value,*,default,label):
    """Decode the supported Word boolean spellings without guessing unknowns."""
    if value is None:return default
    if value in {'1','true','on'}:return True
    if value in {'0','false','off'}:return False
    raise ValueError('unresolved '+label+' boolean: '+repr(value))


def _check_style_lock(node):
    locks=node.findall(W+'locked')
    if len(locks)>1:raise ValueError('duplicate style lock declarations')
    if locks and _on_off(locks[0].get(W+'val'),default=True,label='style lock'):
        raise ValueError('locked style definition: '+str(node.get(W+'styleId')))


def _check_formatting_protection(doc):
    # Reading doc.settings would create a new settings part when none exists.
    # A refusal must not create parts or relationships as a side effect.
    rels=[r for r in doc.part.rels.values()if r.reltype.endswith('/settings')]
    if not rels:return
    if len(rels)!=1 or rels[0].is_external:
        raise ValueError('unresolved document settings relationship')
    part=rels[0].target_part;settings=getattr(part,'element',None)
    if settings is None:settings=etree.fromstring(part.blob,etree.XMLParser(resolve_entities=False,no_network=True))
    if settings.tag!=W+'settings':raise ValueError('unresolved document settings root')
    direct=settings.findall(W+'documentProtection')
    all_nodes=list(settings.iter(W+'documentProtection'))
    if len(direct)>1 or len(all_nodes)!=len(direct):
        raise ValueError('unresolved document formatting protection structure')
    if not direct:return
    protection=direct[0]
    # Omitted documentProtection attributes do not activate restrictions.
    # An existing locked style element, in contrast, has implicit true Val.
    enforcement=_on_off(protection.get(W+'enforcement'),default=False,label='document enforcement')
    formatting=_on_off(protection.get(W+'formatting'),default=False,label='document formatting protection')
    if enforcement and formatting:
        raise ValueError('enforced document formatting protection')


def toc_style_identity(style):
    """Resolve a known TOC level independently of document-local style IDs.

    A matching custom name is not a native style. A numeric/renamed style ID
    can identify an existing built-in through its native name and flag. The
    ID alone cannot authorize turning an unrelated custom style into a TOC.
    """
    if style.type != WD_STYLE_TYPE.PARAGRAPH:
        raise ValueError('TOC identity is not a paragraph style')
    level = _style_level(style)
    names = style.element.findall(W+'name')
    named = re.fullmatch(r'(?:toc|目录)\s*([1-9])', names[0].get(W+'val', ''), re.I) if len(names)==1 else None
    if level is None or named is None or int(named[1]) != level:
        raise ValueError('TOC native name/level unresolved: '+str(style.style_id))
    custom = _on_off(style.element.get(W+'customStyle'), default=False, label='custom style identity')
    return {'level':level, 'style_id':style.style_id, 'name':names[0].get(W+'val'),
            'builtin':not custom, 'custom_style':custom}


def apply_toc_style_rules(doc,contract,*,scope_resolver,set_font,set_paragraph):
    report={'status':'not_required','source_contract':contract,'changes':[],'preserved_dependencies':[],
            'findings':[],'native_verified':False}
    if contract['status']!='ready':
        report['status']=contract['status'];report['findings']=copy.deepcopy(contract['findings']);return report
    try:
        if hashlib.sha256(Path(contract['source_path']).read_bytes()).hexdigest()!=contract['source_sha256']:raise ValueError('TOC source hash changed')
        _check_formatting_protection(doc)
        preflight=request_toc_refresh(doc,apply=False)
        if preflight['status']!='native_update_required':raise ValueError('TOC field boundary is absent or unhandled')
        cache,incomplete=scope_resolver(doc,{'TOC'})
        if incomplete or not cache:raise ValueError('incomplete dynamic TOC scope')
        if not any(r.reltype.endswith('/styles')and not r.is_external for r in doc.part.rels.values()):
            raise ValueError('source has no bound styles part')
        by_level={}
        for st in doc.styles:
            if st.type==WD_STYLE_TYPE.PARAGRAPH:
                level=_style_level(st)
                if level:by_level.setdefault(str(level),[]).append(st)
        default=doc.styles.default(WD_STYLE_TYPE.PARAGRAPH)
        if default is None:raise ValueError('no sound default paragraph style')
        _chain(default)
        all_styles=list(doc.styles);styles_by_id={s.style_id:s for s in all_styles}
        if len(styles_by_id)!=len(all_styles)or not all(styles_by_id):
            raise ValueError('duplicate or missing TOC style reference identity')
        users=[];character_refs=set()
        for part in doc.part.package.parts:
            if not str(part.partname).startswith('/word/')or not str(part.partname).endswith('.xml')or str(part.partname)=='/word/styles.xml':continue
            root=getattr(part,'element',None)
            if root is None:root=etree.fromstring(part.blob,etree.XMLParser(resolve_entities=False,no_network=True))
            for p in root.iter(W+'p'):
                ref=p.find(W+'pPr/'+W+'pStyle');sid=ref.get(W+'val')if ref is not None else default.style_id
                users.append((p,sid))
            character_refs.update(n.get(W+'val')for n in root.iter(W+'rStyle'))
        plans=[];identities={}
        for level,props in contract['rules'].items():
            found=by_level.get(level,[])
            if len(found)>1:raise ValueError('ambiguous built-in/custom TOC level '+level)
            if found:
                st=found[0];_chain(st)
                identity=toc_style_identity(st)
                identities[level]={**identity,'binding':'existing_builtin'if identity['builtin']else'promoted_cache_exclusive_custom'}
                if identity['custom_style']and not any(sid==st.style_id and p in cache for p,sid in users):
                    raise ValueError('custom TOC name lacks an actual bound cache use: '+st.style_id)
                _check_style_lock(st.element)
                if st.element.get(W+'default')in {'1','true','on'}or _source_structure_issues(st.element)or st.element.find(W+'link')is not None:raise ValueError('protected/linked TOC style '+level)
                for p,sid in users:
                    chain=_chain(styles_by_id.get(sid,default))
                    if sid==st.style_id and p not in cache:
                        raise ValueError('TOC style has shared use: '+st.style_id)
                if st.style_id in character_refs:raise ValueError('TOC paragraph style used as character style')
                original=st.element;clone=copy.deepcopy(original)
            else:
                sid='TOC'+level
                if any(s.style_id==sid for s in doc.styles):raise ValueError('TOC style ID collision')
                if sid in character_refs or any(ref==sid and p not in cache for p,ref in users):raise ValueError('missing TOC style has non-TOC references')
                original=None;clone=OxmlElement('w:style');clone.set(W+'type','paragraph');clone.set(W+'styleId',sid)
                n=OxmlElement('w:name');n.set(W+'val','toc '+level);clone.append(n)
                n=OxmlElement('w:basedOn');n.set(W+'val',default.style_id);clone.append(n)
                identities[level]={'level':int(level),'style_id':sid,'name':'toc '+level,
                                   'builtin':False,'custom_style':None,'binding':'created_builtin'}
            # Native rebuilds select the built-in identity, not a same-name
            # custom twin. Existing targets reached this point only after
            # shared-use/protection/dependency checks. Keep their local IDs
            # and all paragraph references; never bind to a WPS numeric ID.
            clone.set(W+'customStyle','0')
            clone.find(W+'name').set(W+'val','toc '+level)
            staged=type(default)(clone)
            para=copy.deepcopy(props.get('paragraph',{}));font=props.get('font',{})
            size=font.get('size_pt')
            if size is not None and(not math.isfinite(float(size))or float(size)<=0):raise ValueError('invalid TOC font size')
            if not size and any(para.get(k)for k in ('first_line_indent_chars','space_before_lines','space_after_lines')):
                raise ValueError('relative TOC style unit lacks explicit font size')
            for side in ('left','right'):
                value=para.pop(side+'_indent_chars',None)
                if value is not None:
                    if value and not size:raise ValueError('TOC character indent lacks explicit font size')
                    para[side+'_indent_pt']=value*(size or 1)
            if font:set_font(staged,font)
            if para:set_paragraph(staged,para)
            for side in ('left','right'):
                value=props.get('paragraph',{}).get(side+'_indent_chars')
                if value is not None:staged.element.get_or_add_pPr().get_or_add_ind().set(W+side+'Chars',str(round(value*100)))
            if size is not None:
                n=clone.get_or_add_rPr().find(W+'szCs')
                if n is None:n=OxmlElement('w:szCs');clone.get_or_add_rPr().append(n)
                n.set(W+'val',str(round(float(size)*2)))
            identities[level]['target']=toc_style_identity(staged)
            plans.append((level,original,clone))
        # Updating a native style must not change other styles inheriting it,
        # even if those styles happen to be unused in this document. Preserve
        # the complete old parent definition behind a private identity and
        # rebase direct dependants onto it; their descendants retain the same
        # chain. No effective-font/property guessing is needed.
        auxiliary=[];rebased={};occupied=set(styles_by_id);parent_snapshots={}
        for level,original,clone in plans:
            if original is None:continue
            sid=original.get(W+'styleId')
            children=[s for s in doc.styles if s.element.find(W+'basedOn')is not None
                      and s.element.find(W+'basedOn').get(W+'val')==sid]
            if not children:continue
            if any(s.type!=WD_STYLE_TYPE.PARAGRAPH or _source_structure_issues(s.element)for s in children):
                raise ValueError('unresolved dependent TOC style structure: '+sid)
            for child in children:_check_style_lock(child.element)
            snapshot=copy.deepcopy(original);stem='TFTOCBASE_'+style_digest(original)[:24];private=stem;index=0
            while private in occupied:
                index+=1;private=stem+'_'+str(index)
            occupied.add(private);snapshot.set(W+'styleId',private);parent_snapshots[sid]=private
            snapshot.find(W+'name').set(W+'val','Preserved TOC inheritance '+private)
            for alias in list(snapshot.findall(W+'aliases')):snapshot.remove(alias)
            auxiliary.append(snapshot)
            for child in children:
                cid=child.style_id
                selected=next((c for _,o,c in plans if o is child.element),None)
                replacement=selected if selected is not None else rebased.setdefault(cid,copy.deepcopy(child.element))
                replacement.find(W+'basedOn').set(W+'val',private)
        # A preserved parent can itself inherit another selected TOC level.
        # Its snapshot must use the old chain too, rather than the updated one.
        for snapshot in auxiliary:
            parent=snapshot.find(W+'basedOn')
            if parent is not None and parent.get(W+'val')in parent_snapshots:
                parent.set(W+'val',parent_snapshots[parent.get(W+'val')])
        dependency_records=[{'style_id':n.get(W+'styleId'),'kind':'original_parent_snapshot',
                             'style_xml_sha256':style_digest(n)}for n in auxiliary]
        dependency_records += [{'style_id':cid,'kind':'preserved_inheritance',
                                'style_xml_sha256':style_digest(n)}for cid,n in rebased.items()]
        # Validate receipts before the first mutation too. Unknown identity
        # levels or uncanonicalizable source XML must not leave a partial
        # style write behind while returning unhandled_preserved.
        change_records=[{'level':int(level),'style_id':clone.get(W+'styleId'),'created':original is None,
                         'style_xml_sha256':style_digest(clone),'properties':contract['rules'][level],
                         'native_identity':identities[level]['target'],
                         'identity_binding':identities[level]}
                        for level,original,clone in plans]
        if hashlib.sha256(Path(contract['source_path']).read_bytes()).hexdigest()!=contract['source_sha256']:raise ValueError('TOC source changed before style commit')
        for snapshot in auxiliary:doc.styles.element.append(snapshot)
        for cid,node in rebased.items():
            original=styles_by_id[cid].element;original.getparent().replace(original,node)
        for level,original,clone in plans:
            if original is None:doc.styles.element.append(clone)
            else:original.getparent().replace(original,clone)
        report['changes']=change_records
        report['preserved_dependencies']=dependency_records
        report['status']='style_definitions_applied'
    except (ValueError,TypeError,KeyError,OverflowError,OSError,AttributeError,etree.C14NError)as exc:
        report['status']='unhandled_preserved';report['findings'].append({'code':'toc_style_application_unresolved','reason':str(exc)})
    return report


def audit_toc_style_rules(doc,report):
    def verify(item):
        nodes=[s.element for s in doc.styles if s.style_id==item['style_id']]
        actual=style_digest(nodes[0])if len(nodes)==1 else None
        ok=actual==item['style_xml_sha256']
        identity=None;identity_issue=None
        if 'level'in item:
            try:
                styles=[s for s in doc.styles if s.style_id==item['style_id']]
                if len(styles)!=1:raise ValueError('TOC identity absent or ambiguous')
                identity=toc_style_identity(styles[0])
                if not identity['builtin']or identity['level']!=item['level']:
                    raise ValueError('serialized TOC target is not the required built-in level')
                peers=[s for s in doc.styles if s.type==WD_STYLE_TYPE.PARAGRAPH and _style_level(s)==item['level']]
                if len(peers)!=1:raise ValueError('serialized built-in/custom TOC collision')
            except (ValueError,TypeError,AttributeError)as exc:
                identity_issue=str(exc);ok=False
        return {'style_id':item['style_id'],'serialized_definition_matches':ok,
                'expected_style_xml_sha256':item['style_xml_sha256'],'actual_style_xml_sha256':actual,
                'native_identity':identity,'native_identity_issue':identity_issue}
    records=[verify(item)for item in report['changes']]
    dependencies=[verify(item)for item in report.get('preserved_dependencies',[])]
    return {'status':report['status'],'definitions':records,'preserved_dependencies':dependencies,
            'valid':all(r['serialized_definition_matches']for r in records+dependencies),
            'native_rebuild_verified':False,'findings':report['findings']}
