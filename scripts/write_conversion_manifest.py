#!/usr/bin/env python3
"""Write an atomic, source/config/toolchain manifest for one conversion."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from semantic_contract import strict_json_read


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version(executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), '--version'], check=False,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0] if text else None


def digest_or_none(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).resolve()
    return sha256(path) if path.is_file() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--input-config', type=Path)
    parser.add_argument('--dependencies', type=Path)
    parser.add_argument('--bibliography', type=Path)
    parser.add_argument('--csl', type=Path)
    parser.add_argument('--pandoc', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    args = parser.parse_args(argv)

    sources = {
        'tex': args.input.resolve(),
        'config': args.config.resolve(),
        'input_config': args.input_config.resolve() if args.input_config else None,
        'bibliography': args.bibliography.resolve() if args.bibliography else None,
        'csl': args.csl.resolve() if args.csl else None,
    }
    data = {
        'schema_version': '1.0',
        'pipeline': 'thesis-forge',
        'citation_mode': 'citeproc' if args.bibliography else 'legacy-bbl-or-none',
        'sources': {
            key: {'path': str(path), 'sha256': sha256(path)} if path is not None and path.is_file() else None
            for key, path in sources.items()
        },
        'toolchain': {
            'python': {'path': str(args.python.resolve()), 'version': tool_version(args.python)},
            'pandoc': {'path': str(args.pandoc.resolve()), 'version': tool_version(args.pandoc)},
        },
        'artifact': {
            'path': str(args.output.resolve()),
            'sha256': sha256(args.output.resolve()),
        },
    }
    if args.dependencies is not None:
        try:
            data['source_dependencies'] = strict_json_read(args.dependencies)
        except (OSError, ValueError) as exc:
            raise ValueError(f'cannot read source dependency manifest: {exc}') from exc

    manifest = args.manifest.resolve()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f'.{manifest.name}-', suffix='.tmp', dir=str(manifest.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temp_path, manifest)
    finally:
        temp_path.unlink(missing_ok=True)
    print(str(manifest))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
