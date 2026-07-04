#!/opt/homebrew/bin/python3
from __future__ import annotations

import argparse
import copy
import re
import sys
import tempfile
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
M_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'
XML_NS = 'http://www.w3.org/XML/1998/namespace'
WP_NS = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PIC_NS = 'http://schemas.openxmlformats.org/drawingml/2006/picture'

M_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
ET.register_namespace('w', W_NS)
ET.register_namespace('r', R_NS)
ET.register_namespace('m', M_NS)
ET.register_namespace('', CT_NS)

PAGE_W = '10431'
PAGE_H = '14740'
TOP = '1134'
BOTTOM = '850'
LEFT = '1134'
RIGHT = '1134'
HEADER = '850'
FOOTER = '992'
TOC_PLACEHOLDER = '__TJUFE_TOC_PLACEHOLDER__'
FOOTNOTE_NUMFMT = 'decimalEnclosedCircleChinese'
UNIT_PREFIXES = ('单位：', '单位:', '计量单位：', '计量单位:', '数据单位：', '数据单位:')
SOURCE_PREFIXES = ('资料来源：', '资料来源:', '数据来源：', '数据来源:', '来源：', '来源:')
XREF_RE = re.compile(r'\[\[\[TJUFE_XREF:([^|\]]+)\|([^\]]+)\]\]\]')
EQLABEL_RE = re.compile(r'^\[\[\[TJUFE_EQLABEL:([^\]]+)\]\]\]$')
LABEL_RE = re.compile(r'TJUFE_LABEL__([^_]+(?:_[^_]+)*)__')


def sanitize_bookmark_name(label: str) -> str:
    name = re.sub(r'[^0-9A-Za-z_]', '_', label)
    if not name:
        name = 'eqref'
    if name[0].isdigit():
        name = f'bm_{name}'
    return f'TJUFE_{name}'


def qn(ns: str, local: str) -> str:
    mapping = {'w': W_NS, 'm': M_NS, 'r': R_NS, 'rel': REL_NS, 'ct': CT_NS, 'wp': WP_NS, 'a': A_NS, 'pic': PIC_NS}
    return f'{{{mapping[ns]}}}{local}'


def paragraph_text(p: ET.Element) -> str:
    texts = []
    for t in p.findall('.//' + qn('w', 't')):
        if t.text:
            texts.append(t.text)
    return ''.join(texts).strip()


def paragraph_style(p: ET.Element) -> str | None:
    ppr = p.find(qn('w', 'pPr'))
    if ppr is None:
        return None
    pstyle = ppr.find(qn('w', 'pStyle'))
    if pstyle is None:
        return None
    return pstyle.get(qn('w', 'val'))


def set_paragraph_style(p: ET.Element, style_id: str) -> None:
    ppr = p.find(qn('w', 'pPr'))
    if ppr is None:
        ppr = ET.SubElement(p, qn('w', 'pPr'))
    pstyle = ppr.find(qn('w', 'pStyle'))
    if pstyle is None:
        pstyle = ET.SubElement(ppr, qn('w', 'pStyle'))
    pstyle.set(qn('w', 'val'), style_id)


def has_run_style(p: ET.Element, style_id: str) -> bool:
    for rstyle in p.findall('.//' + qn('w', 'rStyle')):
        if rstyle.get(qn('w', 'val')) == style_id:
            return True
    return False


def paragraph_has_math(p: ET.Element) -> bool:
    return p.find('.//' + qn('m', 'oMathPara')) is not None or p.find('.//' + qn('m', 'oMath')) is not None


def paragraph_has_math_para(p: ET.Element) -> bool:
    return p.find('.//' + qn('m', 'oMathPara')) is not None


def paragraph_has_numpr(p: ET.Element) -> bool:
    ppr = p.find(qn('w', 'pPr'))
    if ppr is None:
        return False
    return ppr.find(qn('w', 'numPr')) is not None


def ensure_text_run(p: ET.Element) -> ET.Element:
    run = p.find(qn('w', 'r'))
    if run is None:
        run = ET.SubElement(p, qn('w', 'r'))
    text = run.find(qn('w', 't'))
    if text is None:
        text = ET.SubElement(run, qn('w', 't'))
    return text


def set_paragraph_text(p: ET.Element, text: str) -> None:
    texts = p.findall('.//' + qn('w', 't'))
    if texts:
        texts[0].text = text
        for extra in texts[1:]:
            extra.text = ''
    else:
        ensure_text_run(p).text = text


def clear_paragraph_content(p: ET.Element) -> None:
    for child in list(p):
        if child.tag != qn('w', 'pPr'):
            p.remove(child)


def append_plain_run(p: ET.Element, text: str) -> None:
    if not text:
        return
    r = ET.SubElement(p, qn('w', 'r'))
    t = ET.SubElement(r, qn('w', 't'))
    t.set(f'{{{XML_NS}}}space', 'preserve')
    t.text = text


def append_internal_hyperlink(p: ET.Element, anchor: str, text: str) -> None:
    hl = ET.SubElement(p, qn('w', 'hyperlink'))
    hl.set(qn('w', 'anchor'), anchor)
    hl.set(qn('w', 'history'), '1')
    r = ET.SubElement(hl, qn('w', 'r'))
    t = ET.SubElement(r, qn('w', 't'))
    t.set(f'{{{XML_NS}}}space', 'preserve')
    t.text = text


def localize_xref_text(text: str) -> str:
    text = text.strip()
    patterns = [
        (r'^Equation\s*\(?\s*([A-Za-z0-9.\-]+)\s*\)?$', r'式（\1）'),
        (r'^Figure\s+([A-Za-z0-9.\-]+)$', r'图\1'),
        (r'^Table\s+([A-Za-z0-9.\-]+)$', r'表\1'),
        (r'^Theorem\s+([A-Za-z0-9.\-]+)$', r'定理\1'),
        (r'^Lemma\s+([A-Za-z0-9.\-]+)$', r'引理\1'),
        (r'^Proposition\s+([A-Za-z0-9.\-]+)$', r'命题\1'),
        (r'^Corollary\s+([A-Za-z0-9.\-]+)$', r'推论\1'),
        (r'^Definition\s+([A-Za-z0-9.\-]+)$', r'定义\1'),
        (r'^Remark\s+([A-Za-z0-9.\-]+)$', r'注\1'),
        (r'^Lemma\s*\[([^\]]+)\)$', r'引理[\1)'),
        (r'^Lemma\s*\[([^\]]+)\]$', r'引理[\1]'),
        (r'^Proposition\s*\[([^\]]+)\)$', r'命题[\1)'),
        (r'^Proposition\s*\[([^\]]+)\]$', r'命题[\1]'),
        (r'^Corollary\s*\[([^\]]+)\)$', r'推论[\1)'),
        (r'^Corollary\s*\[([^\]]+)\]$', r'推论[\1]'),
        (r'^Theorem\s*\[([^\]]+)\)$', r'定理[\1)'),
        (r'^Theorem\s*\[([^\]]+)\]$', r'定理[\1]'),
        (r'^Reference\s*\[([^\]]+)\)$', r'\1'),
        (r'^Reference\s*\[([^\]]+)\]$', r'\1'),
    ]
    for pattern, repl in patterns:
        localized = re.sub(pattern, repl, text)
        if localized != text:
            return localized
    return text


def replace_xref_placeholders_in_paragraph(p: ET.Element, bookmark_map: dict[str, str], eq_display_map: dict[str, str]) -> None:
    text = paragraph_text(p)
    if '[[[TJUFE_XREF:' not in text:
        return
    matches = list(XREF_RE.finditer(text))
    if not matches:
        return
    ppr = p.find(qn('w', 'pPr'))
    clear_paragraph_content(p)
    if ppr is not None and (len(p) == 0 or p[0].tag != qn('w', 'pPr')):
        p.insert(0, ppr)
    pos = 0
    for m in matches:
        append_plain_run(p, text[pos:m.start()])
        label = m.group(1).strip()
        shown = localize_xref_text(eq_display_map.get(label, m.group(2)))
        anchor = bookmark_map.get(label)
        if anchor:
            append_internal_hyperlink(p, anchor, shown)
        else:
            append_plain_run(p, shown)
        pos = m.end()
    append_plain_run(p, text[pos:])


def extract_eq_label_marker(text: str) -> str | None:
    m = EQLABEL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).strip()


def extract_generic_label_markers(text: str) -> list[str]:
    return [m.group(1).strip() for m in LABEL_RE.finditer(text) if m.group(1).strip()]


def strip_label_markers_in_paragraph(p: ET.Element) -> None:
    for t in p.findall('.//' + qn('w', 't')):
        if t.text:
            t.text = LABEL_RE.sub('', t.text)


def attach_bookmarks_to_paragraph(p: ET.Element, labels: list[str], bookmark_map: dict[str, str], next_bookmark_id: int) -> int:
    if not labels:
        return next_bookmark_id
    insert_at = 1 if len(p) > 0 and p[0].tag == qn('w', 'pPr') else 0
    for label in labels:
        bookmark_name = sanitize_bookmark_name(label)
        bookmark_map[label] = bookmark_name
        bm_start = ET.Element(qn('w', 'bookmarkStart'))
        bm_start.set(qn('w', 'id'), str(next_bookmark_id))
        bm_start.set(qn('w', 'name'), bookmark_name)
        bm_end = ET.Element(qn('w', 'bookmarkEnd'))
        bm_end.set(qn('w', 'id'), str(next_bookmark_id))
        p.insert(insert_at, bm_start)
        p.insert(insert_at + 1, bm_end)
        insert_at += 2
        next_bookmark_id += 1
    return next_bookmark_id


def rebuild_heading_with_tab(p: ET.Element, prefix: str, title: str) -> None:
    ppr = p.find(qn('w', 'pPr'))
    for child in list(p):
        if child.tag != qn('w', 'pPr'):
            p.remove(child)

    r_prefix = ET.SubElement(p, qn('w', 'r'))
    t_prefix = ET.SubElement(r_prefix, qn('w', 't'))
    t_prefix.set(f'{{{XML_NS}}}space', 'preserve')
    t_prefix.text = prefix

    r_tab = ET.SubElement(p, qn('w', 'r'))
    ET.SubElement(r_tab, qn('w', 'tab'))

    r_title = ET.SubElement(p, qn('w', 'r'))
    t_title = ET.SubElement(r_title, qn('w', 't'))
    t_title.set(f'{{{XML_NS}}}space', 'preserve')
    t_title.text = title


def normalize_heading_separator(p: ET.Element, style: str | None, text: str) -> str:
    match = None
    if style == 'Heading2':
        match = re.match(r'^((?:\d+\.\d+|[A-Z]\.\d+))\s+(.+)$', text)
    elif style == 'Heading3':
        match = re.match(r'^((?:\d+\.\d+\.\d+|[A-Z]\.\d+\.\d+))\s+(.+)$', text)
    if not match:
        return text
    prefix, title = match.groups()
    normalized = f'{prefix}  {title}'
    set_paragraph_text(p, normalized)
    set_paragraph_style(p, style)
    return normalized


def is_body_heading1(text: str) -> bool:
    return bool(re.match(r'^第\d+章\s+.+', text))


def is_appendix_heading1(text: str) -> bool:
    return bool(re.match(r'^附录([A-Z])\s+.+', text))


def chapter_label_from_heading(text: str) -> str | None:
    m = re.match(r'^第(\d+)章', text)
    if m:
        return m.group(1)
    m = re.match(r'^附录([A-Z])', text)
    if m:
        return m.group(1)
    return None


def is_body_heading2(text: str) -> bool:
    return bool(re.match(r'^(\d+\.\d+|[A-Z]\.\d+)\s+.+', text))


def is_body_heading3(text: str) -> bool:
    return bool(re.match(r'^(\d+\.\d+\.\d+|[A-Z]\.\d+\.\d+)\s+.+', text))


def is_research_outputs_heading(text: str) -> bool:
    return text == '在学期间发表的学术论文与研究成果'


def append_equation_number(p: ET.Element, label: str, bookmark_name: str | None = None, bookmark_id: int | None = None) -> None:
    ppr = ensure_p_pr(p)
    jc = ppr.find(qn('w', 'jc'))
    if jc is not None:
        ppr.remove(jc)

    tabs = ppr.find(qn('w', 'tabs'))
    if tabs is None:
        tabs = ET.SubElement(ppr, qn('w', 'tabs'))
    for child in list(tabs):
        if child.tag == qn('w', 'tab'):
            tabs.remove(child)

    tab_center = ET.SubElement(tabs, qn('w', 'tab'))
    tab_center.set(qn('w', 'val'), 'center')
    tab_center.set(qn('w', 'pos'), '4500')

    tab_right = ET.SubElement(tabs, qn('w', 'tab'))
    tab_right.set(qn('w', 'val'), 'right')
    tab_right.set(qn('w', 'pos'), '9000')

    children = list(p)
    insert_at = 1 if children and children[0].tag == qn('w', 'pPr') else 0
    if not any(child.tag == qn('w', 'r') and child.find(qn('w', 'tab')) is not None for child in children[:insert_at+2]):
        r_center = ET.Element(qn('w', 'r'))
        ET.SubElement(r_center, qn('w', 'tab'))
        p.insert(insert_at, r_center)

    r_tab = ET.SubElement(p, qn('w', 'r'))
    ET.SubElement(r_tab, qn('w', 'tab'))
    if bookmark_name and bookmark_id is not None:
        bm_start = ET.SubElement(p, qn('w', 'bookmarkStart'))
        bm_start.set(qn('w', 'id'), str(bookmark_id))
        bm_start.set(qn('w', 'name'), bookmark_name)
    r_num = ET.SubElement(p, qn('w', 'r'))
    rpr = ET.SubElement(r_num, qn('w', 'rPr'))
    fonts = ET.SubElement(rpr, qn('w', 'rFonts'))
    fonts.set(qn('w', 'ascii'), 'Times New Roman')
    fonts.set(qn('w', 'hAnsi'), 'Times New Roman')
    fonts.set(qn('w', 'eastAsia'), 'SimSun')
    sz = ET.SubElement(rpr, qn('w', 'sz'))
    sz.set(qn('w', 'val'), '24')
    szcs = ET.SubElement(rpr, qn('w', 'szCs'))
    szcs.set(qn('w', 'val'), '24')
    t = ET.SubElement(r_num, qn('w', 't'))
    t.text = label
    if bookmark_name and bookmark_id is not None:
        bm_end = ET.SubElement(p, qn('w', 'bookmarkEnd'))
        bm_end.set(qn('w', 'id'), str(bookmark_id))


def ensure_child(parent: ET.Element, tag: str) -> ET.Element:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    return child


def remove_children(parent: ET.Element, tags: set[str]) -> None:
    for child in list(parent):
        if child.tag in tags:
            parent.remove(child)


def configure_footnote_pr(footnote_pr: ET.Element) -> None:
    remove_children(footnote_pr, {qn('w', 'numRestart'), qn('w', 'numFmt')})
    num_restart = ET.SubElement(footnote_pr, qn('w', 'numRestart'))
    num_restart.set(qn('w', 'val'), 'eachPage')
    num_fmt = ET.SubElement(footnote_pr, qn('w', 'numFmt'))
    num_fmt.set(qn('w', 'val'), FOOTNOTE_NUMFMT)


def configure_sectpr(sectpr: ET.Element, *, page_fmt: str, page_start: int | None, header_rid: str | None, footer_rid: str | None) -> None:
    remove_children(sectpr, {qn('w', 'headerReference'), qn('w', 'footerReference'), qn('w', 'pgSz'), qn('w', 'pgMar'), qn('w', 'pgNumType'), qn('w', 'footnotePr'), qn('w', 'docGrid')})

    if header_rid:
        hr = ET.SubElement(sectpr, qn('w', 'headerReference'))
        hr.set(qn('w', 'type'), 'default')
        hr.set(qn('r', 'id'), header_rid)
    if footer_rid:
        fr = ET.SubElement(sectpr, qn('w', 'footerReference'))
        fr.set(qn('w', 'type'), 'default')
        fr.set(qn('r', 'id'), footer_rid)

    pgsz = ET.SubElement(sectpr, qn('w', 'pgSz'))
    pgsz.set(qn('w', 'w'), PAGE_W)
    pgsz.set(qn('w', 'h'), PAGE_H)

    pgmar = ET.SubElement(sectpr, qn('w', 'pgMar'))
    pgmar.set(qn('w', 'top'), TOP)
    pgmar.set(qn('w', 'right'), RIGHT)
    pgmar.set(qn('w', 'bottom'), BOTTOM)
    pgmar.set(qn('w', 'left'), LEFT)
    pgmar.set(qn('w', 'header'), HEADER)
    pgmar.set(qn('w', 'footer'), FOOTER)
    pgmar.set(qn('w', 'gutter'), '0')

    pgnum = ET.SubElement(sectpr, qn('w', 'pgNumType'))
    pgnum.set(qn('w', 'fmt'), page_fmt)
    if page_start is not None:
        pgnum.set(qn('w', 'start'), str(page_start))

    footnote_pr = ET.SubElement(sectpr, qn('w', 'footnotePr'))
    configure_footnote_pr(footnote_pr)

    doc_grid = ET.SubElement(sectpr, qn('w', 'docGrid'))
    doc_grid.set(qn('w', 'linePitch'), '326')
    doc_grid.set(qn('w', 'charSpace'), '0')


def next_rel_id(rels_root: ET.Element) -> str:
    nums = []
    for rel in rels_root.findall(qn('rel', 'Relationship')):
        rid = rel.get('Id', '')
        if rid.startswith('rId'):
            try:
                nums.append(int(rid[3:]))
            except ValueError:
                pass
    return f'rId{max(nums, default=0) + 1}'


def ensure_relationship(rels_root: ET.Element, target: str, rel_type: str) -> str:
    for rel in rels_root.findall(qn('rel', 'Relationship')):
        if rel.get('Target') == target and rel.get('Type') == rel_type:
            return rel.get('Id')
    rid = next_rel_id(rels_root)
    rel = ET.SubElement(rels_root, qn('rel', 'Relationship'))
    rel.set('Id', rid)
    rel.set('Type', rel_type)
    rel.set('Target', target)
    return rid


def ensure_content_type(ct_root: ET.Element, part_name: str, content_type: str) -> None:
    for ov in ct_root.findall(qn('ct', 'Override')):
        if ov.get('PartName') == part_name:
            ov.set('ContentType', content_type)
            return
    ov = ET.SubElement(ct_root, qn('ct', 'Override'))
    ov.set('PartName', part_name)
    ov.set('ContentType', content_type)


def is_fixed_header_heading(style: str | None, text: str) -> bool:
    return style == 'Heading1' and (
        is_body_heading1(text)
        or is_appendix_heading1(text)
        or text in {'参考文献', '后 记', '后记'}
        or is_research_outputs_heading(text)
    )


def collect_fixed_header_sections(body: ET.Element) -> list[tuple[int, str]]:
    sections: list[tuple[int, str]] = []
    for idx, child in enumerate(list(body)):
        if child.tag != qn('w', 'p'):
            continue
        style = paragraph_style(child)
        text = paragraph_text(child)
        if is_fixed_header_heading(style, text):
            sections.append((idx, text))
    return sections


def header_xml(title: str) -> bytes:
    hdr = ET.Element(qn('w', 'hdr'))
    p = ET.SubElement(hdr, qn('w', 'p'))
    ppr = ET.SubElement(p, qn('w', 'pPr'))
    pstyle = ET.SubElement(ppr, qn('w', 'pStyle'))
    pstyle.set(qn('w', 'val'), 'Header')
    jc = ET.SubElement(ppr, qn('w', 'jc'))
    jc.set(qn('w', 'val'), 'center')
    pbdr = ET.SubElement(ppr, qn('w', 'pBdr'))
    bottom = ET.SubElement(pbdr, qn('w', 'bottom'))
    bottom.set(qn('w', 'val'), 'thinThickSmallGap')
    bottom.set(qn('w', 'sz'), '12')
    bottom.set(qn('w', 'space'), '1')
    bottom.set(qn('w', 'color'), 'auto')
    run = ET.SubElement(p, qn('w', 'r'))
    text = ET.SubElement(run, qn('w', 't'))
    text.text = title
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(hdr, encoding='utf-8')


def write_header_part(unzip_dir: Path, rels_root: ET.Element, ct_root: ET.Element, *, index: int, title: str) -> str:
    target = f'header-fixed-{index}.xml'
    rid = ensure_relationship(rels_root, target, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/header')
    ensure_content_type(ct_root, f'/word/{target}', 'application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml')
    (unzip_dir / 'word' / target).write_bytes(header_xml(title))
    return rid


def footer_xml() -> bytes:
    xml = fr'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="{W_NS}" xmlns:r="{R_NS}">
  <w:p>
    <w:pPr>
      <w:pStyle w:val="Footer"/>
      <w:jc w:val="center"/>
    </w:pPr>
    <w:fldSimple w:instr=" PAGE \\* MERGEFORMAT ">
      <w:r><w:t>1</w:t></w:r>
    </w:fldSimple>
  </w:p>
</w:ftr>
'''
    return xml.encode('utf-8')


def make_toc_paragraph() -> ET.Element:
    p = ET.Element(qn('w', 'p'))
    ppr = ET.SubElement(p, qn('w', 'pPr'))
    pstyle = ET.SubElement(ppr, qn('w', 'pStyle'))
    pstyle.set(qn('w', 'val'), 'Normal')

    r1 = ET.SubElement(p, qn('w', 'r'))
    fld1 = ET.SubElement(r1, qn('w', 'fldChar'))
    fld1.set(qn('w', 'fldCharType'), 'begin')

    r2 = ET.SubElement(p, qn('w', 'r'))
    instr = ET.SubElement(r2, qn('w', 'instrText'))
    instr.set(f'{{{XML_NS}}}space', 'preserve')
    instr.text = ' TOC \\o "1-3" \\h \\z \\u '

    r3 = ET.SubElement(p, qn('w', 'r'))
    fld3 = ET.SubElement(r3, qn('w', 'fldChar'))
    fld3.set(qn('w', 'fldCharType'), 'separate')

    r4 = ET.SubElement(p, qn('w', 'r'))
    t = ET.SubElement(r4, qn('w', 't'))
    t.text = ''

    r5 = ET.SubElement(p, qn('w', 'r'))
    fld5 = ET.SubElement(r5, qn('w', 'fldChar'))
    fld5.set(qn('w', 'fldCharType'), 'end')
    return p


def update_settings(settings_root: ET.Element) -> None:
    update_fields = settings_root.find(qn('w', 'updateFields'))
    if update_fields is None:
        update_fields = ET.SubElement(settings_root, qn('w', 'updateFields'))
    update_fields.set(qn('w', 'val'), 'true')

    footnote_pr = settings_root.find(qn('w', 'footnotePr'))
    if footnote_pr is None:
        footnote_pr = ET.SubElement(settings_root, qn('w', 'footnotePr'))
    configure_footnote_pr(footnote_pr)


def clean_caption_title(text: str, kind: str) -> tuple[bool, str]:
    text = text.strip()
    explicit_continued = text.startswith(f'续{kind}')
    text = re.sub(rf'^续?{kind}[A-Z0-9]+\.\d+\s*', '', text)
    if explicit_continued:
        text = re.sub(rf'^{kind}', '', text).strip()
    return explicit_continued, text.strip()


def format_caption_text(kind: str, label: str, title: str, continued: bool) -> str:
    prefix = f'续{kind}{label}' if continued else f'{kind}{label}'
    return f'{prefix}    {title}'.strip()


def make_caption_label(chapter_label: str, seq: int) -> str:
    if chapter_label.isdigit():
        return f'{chapter_label}-{seq}'
    return f'{chapter_label}{seq}'


def is_blank_paragraph(elem: ET.Element) -> bool:
    return elem.tag == qn('w', 'p') and paragraph_text(elem) == ''


def next_nonblank_index(children: list[ET.Element], start: int) -> int | None:
    idx = start
    while idx < len(children):
        child = children[idx]
        if child.tag == qn('w', 'p') and is_blank_paragraph(child):
            idx += 1
            continue
        if child.tag not in {qn('w', 'p'), qn('w', 'tbl')}:
            idx += 1
            continue
        return idx
    return None


def prev_nonblank_index(children: list[ET.Element], start: int) -> int | None:
    idx = start
    while idx >= 0:
        child = children[idx]
        if child.tag == qn('w', 'p') and is_blank_paragraph(child):
            idx -= 1
            continue
        if child.tag not in {qn('w', 'p'), qn('w', 'tbl')}:
            idx -= 1
            continue
        return idx
    return None


def table_style(tbl: ET.Element) -> str | None:
    tbl_pr = tbl.find(qn('w', 'tblPr'))
    if tbl_pr is None:
        return None
    tbl_style = tbl_pr.find(qn('w', 'tblStyle'))
    if tbl_style is None:
        return None
    return tbl_style.get(qn('w', 'val'))


def ensure_tbl_pr(tbl: ET.Element) -> ET.Element:
    tbl_pr = tbl.find(qn('w', 'tblPr'))
    if tbl_pr is None:
        tbl_pr = ET.Element(qn('w', 'tblPr'))
        tbl.insert(0, tbl_pr)
    return tbl_pr


def ensure_tc_pr(tc: ET.Element) -> ET.Element:
    tc_pr = tc.find(qn('w', 'tcPr'))
    if tc_pr is None:
        tc_pr = ET.Element(qn('w', 'tcPr'))
        tc.insert(0, tc_pr)
    return tc_pr


def ensure_p_pr(p: ET.Element) -> ET.Element:
    ppr = p.find(qn('w', 'pPr'))
    if ppr is None:
        ppr = ET.Element(qn('w', 'pPr'))
        p.insert(0, ppr)
    return ppr


def set_paragraph_alignment(p: ET.Element, align: str = 'center') -> None:
    ppr = ensure_p_pr(p)
    jc = ppr.find(qn('w', 'jc'))
    if jc is None:
        jc = ET.SubElement(ppr, qn('w', 'jc'))
    jc.set(qn('w', 'val'), align)


def set_border(elem: ET.Element, edge: str, val: str, *, sz: str | None = None, space: str = '0', color: str = 'auto') -> None:
    border = elem.find(qn('w', edge))
    if border is None:
        border = ET.SubElement(elem, qn('w', edge))
    border.attrib.clear()
    border.set(qn('w', 'val'), val)
    if val != 'nil':
        if sz is not None:
            border.set(qn('w', 'sz'), sz)
        border.set(qn('w', 'space'), space)
        border.set(qn('w', 'color'), color)


def apply_three_line_table_style(tbl: ET.Element) -> None:
    tbl_pr = ensure_tbl_pr(tbl)

    jc = tbl_pr.find(qn('w', 'jc'))
    if jc is None:
        jc = ET.SubElement(tbl_pr, qn('w', 'jc'))
    jc.set(qn('w', 'val'), 'center')

    tbl_borders = tbl_pr.find(qn('w', 'tblBorders'))
    if tbl_borders is None:
        tbl_borders = ET.SubElement(tbl_pr, qn('w', 'tblBorders'))
    for child in list(tbl_borders):
        tbl_borders.remove(child)
    set_border(tbl_borders, 'top', 'single', sz='12')
    set_border(tbl_borders, 'left', 'nil')
    set_border(tbl_borders, 'bottom', 'single', sz='12')
    set_border(tbl_borders, 'right', 'nil')
    set_border(tbl_borders, 'insideH', 'nil')
    set_border(tbl_borders, 'insideV', 'nil')

    rows = tbl.findall(qn('w', 'tr'))
    for row_idx, tr in enumerate(rows):
        for tc in tr.findall(qn('w', 'tc')):
            tc_pr = ensure_tc_pr(tc)
            tc_borders = tc_pr.find(qn('w', 'tcBorders'))
            if tc_borders is not None:
                tc_pr.remove(tc_borders)
            if row_idx == 0:
                tc_borders = ET.SubElement(tc_pr, qn('w', 'tcBorders'))
                set_border(tc_borders, 'bottom', 'single', sz='8')
            for p in tc.findall(qn('w', 'p')):
                set_paragraph_alignment(p, 'center')



def is_unit_line(text: str) -> bool:
    return text.startswith(UNIT_PREFIXES)


def is_source_line(text: str) -> bool:
    return text.startswith(SOURCE_PREFIXES)


def table_plain_text(tbl: ET.Element) -> str:
    texts = []
    for t in tbl.findall('.//' + qn('w', 't')):
        if t.text:
            texts.append(t.text)
    return ''.join(texts).strip()


def is_stylable_body_table(children: list[ET.Element], idx: int, content_start_idx: int | None) -> bool:
    if content_start_idx is None or idx < content_start_idx:
        return False
    tbl = children[idx]
    if tbl.tag != qn('w', 'tbl'):
        return False
    if table_style(tbl) == 'FigureTable':
        return False
    if table_plain_text(tbl) == '':
        return False
    return True


def inline_to_anchor(inline: ET.Element) -> ET.Element:
    anchor = ET.Element(qn('wp', 'anchor'))
    anchor.set('distT', '0')
    anchor.set('distB', '0')
    anchor.set('distL', '0')
    anchor.set('distR', '0')
    anchor.set('simplePos', '0')
    anchor.set('relativeHeight', '251659264')
    anchor.set('behindDoc', '0')
    anchor.set('locked', '0')
    anchor.set('layoutInCell', '1')
    anchor.set('allowOverlap', '1')

    simple_pos = ET.SubElement(anchor, qn('wp', 'simplePos'))
    simple_pos.set('x', '0')
    simple_pos.set('y', '0')

    position_h = ET.SubElement(anchor, qn('wp', 'positionH'))
    position_h.set('relativeFrom', 'column')
    align_h = ET.SubElement(position_h, qn('wp', 'align'))
    align_h.text = 'center'

    position_v = ET.SubElement(anchor, qn('wp', 'positionV'))
    position_v.set('relativeFrom', 'paragraph')
    pos_offset = ET.SubElement(position_v, qn('wp', 'posOffset'))
    pos_offset.text = '0'

    extent = inline.find(qn('wp', 'extent'))
    if extent is not None:
        anchor.append(copy.deepcopy(extent))
    else:
        fallback_extent = ET.SubElement(anchor, qn('wp', 'extent'))
        fallback_extent.set('cx', '0')
        fallback_extent.set('cy', '0')

    effect_extent = inline.find(qn('wp', 'effectExtent'))
    if effect_extent is not None:
        anchor.append(copy.deepcopy(effect_extent))

    ET.SubElement(anchor, qn('wp', 'wrapTopAndBottom'))

    doc_pr = inline.find(qn('wp', 'docPr'))
    if doc_pr is not None:
        anchor.append(copy.deepcopy(doc_pr))
    else:
        fallback_docpr = ET.SubElement(anchor, qn('wp', 'docPr'))
        fallback_docpr.set('id', '1')
        fallback_docpr.set('name', 'Picture')

    c_nv = inline.find(qn('wp', 'cNvGraphicFramePr'))
    if c_nv is not None:
        anchor.append(copy.deepcopy(c_nv))
    else:
        c_nv = ET.SubElement(anchor, qn('wp', 'cNvGraphicFramePr'))
        locks = ET.SubElement(c_nv, qn('a', 'graphicFrameLocks'))
        locks.set('noChangeAspect', '1')

    graphic = inline.find(qn('a', 'graphic'))
    if graphic is not None:
        anchor.append(copy.deepcopy(graphic))

    return anchor


def convert_paragraph_drawings_to_anchor(p: ET.Element) -> bool:
    changed = False
    for drawing in p.findall('.//' + qn('w', 'drawing')):
        for child in list(drawing):
            if child.tag == qn('wp', 'inline'):
                drawing.remove(child)
                drawing.append(inline_to_anchor(child))
                changed = True
    if changed:
        set_paragraph_style(p, 'Normal')
        set_paragraph_alignment(p, 'center')
    return changed


def extract_figure_table_paragraphs(tbl: ET.Element) -> list[ET.Element]:
    paras: list[ET.Element] = []
    for p in tbl.findall('.//' + qn('w', 'p')):
        if not p.findall('.//' + qn('w', 'drawing')):
            continue
        new_p = copy.deepcopy(p)
        set_paragraph_style(new_p, 'Normal')
        set_paragraph_alignment(new_p, 'center')
        convert_paragraph_drawings_to_anchor(new_p)
        paras.append(new_p)
    return paras


def unwrap_figure_tables(body: ET.Element) -> None:
    idx = 0
    while idx < len(body):
        child = body[idx]
        if child.tag == qn('w', 'tbl') and table_style(child) == 'FigureTable':
            paras = extract_figure_table_paragraphs(child)
            body.remove(child)
            insert_at = idx
            for p in paras:
                body.insert(insert_at, p)
                insert_at += 1
            idx = insert_at
            continue
        idx += 1


def strip_leading_tab_runs(p: ET.Element) -> None:
    removable = []
    started = False
    for child in list(p):
        if child.tag == qn('w', 'pPr'):
            continue
        if child.tag != qn('w', 'r'):
            break
        has_tab = child.find(qn('w', 'tab')) is not None
        texts = [t.text or '' for t in child.findall('.//' + qn('w', 't'))]
        if not started and has_tab and not ''.join(texts).strip():
            removable.append(child)
            continue
        started = True
        break
    for child in removable:
        p.remove(child)


def tighten_list_indentation(numbering_root: ET.Element) -> None:
    for lvl in numbering_root.findall('.//' + qn('w', 'lvl')):
        ppr = lvl.find(qn('w', 'pPr'))
        if ppr is None:
            ppr = ET.SubElement(lvl, qn('w', 'pPr'))
        ind = ppr.find(qn('w', 'ind'))
        if ind is None:
            ind = ET.SubElement(ppr, qn('w', 'ind'))
        ilvl = int(lvl.get(qn('w', 'ilvl'), '0'))
        ind.set(qn('w', 'left'), str(420 + ilvl * 420))
        ind.set(qn('w', 'hanging'), '180')
        suff = lvl.find(qn('w', 'suff'))
        if suff is None:
            suff = ET.SubElement(lvl, qn('w', 'suff'))
        suff.set(qn('w', 'val'), 'space')


def normalize_body(body: ET.Element) -> None:
    unwrap_figure_tables(body)

    current_chapter_label: str | None = None
    equation_no = 0
    table_no = 0
    figure_no = 0
    table_labels: dict[str, str] = {}
    figure_labels: dict[str, str] = {}
    first_abstract_idx = None
    pending_eq_label: str | None = None
    pending_generic_labels: list[str] = []
    bookmark_map: dict[str, str] = {}
    eq_display_map: dict[str, str] = {}
    next_bookmark_id = 1

    paragraphs = list(body.findall(qn('w', 'p')))
    for idx, p in enumerate(paragraphs):
        style = paragraph_style(p)
        text = paragraph_text(p)
        marker_label = extract_eq_label_marker(text)
        if marker_label:
            pending_eq_label = marker_label
            if p in list(body):
                body.remove(p)
            continue

        generic_labels = extract_generic_label_markers(text)
        marker_only_text = LABEL_RE.sub('', text).strip()
        if generic_labels and marker_only_text == '':
            prev_idx = prev_nonblank_index(paragraphs, idx - 1)
            attached = False
            if prev_idx is not None:
                prev_p = paragraphs[prev_idx]
                prev_style = paragraph_style(prev_p)
                if prev_p in list(body) and prev_style in {'Heading1', 'Heading2', 'Heading3', 'TableCaption', 'ImageCaption'}:
                    next_bookmark_id = attach_bookmarks_to_paragraph(prev_p, generic_labels, bookmark_map, next_bookmark_id)
                    attached = True
            if not attached:
                pending_generic_labels.extend(generic_labels)
            if p in list(body):
                body.remove(p)
            continue
        elif generic_labels:
            strip_label_markers_in_paragraph(p)
            text = paragraph_text(p)
            next_bookmark_id = attach_bookmarks_to_paragraph(p, generic_labels, bookmark_map, next_bookmark_id)

        text = normalize_heading_separator(p, style, text)
        if first_abstract_idx is None and style == 'Heading1' and text in {'摘 要', '摘要', 'Abstract'}:
            first_abstract_idx = idx
        if style == 'Heading1':
            if text in {'摘 要', '摘要'}:
                set_paragraph_style(p, 'AbstractTitleCN')
            elif text == 'Abstract':
                set_paragraph_style(p, 'AbstractTitleEN')
            elif text in {'后 记', '后记', '参考文献'} or is_research_outputs_heading(text) or is_appendix_heading1(text) or is_body_heading1(text):
                pass
            else:
                set_paragraph_style(p, 'Normal')
        elif style == 'Heading2':
            if not is_body_heading2(text):
                set_paragraph_style(p, 'Normal')
        elif style == 'Heading3':
            if not is_body_heading3(text):
                set_paragraph_style(p, 'Normal')

        if pending_generic_labels and p in list(body):
            next_bookmark_id = attach_bookmarks_to_paragraph(p, pending_generic_labels, bookmark_map, next_bookmark_id)
            pending_generic_labels = []

        if paragraph_has_math(p):
            if paragraph_has_math_para(p):
                set_paragraph_style(p, 'EquationBlock')
            else:
                set_paragraph_style(p, 'Normal')

        chapter_label = chapter_label_from_heading(text)
        if chapter_label:
            current_chapter_label = chapter_label
            equation_no = 0
            table_no = 0
            figure_no = 0
            table_labels = {}
            figure_labels = {}
        elif current_chapter_label and paragraph_has_math(p):
            equation_no += 1
            if current_chapter_label.isdigit():
                eq_label = f'（{current_chapter_label}.{equation_no}）'
            else:
                eq_label = f'（{current_chapter_label}{equation_no}）'
            bookmark_name = None
            bookmark_id = None
            if pending_eq_label:
                bookmark_name = sanitize_bookmark_name(pending_eq_label)
                bookmark_map[pending_eq_label] = bookmark_name
                eq_display_map[pending_eq_label] = f'式{eq_label}'
                bookmark_id = next_bookmark_id
                next_bookmark_id += 1
                pending_eq_label = None
            append_equation_number(p, eq_label, bookmark_name, bookmark_id)
        elif current_chapter_label and re.match(r'^（[A-Z]\.\d+）$', text):
            set_paragraph_text(p, re.sub(r'^（([A-Z])\.(\d+)）$', r'（）', text))
        elif current_chapter_label and style == 'TableCaption' and text:
            explicit_continued, title = clean_caption_title(text, '表')
            if explicit_continued or title in table_labels:
                label = table_labels.get(title, make_caption_label(current_chapter_label, max(table_no, 1)))
                set_paragraph_text(p, format_caption_text('表', label, title, True))
            else:
                table_no += 1
                label = make_caption_label(current_chapter_label, table_no)
                table_labels[title] = label
                set_paragraph_text(p, format_caption_text('表', label, title, False))
        elif current_chapter_label and style == 'ImageCaption' and text:
            explicit_continued, title = clean_caption_title(text, '图')
            if explicit_continued or title in figure_labels:
                label = figure_labels.get(title, make_caption_label(current_chapter_label, max(figure_no, 1)))
                set_paragraph_text(p, format_caption_text('图', label, title, True))
            else:
                figure_no += 1
                label = make_caption_label(current_chapter_label, figure_no)
                figure_labels[title] = label
                set_paragraph_text(p, format_caption_text('图', label, title, False))

    children = list(body)
    content_start_idx = 0
    for probe_idx, probe_child in enumerate(children):
        if probe_child.tag != qn('w', 'p'):
            continue
        probe_style = paragraph_style(probe_child)
        if probe_style in {'AbstractTitleCN', 'AbstractTitleEN'}:
            content_start_idx = probe_idx
            break
    for idx, child in enumerate(children):
        if child.tag == qn('w', 'tbl') and is_stylable_body_table(children, idx, content_start_idx):
            apply_three_line_table_style(child)
        if child.tag != qn('w', 'p'):
            continue
        replace_xref_placeholders_in_paragraph(child, bookmark_map, eq_display_map)
        style = paragraph_style(child)
        if style == 'TableCaption':
            prev_idx = prev_nonblank_index(children, idx - 1)
            if prev_idx is not None and children[prev_idx].tag == qn('w', 'p') and is_unit_line(paragraph_text(children[prev_idx])):
                unit_para = children[prev_idx]
                set_paragraph_style(unit_para, 'TableMetaLine')
                body.remove(unit_para)
                insert_pos = list(body).index(child) + 1
                body.insert(insert_pos, unit_para)
                children = list(body)
                idx = children.index(child)

            next_idx = next_nonblank_index(children, idx + 1)
            if next_idx is not None and children[next_idx].tag == qn('w', 'p'):
                text = paragraph_text(children[next_idx])
                if is_unit_line(text):
                    set_paragraph_style(children[next_idx], 'TableMetaLine')
                    next_idx = next_nonblank_index(children, next_idx + 1)
            if next_idx is not None and children[next_idx].tag == qn('w', 'tbl'):
                apply_three_line_table_style(children[next_idx])
                source_idx = next_nonblank_index(children, next_idx + 1)
                if source_idx is not None and children[source_idx].tag == qn('w', 'p') and is_source_line(paragraph_text(children[source_idx])):
                    set_paragraph_style(children[source_idx], 'SourceNote')
        elif style == 'ImageCaption':
            next_idx = next_nonblank_index(children, idx + 1)
            if next_idx is not None and children[next_idx].tag == qn('w', 'p') and is_source_line(paragraph_text(children[next_idx])):
                set_paragraph_style(children[next_idx], 'SourceNote')


def process_docx(input_path: Path, output_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        unzip_dir = td_path / 'unzipped'
        unzip_dir.mkdir()
        with zipfile.ZipFile(input_path) as zf:
            zf.extractall(unzip_dir)

        document_path = unzip_dir / 'word' / 'document.xml'
        settings_path = unzip_dir / 'word' / 'settings.xml'
        rels_path = unzip_dir / 'word' / '_rels' / 'document.xml.rels'
        ct_path = unzip_dir / '[Content_Types].xml'

        document_tree = ET.parse(document_path)
        document_root = document_tree.getroot()
        settings_tree = ET.parse(settings_path)
        settings_root = settings_tree.getroot()
        rels_tree = ET.parse(rels_path)
        rels_root = rels_tree.getroot()
        ct_tree = ET.parse(ct_path)
        ct_root = ct_tree.getroot()

        # create footer part and relationships
        footer_rid = ensure_relationship(rels_root, 'footer1.xml', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer')
        ensure_content_type(ct_root, '/word/footer1.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml')
        (unzip_dir / 'word' / 'footer1.xml').write_bytes(footer_xml())

        update_settings(settings_root)

        body = document_root.find(qn('w', 'body'))
        if body is None:
            raise RuntimeError('document body not found')

        # remove pandoc auto title-block residue at document top
        removable = {'Author', 'Date'}
        while len(body) > 0:
            first = body[0]
            if first.tag != qn('w', 'p'):
                break
            style = paragraph_style(first)
            if style in removable:
                body.remove(first)
                continue
            break

        # replace TOC placeholder paragraphs
        children = list(body)
        for idx, child in enumerate(children):
            if child.tag == qn('w', 'p') and paragraph_text(child) == TOC_PLACEHOLDER:
                body.remove(child)
                body.insert(idx, make_toc_paragraph())
                break

        normalize_body(body)

        # locate abstract start and fixed-header sections
        body_children = list(body)
        abstract_body_idx = None
        for idx, child in enumerate(body_children):
            if child.tag != qn('w', 'p'):
                continue
            style = paragraph_style(child)
            if style == 'AbstractTitleCN':
                abstract_body_idx = idx
                break
            if style == 'AbstractTitleEN' and abstract_body_idx is None:
                abstract_body_idx = idx
                break

        # cover/title section ends at first abstract page
        if abstract_body_idx is not None:
            title_break_p = ET.Element(qn('w', 'p'))
            ppr = ET.SubElement(title_break_p, qn('w', 'pPr'))
            sectpr = ET.SubElement(ppr, qn('w', 'sectPr'))
            configure_sectpr(sectpr, page_fmt='decimal', page_start=1, header_rid=None, footer_rid=None)
            body.insert(abstract_body_idx, title_break_p)

        section_starts = collect_fixed_header_sections(body)
        header_rids = [
            write_header_part(unzip_dir, rels_root, ct_root, index=i + 1, title=title)
            for i, (_, title) in enumerate(section_starts)
        ]

        # final section properties = last fixed-header section (or headerless fallback)
        final_sectpr = body.find(qn('w', 'sectPr'))
        if final_sectpr is None:
            final_sectpr = ET.SubElement(body, qn('w', 'sectPr'))
        if section_starts:
            final_page_start = 1 if len(section_starts) == 1 else None
            configure_sectpr(final_sectpr, page_fmt='decimal', page_start=final_page_start, header_rid=header_rids[-1], footer_rid=footer_rid)
        else:
            configure_sectpr(final_sectpr, page_fmt='decimal', page_start=1, header_rid=None, footer_rid=footer_rid)

        # end each completed body section right before the next heading and bind it to a fixed header
        for section_idx in range(len(section_starts) - 1, 0, -1):
            insert_at, _ = section_starts[section_idx]
            prev_header_rid = header_rids[section_idx - 1]
            page_start = 1 if section_idx - 1 == 0 else None
            body_break_p = ET.Element(qn('w', 'p'))
            ppr = ET.SubElement(body_break_p, qn('w', 'pPr'))
            sectpr = ET.SubElement(ppr, qn('w', 'sectPr'))
            configure_sectpr(sectpr, page_fmt='decimal', page_start=page_start, header_rid=prev_header_rid, footer_rid=footer_rid)
            body.insert(insert_at, body_break_p)

        # abstract/toc section ends right before first fixed-header body section
        if section_starts:
            first_heading_idx = section_starts[0][0]
            front_break_p = ET.Element(qn('w', 'p'))
            ppr = ET.SubElement(front_break_p, qn('w', 'pPr'))
            sectpr = ET.SubElement(ppr, qn('w', 'sectPr'))
            configure_sectpr(sectpr, page_fmt='upperRoman', page_start=1, header_rid=None, footer_rid=footer_rid)
            body.insert(first_heading_idx, front_break_p)

        document_tree.write(document_path, encoding='utf-8', xml_declaration=True)
        settings_tree.write(settings_path, encoding='utf-8', xml_declaration=True)
        rels_tree.write(rels_path, encoding='utf-8', xml_declaration=True)
        ct_tree.write(ct_path, encoding='utf-8', xml_declaration=True)

        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(unzip_dir.rglob('*')):
                if path.is_file():
                    zf.write(path, path.relative_to(unzip_dir))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Post-process pandoc-generated DOCX for TJFE thesis spec.')
    p.add_argument('input', type=Path, help='Input .docx file')
    p.add_argument('output', type=Path, help='Output .docx file')
    p.add_argument('--embed-fonts', action='store_true',
                   help='Embed fonts into the DOCX for cross-software (Word/WPS) consistency.')
    p.add_argument('--font-dir', type=Path, action='append', dest='font_dirs',
                   help='Additional font directory to scan (repeatable).')
    p.add_argument('--no-obfuscate-fonts', action='store_true',
                   help='Skip font obfuscation (use only if font license permits).')
    p.add_argument('--font-map', action='append', dest='font_maps', metavar='DOC_NAME=FONT_NAME',
                   help='Map a font name in the document to a different font file, e.g. "SimHei=Heiti TC".')
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    input_path = args.input.resolve()
    output_path = args.output.resolve()

    # If embedding fonts, use a temp file for the intermediate result
    if args.embed_fonts:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tf:
            intermediate = Path(tf.name)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            process_docx(input_path, intermediate)
            _embed_fonts_step(intermediate, output_path, args)
        finally:
            intermediate.unlink(missing_ok=True)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        process_docx(input_path, output_path)

    print(str(output_path))
    return 0


def _embed_fonts_step(input_path: Path, output_path: Path, args: argparse.Namespace) -> None:
    """Run font embedding and print a summary."""
    # Add scripts dir to path so we can import the sibling module
    import importlib.util
    embed_path = Path(__file__).with_name('embed_fonts.py')
    spec = importlib.util.spec_from_file_location('embed_fonts', embed_path)
    if spec is None or spec.loader is None:
        print('[embed-fonts] ERROR: cannot load embed_fonts.py', file=sys.stderr)
        return
    embed_fonts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(embed_fonts)

    font_dirs = list(args.font_dirs) if args.font_dirs else None

    extra_mappings: dict[str, str] = {}
    if args.font_maps:
        for m in args.font_maps:
            if '=' in m:
                k, v = m.split('=', 1)
                extra_mappings[k.strip()] = v.strip()

    result = embed_fonts.embed_fonts_in_docx(
        input_path,
        output_path,
        font_dirs=font_dirs,
        extra_mappings=extra_mappings or None,
        obfuscate=not args.no_obfuscate_fonts,
    )

    print(f'[embed-fonts] embedded: {len(result.embedded)} font(s)', file=sys.stderr)
    if result.embedded:
        for name in result.embedded:
            print(f'[embed-fonts]   ✓ {name}', file=sys.stderr)
    if result.skipped_not_embeddable:
        print(f'[embed-fonts]   ✗ license-restricted: {", ".join(result.skipped_not_embeddable)}', file=sys.stderr)
    if result.skipped_not_found:
        print(f'[embed-fonts]   ? not found: {", ".join(result.skipped_not_found)}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
