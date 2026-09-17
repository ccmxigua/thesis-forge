#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

try:
    import yaml
except ImportError:
    yaml = None

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

# ── format defaults (TJFE) ──
# These may be overridden by --config overlay YAML.
_FORMAT_DEFAULTS = {
    'page_w': '10431',
    'page_h': '14740',
    'top': '1134',       # 2.0 cm
    'bottom': '850',     # 1.5 cm
    'left': '1134',      # 2.0 cm
    'right': '1134',     # 2.0 cm
    'header': '850',
    'footer': '992',
    'doc_grid_line_pitch': '326',
}

PAGE_W = _FORMAT_DEFAULTS['page_w']
PAGE_H = _FORMAT_DEFAULTS['page_h']
TOP = _FORMAT_DEFAULTS['top']
BOTTOM = _FORMAT_DEFAULTS['bottom']
LEFT = _FORMAT_DEFAULTS['left']
RIGHT = _FORMAT_DEFAULTS['right']
HEADER = _FORMAT_DEFAULTS['header']
FOOTER = _FORMAT_DEFAULTS['footer']
LINE_PITCH = _FORMAT_DEFAULTS['doc_grid_line_pitch']

# ── heading font overrides (from overlay) ──
_FONT_SIZES: dict[str, int | None] = {
    'body': None,
    'heading1': None,
    'heading2': None,
    'heading3': None,
}
_TYPOGRAPHY: dict[str, str | None] = {
    'body_font_cn': None,
    'heading_font_cn': None,
    'body_font_en': None,
    'heading_font_en': None,
}
_EQUATION_LABEL_FORMAT = '（{chapter}.{seq}）'
EQUATION_NUMBER_STYLE_ID = 'TJUFEEquationNumber'


def _format_equation_number(chapter: str, sequence: int | str) -> str:
    """Format an appendix/body equation number from the resolved config."""
    try:
        return _EQUATION_LABEL_FORMAT.format(chapter=chapter, seq=sequence)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f'invalid equations.label_format: {_EQUATION_LABEL_FORMAT!r}') from exc


def _patch_styles(unzip_dir: Path) -> None:
    """Patch styles.xml heading font sizes/fonts from overlay YAML overrides."""
    heading_map = {'heading1': 'Heading1', 'heading2': 'Heading2', 'heading3': 'Heading3'}
    has_overrides = any(v is not None for v in _FONT_SIZES.values()) or \
                    any(v is not None for v in _TYPOGRAPHY.values())
    styles_path = unzip_dir / 'word' / 'styles.xml'
    if not styles_path.exists():
        return

    styles_tree = ET.parse(styles_path)
    styles_root = styles_tree.getroot()

    body_ids = {'Normal', 'BodyText', 'FirstParagraph', 'AbstractBodyCN', 'AbstractBodyEN', 'AcknowledgementsBody', 'StatementBody'}
    for style_elem in styles_root.findall(qn('w', 'style')):
        sid = style_elem.get(qn('w', 'styleId'))
        style_name = style_elem.find(qn('w', 'name'))
        style_label = style_name.get(qn('w', 'val'), '') if style_name is not None else ''
        if sid == 'TOCHeading' or style_label.lower() == 'toc heading':
            ppr = style_elem.find(qn('w', 'pPr'))
            if ppr is not None:
                outline = ppr.find(qn('w', 'outlineLvl'))
                if outline is not None:
                    ppr.remove(outline)
        if sid not in heading_map.values() and sid not in body_ids:
            continue
        hkey = {v: k for k, v in heading_map.items()}.get(sid)
        if hkey is None:
            continue

        rpr = style_elem.find(qn('w', 'rPr'))
        if rpr is None:
            rpr = ET.SubElement(style_elem, qn('w', 'rPr'))

        # font size override (pt -> half-pt)
        size_key = hkey if hkey is not None else 'body'
        if _FONT_SIZES.get(size_key) is not None:
            sz_val = str(_FONT_SIZES[size_key] * 2)
            sz = rpr.find(qn('w', 'sz'))
            if sz is None:
                sz = ET.SubElement(rpr, qn('w', 'sz'))
            sz.set(qn('w', 'val'), sz_val)
            szCs = rpr.find(qn('w', 'szCs'))
            if szCs is None:
                szCs = ET.SubElement(rpr, qn('w', 'szCs'))
            szCs.set(qn('w', 'val'), sz_val)

        # font face override
        fonts = rpr.find(qn('w', 'rFonts'))
        if fonts is None:
            fonts = ET.SubElement(rpr, qn('w', 'rFonts'))
        cn_font = _TYPOGRAPHY.get('heading_font_cn' if hkey is not None else 'body_font_cn')
        en_font = _TYPOGRAPHY.get('heading_font_en' if hkey is not None else 'body_font_en')
        if cn_font:
            fonts.set(qn('w', 'eastAsia'), cn_font)
        if en_font:
            fonts.set(qn('w', 'ascii'), en_font)
            fonts.set(qn('w', 'hAnsi'), en_font)
            fonts.set(qn('w', 'cs'), en_font)

    styles_tree.write(styles_path, xml_declaration=True, encoding='UTF-8')
    if has_overrides:
        overridden = []
        for k, v in _FONT_SIZES.items():
            if v is not None:
                overridden.append(f'{k}={v}pt')
        if overridden:
                print(f'[postprocess] style overrides: {", ".join(overridden)}', file=sys.stderr)


def ensure_equation_number_style(unzip_dir: Path) -> None:
    """Register the private run style used to make equation numbering idempotent."""
    styles_path = unzip_dir / 'word' / 'styles.xml'
    if not styles_path.exists():
        return
    tree = ET.parse(styles_path)
    root = tree.getroot()
    if any(style.get(qn('w', 'styleId')) == EQUATION_NUMBER_STYLE_ID
           for style in root.findall(qn('w', 'style'))):
        return
    style = ET.SubElement(root, qn('w', 'style'))
    style.set(qn('w', 'type'), 'character')
    style.set(qn('w', 'styleId'), EQUATION_NUMBER_STYLE_ID)
    style.set(qn('w', 'customStyle'), '1')
    name = ET.SubElement(style, qn('w', 'name'))
    name.set(qn('w', 'val'), 'TJUFE Equation Number')
    based_on = ET.SubElement(style, qn('w', 'basedOn'))
    based_on.set(qn('w', 'val'), 'DefaultParagraphFont')
    ET.SubElement(style, qn('w', 'qFormat'))
    tree.write(styles_path, encoding='utf-8', xml_declaration=True)


def _apply_format_overrides(config_path: str) -> None:
    """Parse a config-schema / overlay YAML and override global format variables.

    Supports both nested ``page.margins.top`` and flat keys.
    If a key is missing the TJFE default is kept.
    """
    if yaml is None:
        raise RuntimeError('PyYAML is required to load --config')
    try:
        with open(config_path, 'r', encoding='utf-8') as fh:
            raw = yaml.safe_load(fh)
    except Exception as exc:
        raise RuntimeError(f'failed to load config {config_path}: {exc}') from exc
    if not isinstance(raw, dict):
        raise ValueError(f'config {config_path} must contain a YAML mapping')

    # A process can invoke ``main`` more than once in tests or in an embedding
    # host.  Do not leak the previous document's configuration into the next
    # invocation.
    for name, value in _FORMAT_DEFAULTS.items():
        globals()[{
            'page_w': 'PAGE_W', 'page_h': 'PAGE_H', 'top': 'TOP',
            'bottom': 'BOTTOM', 'left': 'LEFT', 'right': 'RIGHT',
            'header': 'HEADER', 'footer': 'FOOTER',
            'doc_grid_line_pitch': 'LINE_PITCH',
        }[name]] = str(value)
    for key in _FONT_SIZES:
        _FONT_SIZES[key] = None
    for key in _TYPOGRAPHY:
        _TYPOGRAPHY[key] = None
    global _EQUATION_LABEL_FORMAT
    _EQUATION_LABEL_FORMAT = '（{chapter}.{seq}）'

    page = raw.get('page', {})
    margins = page.get('margins', {}) if isinstance(page, dict) else {}

    # Build a flat lookup that respects nesting
    def _lookup(*keys: str) -> str | None:
        for k in keys:
            # 1) page.margins.<k>
            if isinstance(margins, dict) and k in margins:
                return str(margins[k])
            # 2) page.<k>
            if isinstance(page, dict) and k in page:
                return str(page[k])
            # 3) root level
            if k in raw:
                return str(raw[k])
        return None

    page_size = page.get('size') if isinstance(page, dict) else None
    if page_size in {'A4', 'a4'}:
        globals()['PAGE_W'], globals()['PAGE_H'] = '11906', '16838'
    elif page_size in {'16K', '16k', 'B5'}:
        globals()['PAGE_W'], globals()['PAGE_H'] = '10431', '14740'
    elif isinstance(page_size, dict):
        if page_size.get('width') is not None:
            globals()['PAGE_W'] = str(page_size['width'])
        if page_size.get('height') is not None:
            globals()['PAGE_H'] = str(page_size['height'])

    pairs = [
        ('PAGE_W', 'width', 'page_width'),
        ('PAGE_H', 'height', 'page_height'),
        ('TOP', 'top'),
        ('BOTTOM', 'bottom'),
        ('LEFT', 'left'),
        ('RIGHT', 'right'),
        ('HEADER', 'header', 'header_distance'),
        ('FOOTER', 'footer', 'footer_distance'),
        ('LINE_PITCH', 'doc_grid_line_pitch'),
    ]
    overridden = []
    for var_name, *keys in pairs:
        val = _lookup(*keys)
        if val is not None:
            globals()[var_name] = val
            overridden.append(f'{var_name}={val}')
    # font sizes override
    font_sizes = raw.get('font_sizes', {})
    if isinstance(font_sizes, dict):
        for k in ('body', 'heading1', 'heading2', 'heading3'):
            if k in font_sizes and font_sizes[k] is not None:
                _FONT_SIZES[k] = int(font_sizes[k])
                overridden.append(f'font_{k}={font_sizes[k]}pt')

    # typography override
    typo = raw.get('typography', {})
    if isinstance(typo, dict):
        for k in ('body_font_cn', 'heading_font_cn', 'body_font_en', 'heading_font_en'):
            if k in typo and typo[k] is not None:
                _TYPOGRAPHY[k] = str(typo[k])
                overridden.append(k)

    equations = raw.get('equations', {})
    if isinstance(equations, dict):
        label_format = equations.get('label_format')
        if isinstance(label_format, str):
            if '{chapter}' in label_format and '{seq}' in label_format:
                _EQUATION_LABEL_FORMAT = label_format
                overridden.append(f'equation_label_format={_EQUATION_LABEL_FORMAT}')

    if page_size is not None:
        overridden.extend([f'PAGE_W={PAGE_W}', f'PAGE_H={PAGE_H}'])
    if overridden:
        print(f'[postprocess] format overrides from {config_path}: {", ".join(overridden)}', file=sys.stderr)
TOC_PLACEHOLDER = '__TJUFE_TOC_PLACEHOLDER__'
FOOTNOTE_NUMFMT = 'decimalEnclosedCircleChinese'
UNIT_PREFIXES = ('单位：', '单位:', '计量单位：', '计量单位:', '数据单位：', '数据单位:')
SOURCE_PREFIXES = ('资料来源：', '资料来源:', '数据来源：', '数据来源:', '来源：', '来源:')
# Placeholder display text is escaped by replacing ``]`` with ``)`` but may
# still contain ``[`` (for example ``Reference [fig:x)``).  Match through the
# explicit triple-close sentinel rather than stopping at the first bracket.
# The displayed fallback can itself contain a literal ``]`` (for example
# ``Reference [fig:foo)`` after sanitising a missing label).  Stop only at the
# sentinel's complete closing delimiter, not at the first closing bracket.
XREF_RE = re.compile(r'\[\[\[TJUFE_XREF:([^|\]]+)\|(.*?)\]\]\]')
EQLABEL_RE = re.compile(r'^\[\[\[TJUFE_EQLABEL:([^\]]+)\]\]\]$')
EQCONTROL_RE = re.compile(r'^\[\[\[TJUFE_EQCONTROL:(numbered|unnumbered|tag):([^\]]*)\]\]\]$')
LABEL_RE = re.compile(r'TJUFE_LABEL__([^_]+(?:_[^_]+)*)__')


def sanitize_bookmark_name(label: str) -> str:
    name = re.sub(r'[^0-9A-Za-z_]', '_', label)
    if not name:
        name = 'eqref'
    if name[0].isdigit():
        name = f'bm_{name}'
    candidate = f'TJUFE_{name}'
    if len(candidate) <= 40:
        return candidate
    digest = hashlib.sha1(label.encode('utf-8')).hexdigest()[:8]
    return f'{candidate[:31]}_{digest}'


def unique_bookmark_name(label: str, used_names: set[str]) -> str:
    """Return a deterministic, package-unique Word bookmark name.

    Duplicate source labels are rejected by the preprocessor.  This extra
    serialization guard prevents collisions with imported/template bookmarks
    and keeps OOXML valid even for callers that invoke postprocessing directly.
    """
    base = sanitize_bookmark_name(label)
    candidate = base
    suffix = 2
    while candidate in used_names:
        suffix_text = f'_{suffix}'
        candidate = f'{base[:40 - len(suffix_text)]}{suffix_text}'
        suffix += 1
    used_names.add(candidate)
    return candidate


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


def ensure_page_break_before(p: ET.Element) -> bool:
    """Serialize ``w:pageBreakBefore`` on *p* and report whether it changed.

    Chapter pagination is a document-structure requirement, not a rendering
    hint that may be left to a particular Word/WPS style definition.  Writing
    the property on each applicable paragraph also makes the requirement
    survive official-template assembly, where source and template styles can
    otherwise differ.
    """
    ppr = p.find(qn('w', 'pPr'))
    if ppr is None:
        ppr = ET.Element(qn('w', 'pPr'))
        p.insert(0, ppr)
    page_break = ppr.find(qn('w', 'pageBreakBefore'))
    if page_break is not None:
        # ``w:val=0`` explicitly disables the property.  Normalize that shape
        # to the unambiguous enabled form used by both Microsoft Word and WPS.
        changed = page_break.get(qn('w', 'val')) in {'0', 'false', 'off'}
        page_break.attrib.pop(qn('w', 'val'), None)
        return changed
    ET.SubElement(ppr, qn('w', 'pageBreakBefore'))
    return True


def has_run_style(p: ET.Element, style_id: str) -> bool:
    for rstyle in p.findall('.//' + qn('w', 'rStyle')):
        if rstyle.get(qn('w', 'val')) == style_id:
            return True
    return False


def paragraph_has_math(p: ET.Element) -> bool:
    return p.find('.//' + qn('m', 'oMathPara')) is not None or p.find('.//' + qn('m', 'oMath')) is not None


def paragraph_has_math_para(p: ET.Element) -> bool:
    return p.find('.//' + qn('m', 'oMathPara')) is not None


def _equation_tab_positions() -> tuple[int, int]:
    """Compute center/right tabs inside the current section's text width."""
    try:
        page_width = int(float(PAGE_W))
        left = int(float(LEFT))
        right_margin = int(float(RIGHT))
    except (TypeError, ValueError) as exc:
        raise ValueError('page width and margins must be integer twips before equation layout') from exc
    usable_width = page_width - left - right_margin
    if usable_width <= 0:
        raise ValueError(
            f'page width {page_width} is not larger than left/right margins {left}+{right_margin}'
        )
    # ``w:tab/@w:pos`` is measured from the left text margin, not from the
    # physical page edge.  Keeping the right stop at the usable text width
    # avoids the old 9000-twip stop protruding into the right margin on A4.
    return usable_width // 2, usable_width


def _has_generated_equation_number(p: ET.Element, chapter_label: str | None = None) -> bool:
    """Recognize numbers emitted by this postprocessor, including old output."""
    if p.find(f'.//{qn("w", "rStyle") }[@{qn("w", "val") }="{EQUATION_NUMBER_STYLE_ID}"]') is not None:
        return True
    # Older output did not carry the private character style.  The narrow
    # trailing-number check prevents a second pass from appending a duplicate
    # default number while avoiding title/caption text elsewhere in the run.
    suffix = r'[A-Za-z0-9]+[.\-－]\d+'
    if chapter_label:
        suffix = rf'{re.escape(chapter_label)}[.\-－]\d+'
    pattern = re.compile(rf'^(?:（{suffix}）|\({suffix}\))\s*$')
    for child in reversed(list(p)):
        if child.tag != qn('w', 'r'):
            continue
        value = ''.join(node.text or '' for node in child.findall(qn('w', 't'))).strip()
        if value:
            return bool(pattern.fullmatch(value))
    return False


def paragraph_has_numpr(p: ET.Element) -> bool:
    ppr = p.find(qn('w', 'pPr'))
    if ppr is None:
        return False
    return ppr.find(qn('w', 'numPr')) is not None


def is_page_break_only_paragraph(p: ET.Element) -> bool:
    """Return whether *p* contains only an explicit page break.

    This intentionally does not treat ``pageBreakBefore`` or a paragraph
    containing ordinary text as an explicit break.  The narrow predicate is
    important here: a section boundary may absorb only the redundant break
    paragraph immediately before it, never a normal body pagination.
    """
    if p.tag != qn('w', 'p') or paragraph_text(p):
        return False
    breaks = p.findall('.//' + qn('w', 'br'))
    if not breaks or any(br.get(qn('w', 'type')) != 'page' for br in breaks):
        return False
    ppr = p.find(qn('w', 'pPr'))
    for child in p:
        if child is ppr:
            continue
        if child.tag != qn('w', 'r'):
            return False
        for run_child in child:
            if run_child.tag == qn('w', 'rPr'):
                continue
            if run_child.tag != qn('w', 'br') or run_child.get(qn('w', 'type')) != 'page':
                return False
    return True


def is_section_property_paragraph(p: ET.Element) -> bool:
    """Return whether a paragraph carries a section property."""
    return p.tag == qn('w', 'p') and p.find('.//' + qn('w', 'sectPr')) is not None


def remove_redundant_page_break_before(body: ET.Element, insert_at: int) -> bool:
    """Remove only a page-break-only paragraph immediately before a section.

    The caller inserts a next-page ``sectPr`` at ``insert_at``.  A preceding
    explicit page break is redundant in that exact adjacency and can make
    Word render a header-only intermediate page.  Returning a boolean keeps
    this operation auditable and makes the no-op behavior explicit.
    """
    if insert_at <= 0:
        return False
    previous = body[insert_at - 1]
    if is_page_break_only_paragraph(previous):
        body.remove(previous)
        return True

    # Pandoc may already have serialized the section paragraph immediately
    # before the heading.  In that shape the redundant page-break-only
    # paragraph is immediately before that existing sectPr, one slot before
    # the requested insertion point.  Remove only that exact pair; the
    # existing section paragraph is left intact so its semantics are retained.
    if (is_section_property_paragraph(previous) and insert_at >= 2 and
            is_page_break_only_paragraph(body[insert_at - 2])):
        body.remove(body[insert_at - 2])
        return True
    return False


def insert_section_break_before(body: ET.Element, insert_at: int, section_paragraph: ET.Element) -> bool:
    """Insert a section paragraph, absorbing only an adjacent page-only break."""
    removed = remove_redundant_page_break_before(body, insert_at)
    body.insert(insert_at - int(removed), section_paragraph)
    return removed


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


def replace_caption_text_preserving_math(p: ET.Element, prefix: str, title: str) -> None:
    """Replace a caption label/title without erasing inline OMML formulas.

    ``set_paragraph_text`` writes the complete visible caption into its first
    ``w:t`` and blanks every later text node.  That is appropriate for plain
    captions, but a caption such as ``不同 $H$ 值`` stores ``H`` in ``m:oMath``;
    flattening only the text nodes puts that formula after the sentence and
    leaves an apparent blank inside it.  Keep the original inline content and
    change only the first text node from the source title to the numbered
    prefix.
    """
    if paragraph_has_math(p):
        texts = p.findall('.//' + qn('w', 't'))
        first_nonblank = next((node for node in texts if node.text and node.text.strip()), None)
        if first_nonblank is not None:
            original = first_nonblank.text or ''
            leading = original[:len(original) - len(original.lstrip())]
            first_nonblank.text = f'{leading}{prefix}    {original.lstrip()}'
            return
    set_paragraph_text(p, f'{prefix}    {title}'.strip())


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


def resolve_final_xref_text(label: str, fallback: str, display_map: dict[str, str]) -> str:
    r"""Apply the final object number without erasing the source ref mode.

    ``\ref`` normally displays a bare number, ``\eqref`` adds parentheses,
    and ``\autoref``/``\cref`` add an object type.  The postprocessor knows
    the final number from the serialized bookmark, but the old map replaced
    every mode with a typed display such as ``式（1-1）``.  Use the placeholder
    fallback as the small amount of mode information retained by the
    preprocessor, while still accepting legacy markers with no map entry.
    """
    fallback = fallback.strip()
    display = display_map.get(label)
    if not display:
        return localize_xref_text(fallback)

    typed_prefixes = (
        ('式', ('Equation', 'equation')),
        ('图', ('Figure', 'figure')),
        ('表', ('Table', 'table')),
        ('定理', ('Theorem', 'theorem')),
        ('引理', ('Lemma', 'lemma')),
        ('命题', ('Proposition', 'proposition')),
        ('推论', ('Corollary', 'corollary')),
        ('定义', ('Definition', 'definition')),
        ('注', ('Remark', 'remark')),
    )
    for prefix, english_names in typed_prefixes:
        if not display.startswith(prefix):
            continue
        number = display[len(prefix):]
        typed = fallback.startswith(english_names) or fallback.startswith(prefix)
        if prefix == '式':
            typed = typed or fallback.startswith(('(', '（'))
        return display if typed else number.strip('()（）')
    return display


def _copy_run_properties(run: ET.Element | None) -> ET.Element | None:
    """Return a detached copy of a run's character properties.

    Cross-reference replacement must not make the new hyperlink inherit the
    paragraph's default font accidentally.  In particular, a marker may be
    inside an italic/bold run or a run using a CJK-specific font.  Copy only
    ``w:rPr`` so the replacement remains a normal text run.
    """
    if run is None:
        return None
    rpr = run.find(qn('w', 'rPr'))
    return copy.deepcopy(rpr) if rpr is not None else None


def _xref_inline(anchor: str | None, text: str, rpr: ET.Element | None = None) -> ET.Element:
    """Build one replacement inline without attaching it to a paragraph."""
    if anchor:
        inline = ET.Element(qn('w', 'hyperlink'))
        inline.set(qn('w', 'anchor'), anchor)
        inline.set(qn('w', 'history'), '1')
        run = ET.SubElement(inline, qn('w', 'r'))
    else:
        inline = ET.Element(qn('w', 'r'))
        run = inline
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    node = ET.SubElement(run, qn('w', 't'))
    node.set(f'{{{XML_NS}}}space', 'preserve')
    node.text = text
    return inline


def _plain_inline(text: str, rpr: ET.Element | None = None) -> ET.Element:
    inline = ET.Element(qn('w', 'r'))
    if rpr is not None:
        inline.append(copy.deepcopy(rpr))
    node = ET.SubElement(inline, qn('w', 't'))
    node.set(f'{{{XML_NS}}}space', 'preserve')
    node.text = text
    return inline


class UnsafeXRefError(RuntimeError):
    """A reference marker crosses OOXML that cannot be edited safely."""


def _xref_text_nodes(p: ET.Element) -> list[ET.Element]:
    """Return direct paragraph text nodes used by the xref text stream.

    Generated internal hyperlinks contain their own ``w:t`` result.  They
    must not be reintroduced into the stream while the remaining markers are
    being located, otherwise repeated identical markers can be matched to the
    wrong occurrence.  A source marker inside any hyperlink remains unsafe and
    is rejected separately.
    """
    parent_map = {child: parent for parent in p.iter() for child in parent}
    for node in p.iter(qn('w', 't')):
        if '[[[TJUFE_XREF:' not in (node.text or ''):
            continue
        ancestor = parent_map.get(node)
        while ancestor is not None and ancestor is not p:
            if ancestor.tag == qn('w', 'hyperlink'):
                raise UnsafeXRefError(
                    'xref marker is inside a hyperlink; refusing to rewrite nested OOXML'
                )
            ancestor = parent_map.get(ancestor)

    direct_nodes: list[ET.Element] = []
    for node in p.iter(qn('w', 't')):
        run = parent_map.get(node)
        if run is not None and run.tag == qn('w', 'r') and parent_map.get(run) is p:
            direct_nodes.append(node)
    return direct_nodes


def _replace_xref_placeholders_in_paragraph_in_place(
    p: ET.Element, bookmark_map: dict[str, str], eq_display_map: dict[str, str]
) -> None:
    """Replace textual xref sentinels while preserving all non-text OOXML.

    The old implementation cleared and rebuilt the whole paragraph from
    ``w:t`` text.  That silently deleted sibling ``m:oMath`` nodes whenever a
    sentence contained both an inline formula and a cross-reference.  Work on
    the participating text nodes in place instead, processing matches from
    right to left so offsets remain stable.  Runs, drawings, fields and math
    that are not part of the sentinel are never removed.
    """
    text_nodes = _xref_text_nodes(p)
    raw_text = ''.join(node.text or '' for node in text_nodes)
    if '[[[TJUFE_XREF:' not in raw_text:
        return
    matches = list(XREF_RE.finditer(raw_text))
    if not matches:
        return

    def replace_one(match: re.Match[str]) -> None:
        # Recompute the stream for every match.  This is required when two
        # markers share a run: the right-hand replacement may split that run,
        # so offsets from the original stream are no longer authoritative.
        current_nodes = _xref_text_nodes(p)
        current_raw = ''.join(node.text or '' for node in current_nodes)
        candidates = [
            candidate for candidate in XREF_RE.finditer(current_raw)
            if candidate.group(1).strip() == match.group(1).strip()
            and candidate.group(2) == match.group(2)
        ]
        # Markers are consumed from right to left.  Any identical markers to
        # the right have already disappeared, so the rightmost remaining
        # candidate is the original marker currently being processed.
        current_match = candidates[-1] if candidates else None
        if current_match is None:
            return

        ranges: list[tuple[int, int, ET.Element]] = []
        cursor = 0
        for node in current_nodes:
            value = node.text or ''
            ranges.append((cursor, cursor + len(value), node))
            cursor += len(value)
        parent_map = {child: parent for parent in p.iter() for child in parent}

        def top_level_child(node: ET.Element) -> ET.Element:
            current = node
            while parent_map.get(current) is not p:
                parent = parent_map.get(current)
                if parent is None:
                    raise UnsafeXRefError('xref marker has no paragraph owner')
                current = parent
            return current

        def direct_text_run(node: ET.Element) -> ET.Element:
            run = parent_map.get(node)
            if run is None or run.tag != qn('w', 'r') or parent_map.get(run) is not p:
                raise UnsafeXRefError(
                    'xref marker crosses a nested OOXML container; refusing to flatten it'
                )
            non_properties = [child for child in run if child.tag != qn('w', 'rPr')]
            if len(non_properties) != 1 or non_properties[0] is not node:
                raise UnsafeXRefError(
                    'xref marker participates in a run containing non-text OOXML'
                )
            return run

        participants = [item for item in ranges
                        if item[0] < current_match.end() and item[1] > current_match.start()]
        if not participants:
            return
        first_start, _first_end, first = participants[0]
        last_start, _last_end, last = participants[-1]
        first_run = direct_text_run(first)
        last_run = direct_text_run(last)
        first_owner = top_level_child(first)
        last_owner = top_level_child(last)
        children = list(p)
        first_index = children.index(first_owner)
        last_index = children.index(last_owner)

        # The replacement is safe only for a contiguous sequence of direct,
        # text-only runs.  A drawing, field, OMML node, hyperlink, bookmark,
        # revision container, or tab in the marker span is ambiguous: fail
        # before mutating the document instead of silently deleting it.
        for child in children[first_index:last_index + 1]:
            if child.tag != qn('w', 'r'):
                raise UnsafeXRefError(
                    'xref marker crosses non-text OOXML; add the marker to a plain text run'
                )
            non_properties = [item for item in child if item.tag != qn('w', 'rPr')]
            if len(non_properties) != 1 or non_properties[0].tag != qn('w', 't'):
                raise UnsafeXRefError(
                    'xref marker crosses a run with math, drawing, field, tab, or other OOXML'
                )

        prefix = (first.text or '')[:current_match.start() - first_start]
        suffix = (last.text or '')[current_match.end() - last_start:]
        first_rpr = _copy_run_properties(first_run)
        first.text = prefix
        for _start, _end, node in participants[1:]:
            node.text = ''
        if last is not first:
            last.text = suffix

        label = current_match.group(1).strip()
        shown = resolve_final_xref_text(
            label, current_match.group(2), eq_display_map
        )
        if shown.startswith('式（') and re.search(r'式\s*$', current_raw[:current_match.start()]):
            shown = shown[1:]

        insert_at = list(p).index(first_owner) + 1
        p.insert(insert_at, _xref_inline(bookmark_map.get(label), shown, first_rpr))
        if last is first and suffix:
            p.insert(insert_at + 1, _plain_inline(suffix, first_rpr))

    # Process right-to-left to keep unrelated text ranges stable; replace_one
    # still recomputes its own stream for shared-run markers.
    for match in reversed(matches):
        replace_one(match)


def replace_xref_placeholders_in_paragraph(
    p: ET.Element, bookmark_map: dict[str, str], eq_display_map: dict[str, str]
) -> None:
    """Apply xref replacement transactionally for one paragraph.

    Unsafe markers must not leave a half-rewritten in-memory paragraph behind
    when callers use this helper directly.  The production DOCX path already
    writes through a temporary directory; the detached copy here gives the
    same fail-closed behavior to unit tests and library consumers.
    """
    working = copy.deepcopy(p)
    _replace_xref_placeholders_in_paragraph_in_place(working, bookmark_map, eq_display_map)
    p[:] = list(working)


def extract_eq_label_marker(text: str) -> str | None:
    m = EQLABEL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).strip()


def extract_eq_control_marker(text: str) -> tuple[str, str] | None:
    m = EQCONTROL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def extract_generic_label_markers(text: str) -> list[str]:
    return [m.group(1).strip() for m in LABEL_RE.finditer(text) if m.group(1).strip()]


def strip_label_markers_in_paragraph(p: ET.Element) -> None:
    for t in p.findall('.//' + qn('w', 't')):
        if t.text:
            t.text = LABEL_RE.sub('', t.text)


def attach_bookmarks_to_paragraph(p: ET.Element, labels: list[str], bookmark_map: dict[str, str],
                                  next_bookmark_id: int, used_names: set[str]) -> int:
    if not labels:
        return next_bookmark_id
    insert_at = 1 if len(p) > 0 and p[0].tag == qn('w', 'pPr') else 0
    for label in labels:
        bookmark_name = unique_bookmark_name(label, used_names)
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


def caption_xref_display_map(body: ET.Element, bookmark_map: dict[str, str]) -> dict[str, str]:
    """Return visible figure/table reference labels from normalized captions.

    Source/AUX numbers are only hints.  Caption normalization can deliberately
    collapse repeated multi-panel captions into ``续图`` entries, so the final
    serialized caption is the authority for the hyperlink's visible number.
    """
    by_bookmark = {bookmark: label for label, bookmark in bookmark_map.items()}
    displays: dict[str, str] = {}
    for p in body.findall('.//' + qn('w', 'p')):
        caption = paragraph_text(p).strip()
        match = re.match(r'^(?:续)?([图表])\s*([A-Za-z0-9]+)[.\-－]([0-9]+)', caption)
        if not match:
            continue
        shown = f'{match.group(1)}{match.group(2)}.{match.group(3)}'
        for bookmark in p.findall('.//' + qn('w', 'bookmarkStart')):
            label = by_bookmark.get(bookmark.get(qn('w', 'name'), ''))
            if label:
                displays[label] = shown
    return displays


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


def requires_page_break_before(style: str | None, text: str) -> bool:
    """Return whether a serialized thesis heading must start a new page."""
    return style == 'Heading1' and (
        is_body_heading1(text)
        or is_appendix_heading1(text)
        or text in {'参考文献', '后 记', '后记'}
        or is_research_outputs_heading(text)
    )


def append_equation_number(
    p: ET.Element,
    label: str,
    bookmark_name: str | None = None,
    bookmark_id: int | None = None,
    extra_bookmarks: list[tuple[str, int]] | None = None,
) -> None:
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

    center_pos, right_pos = _equation_tab_positions()
    tab_center = ET.SubElement(tabs, qn('w', 'tab'))
    tab_center.set(qn('w', 'val'), 'center')
    tab_center.set(qn('w', 'pos'), str(center_pos))

    tab_right = ET.SubElement(tabs, qn('w', 'tab'))
    tab_right.set(qn('w', 'val'), 'right')
    tab_right.set(qn('w', 'pos'), str(right_pos))

    children = list(p)
    insert_at = 1 if children and children[0].tag == qn('w', 'pPr') else 0
    if not any(child.tag == qn('w', 'r') and child.find(qn('w', 'tab')) is not None for child in children[:insert_at+2]):
        r_center = ET.Element(qn('w', 'r'))
        ET.SubElement(r_center, qn('w', 'tab'))
        p.insert(insert_at, r_center)

    r_tab = ET.SubElement(p, qn('w', 'r'))
    ET.SubElement(r_tab, qn('w', 'tab'))
    bookmarks: list[tuple[str, int]] = []
    if bookmark_name and bookmark_id is not None:
        bookmarks.append((bookmark_name, bookmark_id))
    bookmarks.extend(extra_bookmarks or [])
    for name, identifier in bookmarks:
        bm_start = ET.SubElement(p, qn('w', 'bookmarkStart'))
        bm_start.set(qn('w', 'id'), str(identifier))
        bm_start.set(qn('w', 'name'), name)
    r_num = ET.SubElement(p, qn('w', 'r'))
    rpr = ET.SubElement(r_num, qn('w', 'rPr'))
    rstyle = ET.SubElement(rpr, qn('w', 'rStyle'))
    rstyle.set(qn('w', 'val'), EQUATION_NUMBER_STYLE_ID)
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
    for _name, identifier in reversed(bookmarks):
        bm_end = ET.SubElement(p, qn('w', 'bookmarkEnd'))
        bm_end.set(qn('w', 'id'), str(identifier))


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
    doc_grid.set(qn('w', 'linePitch'), LINE_PITCH)
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
    # Accept the two formats emitted by common LaTeX classes and by the V2
    # schema.  The old dot-only expression left ``图1-1`` in the title, which
    # then caused a second post-processing pass to number the caption again.
    text = re.sub(rf'^续?{kind}[A-Z0-9]+[.\-－][0-9]+\s*', '', text)
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


def normalize_figure_paragraph(p: ET.Element) -> bool:
    """Keep extracted figures inline so they reserve vertical layout space.

    The former implementation converted every ``wp:inline`` picture into a
    floating ``wp:anchor`` with top-and-bottom wrapping.  Word/WPS may then
    float the pictures past the following body paragraph while the caption
    remains in document order, which visibly inserts正文 between a figure and
    its caption.  Inline drawings are portable and make the paragraph height
    include the pictures, so the caption necessarily follows the image row.
    """
    has_drawing = bool(p.findall('.//' + qn('w', 'drawing')))
    if has_drawing:
        set_paragraph_style(p, 'Normal')
        set_paragraph_alignment(p, 'center')
    return has_drawing


def extract_figure_table_paragraphs(tbl: ET.Element) -> list[ET.Element]:
    paras: list[ET.Element] = []
    for p in tbl.findall('.//' + qn('w', 'p')):
        new_p = copy.deepcopy(p)
        if p.findall('.//' + qn('w', 'drawing')):
            set_paragraph_style(new_p, 'Normal')
            set_paragraph_alignment(new_p, 'center')
            normalize_figure_paragraph(new_p)
        # FigureTable cells can contain a text-only sub-caption, source note,
        # or an explanatory line in addition to the drawing.  Preserve every
        # paragraph in document order; dropping non-drawing paragraphs was a
        # silent content loss in the former expansion path.
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


def normalize_body(body: ET.Element) -> dict[str, object]:
    unwrap_figure_tables(body)

    current_chapter_label: str | None = None
    equation_no = 0
    table_no = 0
    figure_no = 0
    last_table_label: str | None = None
    last_figure_label: str | None = None
    first_abstract_idx = None
    pending_eq_labels: list[str] = []
    pending_eq_number: bool | None = None
    pending_eq_tag: str | None = None
    pending_generic_labels: list[str] = []
    bookmark_map: dict[str, str] = {}
    eq_display_map: dict[str, str] = {}
    used_bookmark_names = {
        node.get(qn('w', 'name')) for node in body.findall('.//' + qn('w', 'bookmarkStart'))
        if node.get(qn('w', 'name'))
    }
    existing_ids = []
    for node in body.findall('.//' + qn('w', 'bookmarkStart')) + body.findall('.//' + qn('w', 'bookmarkEnd')):
        try:
            existing_ids.append(int(node.get(qn('w', 'id'), '0')))
        except ValueError:
            continue
    next_bookmark_id = max(existing_ids, default=0) + 1

    paragraphs = list(body.findall(qn('w', 'p')))
    for idx, p in enumerate(paragraphs):
        style = paragraph_style(p)
        text = paragraph_text(p)
        control = extract_eq_control_marker(text)
        if control:
            mode, tag = control
            pending_eq_number = mode != 'unnumbered'
            pending_eq_tag = tag if mode == 'tag' and tag else None
            if p in list(body):
                body.remove(p)
            continue
        marker_label = extract_eq_label_marker(text)
        if marker_label:
            pending_eq_labels.append(marker_label)
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
                    next_bookmark_id = attach_bookmarks_to_paragraph(
                        prev_p, generic_labels, bookmark_map, next_bookmark_id, used_bookmark_names
                    )
                    attached = True
            if not attached:
                pending_generic_labels.extend(generic_labels)
            if p in list(body):
                body.remove(p)
            continue
        elif generic_labels:
            strip_label_markers_in_paragraph(p)
            text = paragraph_text(p)
            next_bookmark_id = attach_bookmarks_to_paragraph(
                p, generic_labels, bookmark_map, next_bookmark_id, used_bookmark_names
            )

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

        # Every numbered chapter and terminal Heading-1 matter must carry an
        # explicit page boundary.  Do this after Heading-1 classification so
        # arbitrary Pandoc headings that were demoted to Normal are untouched.
        if requires_page_break_before(paragraph_style(p), text):
            ensure_page_break_before(p)

        if pending_generic_labels and p in list(body):
            next_bookmark_id = attach_bookmarks_to_paragraph(
                p, pending_generic_labels, bookmark_map, next_bookmark_id, used_bookmark_names
            )
            pending_generic_labels = []

        if paragraph_has_math(p):
            if paragraph_has_math_para(p):
                set_paragraph_style(p, 'EquationBlock')
            # Inline math is content, not a paragraph role.  Do not overwrite
            # AbstractBody/Bibliography/Caption/etc. merely because a run
            # contains an inline ``m:oMath`` node.

        chapter_label = chapter_label_from_heading(text)
        if chapter_label:
            current_chapter_label = chapter_label
            equation_no = 0
            table_no = 0
            figure_no = 0
            last_table_label = None
            last_figure_label = None
        elif current_chapter_label and paragraph_has_math_para(p):
            if pending_eq_number is False:
                # A starred/\nonumber display may still carry a label.  Keep
                # a zero-width bookmark on the formula so references remain
                # closed, but never manufacture a number for an explicitly
                # unnumbered source object.
                if pending_eq_labels:
                    next_bookmark_id = attach_bookmarks_to_paragraph(
                        p, pending_eq_labels, bookmark_map, next_bookmark_id, used_bookmark_names
                    )
                    for pending_label in pending_eq_labels:
                        eq_display_map[pending_label] = ''
                pending_eq_labels = []
                pending_eq_number = None
                pending_eq_tag = None
                continue
            if _has_generated_equation_number(p, current_chapter_label):
                # Post-processing an already generated DOCX is a supported
                # no-op for equation numbering.  This also covers old output
                # without the private run style when its standard number is
                # still recognizable at the end of the paragraph.
                pending_eq_labels = []
                pending_eq_number = None
                pending_eq_tag = None
                continue
            equation_no += 1
            if pending_eq_tag:
                eq_label = pending_eq_tag
            else:
                eq_label = _format_equation_number(current_chapter_label, equation_no)
            bookmark_name = None
            bookmark_id = None
            extra_bookmarks: list[tuple[str, int]] = []
            if pending_eq_labels:
                label_specs: list[tuple[str, int]] = []
                for pending_label in pending_eq_labels:
                    name = unique_bookmark_name(pending_label, used_bookmark_names)
                    identifier = next_bookmark_id
                    next_bookmark_id += 1
                    bookmark_map[pending_label] = name
                    eq_display_map[pending_label] = f'式{eq_label}'
                    label_specs.append((name, identifier))
                bookmark_name, bookmark_id = label_specs[0]
                extra_bookmarks = label_specs[1:]
                pending_eq_labels = []
            pending_eq_number = None
            pending_eq_tag = None
            append_equation_number(p, eq_label, bookmark_name, bookmark_id, extra_bookmarks)
        elif current_chapter_label and re.match(r'^[（(][A-Z][.\-－]\d+[）)]$', text):
            match = re.match(r'^[（(]([A-Z])([.\-－])(\d+)[）)]$', text)
            if match:
                # Use a callable replacement so capture groups can never be
                # serialized as U+0001/U+0002 control characters.
                set_paragraph_text(p, _format_equation_number(match.group(1), match.group(3)))
        elif current_chapter_label and style == 'TableCaption' and text:
            explicit_continued, title = clean_caption_title(text, '表')
            if explicit_continued:
                if last_table_label is None:
                    raise RuntimeError('续表 caption has no preceding table object')
                label = last_table_label
                set_paragraph_text(p, format_caption_text('表', label, title, True))
            else:
                table_no += 1
                label = make_caption_label(current_chapter_label, table_no)
                last_table_label = label
                set_paragraph_text(p, format_caption_text('表', label, title, False))
        elif current_chapter_label and style == 'ImageCaption' and text:
            explicit_continued, title = clean_caption_title(text, '图')
            if explicit_continued:
                if last_figure_label is None:
                    raise RuntimeError('续图 caption has no preceding figure object')
                label = last_figure_label
                replace_caption_text_preserving_math(p, f'续图{label}', title)
            else:
                figure_no += 1
                label = make_caption_label(current_chapter_label, figure_no)
                last_figure_label = label
                replace_caption_text_preserving_math(p, f'图{label}', title)

    xref_display_map = dict(eq_display_map)
    xref_display_map.update(caption_xref_display_map(body, bookmark_map))
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
        if child.tag == qn('w', 'tbl'):
            if is_stylable_body_table(children, idx, content_start_idx):
                apply_three_line_table_style(child)
            continue
        if child.tag != qn('w', 'p'):
            continue
        replace_xref_placeholders_in_paragraph(child, bookmark_map, xref_display_map)
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

    # Role/numbering normalization is intentionally top-level, but xref
    # replacement is a package-content operation.  Walk table cells and
    # other nested paragraphs here as well so no supported container can leak
    # a literal marker into the final DOCX.
    for paragraph in body.iter(qn('w', 'p')):
        replace_xref_placeholders_in_paragraph(paragraph, bookmark_map, xref_display_map)

    return {
        'bookmark_map': bookmark_map,
        'xref_display_map': xref_display_map,
        'eq_display_map': eq_display_map,
    }


def replace_xrefs_in_story_parts(unzip_dir: Path, maps: dict[str, object]) -> int:
    """Resolve xrefs in footnotes/endnotes/comments and header/footer parts.

    These parts are separate XML documents in an OOXML package and therefore
    cannot be reached through ``word/document.xml``'s body traversal.  Only
    parts containing a marker are rewritten; unrelated package metadata is
    left byte-for-byte untouched.
    """
    bookmark_map = maps.get('bookmark_map', {})
    display_map = maps.get('xref_display_map', {})
    if not isinstance(bookmark_map, dict) or not isinstance(display_map, dict):
        raise TypeError('invalid xref map returned by normalize_body')
    word_dir = unzip_dir / 'word'
    changed = 0
    excluded = {'document.xml', 'styles.xml', 'settings.xml', 'numbering.xml', 'fontTable.xml', 'webSettings.xml'}
    for path in sorted(word_dir.glob('*.xml')):
        if path.name in excluded:
            continue
        tree = ET.parse(path)
        root = tree.getroot()
        paragraphs = list(root.iter(qn('w', 'p')))
        if any('[[[TJUFE_XREF:' in ''.join(node.text or '' for node in p.findall('.//' + qn('w', 't'))) for p in paragraphs):
            for paragraph in paragraphs:
                replace_xref_placeholders_in_paragraph(paragraph, bookmark_map, display_map)
            tree.write(path, encoding='utf-8', xml_declaration=True)
            changed += 1
    return changed


_INVALID_XML_CONTROL_BYTES = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def ensure_font_table_declarations(unzip_dir: Path) -> int:
    """Declare every font referenced by the package in ``fontTable.xml``.

    Pandoc's reference document can contain a font in ``styles.xml`` or in a
    generated run without declaring it in the seed font table.  Word often
    repairs that package silently, while stricter OOXML consumers report the
    package as incomplete.  Add minimal ``w:font`` declarations for missing
    names; this does not embed a font or change the selected fallback.
    """
    font_table_path = unzip_dir / 'word' / 'fontTable.xml'
    if not font_table_path.exists():
        return 0

    font_tree = ET.parse(font_table_path)
    font_root = font_tree.getroot()
    font_name_attr = qn('w', 'name')
    declared = {
        node.get(font_name_attr)
        for node in font_root.findall(qn('w', 'font'))
        if node.get(font_name_attr)
    }
    referenced: set[str] = set()
    for path in sorted((unzip_dir / 'word').glob('*.xml')):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            # The normal validation pass below reports the package error with
            # the exact part path.  Do not obscure it here.
            continue
        for rfonts in root.iter(qn('w', 'rFonts')):
            for attr in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
                value = rfonts.get(qn('w', attr))
                if value:
                    referenced.add(value)

    missing = sorted(referenced - declared)
    for name in missing:
        font = ET.SubElement(font_root, qn('w', 'font'))
        font.set(font_name_attr, name)
    if missing:
        font_tree.write(font_table_path, encoding='utf-8', xml_declaration=True)
    return len(missing)


def validate_xml_parts(unzip_dir: Path) -> None:
    """Parse every XML part and reject raw XML 1.0 control characters."""
    for path in sorted(unzip_dir.rglob('*')):
        if not path.is_file() or path.suffix not in {'.xml', '.rels'}:
            continue
        data = path.read_bytes()
        bad = _INVALID_XML_CONTROL_BYTES.search(data)
        if bad:
            raise ValueError(f'invalid XML control character 0x{bad.group(0)[0]:02x} in {path.relative_to(unzip_dir)}')
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            raise ValueError(f'invalid XML part {path.relative_to(unzip_dir)}: {exc}') from exc


_RESIDUAL_MARKER_BYTES = re.compile(rb'TJUFE_(?:XREF|LABEL|EQLABEL|EQCONTROL|OPAQUE_REGION)')


def strip_label_markers_from_package(unzip_dir: Path) -> int:
    """Remove only known label sentinels from OOXML text and attributes.

    Pandoc serializes a table caption into ``w:tblCaption/@w:val`` rather
    than a paragraph run, so paragraph-only cleanup can leave a marker in an
    otherwise valid package.  This narrow pass removes label sentinels from
    all XML node values; other conversion sentinels remain errors.
    """
    changed = 0
    for path in sorted(unzip_dir.rglob('*.xml')):
        tree = ET.parse(path)
        root = tree.getroot()
        part_changed = False
        for node in root.iter():
            if node.text and LABEL_RE.search(node.text):
                node.text = LABEL_RE.sub('', node.text)
                part_changed = True
            for key, value in list(node.attrib.items()):
                if LABEL_RE.search(value):
                    node.set(key, LABEL_RE.sub('', value))
                    part_changed = True
        if part_changed:
            tree.write(path, encoding='utf-8', xml_declaration=True)
            changed += 1
    return changed


def validate_no_conversion_markers(unzip_dir: Path) -> None:
    """Reject any unresolved pipeline marker before an output is published."""
    for path in sorted(unzip_dir.rglob('*.xml')):
        data = path.read_bytes()
        if _RESIDUAL_MARKER_BYTES.search(data):
            raise ValueError(f'residual conversion marker in {path.relative_to(unzip_dir)}')


def _zip_docx_atomic(unzip_dir: Path, output_path: Path) -> None:
    """Create the final package beside the destination, then replace atomically."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f'.{output_path.stem}-', suffix='.docx.tmp', dir=str(output_path.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(unzip_dir.rglob('*')):
                if path.is_file():
                    zf.write(path, path.relative_to(unzip_dir))
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def normalize_citation_bookmarks_for_wps(document_root: ET.Element) -> dict[str, str]:
    """Rename citeproc bookmarks to conservative Word/WPS-safe identifiers.

    Pandoc derives citation bookmark names from bibliography keys (normally
    ``ref-<key>``).  The hyphen is accepted by Microsoft Word, but WPS Writer
    versions can reject such internal hyperlink targets because their bookmark
    parser enforces the UI naming rules more strictly.  Replace every citeproc
    target with a short ASCII-alphanumeric name and update all matching direct
    hyperlinks and field instructions in the same document.

    Names are assigned in bibliography/bookmark order, making the output stable
    for a stable bibliography while avoiding dependence on user-controlled keys.
    Existing non-citation bookmarks are deliberately left unchanged.
    """
    bookmark_name = qn('w', 'name')
    anchor_name = qn('w', 'anchor')
    existing_names = {
        node.get(bookmark_name)
        for node in document_root.iter(qn('w', 'bookmarkStart'))
        if node.get(bookmark_name)
    }
    mapping: dict[str, str] = {}
    next_number = 1

    for node in document_root.iter(qn('w', 'bookmarkStart')):
        old_name = node.get(bookmark_name) or ''
        if not old_name.startswith('ref-'):
            continue
        if old_name not in mapping:
            while True:
                candidate = f'REF{next_number:04d}'
                next_number += 1
                if candidate not in existing_names:
                    break
            mapping[old_name] = candidate
            existing_names.add(candidate)
        node.set(bookmark_name, mapping[old_name])

    if not mapping:
        return mapping

    for hyperlink in document_root.iter(qn('w', 'hyperlink')):
        old_anchor = hyperlink.get(anchor_name)
        if old_anchor in mapping:
            hyperlink.set(anchor_name, mapping[old_anchor])

    # Some producers represent internal hyperlinks as field codes rather than
    # w:hyperlink/@w:anchor.  Keep those valid if such fields coexist with
    # Pandoc-generated citation bookmarks.
    for instruction in document_root.iter(qn('w', 'instrText')):
        text = instruction.text or ''
        for old_name, new_name in mapping.items():
            text = re.sub(
                rf'(?P<prefix>\\l\s+["\u201c]?)({re.escape(old_name)})(?P<suffix>["\u201d]?)',
                rf'\g<prefix>{new_name}\g<suffix>',
                text,
                flags=re.IGNORECASE,
            )
        instruction.text = text

    return mapping


def convert_citation_hyperlinks_to_fields_for_wps(document_root: ET.Element) -> int:
    """Convert citation links to ``HYPERLINK \\l`` complex fields.

    WPS Writer for macOS can display a standard OOXML internal hyperlink but
    route a click through its external-file opener, producing “无法打开指定的文件”.
    Its field-code path handles document-local bookmarks correctly.  Convert
    only the conservative ``REFdddd`` citation anchors; all other hyperlinks
    keep their original OOXML representation.
    """
    parent_map = {child: parent for parent in document_root.iter() for child in parent}
    converted = 0
    for hyperlink in list(document_root.iter(qn('w', 'hyperlink'))):
        anchor = hyperlink.get(qn('w', 'anchor')) or ''
        if not re.fullmatch(r'REF[0-9]{4}', anchor):
            continue
        parent = parent_map.get(hyperlink)
        if parent is None:
            continue
        position = list(parent).index(hyperlink)
        field_nodes: list[ET.Element] = []

        begin_run = ET.Element(qn('w', 'r'))
        begin = ET.SubElement(begin_run, qn('w', 'fldChar'))
        begin.set(qn('w', 'fldCharType'), 'begin')
        field_nodes.append(begin_run)

        instruction_run = ET.Element(qn('w', 'r'))
        instruction = ET.SubElement(instruction_run, qn('w', 'instrText'))
        instruction.set(f'{{{XML_NS}}}space', 'preserve')
        instruction.text = f' HYPERLINK \\l "{anchor}" '
        field_nodes.append(instruction_run)

        separate_run = ET.Element(qn('w', 'r'))
        separate = ET.SubElement(separate_run, qn('w', 'fldChar'))
        separate.set(qn('w', 'fldCharType'), 'separate')
        field_nodes.append(separate_run)

        # Preserve all displayed runs (including formatting) as the field result.
        field_nodes.extend(list(hyperlink))

        end_run = ET.Element(qn('w', 'r'))
        end = ET.SubElement(end_run, qn('w', 'fldChar'))
        end.set(qn('w', 'fldCharType'), 'end')
        field_nodes.append(end_run)

        parent.remove(hyperlink)
        for offset, node in enumerate(field_nodes):
            parent.insert(position + offset, node)
        converted += 1
    return converted


def normalize_bibliography_label_spacing(body: ET.Element) -> int:
    """Collapse citeproc's label separator to one ordinary space.

    Pandoc currently serializes numeric bibliography labels as three runs:
    ``[n]``, a space, and a literal tab.  Microsoft Word turns the literal tab
    into ``w:tab`` while saving, and without a bibliography-specific tab stop
    this creates a conspicuously large gap.  Restrict the repair to paragraphs
    explicitly styled ``Bibliography`` and only to whitespace immediately
    following the leading numeric label; tabs elsewhere remain untouched.
    """
    changed = 0
    label_re = re.compile(r'^\s*(\[[0-9０-９]+(?:\s*[-,，–—]\s*[0-9０-９]+)*\])\s*$')
    for paragraph in body.iter(qn('w', 'p')):
        if paragraph_style(paragraph) != 'Bibliography':
            continue
        nodes = list(paragraph.iter())
        label_index = None
        for index, node in enumerate(nodes):
            if node.tag != qn('w', 't'):
                continue
            match = label_re.match(node.text or '')
            if match:
                node.text = match.group(1) + ' '
                node.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                label_index = index
                break
            if (node.text or '').strip():
                break
        if label_index is None:
            continue

        parent_map = {child: parent for parent in paragraph.iter() for child in parent}
        paragraph_changed = False
        for node in nodes[label_index + 1:]:
            if node.tag == qn('w', 'tab'):
                parent = parent_map.get(node)
                if parent is not None:
                    parent.remove(node)
                    paragraph_changed = True
                continue
            if node.tag != qn('w', 't'):
                continue
            if (node.text or '').strip():
                break
            if node.text:
                node.text = ''
                paragraph_changed = True
        if paragraph_changed:
            changed += 1
    return changed


def process_docx(input_path: Path, output_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        unzip_dir = td_path / 'unzipped'
        unzip_dir.mkdir()
        with zipfile.ZipFile(input_path) as zf:
            zf.extractall(unzip_dir)

        _patch_styles(unzip_dir)
        ensure_equation_number_style(unzip_dir)

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

        maps = normalize_body(body)
        replace_xrefs_in_story_parts(unzip_dir, maps)
        normalize_bibliography_label_spacing(body)
        citation_bookmark_map = normalize_citation_bookmarks_for_wps(document_root)
        citation_fields = convert_citation_hyperlinks_to_fields_for_wps(document_root)
        if citation_bookmark_map or citation_fields:
            print(
                f'[postprocess] normalized {len(citation_bookmark_map)} citation bookmarks and '
                f'converted {citation_fields} links to internal fields for Word/WPS compatibility',
                file=sys.stderr,
            )

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
            insert_section_break_before(body, abstract_body_idx, title_break_p)

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
            insert_section_break_before(body, insert_at, body_break_p)

        # abstract/toc section ends right before first fixed-header body section
        if section_starts:
            first_heading_idx = section_starts[0][0]
            front_break_p = ET.Element(qn('w', 'p'))
            ppr = ET.SubElement(front_break_p, qn('w', 'pPr'))
            sectpr = ET.SubElement(ppr, qn('w', 'sectPr'))
            configure_sectpr(sectpr, page_fmt='upperRoman', page_start=1, header_rid=None, footer_rid=footer_rid)
            insert_section_break_before(body, first_heading_idx, front_break_p)

        document_tree.write(document_path, encoding='utf-8', xml_declaration=True)
        settings_tree.write(settings_path, encoding='utf-8', xml_declaration=True)
        rels_tree.write(rels_path, encoding='utf-8', xml_declaration=True)
        ct_tree.write(ct_path, encoding='utf-8', xml_declaration=True)
        removed_label_markers = strip_label_markers_from_package(unzip_dir)
        if removed_label_markers:
            print(
                f'[postprocess] removed label sentinel(s) from {removed_label_markers} XML part(s)',
                file=sys.stderr,
            )
        declared_fonts = ensure_font_table_declarations(unzip_dir)
        if declared_fonts:
            print(
                f'[postprocess] added {declared_fonts} missing font declaration(s)',
                file=sys.stderr,
            )
        validate_xml_parts(unzip_dir)
        validate_no_conversion_markers(unzip_dir)
        _zip_docx_atomic(unzip_dir, output_path)


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
    p.add_argument('--config', type=Path,
                   help='YAML config / overlay file with page/typography overrides (replaces built-in defaults).')
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.config:
        _apply_format_overrides(str(args.config.resolve()))
    input_path = args.input.resolve()
    output_path = args.output.resolve()

    if not input_path.is_file():
        raise FileNotFoundError(f'input DOCX not found: {input_path}')
    if args.embed_fonts:
        embed_path = Path(__file__).with_name('embed_fonts.py')
        if not embed_path.is_file():
            raise RuntimeError(
                '--embed-fonts is unavailable in this distribution: '
                f'missing {embed_path.name}; no output was published'
            )

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
        raise RuntimeError('[embed-fonts] cannot load embed_fonts.py')
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
