#!/usr/bin/env python3
"""Extract independently readable OOXML facts from a DOCX package.

The extractor is deliberately structural: it reads OOXML attributes directly,
walks all supported Word stories (including tables, footnotes and endnotes),
and records missing values as ``null`` instead of manufacturing defaults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
M_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS = {'w': W_NS, 'r': R_NS, 'rel': REL_NS}


def qn(local: str, ns: str = W_NS) -> str:
    return f'{{{ns}}}{local}'


def attr(element: ET.Element | None, local: str, ns: str = W_NS) -> str | None:
    return element.get(qn(local, ns)) if element is not None else None


def int_or_value(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def child_attr(element: ET.Element | None, child: str, attribute: str = 'val') -> str | None:
    if element is None:
        return None
    return attr(element.find(qn(child)), attribute)


def _parse_spacing(parent: ET.Element | None) -> dict[str, int | str]:
    spacing = parent.find(qn('spacing')) if parent is not None else None
    return {
        key: parsed
        for key, parsed in (
            (key, int_or_value(attr(spacing, key)))
            for key in ('before', 'after', 'line', 'lineRule')
        )
        if parsed is not None
    }


def _parse_ind(parent: ET.Element | None) -> dict[str, int | str]:
    ind = parent.find(qn('ind')) if parent is not None else None
    keys = ('left', 'right', 'firstLine', 'hanging', 'leftChars', 'rightChars', 'firstLineChars', 'hangingChars')
    return {
        key: parsed
        for key, parsed in (
            (key, int_or_value(attr(ind, key)))
            for key in keys
        )
        if parsed is not None
    }


def _parse_justify(parent: ET.Element | None) -> str | None:
    # w:jc stores w:val as an attribute on the already-located element.  It is
    # not a child element.
    return child_attr(parent, 'jc')


def _parse_fonts(rpr: ET.Element | None) -> dict[str, str]:
    fonts = rpr.find(qn('rFonts')) if rpr is not None else None
    if fonts is None:
        return {}
    return {
        key: value
        for key, value in ((key, attr(fonts, key)) for key in ('ascii', 'hAnsi', 'eastAsia', 'cs'))
        if value is not None
    }


def _parse_run_format(rpr: ET.Element | None) -> dict[str, Any]:
    size = child_attr(rpr, 'sz')
    return {
        'fonts': _parse_fonts(rpr),
        'size_halfpt': int_or_value(size),
        'bold': _bool_element(rpr.find(qn('b')) if rpr is not None else None),
        'italic': _bool_element(rpr.find(qn('i')) if rpr is not None else None),
    }


def _bool_element(element: ET.Element | None) -> bool | None:
    if element is None:
        return None
    value = attr(element, 'val')
    if value is None:
        return True
    return value not in {'0', 'false', 'off', 'no'}


def extract_styles(style_xml: bytes | str) -> dict[str, dict[str, Any]]:
    root = ET.fromstring(style_xml)
    styles: dict[str, dict[str, Any]] = {}
    for style in root.findall(qn('style')):
        style_id = attr(style, 'styleId')
        if not style_id or style_id in styles:
            continue
        ppr = style.find(qn('pPr'))
        rpr = style.find(qn('rPr'))
        run = _parse_run_format(rpr)
        styles[style_id] = {
            'name': child_attr(style, 'name'),
            'type': attr(style, 'type'),
            'basedOn': child_attr(style, 'basedOn'),
            **run,
            'spacing': _parse_spacing(ppr),
            'indent': _parse_ind(ppr),
            'justify': _parse_justify(ppr),
        }
    return styles


def _paragraph_text(paragraph: ET.Element) -> str:
    chunks = []
    for node in paragraph.iter():
        if node.tag in {qn('t'), qn('instrText'), qn('delText'), qn('delInstrText'), qn('t', 'http://schemas.openxmlformats.org/officeDocument/2006/math')}:
            if node.text:
                chunks.append(node.text)
        elif node.tag == qn('tab'):
            chunks.append('\t')
        elif node.tag == qn('br'):
            chunks.append('\n')
    return ''.join(chunks)


def _paragraph_has_image(paragraph: ET.Element) -> bool:
    return any(node.tag == qn('drawing') for node in paragraph.iter())


def extract_paragraphs(doc_xml: bytes | str, max_text: int = 120, story: str = 'word/document.xml') -> list[dict[str, Any]]:
    root = ET.fromstring(doc_xml)
    result: list[dict[str, Any]] = []
    for paragraph in root.iter(qn('p')):
        ppr = paragraph.find(qn('pPr'))
        pstyle = child_attr(ppr, 'pStyle')
        text = _paragraph_text(paragraph)
        if not text and _paragraph_has_image(paragraph):
            text = '[IMAGE]'
        runs = list(paragraph.iter(qn('r')))
        first_rpr = runs[0].find(qn('rPr')) if runs else None
        result.append({
            'story': story,
            'text': text[:max_text],
            'full_length': len(text),
            'style': pstyle,
            **_parse_run_format(first_rpr),
            'spacing': _parse_spacing(ppr),
            'indent': _parse_ind(ppr),
            'justify': _parse_justify(ppr),
            'has_math': any(node.tag in {qn('oMath', M_NS), qn('oMathPara', M_NS), qn('t', M_NS)} for node in paragraph.iter()),
            'has_image': _paragraph_has_image(paragraph),
        })
    return result


def extract_numbering(num_xml: bytes | str | None) -> dict[str, Any]:
    if not num_xml:
        return {}
    root = ET.fromstring(num_xml)
    numbering: dict[str, Any] = {}
    abstract_levels: dict[str, dict[str, Any]] = {}
    for abstract in root.findall(qn('abstractNum')):
        abstract_id = attr(abstract, 'abstractNumId')
        if abstract_id is None:
            continue
        levels: dict[str, Any] = {}
        for level in abstract.findall(qn('lvl')):
            ilvl = attr(level, 'ilvl')
            if ilvl is None:
                continue
            levels[ilvl] = {
                'format': child_attr(level, 'numFmt'),
                'text': child_attr(level, 'lvlText'),
                'start': int_or_value(child_attr(level, 'start')),
                'indent': _parse_ind(level.find(qn('pPr'))),
                'justify': _parse_justify(level.find(qn('pPr'))),
            }
        abstract_levels[abstract_id] = levels
    for num in root.findall(qn('num')):
        num_id = attr(num, 'numId')
        if num_id is None:
            continue
        abstract_id = child_attr(num, 'abstractNumId')
        numbering[num_id] = {
            'abstractNumId': abstract_id,
            'levels': abstract_levels.get(abstract_id, {}) if abstract_id is not None else {},
        }
    return numbering


def _page_section(sect: ET.Element) -> dict[str, Any]:
    page_size = sect.find(qn('pgSz'))
    margins = sect.find(qn('pgMar'))
    result: dict[str, Any] = {
        'width': int_or_value(attr(page_size, 'w')),
        'height': int_or_value(attr(page_size, 'h')),
        'orient': attr(page_size, 'orient'),
        'margins': {},
        'page_number_format': child_attr(sect, 'pgNumType', 'fmt'),
        'page_number_start': int_or_value(child_attr(sect, 'pgNumType', 'start')),
    }
    result['margins'] = {
        key: parsed
        for key, parsed in ((key, int_or_value(attr(margins, key))) for key in ('top', 'bottom', 'left', 'right', 'header', 'footer', 'gutter'))
        if parsed is not None
    }
    return result


def extract_page_settings(doc_xml: bytes | str) -> dict[str, Any]:
    root = ET.fromstring(doc_xml)
    sections = [_page_section(sect) for sect in root.iter(qn('sectPr'))]
    if not sections:
        return {'sections': []}
    result = dict(sections[0])
    result['sections'] = sections
    return result


def _story_files(names: list[str]) -> list[str]:
    preferred = ['word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml', 'word/comments.xml']
    return [name for name in preferred if name in names] + sorted(
        name for name in names if name.startswith('word/header') or name.startswith('word/footer')
    )


def extract_docx(docx_path: Path) -> dict[str, Any]:
    package_hash = hashlib.sha256(docx_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(docx_path) as archive:
        names = archive.namelist()
        xml_data = {name: archive.read(name) for name in names if name.endswith('.xml') or name.endswith('.rels')}
        if 'word/document.xml' not in xml_data:
            raise ValueError('DOCX package has no word/document.xml')
        for name, data in xml_data.items():
            try:
                ET.fromstring(data)
            except ET.ParseError as exc:
                raise ValueError(f'invalid XML part {name}: {exc}') from exc
        paragraphs: list[dict[str, Any]] = []
        stories = _story_files(names)
        for name in stories:
            paragraphs.extend(extract_paragraphs(xml_data[name], story=name))
        page = extract_page_settings(xml_data['word/document.xml'])
        return {
            'schema_version': '2.0',
            'package_hash': package_hash,
            'styles': extract_styles(xml_data['word/styles.xml']) if 'word/styles.xml' in xml_data else {},
            'document': paragraphs,
            'numbering': extract_numbering(xml_data.get('word/numbering.xml')),
            'page': page,
            'manifest': {
                'xml_parts': {
                    name: hashlib.sha256(data).hexdigest() for name, data in sorted(xml_data.items())
                },
                'media_parts': {
                    name: hashlib.sha256(archive.read(name)).hexdigest()
                    for name in sorted(names)
                    if name.startswith('word/media/')
                },
                'stories': stories,
                'paragraph_count': len(paragraphs),
                'image_paragraph_count': sum(bool(item['has_image']) for item in paragraphs),
                'math_paragraph_count': sum(bool(item['has_math']) for item in paragraphs),
            },
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    data = extract_docx(args.input)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for key, filename in (
            ('styles', 'styles.json'), ('document', 'paragraphs.json'),
            ('numbering', 'numbering.json'), ('page', 'page.json'), ('manifest', 'manifest.json'),
        ):
            (args.out / filename).write_text(json.dumps(data[key], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (args.out / 'document.json').write_text(json.dumps(data['document'], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(__import__('sys').argv[1:]))
