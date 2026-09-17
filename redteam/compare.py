#!/usr/bin/env python3
"""Compare independently extracted DOCX structure with a complete baseline.

The comparator is a regression gate, not a thesis correctness oracle.  It
refuses incomplete JSON inputs and treats missing structural evidence as a
failure instead of allowing a text-only fake document to pass.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CRITICAL = 'CRITICAL'
HIGH = 'HIGH'
MEDIUM = 'MEDIUM'
LOW = 'LOW'
INFO = 'INFO'


@dataclass
class Finding:
    severity: str
    check: str
    baseline: Any
    actual: Any
    note: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'severity': self.severity, 'check': self.check,
            'baseline': self.baseline, 'actual': self.actual, 'note': self.note,
        }


REQUIRED = ('styles.json', 'paragraphs.json', 'numbering.json', 'page.json', 'manifest.json')


def load_json_dir(directory: str | Path) -> dict[str, Any]:
    path = Path(directory)
    missing = [name for name in REQUIRED if not (path / name).is_file()]
    if missing:
        raise ValueError(f'incomplete extraction directory {path}; missing {", ".join(missing)}')
    data: dict[str, Any] = {}
    for filename, key in (
        ('styles.json', 'styles'), ('paragraphs.json', 'document'),
        ('numbering.json', 'numbering'), ('page.json', 'page'), ('manifest.json', 'manifest'),
    ):
        try:
            data[key] = json.loads((path / filename).read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'invalid {path / filename}: {exc}') from exc
    manifest = data['manifest']
    if manifest.get('schema_version') != '2.0':
        raise ValueError(f'{path / "manifest.json"} has unsupported schema_version')
    if not isinstance(manifest.get('xml_parts'), dict) or not manifest.get('xml_parts'):
        raise ValueError(f'{path / "manifest.json"} lacks complete XML-part hashes')
    if not isinstance(manifest.get('stories'), list):
        raise ValueError(f'{path / "manifest.json"} lacks story topology')
    return data


def compare_page(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    bp, cp = baseline.get('page', {}), candidate.get('page', {})
    if not bp.get('sections') or not cp.get('sections'):
        return [Finding(CRITICAL, 'page sections', bool(bp.get('sections')), bool(cp.get('sections')), 'all section properties are required')]
    for key in ('width', 'height'):
        if bp.get(key) is not None and cp.get(key) != bp.get(key):
            findings.append(Finding(HIGH, f'page {key}', bp.get(key), cp.get(key), 'page dimension mismatch'))
    bm, cm = bp.get('margins', {}), cp.get('margins', {})
    for key in ('top', 'bottom', 'left', 'right', 'header', 'footer', 'gutter'):
        if key in bm and cm.get(key) != bm.get(key):
            findings.append(Finding(HIGH, f'margin {key}', bm.get(key), cm.get(key), 'margin mismatch'))
    if len(bp['sections']) != len(cp['sections']):
        findings.append(Finding(HIGH, 'section count', len(bp['sections']), len(cp['sections']), 'section topology changed'))
    return findings


def compare_styles(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    bs, cs = baseline.get('styles', {}), candidate.get('styles', {})
    for style_id in sorted(set(bs) - set(cs)):
        findings.append(Finding(HIGH, 'missing style', style_id, None, 'style required by baseline is absent'))
    for style_id in sorted(set(bs) & set(cs)):
        b, c = bs[style_id], cs[style_id]
        for key in ('size_halfpt', 'bold', 'italic', 'justify'):
            if b.get(key) is not None and c.get(key) != b.get(key):
                findings.append(Finding(MEDIUM, f'style {style_id}.{key}', b.get(key), c.get(key), 'style property mismatch'))
        for key in ('ascii', 'hAnsi', 'eastAsia'):
            bv = b.get('fonts', {}).get(key)
            if bv is not None and cs[style_id].get('fonts', {}).get(key) != bv:
                findings.append(Finding(MEDIUM, f'style {style_id}.fonts.{key}', bv, cs[style_id].get('fonts', {}).get(key), 'font mismatch'))
    return findings


def _norm(text: str) -> str:
    return ''.join(text.replace('\xa0', '').split()).lower()


def _structural_evidence(document: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [item.get('text', '') for item in document]
    return {
        'paragraphs': len(document),
        'nonempty': sum(bool(_norm(text)) for text in texts),
        'math': sum(bool(item.get('has_math')) or bool('式（' in text or '式(' in text) for item, text in zip(document, texts)),
        'images': sum(bool(item.get('has_image')) or '[IMAGE]' in text for item, text in zip(document, texts)),
        'chapters': sum(_norm(text).startswith('第') and '章' in text for text in texts),
        'sections': sum(term in _norm(text) for text in texts for term in ('摘要', 'abstract', '参考文献', '后记', '目录')),
    }


def compare_document(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    bp, cp = baseline.get('document', []), candidate.get('document', [])
    be, ce = _structural_evidence(bp), _structural_evidence(cp)
    if not bp or not cp:
        findings.append(Finding(CRITICAL, 'document paragraphs', len(bp), len(cp), 'empty document evidence is not comparable'))
        return findings
    if ce['nonempty'] == 0 or ce['chapters'] == 0:
        findings.append(Finding(CRITICAL, 'candidate structural evidence', be, ce, 'candidate has no independently readable chapter/content structure'))
    if be['chapters'] > 0 and ce['chapters'] < be['chapters']:
        findings.append(Finding(HIGH, 'chapter count', be['chapters'], ce['chapters'], 'candidate lost chapter headings'))
    for key in ('math', 'images'):
        if be[key] > 0 and ce[key] < be[key]:
            findings.append(Finding(HIGH, f'{key} evidence', be[key], ce[key], f'candidate lost {key} structure'))
    if abs(be['paragraphs'] - ce['paragraphs']) > max(20, be['paragraphs'] // 2):
        findings.append(Finding(HIGH, 'paragraph count', be['paragraphs'], ce['paragraphs'], 'large structural loss'))
    baseline_terms = {_norm(term): term for term in ('摘要', 'Abstract', '目录', '参考文献', '后记') if any(_norm(term) in _norm(text) for text in [item.get('text', '') for item in bp])}
    candidate_text = [_norm(item.get('text', '')) for item in cp]
    for normalized, term in baseline_terms.items():
        if not any(normalized in text for text in candidate_text):
            findings.append(Finding(HIGH, f'section {term}', 'present', 'missing', 'baseline section is absent'))
    return findings


def compare_numbering(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    bn, cn = baseline.get('numbering', {}), candidate.get('numbering', {})
    if bn and not cn:
        findings.append(Finding(HIGH, 'numbering definitions', len(bn), len(cn), 'numbering evidence missing'))
    return findings


def _config_page(config: dict[str, Any]) -> tuple[int | None, int | None, dict[str, Any]]:
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
    raw_margins = page.get('margins') or {}
    margins = {
        ('header' if key == 'header_distance' else 'footer' if key == 'footer_distance' else key): value
        for key, value in raw_margins.items()
        if key in {'top', 'bottom', 'left', 'right', 'header_distance', 'footer_distance', 'gutter'}
    }
    return width, height, margins


def compare_config(candidate: dict[str, Any], config: dict[str, Any]) -> list[Finding]:
    """Check the candidate against the effective config supplied for this run."""
    findings: list[Finding] = []
    expected_width, expected_height, expected_margins = _config_page(config)
    page = candidate.get('page', {})
    if expected_width is not None and page.get('width') != expected_width:
        findings.append(Finding(HIGH, 'resolved config page width', expected_width, page.get('width'), 'candidate does not consume the supplied config'))
    if expected_height is not None and page.get('height') != expected_height:
        findings.append(Finding(HIGH, 'resolved config page height', expected_height, page.get('height'), 'candidate does not consume the supplied config'))
    sections = page.get('sections') or []
    if not sections:
        findings.append(Finding(CRITICAL, 'resolved config section evidence', True, False, 'candidate has no section evidence'))
    for index, section in enumerate(sections):
        if expected_width is not None and section.get('width') != expected_width:
            findings.append(Finding(HIGH, f'resolved config section {index}.width', expected_width, section.get('width'), 'section does not consume the supplied config'))
        if expected_height is not None and section.get('height') != expected_height:
            findings.append(Finding(HIGH, f'resolved config section {index}.height', expected_height, section.get('height'), 'section does not consume the supplied config'))
        for key, expected in expected_margins.items():
            if section.get('margins', {}).get(key) != expected:
                findings.append(Finding(HIGH, f'resolved config section {index}.margin {key}', expected, section.get('margins', {}).get(key), 'section margin does not consume the supplied config'))
    return findings


def compare_all(
    baseline: dict[str, Any], candidate: dict[str, Any], config: dict[str, Any] | None = None
) -> list[Finding]:
    findings = compare_page(baseline, candidate) + compare_styles(baseline, candidate) + \
        compare_numbering(baseline, candidate) + compare_document(baseline, candidate)
    if config is not None:
        findings.extend(compare_config(candidate, config))
    return findings


def generate_report(
    findings: list[Finding], baseline_dir: str | Path, candidate_dir: str | Path,
    out_path: str | Path | None = None, config_path: str | Path | None = None,
) -> str:
    order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    findings = sorted(findings, key=lambda item: (order.get(item.severity, 9), item.check))
    counts = {severity: sum(item.severity == severity for item in findings) for severity in (CRITICAL, HIGH, MEDIUM, LOW, INFO)}
    verdict = 'FAIL' if counts[CRITICAL] or counts[HIGH] else ('PASS WITH WARNINGS' if counts[MEDIUM] else 'PASS')
    lines = [
        '# DOCX Compliance Report', '', f'- Baseline: `{baseline_dir}`', f'- Candidate: `{candidate_dir}`',
        *([f'- Effective config: `{config_path}`'] if config_path is not None else []), '',
        f'**Verdict: {verdict}**', '', '| Severity | Count |', '|---|---:|',
    ]
    lines.extend(f'| {severity} | {counts[severity]} |' for severity in (CRITICAL, HIGH, MEDIUM, LOW, INFO) if counts[severity])
    lines.extend(['', '## Findings', ''])
    for item in findings:
        lines.append(f'- **{item.severity}** `{item.check}`: baseline={item.baseline!r}; actual={item.actual!r}. {item.note}')
    report = '\n'.join(lines) + '\n'
    if out_path is not None:
        Path(out_path).write_text(report, encoding='utf-8')
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline')
    parser.add_argument('candidate')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--config', type=Path, help='effective YAML config whose page settings must match the candidate')
    args = parser.parse_args(argv)
    baseline = load_json_dir(args.baseline)
    candidate = load_json_dir(args.candidate)
    config = None
    if args.config is not None:
        try:
            config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f'invalid effective config {args.config}: {exc}') from exc
        if not isinstance(config, dict):
            raise ValueError(f'effective config {args.config} must contain a YAML mapping')
    findings = compare_all(baseline, candidate, config)
    report = generate_report(findings, args.baseline, args.candidate, args.out, args.config)
    if not args.out:
        print(report, end='')
    return 1 if any(item.severity in {CRITICAL, HIGH} for item in findings) else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
