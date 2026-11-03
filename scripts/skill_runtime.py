#!/usr/bin/env python3
"""Read-only capability discovery. Tool presence is not native acceptance."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

DEPENDENCIES = {'lxml': '6.1.0', 'python-docx': '1.2.0', 'PyMuPDF': '1.27.2.3',
                'Pillow': '12.3.0', 'pypdf': '6.6.2', 'PyYAML': '6.0.3'}
TOOLS = {'bash': ['--version'], 'pandoc': ['--version'], 'soffice': ['--version'], 'pdftoppm': ['-v'], 'pdftotext': ['-v']}
CAPABILITIES = {'docx', 'tex', 'legacy_doc', 'equation_geometry', 'full_render', 'wps', 'independent_review', 'word_release'}


def probe_capabilities(root: Path, requested: list[str]) -> dict:
    unknown = set(requested) - CAPABILITIES
    if unknown:
        raise ValueError(f'unknown capability: {sorted(unknown)}')
    packages, missing = {}, []
    for name, pinned in DEPENDENCIES.items():
        try:
            found = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found = None
        packages[name] = {'installed': found, 'expected': pinned, 'matches_pin': found == pinned}
        if found != pinned:
            missing.append(f'{name}=={pinned}')
    if sys.version_info < (3, 12):
        missing.append('Python >= 3.12 (validated with 3.12)')
    root = Path(root).resolve()
    for resource in ('scripts/thesis_format.py', 'schema', 'resources'):
        if not (root / resource).exists():
            missing.append(resource)
    tools = {}
    for name, flags in TOOLS.items():
        executable = shutil.which(name)
        info = {'path': executable, 'version': None}
        if executable:
            try:
                result = subprocess.run([executable, *flags], capture_output=True, text=True, timeout=10, check=False)
                info['version'] = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else None
                info['version_probe_returncode'] = result.returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                info['version_probe_error'] = str(exc)
        tools[name] = info
    capabilities = {}
    for name, needs in {'docx': [], 'tex': ['bash', 'pandoc'], 'legacy_doc': ['soffice'],
                        'equation_geometry': ['soffice', 'pdftoppm'],
                        'full_render': ['soffice', 'pdftoppm']}.items():
        absent = missing + [tool for tool in needs if not tools[tool]['path']]
        capabilities[name] = {'status': 'unavailable' if absent else 'available', 'missing': absent}
    for name in ('wps', 'independent_review'):
        capabilities[name] = {'status': 'host_check_required', 'missing': ['current host tool observation']}
    capabilities['word_release'] = {'status': 'unavailable', 'missing': ['separate supported Word renderer and verified release evidence']}
    return {'schema_version': 'thesis-skill-capabilities/v1', 'status': 'ready' if all(capabilities[name]['status'] == 'available' for name in requested) else 'blocked',
            'root': str(root), 'python': platform.python_version(), 'platform': sys.platform,
            'dependencies': packages, 'tools': tools, 'capabilities': capabilities,
            'requested': requested, 'provider_selected': None,
            'scope': 'Read-only executable/dependency inventory, not render or model verification'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--request', action='append', choices=sorted(CAPABILITIES))
    args = parser.parse_args()
    report = probe_capabilities(args.root, args.request or ['docx'])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
