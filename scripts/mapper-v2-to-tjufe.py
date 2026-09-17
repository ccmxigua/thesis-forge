#!/usr/bin/env python3
"""Map one resolved V2 configuration to the Pandoc/Lua metadata contract."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


def load(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f'cannot read config as UTF-8: {path}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{path} must contain a YAML mapping')
    return value


def _rows(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    if not isinstance(node, list):
        raise ValueError('info_fields must be a list')
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(node):
        if not isinstance(row, dict):
            raise ValueError(f'info_fields[{index}] must be a mapping')
        if 'label' not in row or 'value' not in row:
            raise ValueError(f'info_fields[{index}] requires label and value')
        rows.append({'label': row.get('label', ''), 'value': row.get('value', '')})
    return rows


def _rows_defined(node: Any) -> bool:
    """Distinguish an explicit empty list from the schema's blank placeholder."""
    if not isinstance(node, list):
        raise ValueError('info_fields must be a list')
    if not node:
        return True
    return not (
        len(node) == 1
        and isinstance(node[0], dict)
        and node[0].get('label', '') == ''
        and node[0].get('value', '') == ''
    )


def _put_nonempty(out: dict[str, Any], key: str, value: Any) -> None:
    """Do not let blank config defaults erase explicit source metadata."""
    if isinstance(value, str):
        if value != '':
            out[key] = value
    elif value is not None:
        out[key] = value


def map_config(config: dict[str, Any], source: str = '') -> dict[str, Any]:
    """Produce the intentionally small metadata interface consumed by Lua."""
    metadata = config.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise ValueError('metadata must be a mapping')
    cover = config.get('cover') or {}
    title_page = config.get('title_page') or {}
    toc = config.get('toc') or {}
    acknowledgements = config.get('acknowledgements') or {}
    signature = acknowledgements.get('signature') or {}

    out: dict[str, Any] = {
        'insert_cover': bool(cover.get('enabled', False)),
        # Keep an explicit empty list distinguishable from the legacy absence
        # of this key.  Lua may only synthesize the old degree line when the
        # caller did not provide cover.top_lines at all.
        'cover_top_lines': cover.get('top_lines') if isinstance(cover.get('top_lines'), list) else [],
        'cover_top_lines_defined': 'top_lines' in cover,
        'cover_info_rows': _rows(cover.get('info_fields')),
        'cover_info_rows_defined': _rows_defined(cover.get('info_fields')),
        'cover_subtitle_prefix': cover.get('subtitle_prefix', '——'),
        'insert_title_page': bool(title_page.get('enabled', False)),
        'title_page_info_rows': _rows(title_page.get('info_fields')),
        'title_page_info_rows_defined': _rows_defined(title_page.get('info_fields')),
        'insert_toc': bool(toc.get('enabled', False)),
        # This is an explicit value, including false.  Lua must not infer it
        # from author/date fields.
        'ack_signature_enabled': bool(signature.get('enabled', False)),
        'ack_signature_name': metadata.get('author', ''),
        'ack_signature_date': metadata.get('submit_date', ''),
        'font_size_body': (config.get('font_sizes') or {}).get('body', 12),
        'tjufe_config_source': source or 'resolved-config',
    }

    for key, value in {
        'author': metadata.get('author', ''),
        'advisor': metadata.get('advisor', ''),
        'co_advisor': metadata.get('co_advisor', ''),
        'student_id': metadata.get('student_id', ''),
        'cn_title': metadata.get('title', ''),
        'en_title': metadata.get('title_en', ''),
        'cn_subtitle': metadata.get('subtitle', ''),
        'en_subtitle': metadata.get('subtitle_en', ''),
        'submit_date_cn': metadata.get('submit_date', ''),
        'class_no': metadata.get('classification_number', ''),
        'udc': metadata.get('udc', ''),
        'confidentiality': metadata.get('confidentiality_level', ''),
        'discipline': metadata.get('major', ''),
        'college': metadata.get('school', ''),
        'research_direction': metadata.get('research_direction', ''),
        'degree_level': metadata.get('degree_level', ''),
        'degree_type': title_page.get('degree_line', '') or metadata.get('degree_display', ''),
        'cover_degree_line': title_page.get('degree_line', ''),
        'ack_signature_name': metadata.get('author', ''),
        'ack_signature_date': metadata.get('submit_date', ''),
    }.items():
        _put_nonempty(out, key, value)

    # Doctor-specific fields are opt-in.  A populated author/major pair is not
    # evidence that the thesis uses the doctoral title-page template.
    if metadata.get('degree_level') == 'doctor':
        doctor_values = {
            'doctoral_subject': metadata.get('major', ''),
            'doctoral_research_direction': metadata.get('research_direction', ''),
            'doctoral_student_name': metadata.get('author', ''),
            'doctoral_defense_date': metadata.get('defense_date', ''),
            'doctoral_degree_date': metadata.get('degree_conferral_date', ''),
            'doctoral_apply_degree': metadata.get('degree_display', ''),
            'doctoral_college': metadata.get('school', ''),
            'doctoral_major': metadata.get('major', ''),
        }
        for key, value in doctor_values.items():
            _put_nonempty(out, key, value)

    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('output', nargs='?', type=Path)
    args = parser.parse_args(argv)
    mapped = map_config(load(args.config), str(args.config.resolve()))
    stream = sys.stdout if args.output is None else args.output.open('w', encoding='utf-8')
    try:
        yaml.safe_dump(mapped, stream, allow_unicode=True, default_flow_style=False, sort_keys=False, width=120)
    finally:
        if args.output is not None:
            stream.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
