"""Bounded, read-only source and PDF evidence for the opt-in UNO backend.

This is not a glyph/typeface check or native Word/WPS acceptance. Unsupported
source/layout scopes fail rather than choosing targets by order or proximity.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import hashlib
import math
import re
from types import SimpleNamespace
from zipfile import ZipFile

from docx import Document
from lxml import etree
from toc_refresh import BLOCKED, collect_field_ranges, flatten_fields, request_toc_refresh
from toc_navigation import _protect_document, _registry, _visible_paragraph, _owner, _owned_range

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
M='{http://schemas.openxmlformats.org/officeDocument/2006/math}'


def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def key(text):
    return re.sub(r'\s+',' ',text).strip()


def render_key(text):
    return re.sub(r'\s+','',text)


def literal(node):
    return ''.join(n.text or '' if n.tag==W+'t' else '\n'
                   for n in node.iter() if n.tag in {W+'t',W+'tab',W+'br',W+'cr'})


def _level(paragraph):
    direct=paragraph._p.find(W+'pPr/'+W+'outlineLvl')
    if direct is not None:
        return int(direct.get(W+'val'))
    style=paragraph.style
    seen=set()
    while style is not None:
        if style.style_id in seen:
            raise ValueError('heading style inheritance cycle')
        seen.add(style.style_id)
        level=style.element.find(W+'pPr/'+W+'outlineLvl')
        if level is not None:
            return int(level.get(W+'val'))
        style=style.base_style
    return None


def _footer_contract(doc,parts):
    margins=[]
    def flag(node):
        if node is None:return False
        value=node.get(W+'val','true').casefold()
        if value not in {'true','1','on','false','0','off'}:
            raise ValueError('unknown first/even-page footer flag')
        return value in {'true','1','on'}
    if flag(doc.settings.element.find(W+'evenAndOddHeaders')):
        raise ValueError('separate even-page footer mapping is not yet verified')
    active=set();inherited=None
    for section in doc.sections:
        if flag(section._sectPr.find(W+'titlePg')):
            raise ValueError('separate first-page footer mapping is not yet verified')
        defaults=[n for n in section._sectPr.findall(W+'footerReference') if n.get(W+'type')=='default']
        if len(defaults)>1:raise ValueError('ambiguous default footer reference')
        if defaults:
            relationship=doc.part.rels[defaults[0].get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
            if relationship.is_external:raise ValueError('external footer')
            inherited=str(relationship.target_part.partname).lstrip('/')
        if inherited is None:raise ValueError('active default footer absent')
        active.add(inherited)
        margin=section._sectPr.find(W+'pgMar')
        if margin is None:
            raise ValueError('explicit page/footer margins required')
        bottom=int(margin.get(W+'bottom','0'))/20
        footer=int(margin.get(W+'footer','0'))/20
        if not 0<footer<bottom:
            raise ValueError('footer cannot be separated from body margin')
        margins.append(bottom)
        numbering=section._sectPr.find(W+'pgNumType')
        if numbering is not None and numbering.get(W+'fmt','decimal') not in {'decimal','lowerRoman','upperRoman'}:
            raise ValueError('page-number format outside verified footer subset')
    if not margins or len(set(margins))!=1:
        raise ValueError('varying/unknown bottom margins require separate page-label evidence')
    count=0
    for name,root in parts.items():
        if name not in active:
            continue
        simple=list(root.iter(W+'fldSimple'))
        roots,malformed=collect_field_ranges(SimpleNamespace(_element=SimpleNamespace(body=root)))
        fields=list(flatten_fields(roots))
        if malformed or len(simple)+len(fields)!=1:
            raise ValueError('footer must have exactly one plain PAGE field')
        if simple:
            if not re.fullmatch(r'\s*PAGE\s*(?:\\\*\s+(?:ARABIC|ROMAN)\s*)?',simple[0].get(W+'instr',''),re.I):
                raise ValueError('unsupported simple footer instruction')
            allowed=set(simple[0].iter())
        else:
            frame=fields[0]
            if not re.fullmatch(r'\s*PAGE\s*(?:\\\*\s+(?:ARABIC|ROMAN)\s*)?',''.join(frame['instruction']),re.I):
                raise ValueError('unsupported footer instruction')
            nodes=list(root.iter());a=nodes.index(frame['begin']);b=nodes.index(frame['end'])
            allowed=set(nodes[a:b+1])
        if any((n.text or '').strip() for n in root.iter(W+'t') if n not in allowed):
            raise ValueError('footer contains additional text; label attribution unproven')
        count+=1
    if not count:
        raise ValueError('plain PAGE footer evidence absent')
    return {'bottom_margin_pt':margins[0],'footer_parts':count,'active_parts':sorted(active),
            'label_scope':'active default PAGE footer, optional Arabic/Roman format; actual PDF labels, not predicted pages'}


def inspect_docx(path):
    if path.suffix.lower()!='.docx':
        raise ValueError('UNO backend accepts explicit DOCX inputs only')
    parser=etree.XMLParser(resolve_entities=False,no_network=True)
    parts={}
    with ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)):
            raise ValueError('duplicate DOCX package members')
        if sum(i.file_size for i in archive.infolist())>512*1024*1024:
            raise ValueError('DOCX package exceeds backend budget')
        for name in names:
            if 'vbaProject' in name or name.endswith('.bin'):
                raise ValueError('embedded binary/macro scope is not verified by this backend')
            if name.endswith('.rels'):
                relationships=etree.fromstring(archive.read(name),parser)
                if any(n.get('TargetMode','').casefold()=='external' for n in relationships):
                    raise ValueError('external document relationship is not allowed in offline UNO rendering')
            if name.startswith('word/') and name.endswith('.xml'):
                root=etree.fromstring(archive.read(name),parser);parts[name]=root
                if any(n.tag in BLOCKED for n in root.iter()):
                    raise ValueError('revision/MC source scope is unresolved')
                if any(n.tag in {W+'object',W+'drawing',W+'pict',W+'tbl',W+'framePr',W+'sym',W+'altChunk',M+'oMath',M+'oMathPara'} for n in root.iter()):
                    raise ValueError('objects/math/tables require separate render-content validation')
                if any(n.tag==W+'lock' for n in root.iter()):
                    raise ValueError('content-control lock requires separate validation')
                if any(n.tag==W+'dataBinding' for n in root.iter()):
                    raise ValueError('data-bound text requires separate validation')
                if name!='word/document.xml' and not re.fullmatch(r'word/footer\d+\.xml',name):
                    if any(n.tag in {W+'fldChar',W+'fldSimple',W+'instrText',W+'hyperlink'} for n in root.iter()):
                        raise ValueError('non-body reference field scope is not verified')
    doc=Document(path)
    if list(doc._element.body.iter(W+'fldSimple')):
        raise ValueError('simple body field scope is not verified')
    _protect_document(doc)
    refresh=request_toc_refresh(doc,apply=False)
    if refresh['status']!='native_update_required' or refresh['findings'] or refresh['toc_count']!=1:
        raise ValueError('TOC refresh scope protected/unresolved: '+str(refresh['findings']))
    roots,malformed=collect_field_ranges(doc)
    frames=list(flatten_fields(roots));tocs=[f for f in frames if f['opcode']=='TOC']
    if malformed or len(tocs)!=1:
        raise ValueError('exactly one complete TOC required')
    toc_instruction=''.join(tocs[0]['instruction'])
    match=re.fullmatch(r'\s*TOC\s+\\o\s+"1-([1-9])"\s+\\h\s*',toc_instruction,re.I)
    if not match:
        raise ValueError('only explicit outline-depth TOC with internal hyperlinks is currently verified')
    depth=int(match[1]);nodes=list(doc._element.body.iter());positions={n:i for i,n in enumerate(nodes)}
    first,last=positions[tocs[0]['begin']],positions[tocs[0]['end']]
    toc_nodes=set(nodes[first:last+1])
    registry,_=_registry(doc)
    for start,end in registry.values():
        if positions[start]<=last and positions[end]>=first:
            raise ValueError('original bookmark intersects generated TOC scope')
    headings=[];paragraphs={}
    for index,paragraph in enumerate(doc.paragraphs):
        p=paragraph._p
        if any(n in toc_nodes for n in p.iter()):
            continue
        _visible_paragraph(p,doc)
        paragraphs[p]=index
        level=_level(paragraph)
        if level is not None and 0<=level<depth:
            _visible_paragraph(p,doc,target=True)
            title=literal(p).strip()
            if not title:
                raise ValueError('empty outline heading')
            headings.append({'title':title,'level':level+1,'paragraph_index':index})
    if not headings or len({render_key(h['title']) for h in headings})!=len(headings):
        raise ValueError('source heading missing/ambiguous, including layout-space collisions')
    used=defaultdict(list)
    for frame in frames:
        if positions[frame['begin']]>=first and positions[frame['end']]<=last:
            continue
        if frame['opcode'] not in {'REF','PAGEREF'} or frame['children'] or frame['simple_fields'] or frame['unsafe']:
            raise ValueError('body field outside the verified REF/PAGEREF subset')
        if frame['begin'].get(W+'fldLock','').casefold() not in {'','0','false','off'}:
            raise ValueError('locked/unknown body field state')
        p=_owner(frame['begin'])
        if p not in paragraphs or _owner(frame['end']) is not p:
            raise ValueError('body reference scope is not one visible body paragraph')
        instruction=''.join(frame['instruction'])
        m=re.fullmatch(r'\s*(REF|PAGEREF)\s+(?:"([^"\r\n]+)"|([^\s"\\]+))\s*(?:\\h\s*)?',instruction,re.I)
        if not m:
            raise ValueError('unsupported body reference grammar/switches')
        used[p].append({'kind':m[1].upper(),'target':m[2] or m[3],'begin':frame['begin'],'end':frame['end']})
    for p in paragraphs:
        for hyperlink in p.iter(W+'hyperlink'):
            anchor=hyperlink.get(W+'anchor')
            if not anchor:
                raise ValueError('only named internal markup hyperlinks are verified')
            used[p].append({'kind':'markup_hyperlink','target':anchor,'begin':hyperlink,'end':None,'label':literal(hyperlink)})
    refs=[]
    for p,entries in used.items():
        if len(entries)!=1:
            raise ValueError('multiple references in one paragraph require explicit attribution')
        ref=entries[0];pair=registry.get(ref['target'])
        if pair is None or _owner(pair[0]) is not _owner(pair[1]):
            raise ValueError('reference target bookmark absent or not one paragraph')
        target_p=_owner(pair[0])
        if target_p not in paragraphs:
            raise ValueError('reference target outside verified visible body scope')
        _visible_paragraph(target_p,doc,target=True)
        if any(h['paragraph_index']==paragraphs[target_p] for h in headings) and not _owned_range(pair,target_p):
            raise ValueError('partial heading bookmark cannot be proved by a whole-heading PDF destination')
        if list(target_p.iter(W+'fldChar')) or list(target_p.iter(W+'fldSimple')):
            raise ValueError('reference target includes dynamic fields')
        target_nodes=list(target_p.iter());a=target_nodes.index(pair[0]);b=target_nodes.index(pair[1])
        target_text=''.join(n.text or '' for n in target_nodes[a+1:b] if n.tag==W+'t')
        if not target_text:
            raise ValueError('empty reference range has no attributable visible target')
        para_nodes=list(p.iter());a=para_nodes.index(ref['begin'])
        if ref['end'] is None:
            descendants=set(ref['begin'].iter());b=max(i for i,n in enumerate(para_nodes) if n in descendants)
        else:
            b=para_nodes.index(ref['end'])
        prefix=''.join(n.text or '' for n in para_nodes[:a] if n.tag==W+'t')
        suffix=''.join(n.text or '' for n in para_nodes[b+1:] if n.tag==W+'t')
        if not (prefix+suffix).strip():
            raise ValueError('reference lacks a unique literal paragraph context')
        refs.append({'kind':ref['kind'],'target_name':ref['target'],'prefix':prefix,'suffix':suffix,
                     'label':ref.get('label'),'paragraph_index':paragraphs[p],
                     'target_paragraph_text':literal(target_p),'target_text':target_text,
                     'target_paragraph_index':paragraphs[target_p]})
    contexts=[render_key(x['prefix']+'\0'+x['suffix']) for x in refs]
    if len(set(contexts))!=len(contexts):
        raise ValueError('body-reference paragraph context is ambiguous')
    return {'toc_instruction':toc_instruction,'toc_depth':depth,'headings':headings,'references':refs,
            'footer_contract':_footer_contract(doc,parts),'bookmark_names':sorted(registry),
            'source_scope':'visible text-only DOCX, simple PAGE footers, one hyperlink TOC; complex scopes rejected'}


def preflight(source,baseline):
    authoritative=inspect_docx(source);actual=inspect_docx(baseline)
    if [(key(h['title']),h['level']) for h in authoritative['headings']] != [(key(h['title']),h['level']) for h in actual['headings']]:
        raise ValueError('baseline headings differ from the source-bound title/level sequence')
    if authoritative['toc_depth']!=actual['toc_depth']:
        raise ValueError('source/baseline TOC depth differs')
    return actual


def _text_matches(page,text):
    expected=render_key(text);blocks=[b for b in page.get_text('blocks') if b[6]==0 and render_key(b[4])]
    matches=[]
    for start in range(len(blocks)):
        current=''
        for end in range(start,min(len(blocks),start+16)):
            current+=render_key(blocks[end][4])
            if not expected.startswith(current):
                break
            if current==expected:
                rect=list(blocks[start][:4])
                for b in blocks[start+1:end+1]:
                    rect=[min(rect[0],b[0]),min(rect[1],b[1]),max(rect[2],b[2]),max(rect[3],b[3])]
                matches.append({'rect':rect,'text':''.join(b[4] for b in blocks[start:end+1])})
                break
    return matches


def _inside(rect,point,margin=1):
    return rect[0]-margin<=point[0]<=rect[2]+margin and rect[1]-margin<=point[1]<=rect[3]+margin


def _same(a,b):
    return math.hypot(a[0]-b[0],a[1]-b[1])<=.01


def validate_pdf(pdf,plan,native):
    import fitz
    if native.get('explicit_call_returned') is not True or native.get('source_store_called') is not False:
        raise ValueError('explicit update/no-DOCX-store evidence absent')
    rows=[r for r in native['index_text_after_call'].splitlines() if r.strip()]
    parsed=[]
    for row in rows:
        values=row.rsplit('\t',1)
        if len(values)!=2:
            raise ValueError('native index result has no attributable page label')
        parsed.append((key(values[0]),values[1].strip()))
    if [r[0] for r in parsed]!=[key(h['title']) for h in plan['headings']]:
        raise ValueError('native index titles/order differ from source')
    with fitz.open(pdf) as document:
        labels=[]
        for page in document:
            bottom=plan['footer_contract']['bottom_margin_pt']
            candidates=[b[4].strip() for b in page.get_text('blocks') if b[6]==0 and b[1]>=page.rect.height-bottom and b[4].strip()]
            if len(candidates)!=1 or not re.fullmatch(r'[0-9IVXLCDMivxlcdm]+',candidates[0]):
                raise ValueError('actual PDF footer/page label is absent or ambiguous')
            labels.append(candidates[0])
        outlines=document.get_toc(simple=False);targets=[]
        for h,(_,display) in zip(plan['headings'],parsed):
            found=[t for t in outlines if t[0]==h['level'] and key(t[1])==key(h['title'])]
            if len(found)!=1:
                raise ValueError('PDF outline/source heading is absent or ambiguous')
            t=found[0];page=t[2]-1;point=list(t[3]['to'])
            blocks=_text_matches(document[page],h['title'])
            if len(blocks)!=1 or not _inside(blocks[0]['rect'],point) or labels[page]!=display:
                raise ValueError('TOC destination/title/actual footer mismatch')
            targets.append({**h,'physical_page':page+1,'display_page':display,'point':point})
        all_links={(pno,i):a for pno,p in enumerate(document) for i,a in enumerate(p.get_links())}
        consumed=set();body=[]
        for ref in plan['references']:
            heading=next((h for h in targets if h['paragraph_index']==ref['target_paragraph_index']),None)
            if heading:
                target_page=heading['physical_page']-1;target_rect=None;target_point=heading['point']
                target_start_x=None
            else:
                matches=[(pno,m) for pno,p in enumerate(document) for m in _text_matches(p,ref['target_paragraph_text'])]
                if len(matches)!=1:
                    raise ValueError('body bookmark target text is ambiguous in rendered PDF')
                target_page,match=matches[0];target_rect=match['rect'];target_point=None
                target_start_x=target_rect[0]
                if render_key(ref['target_text'])!=render_key(ref['target_paragraph_text']):
                    words=[w for w in document[target_page].get_text('words') if w[4]==ref['target_text']
                           and _inside(target_rect,((w[0]+w[2])/2,(w[1]+w[3])/2),0)]
                    if len(words)!=1:
                        raise ValueError('partial bookmark requires one attributable rendered token')
                    target_start_x=words[0][0]
            value=labels[target_page] if ref['kind']=='PAGEREF' else (ref['label'] if ref['kind']=='markup_hyperlink' else ref['target_text'])
            expected=ref['prefix']+value+ref['suffix']
            matches=[(pno,m) for pno,p in enumerate(document) for m in _text_matches(p,expected)]
            if len(matches)!=1:
                raise ValueError('rendered body reference context/value is ambiguous')
            origin,match=matches[0]
            links=[(k,a) for k,a in all_links.items() if k[0]==origin and _inside(match['rect'],((a['from'].x0+a['from'].x1)/2,(a['from'].y0+a['from'].y1)/2),0)]
            if not links:
                raise ValueError('body reference has no clickable PDF annotation')
            for k,a in links:
                if k in consumed or a['kind']!=fitz.LINK_GOTO or a.get('page')!=target_page or 'to' not in a:
                    raise ValueError('body reference PDF destination mismatch')
                if (target_point is not None and not _same(a['to'],target_point)) or (target_rect is not None and not _inside(target_rect,a['to'])):
                    raise ValueError('body reference does not reach its source-bound paragraph')
                if target_start_x is not None and abs(a['to'].x-target_start_x)>1:
                    raise ValueError('body reference does not reach the source range start')
                consumed.add(k)
            body.append({'kind':ref['kind'],'target_name':ref['target_name'],'source_page':origin+1,
                         'target_physical_page':target_page+1,'target_footer':labels[target_page],
                         'annotation_count':len(links),'value_verified':True})
        groups=defaultdict(list)
        for k,a in all_links.items():
            if k in consumed:
                continue
            choices=[i for i,t in enumerate(targets) if a['kind']==fitz.LINK_GOTO and a.get('page')==t['physical_page']-1 and 'to' in a and _same(a['to'],t['point'])]
            if len(choices)!=1:
                raise ValueError('unattributed PDF link or ambiguous TOC destination')
            rect=a['from'];words=document[k[0]].get_text('words')
            words=[w for w in words if rect.x0<=(w[0]+w[2])/2<=rect.x1 and rect.y0<=(w[1]+w[3])/2<rect.y1]
            groups[choices[0]].append({'source_page':k[0]+1,'rect':list(rect),
                'text':''.join(w[4] for w in sorted(words,key=lambda w:(w[0],w[1])))})
        toc=[]
        for i,target in enumerate(targets):
            annotations=sorted(groups[i],key=lambda a:(a['source_page'],a['rect'][1],a['rect'][0]))
            text=''.join(a['text'] for a in annotations);title=render_key(target['title']);label=render_key(target['display_page'])
            if not text.startswith(title) or not text.endswith(label):
                raise ValueError('TOC source annotation text/page label mismatch')
            middle=text[len(title):len(text)-len(label)]
            if middle.strip('.'):
                raise ValueError('TOC annotation has extra/unproved text or leader representation')
            toc.append({**target,'annotation_count':len(annotations),'source_annotations':annotations})
        return {'protocol':'source_bound_uno_pdf_navigation_v1','status':'local_toc_verified',
                'page_count':len(document),'actual_footer_labels':labels,'toc_entries':toc,'body_references':body,
                'all_PDF_annotations_accounted':True,'annotation_count':len(all_links),
                'Word_WPS_verified':False,'specified_typeface_verified':False,'visual_review_performed':False,
                'repeatability_verified':False,'submission_ready':False}
