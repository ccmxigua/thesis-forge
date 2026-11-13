"""Compact redundant CJK heading faces before native TOC rebuilding.

Only a bound TOC entry can select a source heading. Its original paragraph
style continues to supply the same face. Intentional mixed/unknown overrides
are retained. This is a compatibility candidate, not a native renderer fix.
"""
from __future__ import annotations
import copy,hashlib,re
from pathlib import Path
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree
from font_declarations import (font_comparison_key,style_slot,run_cjk_face,
                               word_run_display_issue,script_selection_unresolved)
from template_role_policy import compact,_cached_title,_source_structure_issues
from toc_style_contract import (_check_formatting_protection,_check_style_lock,
                                _style_level,audit_toc_style_rules)
from toc_refresh import request_toc_refresh

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
M='{http://schemas.openxmlformats.org/officeDocument/2006/math}'

def _digest(node):
    return hashlib.sha256(etree.tostring(node,method='c14n',exclusive=True)).hexdigest()

def _heading(role):
    return bool(re.fullmatch(r'heading_[1-9]',role)or role in {
        'heading_appendix','heading_conclusion','heading_acknowledgments','heading_publications',
        'heading_references','bibliography_heading','abstract_title_zh','abstract_title_en'})

def bound_heading_plan(doc, bindings):
    """Snapshot only unambiguous, pre-mutation source heading ownership."""
    from template_role_policy import Plan
    plan = Plan()
    for paragraph in doc.paragraphs:
        roles = bindings.roles_by_element.get(paragraph._p, ())
        if len(roles) == 1:
            role = next(iter(roles))
            if _heading(role):
                plan.claim(paragraph, role)
    return plan

def _check_style_dependencies(style):
    """Require a resolved, unprotected style chain for this projection only.

    This is a conservative dependency policy, not a claim that Word inherits
    a style's locked metadata. No revision/MC branch is accepted or selected.
    """
    seen=set()
    while style is not None:
        if not style.style_id or style.style_id in seen:
            raise ValueError('source font style dependency cycle or missing identity')
        seen.add(style.style_id)
        if _source_structure_issues(style.element):
            raise ValueError('source font style dependency has unresolved revision/compatibility: '+style.style_id)
        _check_style_lock(style.element)
        parents=style.element.findall(W+'basedOn')
        if not parents:return
        parent=style.base_style
        if(len(parents)!=1 or not parents[0].get(W+'val')or parent is None
                or parent.style_id!=parents[0].get(W+'val')or parent.type!=style.type):
            raise ValueError('source font style dependency is unresolved: '+style.style_id)
        style=parent

def _faces(paragraph,doc):
    result=[]
    for node in paragraph._p.iter(W+'r'):
        if word_run_display_issue(node,paragraph):raise ValueError('source run display scope unresolved')
        run=Run(node,paragraph)
        if script_selection_unresolved(run,paragraph,doc):raise ValueError('source run script scope unresolved')
        _check_style_dependencies(run.style)
        # A character style carrying its own CJK face can still be copied to
        # a TOC entry after direct rFonts are compacted. Do not erase it.
        declared,face=style_slot(run.style,'eastAsia')
        if declared:raise ValueError('source character-style CJK declaration retained')
        face=run_cjk_face(run,paragraph,doc)
        if not face:raise ValueError('source effective CJK family unresolved')
        result.append(font_comparison_key(face))
    return result

def compact_toc_heading_fonts(doc,plan,style_application):
    report={'protocol':'bound_toc_heading_cjk_compaction_v1','status':'not_required',
            'changes':[],'findings':[],'native_font_retention_verified':False}
    if style_application.get('status')!='style_definitions_applied':
        report['status']='unhandled_preserved'if style_application.get('status')=='unhandled_preserved'else'not_required'
        return report
    contract=style_application['source_contract'];report['template_sha256']=contract['source_sha256']
    pending=[]
    try:
        if hashlib.sha256(Path(contract['source_path']).read_bytes()).hexdigest()!=contract['source_sha256']:
            raise ValueError('TOC source hash changed before heading font compaction')
        _check_formatting_protection(doc)
        if not audit_toc_style_rules(doc,style_application)['valid']:raise ValueError('TOC style definitions changed before font compaction')
        preflight=request_toc_refresh(doc,apply=False)
        if preflight['status']!='native_update_required':raise ValueError('TOC field scope protected or unresolved')
        anchors={}
        for node in doc._element.iter(W+'bookmarkStart'):
            if node.get(W+'name'):anchors.setdefault(node.get(W+'name'),[]).append(node)
        seen=set()
        for item in preflight['refresh_requests']:
            if item['opcode']!='PAGEREF':continue
            locator=item['locator']
            try:
                match=re.fullmatch(r'\s*PAGEREF\s+(?:"([^"\r\n]+)"|([^\s"\\]+))(?:\s+\\[hpw]\s*)*\s*',item['instruction'],re.I)
                if not match:raise ValueError('TOC page-reference grammar unresolved')
                found=anchors.get(match[1]or match[2],[])
                if len(found)!=1:raise ValueError('TOC source bookmark absent or ambiguous')
                source=next(found[0].iterancestors(W+'p'),None)
                bookmark_id = found[0].get(W+'id')
                starts = [n for n in doc._element.iter(W+'bookmarkStart') if n.get(W+'id') == bookmark_id]
                ends = [n for n in doc._element.iter(W+'bookmarkEnd') if n.get(W+'id') == bookmark_id]
                if (not bookmark_id or len(starts) != 1 or len(ends) != 1
                        or source is None or next(ends[0].iterancestors(W+'p'), None) is not source
                        or list(source.iter()).index(found[0]) >= list(source.iter()).index(ends[0])):
                    raise ValueError('TOC source bookmark range absent, ambiguous or not within heading')
                if source in seen:continue
                source_p=plan.paragraphs.get(source);role=plan.owners.get(source,'')
                if source_p is None or not _heading(role)or source.getparent()is not doc._element.body:
                    raise ValueError('TOC source is not an owned plain body heading')
                pages=doc._element.getroottree().xpath(locator,namespaces={'w':W[1:-1]})
                if len(pages)!=1:raise ValueError('TOC cached entry locator unresolved')
                cache=next(pages[0].iterancestors(W+'p'),None)
                if cache is None:raise ValueError('TOC cache paragraph unresolved')
                cache_p=Paragraph(cache,doc._body);level=_style_level(cache_p.style)
                props=contract['rules'].get(str(level),{});toc_face=props.get('font',{}).get('cjk')
                if not toc_face:raise ValueError('TOC level has no explicit CJK family intent')
                if compact(_cached_title(cache))!=compact(source_p.text):raise ValueError('TOC entry and bookmark heading disagree')
                if _source_structure_issues(source)or source.xpath('.//w:fldChar | .//w:fldSimple | .//w:instrText | .//w:numPr | .//w:drawing | .//w:pict | .//w:object | .//m:oMath | .//m:oMathPara'):
                    raise ValueError('source heading contains protected structure')
                _check_style_dependencies(source_p.style)
                declared,face=style_slot(source_p.style,'eastAsia')
                if not declared or not face:raise ValueError('heading paragraph-style CJK family unresolved')
                key=font_comparison_key(face);before=_faces(source_p,doc)
                if not before or any(value!=key for value in before):
                    raise ValueError('heading CJK override is not redundant with its paragraph style')
                # Equal source/TOC faces do not need a compatibility projection.
                if key==font_comparison_key(toc_face):seen.add(source);continue
                clone=copy.deepcopy(source);changes=[]
                for node in clone.iter(W+'rFonts'):
                    if W+'eastAsiaTheme'in node.attrib:raise ValueError('source CJK theme declaration unresolved')
                    if W+'eastAsia'not in node.attrib:continue
                    value=node.get(W+'eastAsia')
                    if not value or font_comparison_key(value)!=key:raise ValueError('heading/paragraph-mark CJK override is intentional or unresolved')
                    changes.append((node.getroottree().getpath(node),value));node.attrib.pop(W+'eastAsia')
                if not changes:seen.add(source);continue
                staged=Paragraph(clone,source_p._parent)
                if _faces(staged,doc)!=before:raise ValueError('CJK family changed during staged compaction')
                original_fonts=list(source.iter(W+'rFonts'));staged_fonts=list(clone.iter(W+'rFonts'))
                assert len(original_fonts)==len(staged_fonts)
                operations=[n for n,c in zip(original_fonts,staged_fonts)if W+'eastAsia'in n.attrib and W+'eastAsia'not in c.attrib]
                pending.append((source,operations,{'source_locator':source.getroottree().getpath(source),
                    'source_para_id':source.get('{http://schemas.microsoft.com/office/word/2010/wordml}paraId'),
                    'source_text_sha256':hashlib.sha256(source_p.text.encode()).hexdigest(),'role':role,'toc_level':level,
                    'source_style_id':source_p.style.style_id,'heading_CJK_family':face,'TOC_CJK_family':toc_face,
                    'run_effective_CJK_families_before':before,'removed_eastAsia_attributes':len(operations),
                    'removed_attributes':[{'rFonts_index':i,'attribute':'eastAsia','value':n.get(W+'eastAsia')}
                                          for i,n in enumerate(original_fonts)if n in operations],
                    'paragraph_XML_before_sha256':_digest(source),'paragraph_XML_after_sha256':_digest(clone)}))
                seen.add(source)
            except (ValueError,TypeError,KeyError,AttributeError)as exc:
                report['findings'].append({'code':'toc_source_font_unhandled','locator':locator,'reason':str(exc),'status':'unhandled_preserved'})
        if hashlib.sha256(Path(contract['source_path']).read_bytes()).hexdigest()!=contract['source_sha256']:
            raise ValueError('TOC source hash changed before compaction commit')
        for node,operations,item in pending:
            for fonts in operations:fonts.attrib.pop(W+'eastAsia')
            report['changes'].append(item)
        if report['changes']:report['status']='compacted_with_unhandled_sources'if report['findings']else'compacted_pending_native_inspection'
        elif report['findings']:report['status']='unhandled_preserved'
    except (ValueError,TypeError,KeyError,AttributeError,OSError)as exc:
        report['status']='unhandled_preserved';report['findings'].append({'code':'toc_source_font_preflight_unresolved','reason':str(exc)})
    return report

def audit_toc_heading_fonts(doc,report):
    records=[]
    for item in report['changes']:
        matches=[]
        for p in doc.paragraphs:
            if item['source_para_id']:
                ok=p._p.get('{http://schemas.microsoft.com/office/word/2010/wordml}paraId')==item['source_para_id']
            else:ok=hashlib.sha256(p.text.encode()).hexdigest()==item['source_text_sha256']
            if ok:matches.append(p)
        actual=_digest(matches[0]._p)if len(matches)==1 else None
        family_match=False;dependency_issue=None
        if len(matches)==1:
            try:
                _check_style_dependencies(matches[0].style)
                family_match=_faces(matches[0],doc)==item['run_effective_CJK_families_before']
            except (ValueError,TypeError,KeyError,AttributeError)as exc:dependency_issue=str(exc)
        records.append({'source_locator':item['source_locator'],'source_para_id':item['source_para_id'],
                        'serialized_compaction_matches':actual==item['paragraph_XML_after_sha256']and family_match,
                        'serialized_CJK_families_match':family_match,'style_dependency_issue':dependency_issue,
                        'expected_paragraph_sha256':item['paragraph_XML_after_sha256'],'actual_paragraph_sha256':actual})
    return {'status':report['status'],'valid':all(q['serialized_compaction_matches']for q in records),
            'headings':records,'findings':report['findings'],'native_font_retention_verified':False}
