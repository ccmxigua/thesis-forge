"""Finite, source-bound template comment declarations for appendix prose.

This is not a natural-language fallback. Unrecognised qualifiers, competing
declarations, fields, revisions and MC choices preserve the source formatting.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from requirements_engine import FONT_NAMES, parse_properties

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'


def parse_body_declaration(value):
    """Accept only a complete list of supported affirmative style statements."""
    result = {}
    patterns = (
        r'中文字体(?:为|采用|使用)\s*.+',
        r'西文字体(?:为|采用|使用)\s*.+',
        r'(?:字号(?:为)?\s*)?(?:小?[一二三四五六七八九十]号|初号|小初号|\d+(?:\.\d+)?\s*(?:磅|pt))',
        r'大纲级别(?:为)?正文文本',
        r'(?:两端|居中|左|右|分散)对齐',
        r'首行缩进\s*\d+(?:\.\d+)?\s*字符',
        r'段前(?:和|、)段后\s*\d+(?:\.\d+)?\s*行',
        r'段[前后]\s*\d+(?:\.\d+)?\s*行',
        r'固定(?:值)?\s*\d+(?:\.\d+)?\s*磅',
    )
    tokens = [s.strip() for s in re.split(r'[，,；;。]', value.strip().rstrip('.')) if s.strip()]
    if not tokens:
        raise ValueError('empty body declaration')
    for token in tokens:
        kind = next((i for i, pattern in enumerate(patterns) if re.fullmatch(pattern, token, re.I)), None)
        if kind is None:
            raise ValueError('unsupported or qualified body statement: '+token)
        if kind in (0, 1):
            # Consume the entire declared family, never a known substring of
            # a qualified name, alternative, or applicability statement.
            family = re.sub(r'^(?:中文|西文)字体(?:为|采用|使用)\s*', '', token)
            family = re.sub(r'\s+', ' ', family).strip().casefold()
            names = ({**FONT_NAMES, **{v:v for v in FONT_NAMES.values()}} if kind == 0 else
                     {name:name for name in ('Times New Roman','Arial','Calibri','Cambria','Courier New')})
            aliases = {re.sub(r'\s+', ' ', name).casefold():value for name,value in names.items()}
            if family not in aliases:
                raise ValueError('unresolved complete font family or scope: '+token)
            props = {'font':{'cjk' if kind == 0 else 'latin':aliases[family]}}
        elif kind == 3:
            props = {'paragraph': {'outline_level': 10}}
        else:
            parsed = parse_properties('正文：'+token)
            category, keys = (
                ('font', ('cjk',)), ('font', ('latin',)), ('font', ('size_pt',)),
                ('paragraph', ('outline_level',)), ('paragraph', ('alignment',)),
                ('paragraph', ('first_line_indent_chars',)),
                ('paragraph', ('space_before_lines','space_after_lines')),
                ('paragraph', ('space_before_lines','space_after_lines')),
                ('paragraph', ('line_spacing',)),
            )[kind]
            # The generic parser can also read 20pt line spacing as a font
            # size. The complete token's grammar owns exactly these slots.
            props = {category:{key:parsed.get(category, {})[key]
                               for key in keys if key in parsed.get(category, {})}}
        if not any(props.values()):
            raise ValueError('body statement has no supported executable property: '+token)
        for category, values in props.items():
            for key, val in values.items():
                previous = result.setdefault(category, {})
                if key in previous and previous[key] != val:
                    raise ValueError('conflicting body property: '+category+'.'+key)
                previous[key] = val
    return result


def bound_body_rules(template, expected_sha256=None):
    """Read explicitly labelled appendix-content comments on plain body anchors.

    The selected official template, comment text and anchor are all recorded.
    Title instructions before the content label never become body properties.
    """
    record = {'protocol': 'template_comment_body_rules_v1', 'rules': {},
              'declarations': [], 'findings': [], 'native_verified': False}
    if template is None:
        return record
    template = Path(template)
    data = template.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('official template hash mismatch')
    record.update(source_path=str(template.resolve()), source_sha256=digest)
    with ZipFile(template) as package:
        if 'word/comments.xml' not in package.namelist():
            return record
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        comments = etree.fromstring(package.read('word/comments.xml'), parser)
    doc = Document(template)
    relationships = [rel for rel in doc.part.rels.values() if rel.reltype.endswith('/comments')]
    if len(relationships) != 1 or relationships[0].is_external or str(relationships[0].target_part.partname) != '/word/comments.xml':
        record['findings'].append({'code':'template_comment_source_unbound','status':'unhandled_preserved'})
        return record
    anchors = {}
    ends = {}
    refs = {}
    for paragraph in doc.paragraphs:
        for marker in paragraph._p.iter(W+'commentRangeStart'):
            if next(marker.iterancestors(W+'p'), None) is paragraph._p:
                anchors.setdefault(marker.get(W+'id'), []).append(paragraph)
        for marker in paragraph._p.iter(W+'commentRangeEnd'):
            ends.setdefault(marker.get(W+'id'), []).append(paragraph._p)
        for marker in paragraph._p.iter(W+'commentReference'):
            refs.setdefault(marker.get(W+'id'), []).append(paragraph._p)
    blocked = {W+n for n in ('ins','del','moveFrom','moveTo','delText','pPrChange','rPrChange',
                             'sectPrChange','tblPrChange','trPrChange','tcPrChange','tblGridChange',
                             'numberingChange','fldChar','fldSimple','instrText')}
    blocked |= {MC+n for n in ('AlternateContent','Choice','Fallback')}
    declarations = []
    for comment in comments:
        raw = ''.join(n.text or '' for n in comment.iter(W+'t'))
        matches = list(re.finditer(r'附录(?:正文|内容)\s*[:：]', raw))
        if not matches:
            continue
        cid = comment.get(W+'id')
        points = anchors.get(cid, [])
        issue = None
        if len(matches) != 1 or len(points) != 1:
            issue = 'template_body_declaration_anchor_ambiguous'
        elif ends.get(cid) != [points[0]._p] or refs.get(cid) != [points[0]._p]:
            issue = 'template_body_declaration_range_unresolved'
        elif any(n.tag in blocked for n in points[0]._p.iter()) or any(n.tag in blocked for n in comment.iter()):
            issue = 'template_body_declaration_structure_unresolved'
        elif any(n.tag == W+'p' for n in points[0]._p.iterdescendants()):
            issue = 'template_body_declaration_nested_paragraph_unresolved'
        elif not re.fullmatch(r'\s*附\s*录(?:\s*[A-ZＡ-Ｚ一二三四五六七八九十\d]+)?(?:\s+.*|[:：].*)?', points[0].text, re.I):
            issue = 'template_body_declaration_anchor_not_appendix_heading'
        # Only a plain direct-body anchor is supported; nested/MC/revised
        # anchors are not discovered through an unbounded descendant search.
        if issue:
            record['findings'].append({'code': issue, 'comment_id': cid,
                                       'status': 'unhandled_preserved'})
            continue
        body = raw[matches[0].end():].strip()
        evidence = {'role': 'appendix_body', 'comment_id': cid,
                    'comment_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                    'source_sha256': digest, 'text': body,
                    'anchor_text': points[0].text, 'complete_comment_text': raw,
                    'anchor_locator': points[0]._p.getroottree().getpath(points[0]._p)}
        try:
            prefix = raw[:matches[0].start()]
            # A later role label does not erase an earlier unknown condition.
            for sentence in [s.strip() for s in prefix.split('。') if s.strip()]:
                if re.fullmatch(r'附录中的图、表、附注、参考文献、公式另行编号，如图[A-Z]\d+，表[A-Z]\d+，附注[A-Z]\d+，或文献\[[A-Z]\d+\]', sentence):
                    continue
                for token in [s.strip() for s in re.split(r'[，,；;]', sentence) if s.strip()]:
                    if re.fullmatch(r'另起一页|无缩进|大纲级别[1-9]级|标题下方空[一二两三四五六七八九十\d]+行空白行|若不需要可省略此页', token):
                        continue
                    parse_body_declaration(token)
            evidence['properties'] = parse_body_declaration(body)
        except ValueError as exc:
            record['findings'].append({'code': 'template_body_declaration_unresolved',
                                       **evidence, 'reason': str(exc), 'status': 'unhandled_preserved'})
            continue
        declarations.append(evidence)
    record['declarations'] = declarations
    if record['findings']:
        return record  # Never pick a convenient declaration around unresolved evidence.
    for declaration in declarations:
        role = declaration['role']
        value = declaration['properties']
        if role in record['rules'] and record['rules'][role] != value:
            record['findings'].append({'code': 'template_body_declarations_conflict',
                                       'role': role, 'status': 'unhandled_preserved'})
            record['rules'] = {}
            return record
        record['rules'][role] = value
    if template.read_bytes() != data:
        raise ValueError('official template changed during body rule extraction')
    return record


def merge_body_rules(roles, record):
    """Supplement absent properties; competing explicit contracts block the role."""
    import copy
    roles = copy.deepcopy(roles)
    for role, props in record['rules'].items():
        existing = roles.get(role, {})
        conflict = any(key in existing.get(cat, {}) and existing[cat][key] != val
                       for cat, values in props.items() for key, val in values.items())
        if conflict:
            raise ValueError('explicit contract conflicts with official template body declaration: '+role)
        dest = roles.setdefault(role, {})
        for category, values in props.items():
            dest.setdefault(category, {}).update(copy.deepcopy(values))
    return roles
