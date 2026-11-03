#!/usr/bin/env python3
"""Build and verify portable, deterministic skill archives (not an installer)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

from semantic_contract import strict_json_loads

DIRECTORIES = {'scripts', 'schema', 'resources', 'filters', 'csl', 'agents', 'prompts', 'template_profiles', 'docs', 'references'}
TOP_FILES = {'SKILL.md', 'README.md', 'convert.sh', 'reference.docx', 'requirements-runtime.txt', 'skill-package.json', 'Makefile', 'pytest.ini', 'requirements-test.txt'}
MANIFEST = 'bundle-manifest.json'
# Exact UI-only normalization observed after the supported ChatGPT install.
# This is deliberately not an ignore list or an assertion of platform origin.
# Any new serialization, field, icon or source descriptor requires fresh review.
CHATGPT_UI_METADATA = {
    'source_yaml_sha256': '7e5a7df730730f029709166f1793768c1000ccf7200d5135fbe9b0a14d66c598',
    'source_yaml_size': 305,
    'installed_yaml_sha256': 'ea1a3f2466a454fba678894977a39b7011170166210399dee6ecd67f2c30a037',
    'icon_sha256': '49315dda7125ec5fc77ef5f2b96126d8b2121c6e72944b4702972e90f321d3ee',
}


def _relative(value: str) -> str:
    if not isinstance(value, str) or '\\' in value:
        raise ValueError('invalid bundle path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') or p.startswith('.') for p in path.parts):
        raise ValueError(f'unsafe bundle path: {value}')
    if path.as_posix() != value or (path.parts[0] not in DIRECTORIES and value not in TOP_FILES):
        raise ValueError(f'non-runtime include: {value}')
    return value


def _ignored(path: Path) -> bool:
    return '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo', '.tmp')


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(root: Path, name: str) -> bytes:
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'symlink resource: {name}')
    if not current.is_file():
        raise ValueError(f'missing regular resource: {name}')
    return current.read_bytes()


def build_bundle(repo_root: Path, output: Path) -> dict:
    root, output = Path(repo_root).resolve(), Path(output)
    policy = strict_json_loads(_read_regular(root, 'skill-package.json').decode('utf-8'))
    if policy.get('schema_version') != 'thesis-skill-package/v1' or not isinstance(policy.get('include'), list):
        raise ValueError('unsupported skill package policy')
    names = {'skill-package.json'}
    for value in policy['include']:
        name = _relative(value)
        path = root / name
        if path.is_symlink():
            raise ValueError(f'symlink resource: {name}')
        if path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.is_symlink():
                    raise ValueError(f'symlink resource: {child.relative_to(root)}')
                if child.is_file() and not _ignored(child.relative_to(root)):
                    names.add(_relative(child.relative_to(root).as_posix()))
        else:
            names.add(name)
    payloads = {name: _read_regular(root, name) for name in sorted(names)}
    for entry in policy.get('entrypoints', {}).values():
        if _relative(entry) not in payloads:
            raise ValueError(f'missing entrypoint: {entry}')
    manifest = {
        'schema_version': 'thesis-skill-bundle/v1', 'name': policy['name'],
        'version': policy['version'], 'entrypoints': policy.get('entrypoints', {}),
        'files': [{'path': name, 'sha256': _sha(data), 'size': len(data)} for name, data in payloads.items()],
    }
    payloads[MANIFEST] = _json_bytes(manifest)
    # Validate and collect every source before touching an existing destination.
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.skill-bundle-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(payloads.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
                archive.writestr(info, data)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'status': 'built', 'archive': str(output), 'sha256': _sha(output.read_bytes()), 'manifest': manifest}


def _accept_observed_chatgpt_ui(root: Path, manifest: dict, report: dict) -> None:
    """Recognize one exact observed pair, retaining all other integrity failures."""
    report.update(verification_mode='chatgpt_observed_ui_metadata/v1',
                  accepted_platform_changes=[], platform_origin_authenticated=False)
    if manifest['name'] != 'thesis-latex2docx':
        return
    yaml_name, icon_name = 'agents/openai.yaml', 'assets/icon.svg'
    if yaml_name not in report['mismatched'] or icon_name not in report['unexpected']:
        return
    source = next((item for item in manifest['files'] if item['path'] == yaml_name), {})
    if (source.get('sha256') != CHATGPT_UI_METADATA['source_yaml_sha256']
            or source.get('size') != CHATGPT_UI_METADATA['source_yaml_size']):
        return
    yaml_sha = _sha(_read_regular(root, yaml_name))
    icon_sha = _sha(_read_regular(root, icon_name))
    if (yaml_sha != CHATGPT_UI_METADATA['installed_yaml_sha256']
            or icon_sha != CHATGPT_UI_METADATA['icon_sha256']):
        return
    report['accepted_platform_changes'] = [
        {'path': yaml_name, 'change': 'observed_ui_normalization',
         'source_sha256': source['sha256'], 'installed_sha256': yaml_sha},
        {'path': icon_name, 'change': 'observed_default_icon',
         'source_sha256': None, 'installed_sha256': icon_sha},
    ]
    report['mismatched'].remove(yaml_name)
    report['unexpected'].remove(icon_name)


def verify_bundle(root: Path, *, installation_platform: str | None = None) -> dict:
    root = Path(root).resolve()
    report = {'status': 'blocked', 'missing': [], 'mismatched': [], 'unexpected': [], 'errors': []}
    try:
        if installation_platform not in (None, 'chatgpt'):
            raise ValueError('unsupported installation platform')
        manifest = strict_json_loads(_read_regular(root, MANIFEST).decode('utf-8'))
        if manifest.get('schema_version') != 'thesis-skill-bundle/v1':
            raise ValueError('unsupported bundle manifest')
        expected = set()
        for item in manifest['files']:
            name = _relative(item['path'])
            if name in expected:
                raise ValueError(f'duplicate resource: {name}')
            expected.add(name)
            if not (root / name).exists() and not (root / name).is_symlink():
                report['missing'].append(name)
                continue
            data = _read_regular(root, name)
            if _sha(data) != item['sha256'] or len(data) != item['size']:
                report['mismatched'].append(name)
        actual = {p.relative_to(root).as_posix() for p in root.rglob('*')
                  if (p.is_file() or p.is_symlink()) and not _ignored(p.relative_to(root))}
        report['unexpected'] = sorted(actual - expected - {MANIFEST})
        report['name'], report['version'] = manifest['name'], manifest['version']
        report['manifest_sha256'] = _sha((root / MANIFEST).read_bytes())
        report['byte_identical_to_bundle'] = not any(
            report[key] for key in ('missing', 'mismatched', 'unexpected', 'errors'))
        if installation_platform == 'chatgpt':
            _accept_observed_chatgpt_ui(root, manifest, report)
        if not any(report[key] for key in ('missing', 'mismatched', 'unexpected', 'errors')):
            report['status'] = 'pass'
        report['payload_integrity'] = 'exact' if report['status'] == 'pass' else 'blocked'
    except (ValueError, KeyError, TypeError, OSError) as exc:
        report['errors'].append(str(exc))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    build = subs.add_parser('build')
    build.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument('--output', type=Path, required=True)
    verify = subs.add_parser('verify')
    verify.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    verify.add_argument('--installation-platform', choices=['chatgpt'],
                        help='recognize only the exact observed ChatGPT UI metadata pair; does not prove installation')
    args = parser.parse_args()
    report = (build_bundle(args.root, args.output) if args.command == 'build'
              else verify_bundle(args.root, installation_platform=args.installation_platform))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
