"""Explicit, local LibreOffice/Java UNO PDF export. Never stores a DOCX.

An explicitly fingerprinted installation is required; no discovery, installation,
provider call, or fallback to another renderer happens here. Acceptance is only
for the bounded source/PDF navigation checks in uno_toc_evidence.
"""
from __future__ import annotations

import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

from semantic_contract import strict_json_read
from uno_toc_evidence import preflight, sha256, validate_pdf

REQUIRED_FILES = (
    'program/soffice', 'program/soffice.bin', 'program/versionrc',
    'program/libjpipe.so', 'program/libjuh.so', 'program/libmergedlo.so',
    'program/classes/libreoffice.jar', 'program/classes/unoloader.jar',
)
HELPER = Path(__file__).with_name('UnoTocClient.java')
IMPLEMENTATION = (Path(__file__), HELPER, Path(__file__).with_name('uno_toc_evidence.py'),
                  Path(__file__).with_name('toc_materializer.py'))


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _digest(value, label):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError(label + ' requires a lowercase SHA256')
    return value


def _regular(path, label):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(label + ' is not a regular file')
    return path


def runtime_profile(path):
    """Validate caller-declared identities, not an authenticity/signature claim."""
    path = _regular(path, 'runtime profile')
    raw = strict_json_read(path)
    if not isinstance(raw, dict) or set(raw) != {
        'schema_version', 'backend', 'version', 'build_id', 'office_root',
        'java_executable', 'java_sha256', 'files',
    }:
        raise ValueError('runtime profile fields are absent/unknown')
    if raw['schema_version'] != '1.0' or raw['backend'] != 'libreoffice-uno':
        raise ValueError('unsupported runtime profile protocol')
    if not isinstance(raw['version'], str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2,3}', raw['version']):
        raise ValueError('explicit exact runtime version required')
    if not isinstance(raw['build_id'], str) or not re.fullmatch('[0-9a-f]{40}', raw['build_id']):
        raise ValueError('explicit build fingerprint required')
    root = Path(raw['office_root']).expanduser().resolve(strict=True)
    if not root.is_dir() or not isinstance(raw['files'], dict) or set(raw['files']) != set(REQUIRED_FILES):
        raise ValueError('incomplete runtime component fingerprint')
    protected = {path: sha256(path)}
    for name in REQUIRED_FILES:
        item = _regular(root / name, name)
        if not item.is_relative_to(root):
            raise ValueError('runtime component escapes declared installation')
        expected = _digest(raw['files'][name], name)
        if sha256(item) != expected:
            raise ValueError('runtime component hash mismatch: ' + name)
        protected[item] = expected
    if not os.access(root / 'program/soffice', os.X_OK):
        raise ValueError('declared soffice is not executable')
    version = configparser.ConfigParser(interpolation=None)
    version.read(root / 'program/versionrc')
    if version.get('Version', 'buildid', fallback='') != raw['build_id']:
        raise ValueError('runtime build ID differs from declared profile')
    java = _regular(raw['java_executable'], 'Java executable')
    expected = _digest(raw['java_sha256'], 'Java executable')
    if sha256(java) != expected or not os.access(java, os.X_OK):
        raise ValueError('Java executable fingerprint/access mismatch')
    protected[java] = expected
    protected[HELPER.resolve()] = sha256(HELPER)
    return {**raw, 'office_root': root, 'java_executable': java,
            'profile_path': path, 'protected': protected}


def _unchanged(protected):
    for path, expected in protected.items():
        if not path.is_file() or sha256(path) != expected:
            raise ValueError('protected input/runtime changed: ' + str(path))


def _write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def _run_native(runtime, stage, input_copy, expected_hash, timeout):
    """Own one private profile/pipe and process group, no shared Office process."""
    program = runtime['office_root'] / 'program'
    nonce = 'thesis_toc_' + uuid.uuid4().hex
    profile = stage / 'office-profile'
    (stage / 'tmp').mkdir()
    env = os.environ.copy()
    env['UNO_PATH'] = str(program)
    env['LD_LIBRARY_PATH'] = str(program) + (os.pathsep + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    env['SAL_USE_VCLPLUGIN'] = 'svp'
    server_args = [str(program / 'soffice'), '--headless', '--nologo', '--nodefault',
        '--norestore', '--nofirststartwizard', '-env:UserInstallation=' + profile.as_uri(),
        '--accept=pipe,name=' + nonce + ';urp;StarOffice.ComponentContext']
    client_args = [str(runtime['java_executable']), '-Djava.library.path=' + str(program),
        '-Djava.io.tmpdir=' + str(stage / 'tmp'), '-Djava.util.prefs.userRoot=' + str(stage / 'java-prefs'),
        '--class-path', os.pathsep.join(str(program / p) for p in ('classes/libreoffice.jar', 'classes/unoloader.jar')),
        str(HELPER), 'uno:pipe,name=' + nonce + ';urp;StarOffice.ComponentContext',
        str(input_copy), str(stage / 'rendered.pdf'), str(stage / 'native.json'),
        runtime['version'], expected_hash]
    record = {'started_utc': _utc(), 'server_argv': server_args, 'client_argv': client_args,
        'transport': 'private local named pipe; no TCP listener',
        'profile': str(profile), 'global_configuration_changed': False,
        'native_execution_completed': False}
    server = None
    try:
        with (stage / 'office.stdout.log').open('w') as out, (stage / 'office.stderr.log').open('w') as err:
            server = subprocess.Popen(server_args, env=env, stdout=out, stderr=err, start_new_session=True)
            record['owned_server_pid'] = server.pid
            start = time.monotonic()
            while time.monotonic() - start < min(30, timeout):
                if server.poll() is not None:
                    raise RuntimeError('private Office exited during startup: ' + str(server.returncode))
                if list(Path('/tmp').glob('*' + nonce + '*')):
                    break
                time.sleep(.2)
            else:
                raise TimeoutError('private Office pipe startup timed out')
            with (stage / 'java.stdout.log').open('w') as stdout, (stage / 'java.stderr.log').open('w') as stderr:
                client = subprocess.run(client_args, env=env, stdout=stdout, stderr=stderr, timeout=timeout)
            record['client_returncode'] = client.returncode
            if client.returncode:
                raise RuntimeError('Java UNO client failed; preserved logs are diagnostic only')
            record['server_returncode'] = server.wait(timeout=20)
            if record['server_returncode']:
                raise RuntimeError('private Office did not close cleanly')
        native = strict_json_read(stage / 'native.json')
        record['native_execution_completed'] = True
        return native
    except BaseException as error:
        record['error_type'] = type(error).__name__
        record['error'] = str(error)
        raise
    finally:
        if server is not None and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                record['cleanup_returncode'] = server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Only the process group created by this invocation is owned.
                os.killpg(server.pid, signal.SIGKILL)
                record['cleanup_returncode'] = server.wait(timeout=10)
        record['finished_utc'] = _utc()
        _write_new(stage / 'execution.json', record)


def _publish(pairs):
    """Non-replacing publication; remove only links made by this transaction."""
    linked = []
    try:
        for stage, final in pairs:
            if os.path.lexists(final):
                raise FileExistsError('publication destination already exists: ' + str(final))
            # Register before the call: a filesystem wrapper may raise after
            # creating the link, so an after-write error must also roll back.
            linked.append((stage, final))
            os.link(stage, final)
    except BaseException:
        for stage, final in reversed(linked):
            if final.exists() and os.path.samefile(stage, final):
                final.unlink()
        raise


def export(source, baseline, work_dir, *, profile, source_sha256, baseline_sha256, timeout=150):
    """Export a new PDF after explicit in-memory update; never saves input DOCX."""
    source = _regular(source, 'source DOCX')
    baseline = _regular(baseline, 'baseline DOCX')
    source_hash = _digest(source_sha256, 'source')
    baseline_hash = _digest(baseline_sha256, 'baseline')
    if sha256(source) != source_hash or sha256(baseline) != baseline_hash:
        raise ValueError('input SHA256 does not match explicit caller identity')
    if type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError('timeout must be an integer between 1 and 900 seconds')
    runtime = runtime_profile(profile)
    implementation = {p.resolve(): sha256(p) for p in IMPLEMENTATION}
    protected = {**runtime['protected'], **implementation, source: source_hash, baseline: baseline_hash}
    plan = preflight(source, baseline)
    work_dir = Path(work_dir).expanduser().absolute()
    if os.path.lexists(work_dir):
        raise ValueError('UNO output directory must be completely new, including symlinks')
    work_dir.parent.resolve(strict=True)
    work_dir.mkdir()  # no overwrite, also rejects a concurrent creator
    work_dir = work_dir.resolve()
    stage = work_dir / '_diagnostics'
    stage.mkdir()
    started = _utc()
    try:
        _unchanged(protected)
        input_copy = stage / 'input.docx'
        with input_copy.open('xb') as output, baseline.open('rb') as original:
            shutil.copyfileobj(original, output)
        if sha256(input_copy) != baseline_hash:
            raise ValueError('staged input identity mismatch')
        _write_new(stage / 'source-plan.json', plan)
        native = _run_native(runtime, stage, input_copy, baseline_hash, timeout)
        _unchanged(protected)
        if sha256(input_copy) != baseline_hash:
            raise ValueError('native client changed staged DOCX')
        if (native.get('product', {}).get('ooSetupVersionAboutBox') != runtime['version'] or
            native.get('input_copy_sha256_before') != baseline_hash or
            native.get('input_copy_sha256_after') != baseline_hash or
            native.get('ContentIndex_count') != 1 or
            native.get('index_level') != plan['toc_depth'] or
            native.get('index_create_from_outline') is not True or
            native.get('model_disposed_without_store') is not True or
            native.get('source_store_called') is not False or
            native.get('explicit_call_returned') is not True or
            native.get('explicit_method') not in {'XRefreshable.refresh', 'XDocumentIndex.update'}):
            raise ValueError('native version/input/update/save evidence incomplete')
        before, after = native.get('bookmarks_before'), native.get('bookmarks_after')
        if (not isinstance(before, dict) or not isinstance(after, dict) or
            sorted(before) != plan['bookmark_names'] or
            any(after.get(name) != text for name, text in before.items())):
            raise ValueError('original bookmark identities/range text not retained in memory')
        pdf = stage / 'rendered.pdf'
        evidence = validate_pdf(pdf, plan, native)
        _unchanged(protected)
        report = {'protocol': 'libreoffice_uno_pdf_toc_v1', 'backend': 'libreoffice-uno',
            'status': 'local_toc_verified_review_only', 'started_utc': started, 'finished_utc': _utc(),
            'inputs': {'source': str(source), 'source_sha256': source_hash,
                       'baseline': str(baseline), 'baseline_sha256': baseline_hash},
            'protected_inputs_unchanged': True,
            'renderer': {'version': runtime['version'], 'build_id': runtime['build_id'],
                'profile_sha256': sha256(runtime['profile_path']),
                'components': {str(p): h for p, h in runtime['protected'].items()},
                'fingerprint_is_not_a_signature': True},
            'implementation_sha256': {p.name: digest for p, digest in implementation.items()},
            'PDF': {'path': str(work_dir / 'rendered.pdf'), 'bytes': pdf.stat().st_size, 'sha256': sha256(pdf)},
            'save_state': {'DOCX_saved': False, 'original_DOCX_cache_updated': False,
                           'PDF_exported_from_updated_in_memory_index': True},
            'navigation': evidence, 'native_receipt': '_diagnostics/native.json',
            'Word_WPS_verified': False, 'specified_typeface_verified': False,
            'visual_review_performed': False, 'repeatability_verified': False,
            'submission_ready': False}
        staged_report = stage / 'accepted-report.json'
        _write_new(staged_report, report)
        _publish([(pdf, work_dir / 'rendered.pdf'), (staged_report, work_dir / 'uno-render-report.json')])
        return report
    except BaseException as error:
        # A successful raw native export is not a successful backend receipt.
        failure = {'protocol': 'libreoffice_uno_pdf_toc_v1', 'status': 'blocked',
            'accepted_output_published': False, 'submission_ready': False,
            'error_type': type(error).__name__, 'error': str(error), 'finished_utc': _utc(),
            'diagnostics_only': str(stage), 'DOCX_store_requested': False}
        _write_new(work_dir / 'failure.json', failure)
        raise
