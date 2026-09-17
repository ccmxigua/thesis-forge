#!/usr/bin/env python3
"""Validate a generated DOCX against its source and resolved configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from redteam.extract_docx import extract_docx  # noqa: E402
from scripts.config_v2 import load_resolved_config  # noqa: E402


INVALID_XML_CONTROL_BYTES = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')
RESIDUAL_MARKER = re.compile(r'TJUFE_(?:XREF|LABEL|EQLABEL|EQCONTROL|OPAQUE_REGION)')
DISPLAY_ENV = re.compile(r'\\begin\{(?P<env>equation|align|gather|multline|eqnarray)(?P<star>\*)?\}(?P<body>.*?)\\end\{(?P=env)(?:\*)?\}', re.S)
FIGURE_ENV = re.compile(r'\\begin\{figure(?:\*)?\}')
IMAGE_COMMAND = re.compile(r'\\includegraphics(?:\[[^\]]*\])?\{')
LABEL = re.compile(r'\\label\{[^{}]+\}')
W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def _serialized_content_checks(docx: Path, errors: list[str]) -> dict[str, Any]:
    """Check package-wide marker and bookmark/link closure after serialization."""
    bookmark_names: set[str] = set()
    bookmark_ids: dict[str, dict[str, int]] = {}
    internal_targets: list[tuple[str, str]] = []
    marker_parts: list[str] = []
    try:
        with zipfile.ZipFile(docx) as archive:
            for name in archive.namelist():
                if not (name.endswith('.xml') or name.endswith('.rels')):
                    continue
                data = archive.read(name)
                if RESIDUAL_MARKER.search(data.decode('utf-8', errors='replace')):
                    marker_parts.append(name)
                if not name.endswith('.xml'):
                    continue
                try:
                    root = ET.fromstring(data)
                except ET.ParseError:
                    continue
                for node in root.iter():
                    local = node.tag.rsplit('}', 1)[-1]
                    if local == 'bookmarkStart':
                        name_value = node.get(f'{{{W_NS}}}name')
                        identifier = node.get(f'{{{W_NS}}}id')
                        if name_value:
                            if name_value in bookmark_names:
                                errors.append(f'duplicate bookmark name: {name_value}')
                            bookmark_names.add(name_value)
                        if identifier:
                            bookmark_ids.setdefault(identifier, {'start': 0, 'end': 0})['start'] += 1
                    elif local == 'bookmarkEnd':
                        identifier = node.get(f'{{{W_NS}}}id')
                        if identifier:
                            bookmark_ids.setdefault(identifier, {'start': 0, 'end': 0})['end'] += 1
                    elif local == 'hyperlink':
                        anchor = node.get(f'{{{W_NS}}}anchor')
                        if anchor:
                            internal_targets.append((name, anchor))
                    elif local in {'instrText', 'fldSimple'}:
                        instruction = node.text if local == 'instrText' else node.get(f'{{{W_NS}}}instr')
                        if instruction:
                            match = re.search(r'\\l\s+["“]([^"”]+)["”]', instruction)
                            if match:
                                internal_targets.append((name, match.group(1)))
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f'cannot inspect serialized package: {exc}')
        return {'bookmark_names': 0, 'internal_links': 0, 'marker_parts': []}

    if marker_parts:
        errors.append('residual conversion markers in package parts: ' + ', '.join(marker_parts))
    for identifier, counts in sorted(bookmark_ids.items()):
        if counts['start'] != counts['end']:
            errors.append(
                f'unbalanced bookmark id {identifier}: starts={counts["start"]}, ends={counts["end"]}'
            )
    for part, anchor in internal_targets:
        if anchor not in bookmark_names:
            errors.append(f'internal hyperlink target {anchor!r} has no bookmark ({part})')
    return {
        'bookmark_names': len(bookmark_names),
        'internal_links': len(internal_targets),
        'marker_parts': marker_parts,
    }


def _load_expected_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'invalid expected manifest {path}: {exc}') from exc
    if not isinstance(value, dict) or value.get('schema_version') != '1.0':
        raise ValueError(f'expected manifest {path} must use schema_version 1.0')
    return value


def _check_expected_manifest(
    manifest: dict[str, Any], extracted: dict[str, Any], errors: list[str]
) -> None:
    if not manifest:
        return
    paragraphs = extracted.get('document', [])
    document_paragraphs = [
        item for item in paragraphs if item.get('story') == 'word/document.xml'
    ]
    texts = [str(item.get('text', '')) for item in document_paragraphs]
    normalized = re.sub(r'\s+', '', '\n'.join(texts))
    for required in manifest.get('required_texts', []):
        if re.sub(r'\s+', '', str(required)) not in normalized:
            errors.append(f'expected manifest text missing: {required}')
    actual = {
        'paragraphs': len(document_paragraphs),
        'math_paragraphs': sum(bool(item.get('has_math')) for item in document_paragraphs),
        'image_paragraphs': sum(bool(item.get('has_image')) for item in document_paragraphs),
        'chapter_headings': sum(
            1 for text in texts if re.match(r'^\s*第\d+章\s+', text)
        ),
    }
    for key, expected in (manifest.get('exact') or {}).items():
        if actual.get(key) != expected:
            errors.append(f'expected manifest {key} {expected} != output {actual.get(key)}')
    for key, minimum in (manifest.get('minimum') or {}).items():
        if actual.get(key, 0) < minimum:
            errors.append(f'expected manifest minimum {key} {minimum} > output {actual.get(key, 0)}')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hash(path: Path | None) -> str | None:
    return sha256(path) if path is not None and path.is_file() else None


def _expected_page(config: dict[str, Any]) -> tuple[int | None, int | None, dict[str, int]]:
    page = config.get('page') or {}
    size = page.get('size')
    if size == 'A4':
        width, height = 11906, 16838
    elif size in {'16K', '16k', 'B5'}:
        width, height = 10431, 14740
    elif isinstance(size, dict):
        width, height = size.get('width'), size.get('height')
    else:
        width = height = None
    margins = page.get('margins') or {}
    expected_margins = {
        key: margins[key] for key in ('top', 'bottom', 'left', 'right', 'header_distance', 'footer_distance', 'gutter')
        if margins.get(key) is not None
    }
    expected_margins = {('header' if key == 'header_distance' else 'footer' if key == 'footer_distance' else key): value for key, value in expected_margins.items()}
    return width, height, expected_margins


def _source_expectations(source: str) -> dict[str, int]:
    display = 0
    for match in DISPLAY_ENV.finditer(source):
        if match.group('star') or re.search(r'\\(?:notag|nonumber)\b', match.group('body') or ''):
            continue
        display += 1
    return {
        'numbered_display_math': display,
        'figure_objects': max(len(FIGURE_ENV.findall(source)), len(IMAGE_COMMAND.findall(source))),
        'source_labels': len(LABEL.findall(source)),
    }


def validate(
    docx: Path, source: Path, config: Path,
    compatibility_report: Path | None = None,
    expected_manifest: Path | None = None,
    bibliography: Path | None = None,
    csl: Path | None = None,
    run_manifest: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required_files = ((docx, 'DOCX'), (source, 'source TeX'), (config, 'resolved config'))
    for path, label in required_files:
        if not path.is_file() or not path.stat().st_mode & 0o444:
            errors.append(f'{label} is missing or unreadable: {path}')
    if bibliography is not None and (not bibliography.is_file() or not bibliography.stat().st_mode & 0o444):
        errors.append(f'bibliography is missing or unreadable: {bibliography}')
    if csl is not None and (not csl.is_file() or not csl.stat().st_mode & 0o444):
        errors.append(f'CSL file is missing or unreadable: {csl}')
    if csl is not None and bibliography is None:
        errors.append('CSL validation input requires a bibliography')
    if errors:
        return {
            'schema_version': '1.0', 'valid': False, 'errors': errors,
            'warnings': warnings, 'artifact': str(docx.resolve()),
            'hashes': {
                'artifact_sha256': _file_hash(docx),
                'source_sha256': _file_hash(source),
                'config_sha256': _file_hash(config),
                'bibliography_sha256': _file_hash(bibliography),
                'csl_sha256': _file_hash(csl),
            },
        }
    try:
        extracted = extract_docx(docx)
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        return {'schema_version': '1.0', 'valid': False, 'errors': [f'cannot extract DOCX: {exc}'], 'warnings': []}

    with zipfile.ZipFile(docx) as archive:
        for name in archive.namelist():
            if not (name.endswith('.xml') or name.endswith('.rels')):
                continue
            data = archive.read(name)
            if INVALID_XML_CONTROL_BYTES.search(data):
                errors.append(f'invalid XML control character in {name}')
            try:
                ET.fromstring(data)
            except ET.ParseError as exc:
                errors.append(f'invalid XML part {name}: {exc}')

    package_checks = _serialized_content_checks(docx, errors)
    for item in extracted.get('document', []):
        if RESIDUAL_MARKER.search(item.get('text', '')):
            errors.append(f'residual conversion marker in {item.get("story", "document")}: {item.get("text", "")[:100]}')
    if not extracted.get('document'):
        errors.append('document has no readable paragraphs')
    page = extracted.get('page', {})
    if not page.get('sections'):
        errors.append('document has no section properties')

    resolved_config_hash: str | None = None
    try:
        source_text = source.read_text(encoding='utf-8')
        cfg, config_diagnostics = load_resolved_config(
            ROOT / 'schema' / 'config-schema-v2.yaml', config
        )
        for item in config_diagnostics.get('unknown_fields', []):
            errors.append(f'unknown config field: {item}')
        errors.extend(f'config error: {item}' for item in config_diagnostics.get('errors', []))
        serialized_config = yaml.safe_dump(
            cfg, allow_unicode=True, sort_keys=False, width=120
        ).encode('utf-8')
        resolved_config_hash = hashlib.sha256(serialized_config).hexdigest()
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        errors.append(f'cannot read source/config: {exc}')
        cfg, source_text = {}, ''

    expected_width, expected_height, expected_margins = _expected_page(cfg)
    if expected_width is not None and page.get('width') != expected_width:
        errors.append(f'page width {page.get("width")} != resolved config {expected_width}')
    if expected_height is not None and page.get('height') != expected_height:
        errors.append(f'page height {page.get("height")} != resolved config {expected_height}')
    for key, expected in expected_margins.items():
        if page.get('margins', {}).get(key) != expected:
            errors.append(f'margin {key} {page.get("margins", {}).get(key)} != resolved config {expected}')

    actual = {
        'paragraphs': len(extracted.get('document', [])),
        'math_paragraphs': sum(bool(item.get('has_math')) for item in extracted.get('document', [])),
        'image_paragraphs': sum(bool(item.get('has_image')) for item in extracted.get('document', [])),
    }
    expected = _source_expectations(source_text)
    if expected['numbered_display_math'] > actual['math_paragraphs']:
        errors.append(f'math structure loss: source expects at least {expected["numbered_display_math"]}, output has {actual["math_paragraphs"]}')
    if expected['figure_objects'] > actual['image_paragraphs']:
        errors.append(f'figure structure loss: source expects at least {expected["figure_objects"]}, output has {actual["image_paragraphs"]}')

    try:
        expected_data = _load_expected_manifest(expected_manifest)
        _check_expected_manifest(expected_data, extracted, errors)
    except ValueError as exc:
        errors.append(str(exc))

    compatibility: dict[str, Any] | None = None
    if compatibility_report and compatibility_report.is_file():
        try:
            compatibility = json.loads(compatibility_report.read_text(encoding='utf-8'))
            if compatibility.get('compatible') is not True:
                errors.append('OOXML compatibility audit failed')
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f'invalid compatibility report: {exc}')

    manifest_data: dict[str, Any] | None = None
    if run_manifest is not None:
        try:
            manifest_data = json.loads(run_manifest.read_text(encoding='utf-8'))
            if not isinstance(manifest_data, dict):
                raise ValueError('manifest must be a JSON object')
            artifact_hash = ((manifest_data.get('artifact') or {}).get('sha256'))
            if artifact_hash and artifact_hash != sha256(docx):
                errors.append('run manifest artifact hash does not match DOCX')
            source_hash = (((manifest_data.get('sources') or {}).get('tex') or {}).get('sha256'))
            if source_hash and source_hash != sha256(source):
                errors.append('run manifest source hash does not match TeX')
            config_hash = (((manifest_data.get('sources') or {}).get('config') or {}).get('sha256'))
            supplied_config_hash = sha256(config)
            config_hashes = {supplied_config_hash}
            if resolved_config_hash:
                config_hashes.add(resolved_config_hash)
            input_config_hash = (((manifest_data.get('sources') or {}).get('input_config') or {}).get('sha256'))
            if input_config_hash and input_config_hash != supplied_config_hash:
                errors.append('run manifest input config hash does not match supplied config')
            if config_hash and config_hash not in config_hashes:
                errors.append('run manifest config hash matches neither supplied nor resolved config')
            if bibliography is not None:
                bib_hash = (((manifest_data.get('sources') or {}).get('bibliography') or {}).get('sha256'))
                if bib_hash and bib_hash != sha256(bibliography):
                    errors.append('run manifest bibliography hash does not match input')
            if csl is not None:
                csl_hash = (((manifest_data.get('sources') or {}).get('csl') or {}).get('sha256'))
                if csl_hash and csl_hash != sha256(csl):
                    errors.append('run manifest CSL hash does not match input')
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f'invalid conversion manifest: {exc}')

    return {
        'schema_version': '1.0',
        'valid': not errors,
        'errors': errors,
        'warnings': warnings,
        'artifact': str(docx.resolve()),
        'hashes': {
            'artifact_sha256': sha256(docx),
            'source_sha256': sha256(source),
            'config_sha256': sha256(config),
            'resolved_config_sha256': resolved_config_hash,
            'bibliography_sha256': _file_hash(bibliography),
            'csl_sha256': _file_hash(csl),
            'run_manifest_sha256': _file_hash(run_manifest),
        },
        'expected_from_source': expected,
        'actual_from_docx': actual,
        'page': page,
        'config': str(config.resolve()),
        'compatibility': compatibility,
        'extractor_manifest': extracted.get('manifest', {}),
        'package_checks': package_checks,
        'expected_manifest': str(expected_manifest.resolve()) if expected_manifest else None,
        'citation': {
            'mode': 'citeproc' if bibliography else 'legacy-bbl-or-none',
            'bibliography': str(bibliography.resolve()) if bibliography else None,
            'csl': str(csl.resolve()) if csl else None,
            'manifest': str(run_manifest.resolve()) if run_manifest else None,
            'manifest_data': manifest_data,
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('docx', type=Path)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--compatibility-report', type=Path)
    parser.add_argument('--expected', type=Path,
                        help='independently reviewed expected output manifest')
    parser.add_argument('--bibliography', type=Path)
    parser.add_argument('--csl', type=Path)
    parser.add_argument('--manifest', type=Path,
                        help='conversion manifest to verify against the artifact and inputs')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    result = validate(
        args.docx, args.source, args.config, args.compatibility_report,
        args.expected, args.bibliography, args.csl, args.manifest,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['valid'] else 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
