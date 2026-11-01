"""Opt-in source-preserving role policy for the deterministic formatter.

Written executable properties win. Unproved role defaults do not authorize
overwriting a paragraph's source formatting. No school/font/page is encoded.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from document_text_font import document_font_safe_roles

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
UNSAFE = {'ins', 'del', 'moveFrom', 'moveTo', 'AlternateContent',
          'pPrChange','rPrChange','sectPrChange','tblPrChange','trPrChange',
          'tcPrChange','tblGridChange','numberingChange'}


def text(node):
    return ''.join(n.text or '' for n in node.iter(W+'t'))


def compact(value):
    return re.sub(r'\s+', '', value).casefold()


def unsafe(node):
    return any(n.tag.split('}')[-1] in UNSAFE for n in node.iter())


def _source_structure_issues(node):
    """Source evidence inherits revision/compatibility uncertainty from wrappers.

    This does not choose an MC branch, accept tracked changes, or alter XML.
    Empty unresolved paragraphs also remain evidence in the complete context.
    """
    blocked={W+name for name in ('ins','del','moveFrom','moveTo','delText','pPrChange','rPrChange',
                                'sectPrChange','tblPrChange','trPrChange','tcPrChange',
                                'tblGridChange','numberingChange')}
    blocked.update(MC+name for name in ('AlternateContent','Choice','Fallback'))
    fields={W+name for name in ('fldChar','fldSimple','instrText')}
    issues=[]
    for element in node.iter():
        if element.tag in blocked or element.tag in fields:
            issues.append({'element':element.tag,'locator':element.getroottree().getpath(element),
                           'relation':'paragraph_subtree','reason':'source_selection_unresolved'})
    for element in node.iterancestors():
        if element.tag==W+'body':break
        if element.tag in blocked:
            issues.append({'element':element.tag,'locator':element.getroottree().getpath(element),
                           'relation':'ancestor','reason':'source_selection_unresolved'})
    if unsafe(node) and not issues:
        issues.append({'element':node.tag,'locator':node.getroottree().getpath(node),
                       'relation':'paragraph_subtree','reason':'unsupported_source_structure'})
    return issues


def project_roles(spec):
    """Project only executable, role-wide style properties; keep scope data out."""
    result = {role: {k: copy.deepcopy(v) for k, v in value.items()
                     if k not in {'font', 'paragraph'}}
              for role, value in spec.get('roles', {}).items()}
    evidence = {}
    findings = []
    for req in spec.get('requirements', []):
        role = req.get('role')
        if (not role or any(req.get(key) for key in ('source_scope','field_instance_ids','content_instance_ids',
                                                    'condition','conditions','target_selector'))):
            continue
        applicability = req.get('applicability')
        # A conditional fact is not a role-wide style permission. Preserve it
        # for the existing scoped executors instead of widening it here.
        if applicability not in (None, {}) and not (
                isinstance(applicability, dict)
                and not set(applicability) - {'status', 'conditions', 'exceptions'}
                and applicability.get('status') == 'always'
                and applicability.get('conditions', []) in (None, [])
                and applicability.get('exceptions', []) in (None, [])):
            findings.append({'code':'explicit_applicability_not_projected_role_wide',
                             'requirement_id':req.get('id'), 'role':role,
                             'status':'unhandled_preserved'})
            continue
        props = req.get('properties') or {}
        for category in ('font', 'paragraph'):
            if not isinstance(props.get(category), dict):
                continue
            dest = result.setdefault(role, {}).setdefault(category, {})
            for key, value in props[category].items():
                if key in dest and dest[key] != value:
                    raise ValueError('conflicting explicit role property: '+role+'.'+category+'.'+key)
                dest[key] = copy.deepcopy(value)
                evidence.setdefault(role+'.'+category+'.'+key, []).append(req.get('id'))
    safe = document_font_safe_roles(spec, result)
    for path in list(evidence):
        role, category, key = path.split('.', 2)
        if key not in safe.get(role, {}).get(category, {}):
            findings.append({'code':'document_font_scope_not_general_style_permission',
                             'role':role,'property':category+'.'+key,
                             'requirement_ids':evidence.pop(path),'status':'source_format_preserved'})
    result = safe
    for role, old in spec.get('roles', {}).items():
        for category in ('font', 'paragraph'):
            for key, value in (old.get(category) or {}).items():
                if key not in result.get(role, {}).get(category, {}):
                    findings.append({'code':'unproved_role_default_not_applied', 'role':role,
                                     'property':category+'.'+key, 'default':value,
                                     'status':'source_format_preserved'})
    return result, {'explicit_property_requirements':evidence, 'findings':findings}


@dataclass
class Plan:
    owners: dict = field(default_factory=dict)
    paragraphs: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)

    def claim(self, paragraph, role):
        old = self.owners.get(paragraph._p)
        if old is not None and old != role:
            self.owners[paragraph._p] = '__preserve_unknown__'
            self.findings.append({'code':'role_evidence_conflict', 'locator':paragraph._p.getroottree().getpath(paragraph._p),
                                  'roles':[old,role], 'status':'unhandled_preserved'})
        else:
            self.owners[paragraph._p] = role
        self.paragraphs[paragraph._p] = paragraph

    def report(self):
        return {'policy':'source_bound_v1', 'default_enabled':False,
                'decisions':[{'locator':p.getroottree().getpath(p), 'role':role,
                              'status':'source_format_preserved' if role.startswith('__') else 'identified'}
                             for p,role in self.owners.items()],
                'findings':self.findings, 'native_verified':False, 'submission_ready':False}


def compile_plan(doc, scopes, explicit=None):
    """Use field boundaries, exact semantic headings and local object ownership."""
    explicit = explicit or {}
    plan = Plan()
    toc = scopes.get('toc_results', set())
    ps = doc.paragraphs
    top = {p._p:p for p in ps}
    heading_roles = ('heading_1','heading_2','heading_3','heading_4',
                     'figure_caption','table_caption','heading_appendix',
                     'thesis_title_zh','thesis_title_en')
    for role in heading_roles:
        for element in scopes.get('appendix_headings' if role=='heading_appendix' else role, set()):
            if element in top:plan.claim(top[element],role)
    exact = {'摘要':'abstract_title_zh','abstract':'abstract_title_en',
             '参考文献':'bibliography_heading','references':'bibliography_heading',
             'bibliography':'bibliography_heading','后记':'heading_acknowledgments',
             '致谢':'heading_acknowledgments','acknowledgments':'heading_acknowledgments',
             'acknowledgements':'heading_acknowledgments',
             '在学期间发表的学术论文与研究成果':'heading_publications',
             '在学期间研究成果':'heading_publications',
             '目录':'toc_title','contents':'toc_title','tableofcontents':'toc_title'}
    region = None
    for p in ps:
        if p._p in toc:continue
        value=p.text.strip()
        if not value:continue
        if unsafe(p._p):
            plan.claim(p,'__preserve_unknown__')
            plan.findings.append({'code':'display_scope_unresolved','locator':p._p.getroottree().getpath(p._p),'status':'unhandled_preserved'})
            continue
        role=exact.get(compact(value))
        if role:
            plan.claim(p,role)
            region={'abstract_title_zh':'abstract_body_zh','abstract_title_en':'abstract_body_en',
                    'bibliography_heading':'bibliography_entry',
                    'heading_acknowledgments':'__preserve_acknowledgments_content__'}.get(role)
            continue
        if p._p in plan.owners:
            owner = plan.owners[p._p]
            if owner == 'heading_appendix':
                region = 'appendix_body'
            elif owner.startswith('heading_') and not (
                    region == 'appendix_body' and owner in {'heading_2','heading_3','heading_4'}):
                region = None
            continue
        keyword=re.match(r'^(?:关\s*键\s*词\s*[：:]|key\s*words?\s*[:：])',value,re.I)
        if keyword:
            plan.claim(p,'keywords_zh' if re.match(r'^关',value) else 'keywords_en')
            region=None
            continue
        if region == 'appendix_body':
            if re.match(r'^(?:单位|注|资料来源|数据来源|来源|unit|units|note|notes|source)\s*[：:]',value,re.I):
                continue  # The local object-annotation pass owns these labels.
            # A distinct prose role never inherits appendix-title defaults.
            special = (p._p in scopes.get('opaque_equations', set())
                       or p._p in scopes.get('authored_headings', set())
                       or p._p in scopes.get('unresolved_headings', set())
                       or p._p in scopes.get('semantic_headings', set())
                       or bool(_source_structure_issues(p._p))
                       or bool(p._p.xpath('.//w:numPr | .//w:drawing | .//w:pict | .//w:object | .//m:oMath | .//m:oMathPara'))
                       or bool(re.search(r'caption|list|code|preformat|题注|列表', p.style.name, re.I)))
            style = p.style
            seen = set()
            while style is not None:
                if style.style_id in seen or style.element.xpath('./w:pPr/w:numPr'):
                    special = True
                    break
                seen.add(style.style_id)
                if style.element.find(W+'basedOn') is not None and style.base_style is None:
                    special = True
                    break
                style = style.base_style
            if special:
                plan.claim(p,'__preserve_unknown__')
                plan.findings.append({'code':'appendix_content_scope_unresolved',
                                      'locator':p._p.getroottree().getpath(p._p),'status':'unhandled_preserved'})
                if p._p in scopes.get('unresolved_headings',set()) or p._p in scopes.get('semantic_headings',set()):
                    region = None
            else:
                plan.claim(p,'appendix_body')
        elif region=='bibliography_entry':
            mapped=explicit.get('bibliography_entry')
            if re.match(r'^\s*(?:\[\d+\]|［\d+］|\d+[.、])',value) or (mapped and p.style.name==mapped and mapped not in {'Normal','正文'}):
                plan.claim(p,'bibliography_entry')
            else:
                plan.claim(p,'__preserve_unknown__')
                plan.findings.append({'code':'bibliography_entry_scope_unresolved','locator':p._p.getroottree().getpath(p._p),'status':'unhandled_preserved'})
        elif region:
            plan.claim(p,region)
    # Unit/annotation labels are protected only with local table/figure ownership.
    # An isolated label-looking paragraph is unknown, never permission to apply body.
    context=None
    children=list(doc._element.body)
    for i,node in enumerate(children):
        if node.tag==W+'tbl':context='table';continue
        p=top.get(node)
        if p is None:continue
        if node in toc:context=None;continue
        value=p.text.strip()
        if not value:continue
        if node.xpath('.//w:drawing | .//w:pict') and not node.xpath('.//w:object'):
            context='figure';continue
        owner=plan.owners.get(node)
        if owner in {'figure_caption','table_caption'}:
            context='figure' if owner=='figure_caption' else 'table';continue
        label=re.match(r'^(?:单位|注|资料来源|数据来源|来源|unit|units|note|notes|source)\s*[：:]',value,re.I)
        if label:
            following=next((n for n in children[i+1:i+4] if n.tag==W+'tbl'),None)
            known=context or ('table' if following is not None and re.match(r'^(?:单位|units?)',value,re.I) else None)
            if owner is None:
                plan.claim(p,'__preserve_'+known+'_annotation__' if known else '__preserve_unknown__')
                plan.findings.append({'code':'source_annotation_preserved' if known else 'annotation_scope_unresolved',
                                      'locator':node.getroottree().getpath(node),'status':'source_format_preserved' if known else 'unhandled_preserved'})
            continue
        if not re.match(r'^\(?[a-z]\)',value,re.I):context=None
    return plan


def _cached_title(paragraph):
    output=[]
    for node in paragraph.iter():
        if node.tag==W+'tab' and node.getparent().tag==W+'r':break
        if node.tag==W+'t':output.append(node.text or '')
    return ''.join(output).strip()


def repair_toc_links(doc, scopes, plan):
    """Delegate to the shared full-field navigation implementation."""
    from toc_navigation import repair_toc_links as repair
    return repair(doc, scopes, plan)


def _toc_fields(paragraph):
    """Compatibility reader for standalone field fragments, not navigation scope.

    Existing callers inspecting a single paragraph retain this private shape;
    actual navigation uses the complete document's shared field collector.
    """
    from types import SimpleNamespace
    from toc_refresh import collect_field_ranges,flatten_fields
    from toc_navigation import _reference
    roots,errors=collect_field_ranges(SimpleNamespace(_element=SimpleNamespace(body=paragraph)))
    if errors:return [],errors[0]
    result=[]
    for frame in flatten_fields(roots):
        if frame['opcode']=='TOC':continue
        if frame['opcode']not in {'HYPERLINK','PAGEREF'}:return [],'unsupported_field'
        try:ref=_reference(frame)
        except ValueError:return [],'anchor_not_resolved'
        result.append({'kind':ref['kind'],'begin':frame['begin'],'nodes':frame['instruction_nodes'],
                       'instruction':ref['instruction'],'anchor':ref['anchor'],'anchor_span':ref['span']})
    return result,None


def _header_condition_text(value):
    return bool(re.search(r'适用于|仅限|只限|限于|仅当|只在|如果|若|除|当.+时|'
                          r'\b(?:if|unless|except|applicable|applies|provided|conditional|only|when)\b',value,re.I))


def _header_format_atom(value):
    value=value.strip().rstrip('.')
    value=re.sub(r'^页眉(?:内容为)?', '', value)
    from requirements_engine import FONT_NAMES
    from document_text_font import _FONT_NAMES as LATIN_FONT_NAMES
    known_fonts={name.casefold()for name in [*FONT_NAMES,*FONT_NAMES.values(),*LATIN_FONT_NAMES,*LATIN_FONT_NAMES.values()]}
    face=re.fullmatch(r'(?:(?:中文|西文|英文)?字体(?:为|采用|使用)|采用|使用)(.+)',value)
    if face:return face[1].strip().casefold() in known_fonts
    return bool(re.fullmatch(
        r'(?:边距|距离|距上边界)(?:为)?\s*\d+(?:\.\d+)?\s*(?:cm|mm|厘米|毫米|磅|pt)|'
        r'(?:字号(?:为)?\s*)?(?:小?[一二三四五六七八九十初]+号|\d+(?:\.\d+)?\s*(?:pt|磅))|'
        r'(?:居中|左|右|两端)对齐|线型为(?:实线|双实线|单线|双线|文武线|上粗下细(?:的)?文武线)', value, re.I))


def _header_intents(source_clauses):
    """Recognize only explicit unconditional chapter-title directives and sides.

    Binding bytes establishes provenance, never polarity or scope. Unsupported
    conditions/exceptions and conflicts keep the relevant headers unchanged.
    """
    evidence={};blocked=set();findings=[];last_context=None;unresolved_contexts=set()
    # A caller-supplied list is one complete local rule context. Unknown text
    # before/after the positive atom is not discarded for lacking a cue word.
    raw_context=any(isinstance(c,dict) and re.search(r'页眉|\bheaders?\b',str(c.get('text','')),re.I)
                    for c in source_clauses if not isinstance(c,dict) or not c.get('header_rule_context'))
    for clause in source_clauses:
        if not isinstance(clause,dict):continue
        raw=clause.get('text','')
        if not isinstance(raw,str):continue
        scope=clause.get('header_rule_context')
        if clause.get('structure_unresolved'):
            blocked.update(('header','even_page_header'))
            findings.append({'code':'header_source_structure_unresolved','source_clause_id':clause.get('id'),
                             'structure_issues':clause.get('structure_issues',[]),'status':'unhandled_preserved'})
        if scope and not scope.get('complete',False):
            blocked.update(('header','even_page_header'))
            if scope['id']not in unresolved_contexts:
                findings.append({'code':'header_context_structure_unresolved','context':scope,'status':'unhandled_preserved'})
                unresolved_contexts.add(scope['id'])
            continue
        if scope and not scope.get('text_permission_candidate'):
            continue
        has_header=bool(re.search(r'页眉|\bheaders?\b',raw,re.I))
        continuation=bool(scope) or raw_context or bool(clause.get('continuation_of'))
        if not has_header and not continuation:
            last_context=None;continue
        # Collect constraints before authorizing either side. A separate literal
        # or an unparsed suffix can restrict an earlier chapter-title clause.
        if clause.get('structure_unresolved'):
            continue
        heading=re.sub(r'^\s*[（(][一二三四五六七八九十\d]+[)）]\s*','',raw.strip())
        if re.fullmatch(r'页眉(?:和|与|及)页脚|页眉|headers?(?:\s+and\s+footers?)?',heading,re.I):
            last_context=None;continue
        # Restrict authorization to exact bounded propositions. Any condition,
        # exception, or negation around a title claim is conservative no-write.
        if re.search(r'不得|不能|不应|不要|禁止|不可|并非|不是|不为|未|除|若|如果|当|仅首页|首页|'
                     r'\b(?:not|never|no|unless|except|if|when|first)\b',raw,re.I):
            blocked.update(('header','even_page_header'))
            findings.append({'code':'chapter_header_polarity_or_scope_unresolved','source_clause_id':clause.get('id'),
                             'status':'unhandled_preserved'});continue
        context=bool(re.search(r'页眉(?:内容)?(?:应)?(?:为|采用|使用)|\bheaders?\b.{0,30}\b(?:use|uses|is|be)\b',raw,re.I))
        matched=False;context_kinds=set()
        for atom in re.split(r'[。；;，,\n]',raw):
            value=atom.strip().rstrip('.')
            if not value:continue
            if re.fullmatch(r'页眉从正文开始到论文结尾',value):continue
            if _header_condition_text(value):
                kinds=last_context if continuation and last_context else {'header','even_page_header'}
                blocked.update(kinds)
                findings.append({'code':'chapter_header_applicability_unresolved','source_clause_id':clause.get('id'),
                                 'condition_text':value,'status':'unhandled_preserved'});continue
            if _header_format_atom(value):continue
            m=re.fullmatch(r'(?:(仅)?(奇数页|偶数页|奇偶页|所有页))?页眉(?:内容)?(?:应)?为本章标题',re.sub(r'\s+','',value))
            en=re.fullmatch(r'(?:The\s+)?(?:(odd|even|all)(?:[- ]numbered)?\s+(?:page\s+)?)?(?:running\s+)?headers?\s+(?:uses?|must\s+use|is|should\s+be)\s+(?:the\s+)?(?:current\s+)?chapter\s+title',value,re.I)
            if m or en:
                side=(m[2] if m else (en[1] or '').lower())
                kinds=('header',) if side in {'奇数页','odd'} else ('even_page_header',) if side in {'偶数页','even'} else ('header','even_page_header')
                for kind in kinds:evidence.setdefault(kind,[]).append(clause.get('id'))
                context_kinds.update(kinds);matched=True
            else:
                other=re.fullmatch(r'(?:(?:仅)?(奇数页|偶数页|奇偶页|所有页))?页眉(?:内容)?(?:应)?为(.+)',
                                   re.sub(r'\s+','',value))
                # Font/size/alignment statements are not literal header text.
                if other and not re.search(r'字体|字号|对齐|线型',other[2]):
                    kinds=('header',) if other[1]=='奇数页' else ('even_page_header',) if other[1]=='偶数页' else ('header','even_page_header')
                    blocked.update(kinds)
                    findings.append({'code':'header_side_has_different_content','source_clause_id':clause.get('id'),
                                     'kinds':list(kinds),'status':'unhandled_preserved'})
                    context_kinds.update(kinds);matched=True
                elif context or continuation or re.search(r'页眉|\bheaders?\b',value,re.I):
                    blocked.update(('header','even_page_header'))
                    findings.append({'code':'chapter_header_directive_unparsed','source_clause_id':clause.get('id'),
                                     'unparsed_text':value,'status':'unhandled_preserved'})
        last_context=context_kinds or (last_context if continuation else None)
    for kind in blocked:evidence.pop(kind,None)
    return evidence,findings


def repair_header_placeholders(doc, plan, source_clauses, *, source_bound=False):
    evidence,source_findings=_header_intents(source_clauses)
    source_report={'source_constraint_ids':[c.get('id')for c in source_clauses if isinstance(c,dict)],
                   'source_constraints':copy.deepcopy(source_clauses),
                   'source_contexts':list({c['header_rule_context']['id']:c['header_rule_context']
                                         for c in source_clauses if isinstance(c,dict) and c.get('header_rule_context')}.values())}
    if not source_bound or not evidence:
        return {'changes':[],'findings':source_findings+[{'code':'chapter_header_source_intent_not_bound','status':'unhandled_preserved'}],**source_report}
    # Section-owned source headings, never cached TOC titles.
    by_section={};index=0
    for node in doc._element.body:
        role=plan.owners.get(node,'')
        if role=='heading_1' or role in {'bibliography_heading','heading_acknowledgments','heading_appendix'} or role=='heading_publications':
            by_section.setdefault(index,[]).append(node)
        if node.find(W+'pPr/'+W+'sectPr') is not None:index+=1
    changes=[];findings=list(source_findings)
    for index,section in enumerate(doc.sections):
        targets=by_section.get(index,[])
        if len(targets)!=1:continue
        desired=text(targets[0]).strip()
        for kind in ('header','even_page_header'):
            if kind not in evidence:
                findings.append({'code':'chapter_header_side_not_authorized','section':index,'kind':kind,'status':'unhandled_preserved'});continue
            header=getattr(section,kind)
            if header.is_linked_to_previous:
                # Do not mutate an inherited header shared with another section.
                findings.append({'code':'linked_header_placeholder_not_rewritten','section':index,'kind':kind,'status':'unhandled_preserved'});continue
            paragraphs=[p for p in header.paragraphs if p.text.strip()]
            if len(paragraphs)!=1:continue
            p=paragraphs[0];value=p.text.strip()
            if value==desired:continue
            if not re.fullmatch(r'(?:第\s*\d+\s*章\s*|附录\s*[A-Za-z]\s*)?(?:附录)?(?:标题|title|heading)|\[?chapter\s*title\]?',value,re.I):
                continue
            if unsafe(p._p) or p._p.xpath('.//w:fldChar|.//w:fldSimple|.//w:instrText|.//w:bookmarkStart|.//w:bookmarkEnd|.//w:drawing|.//w:object'):
                findings.append({'code':'header_placeholder_structure_unresolved','section':index,'kind':kind,'status':'unhandled_preserved'});continue
            nodes=list(p._p.iter(W+'t'))
            if not nodes:continue
            nodes[0].text=desired
            for n in nodes[1:]:n.text=''
            changes.append({'section':index,'kind':kind,'before':value,'after':desired,
                            'source_clause_ids':evidence[kind]})
    return {'changes':changes,'findings':findings,'native_verified':False,**source_report}


def bound_header_directives(source_docx, expected_sha256):
    """Recover complete header intent from its actual bound source paragraph.

    Legacy clause splitting can separate 'current chapter title' into several
    atoms. This does not rewrite those frozen atoms or their pending status.
    """
    from zipfile import ZipFile
    from lxml import etree
    source_docx=Path(source_docx)
    if hashlib.sha256(source_docx.read_bytes()).hexdigest()!=expected_sha256:
        raise ValueError('header directive source hash mismatch')
    with ZipFile(source_docx) as package:
        root=etree.fromstring(package.read('word/document.xml'),etree.XMLParser(resolve_entities=False,no_network=True))
    body=root.find(W+'body');result=[];children={node:i for i,node in enumerate(body)};records=[]
    # Contexts are physical source ranges, not selections by condition wording.
    # Read paragraph outline levels through the source's actual style chain.
    with ZipFile(source_docx) as package:
        styles=etree.fromstring(package.read('word/styles.xml'),etree.XMLParser(resolve_entities=False,no_network=True))
    style_map={n.get(W+'styleId'):n for n in styles.findall(W+'style')}
    def outline(node):
        pr=node.find(W+'pPr');refs=pr.findall(W+'pStyle')if pr is not None else [];seen=set()
        while True:
            levels=pr.findall(W+'outlineLvl')if pr is not None else []
            if levels:
                if len(levels)!=1 or not (levels[0].get(W+'val','').isdigit()):return None
                value=int(levels[0].get(W+'val'));return value if value<9 else None
            if not refs:return None
            if len(refs)!=1:return None
            ident=refs[0].get(W+'val')
            if ident in seen or ident not in style_map:return None
            seen.add(ident);style=style_map[ident];pr=style.find(W+'pPr');refs=style.findall(W+'basedOn')
    for ordinal,node in enumerate(body.iter(W+'p')):
        issues=_source_structure_issues(node)
        value=''.join(n.text or ''for n in node.iter() if n.tag in {W+'t',W+'delText'}
                      and next(n.iterancestors(W+'p'),None) is node)
        if not value.strip()and not issues:continue
        owner=node
        while owner.getparent() is not body and owner.getparent() is not None:owner=owner.getparent()
        index=children[owner]
        ident='source-paragraph:'+str(index)+((':p'+str(ordinal))if node.getparent() is not body else '')
        item={'id':ident,'text':value,'source_docx_sha256':expected_sha256,'part':'word/document.xml',
              'body_child_zero_based':index,'paragraph_locator':node.getroottree().getpath(node)}
        if issues:item.update(structure_unresolved=True,structure_issues=issues)
        plain=all(child.tag in {W+'pPr',W+'r',W+'bookmarkStart',W+'bookmarkEnd',W+'proofErr'}for child in node)
        plain=plain and all(child.tag in {W+'rPr',W+'t',W+'tab'}for run in node.findall(W+'r')for child in run)
        records.append({'item':item,'direct_body':node.getparent()is body,'plain_boundary':plain,
                        'level':outline(node)if node.getparent()is body and plain and not item.get('structure_unresolved')else None})
    headings=[i for i,x in enumerate(records)if x['level']is not None]
    def has_header(value):return bool(re.search(r'页眉|\bheaders?\b',value,re.I))
    def footer_boundary(record):
        value=record['item']['text']
        return (record['direct_body'] and record['plain_boundary'] and not record['item'].get('structure_unresolved')
                and bool(re.match(r'^\s*(?:页脚\s*[:：]|footers?\s*:)',value,re.I))
                and not has_header(value))
    ranges={}
    for i,record in enumerate(records):
        if not has_header(record['item']['text']):continue
        preceding=[h for h in headings if h<=i]
        # Prefer an actual enclosing header heading, retaining all descendants.
        enclosing=[h for h in preceding if not any(records[k]['level']<=records[h]['level']for k in headings if h<k<=i)]
        header_heads=[h for h in enclosing if has_header(records[h]['item']['text'])]
        start=header_heads[-1]if header_heads else preceding[-1]if preceding else 0
        level=records[start]['level']
        end=next((h for h in headings if h>i and (level is None or records[h]['level']<=level)),len(records))
        # An explicit disjoint footer-role label is a recorded peer-rule boundary;
        # a mixed header/footer paragraph never qualifies for this shortcut.
        peers=[k for k in range(start,end)if footer_boundary(records[k])]
        before=[k for k in peers if k<i]
        if before:start=before[-1]+1
        end=next((k for k in peers if k>i),end)
        if start>end or not(start<=i<end):continue
        key=(start,end)
        ranges[key]=True
    for start,end in sorted(ranges):
        selected=records[start:end]
        if not selected:continue
        boundary=({'kind':'document_end'}if end==len(records)else
                  {'kind':'footer_role_label'if footer_boundary(records[end])else'outline_heading',
                   'locator':records[end]['item']['paragraph_locator'],'text':records[end]['item']['text']})
        scope={'id':selected[0]['item']['id']+'..'+str(end),'policy':'complete_header_rule_context_v1',
               'start_locator':selected[0]['item']['paragraph_locator'],'end_boundary':boundary,
               'collection_complete':True,'complete':not any(x['item'].get('structure_unresolved')for x in selected),
               'unresolved_source_clause_ids':[x['item']['id']for x in selected if x['item'].get('structure_unresolved')],
               'text_permission_candidate':any(re.search(
                   r'本章标题|chapter\s+title|页眉(?:内容)?(?:应)?为|\bheaders?\b.{0,30}\b(?:uses?|is)\b',x['item']['text'],re.I)
                   for x in selected)}
        previous=None
        for row in selected:
            item=dict(row['item']);item['header_rule_context']=scope
            if previous:item['continuation_of']=previous
            result.append(item);previous=item['id']
    return result


def check_copy_paths(destination, receipt_path, protected):
    from artifact_io import paths_alias
    destination,receipt_path=Path(destination),Path(receipt_path)
    if paths_alias([destination,receipt_path]):raise ValueError('paper output and receipt alias each other')
    for p in (destination,receipt_path):
        if p.is_symlink() or (p.exists() and not p.is_file()):raise ValueError('paper destination is not a regular file')
        if any(paths_alias([Path(source),p]) for source in protected):
            raise ValueError('paper copy aliases a protected input/output')


def write_paper_copy(data, destination, receipt_path, *, source, review_output, format_spec,
                     protected_inputs=(), canonicalizer=None, toc_refresh=None):
    """Publish a separate marker-free draft with its own exact byte receipt."""
    import json
    from artifact_io import commit_files, paths_alias, sibling_temp
    from docx import Document
    from io import BytesIO
    destination,receipt_path=Path(destination),Path(receipt_path)
    protected=list(dict.fromkeys([Path(source),Path(review_output),Path(format_spec),*map(Path,protected_inputs)]))
    check_copy_paths(destination,receipt_path,protected)
    pins={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    staged=staged_receipt=None
    try:
        staged=sibling_temp(destination,suffix='.docx');staged.write_bytes(data)
        if canonicalizer is not None:canonicalizer(staged)
        # The byte snapshot is captured before any inline/review material is inserted.
        doc=Document(staged)
        receipt={'protocol':'separate_paper_review_draft_v1','paper_sha256':hashlib.sha256(staged.read_bytes()).hexdigest(),
                 'source_sha256':pins[Path(source)],'review_artifact_sha256':pins[Path(review_output)],
                 'format_spec_sha256':pins[Path(format_spec)],'review_materials_location':'separate_review_artifact',
                 'body_child_count':len(doc._element.body),'submission_ready':False,'native_verified':False}
        if toc_refresh is not None:
            receipt['toc_page_cache'] = {'status':toc_refresh['status'],
                'paper_sha256':receipt['paper_sha256'], 'native_page_numbers_verified':False,
                'cached_pages_trusted':False, 'field_results_recomputed':False}
        staged_receipt=sibling_temp(receipt_path,suffix='.json');staged_receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        check_copy_paths(destination,receipt_path,protected)
        for p,h in pins.items():
            if hashlib.sha256(p.read_bytes()).hexdigest()!=h:raise ValueError('protected paper input changed')
        commit_files([(staged,destination),(staged_receipt,receipt_path)])
        return receipt
    finally:
        for p in (staged,staged_receipt):
            if p is not None:p.unlink(missing_ok=True)
