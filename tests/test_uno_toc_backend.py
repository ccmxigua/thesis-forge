"""Pure local unit tests; Office subprocesses are blocked, native output is replayed."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

from docx import Document
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import toc_materializer as cli
import uno_toc_backend as backend
import uno_toc_evidence as evidence
from toc_navigation import _registry

FIX = Path(__file__).parent / 'fixtures/uno-toc'
W = evidence.W


def runtime(root):
    office = root / 'office'
    files = {}
    for name in backend.REQUIRED_FILES:
        path = office / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Version]\nbuildid=' + 'a' * 40 + '\n' if name.endswith('versionrc') else 'inert test component ' + name)
        files[name] = evidence.sha256(path)
    (office / 'program/soffice').chmod(0o755)
    java = root / 'java'
    java.write_text('inert test executable, never launched')
    java.chmod(0o755)
    profile = root / 'runtime.json'
    profile.write_text(json.dumps({'schema_version': '1.0', 'backend': 'libreoffice-uno',
        'version': '26.8.2.2', 'build_id': 'a' * 40, 'office_root': str(office),
        'java_executable': str(java), 'java_sha256': evidence.sha256(java), 'files': files}))
    return profile


def rewritten(source, output, part, edit):
    with ZipFile(source) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    root = etree.fromstring(members[part])
    edit(root)
    members[part] = etree.tostring(root)
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output


def replay_native(runtime, stage, input_copy, expected_hash, timeout):
    """Replay old successful PDF only; this test is not a new native run."""
    native = json.loads((FIX / 'prior-native.json').read_text())
    doc = Document(input_copy)
    names, _ = _registry(doc)
    ranges = {}
    for name, (start, end) in names.items():
        nodes = list(start.getparent().iter())
        a, b = nodes.index(start), nodes.index(end)
        ranges[name] = ''.join(n.text or '' for n in nodes[a+1:b] if n.tag == W+'t')
    native.update(bookmarks_before=ranges, bookmarks_after=ranges.copy(),
        input_copy_sha256_before=expected_hash, input_copy_sha256_after=expected_hash)
    shutil.copyfile(FIX / 'verified.pdf', stage / 'rendered.pdf')
    (stage / 'native.json').write_text(json.dumps(native))
    return native


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'input.docx'
        shutil.copyfile(FIX / 'source.docx', self.source)
        self.original = self.source.read_bytes()
        self.profile = runtime(self.root)
        self.digest = evidence.sha256(self.source)
        self.output = self.root / 'new-output'
        # Any accidental external/native call makes these pure unit tests fail.
        self.process = self.enterContext(patch.object(backend.subprocess, 'Popen', side_effect=AssertionError('no native call in unit tests')))

    def export(self, **extra):
        args = dict(profile=self.profile, source_sha256=self.digest, baseline_sha256=self.digest)
        args.update(extra)
        return backend.export(self.source, self.source, self.output, **args)

    def assert_no_accepted(self):
        self.assertFalse((self.output / 'rendered.pdf').exists())
        self.assertFalse((self.output / 'uno-render-report.json').exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.process.assert_not_called()

    def test_known_fixture_links_and_footers_independently_bind(self):
        plan = evidence.preflight(self.source, self.source)
        native = json.loads((FIX / 'prior-native.json').read_text())
        result = evidence.validate_pdf(FIX / 'verified.pdf', plan, native)
        self.assertEqual(result['page_count'], 4)
        self.assertEqual(result['actual_footer_labels'], ['i', '1', '2', '3'])
        self.assertEqual(len(result['toc_entries']), 6)
        self.assertEqual(len(result['body_references']), 14)
        self.assertEqual(result['annotation_count'], 22)
        self.assertFalse(result['specified_typeface_verified'])
        self.assertFalse(result['Word_WPS_verified'])

    def test_replayed_success_publishes_only_pdf_and_bound_report(self):
        with patch.object(backend, '_run_native', side_effect=replay_native):
            report = self.export()
        self.assertTrue(report['protected_inputs_unchanged'])
        self.assertEqual(evidence.sha256(self.output / 'rendered.pdf'), report['PDF']['sha256'])
        self.assertFalse(report['save_state']['DOCX_saved'])
        self.assertFalse(report['save_state']['original_DOCX_cache_updated'])
        self.assertFalse(report['submission_ready'])
        self.assertFalse(report['repeatability_verified'])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.process.assert_not_called()

    def test_wrong_input_identity_never_starts_or_creates_output(self):
        with self.assertRaisesRegex(ValueError, 'input SHA256'):
            self.export(source_sha256='0'*64)
        self.assertFalse(self.output.exists())
        self.assert_no_accepted()

    def test_existing_directory_and_symlink_refused_without_writes(self):
        for kind in ('directory', 'symlink'):
            with self.subTest(kind=kind):
                if kind == 'directory': self.output.mkdir()
                else: self.output.symlink_to(self.root / 'missing')
                with self.assertRaisesRegex(ValueError, 'completely new'):
                    self.export()
                self.assert_no_accepted()
                if kind == 'directory': self.output.rmdir()
                else: self.output.unlink()

    def test_runtime_hash_and_build_and_unknown_fields_block_before_render(self):
        original = self.profile.read_text()
        for mutate in (lambda d: d['files'].__setitem__('program/soffice', '0'*64),
                       lambda d: d.__setitem__('build_id', 'b'*40),
                       lambda d: d.__setitem__('unknown', True)):
            with self.subTest(mutate=mutate):
                data = json.loads(original); mutate(data); self.profile.write_text(json.dumps(data))
                with self.assertRaises(ValueError): self.export()
                self.assertFalse(self.output.exists()); self.assert_no_accepted()
        self.profile.write_text(original)

    def test_runtime_component_cannot_escape_root_via_symlink(self):
        data = json.loads(self.profile.read_text()); outside = self.root / 'outside'
        outside.write_text('inert'); component = Path(data['office_root']) / 'program/libjpipe.so'
        component.unlink(); component.symlink_to(outside)
        data['files']['program/libjpipe.so'] = evidence.sha256(outside)
        self.profile.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'escapes'): self.export()
        self.assert_no_accepted()

    def test_failed_or_timed_out_native_call_is_diagnostic_only(self):
        for error in (RuntimeError('native failed'), TimeoutError('native timeout')):
            with self.subTest(error=error), patch.object(backend, '_run_native', side_effect=error):
                with self.assertRaises(type(error)): self.export()
                failure = json.loads((self.output / 'failure.json').read_text())
                self.assertFalse(failure['accepted_output_published']); self.assert_no_accepted()
                shutil.rmtree(self.output)

    def test_version_refresh_or_bookmark_receipt_cannot_fake_success(self):
        for change in ('version', 'refresh', 'bookmarks', 'depth', 'store'):
            def fake(*args):
                n = replay_native(*args)
                if change == 'version': n['product']['ooSetupVersionAboutBox'] = '0.0.0'
                if change == 'refresh': n['explicit_call_returned'] = False
                if change == 'bookmarks': n['bookmarks_after']['AuthorSpan_B01'] = 'changed'
                if change == 'depth': n['index_level'] = 1
                if change == 'store': n['source_store_called'] = True
                return n
            with self.subTest(change=change), patch.object(backend, '_run_native', side_effect=fake):
                with self.assertRaises(ValueError): self.export()
                self.assert_no_accepted(); shutil.rmtree(self.output)

    def test_modified_staged_input_is_not_published(self):
        def fake(*args):
            native = replay_native(*args)
            args[2].write_bytes(b'changed staging copy')
            return native
        with patch.object(backend, '_run_native', side_effect=fake):
            with self.assertRaisesRegex(ValueError, 'staged DOCX'): self.export()
        self.assert_no_accepted()

    def test_missing_pdf_or_failed_pdf_verification_never_publishes(self):
        with patch.object(backend, '_run_native', side_effect=replay_native), patch.object(backend, 'validate_pdf', side_effect=ValueError('bad link')):
            with self.assertRaisesRegex(ValueError, 'bad link'): self.export()
        self.assert_no_accepted()

    def test_second_publish_failure_removes_first_link_and_keeps_diagnostics(self):
        link = os.link
        def fail_second(source, destination):
            if Path(destination).name == 'uno-render-report.json': raise PermissionError('injected publish failure')
            return link(source, destination)
        with patch.object(backend, '_run_native', side_effect=replay_native), patch.object(backend.os, 'link', side_effect=fail_second):
            with self.assertRaises(PermissionError): self.export()
        self.assert_no_accepted()
        self.assertTrue((self.output / '_diagnostics/rendered.pdf').is_file())
        self.assertTrue((self.output / 'failure.json').is_file())

    def test_every_publish_after_write_failure_removes_both_receipts(self):
        original_link = os.link
        for name in ('rendered.pdf', 'uno-render-report.json'):
            def fail_after(source, destination):
                result = original_link(source, destination)
                if Path(destination).name == name: raise OSError('injected after-write failure')
                return result
            with self.subTest(name=name), patch.object(backend, '_run_native', side_effect=replay_native), patch.object(backend.os, 'link', side_effect=fail_after):
                with self.assertRaisesRegex(OSError, 'after-write'): self.export()
                self.assert_no_accepted()
                failure = json.loads((self.output / 'failure.json').read_text())
                self.assertFalse(failure['accepted_output_published'])
                shutil.rmtree(self.output)

    def test_native_unavailable_input_protection_examples(self):
        def change_part(edit, part='word/document.xml'):
            output = self.root / 'changed.docx'
            return rewritten(FIX / 'source.docx', output, part, edit)
        def locked(root):
            n = next(n for n in root.iter(W+'fldChar') if n.get(W+'fldCharType') == 'begin')
            n.set(W+'fldLock', 'true')
        def revision(root):
            node = etree.SubElement(next(root.iter(W+'pPr')), W+'pPrChange')
            node.set(W+'id', '1')
        def math(root): etree.SubElement(next(root.iter(W+'p')), evidence.M+'oMath')
        def unknown(root):
            roots, _ = evidence.collect_field_ranges(SimpleNamespace(_element=SimpleNamespace(body=root.find(W+'body'))))
            frame = next(f for f in evidence.flatten_fields(roots) if f['opcode'] == 'REF')
            for index, node in enumerate(frame['instruction_nodes']):
                node.text = ' INCLUDETEXT "other.docx" ' if index == 0 else ''
        for edit in (locked, revision, math, unknown):
            with self.subTest(edit=edit):
                path = change_part(edit)
                before = path.read_bytes()
                with self.assertRaises(ValueError): evidence.preflight(path, path)
                self.assertEqual(path.read_bytes(), before)
        self.process.assert_not_called()

    def test_ambiguous_heading_or_invalid_bookmark_never_infers_a_target(self):
        def duplicate(root):
            doc = Document(FIX / 'source.docx')
            p = doc.paragraphs[14]._p
            import copy
            node = copy.deepcopy(p)
            for mark in list(node.iter(W+'bookmarkStart')) + list(node.iter(W+'bookmarkEnd')):
                mark.getparent().remove(mark)
            root.find(W+'body').insert(-1, node)
        def broken(root):
            mark = next(root.iter(W+'bookmarkEnd')); mark.getparent().remove(mark)
        for edit in (duplicate, broken):
            with self.subTest(edit=edit):
                path = rewritten(FIX/'source.docx', self.root/'changed.docx', 'word/document.xml', edit)
                with self.assertRaises(ValueError): evidence.preflight(path, path)
        self.process.assert_not_called()

    def test_native_index_wrong_page_or_wrong_title_rejected(self):
        plan = evidence.preflight(self.source, self.source)
        for field, value in [('index_text_after_call', 'wrong\t1'),
                             ('index_text_after_call', json.loads((FIX/'prior-native.json').read_text())['index_text_after_call'].replace('\t1', '\t9'))]:
            native = json.loads((FIX/'prior-native.json').read_text()); native[field] = value
            with self.assertRaises(ValueError): evidence.validate_pdf(FIX/'verified.pdf', plan, native)

    def test_own_process_timeout_preserves_execution_failure_and_cleans_only_owned_group(self):
        rt = backend.runtime_profile(self.profile)
        stage = self.root / 'process-test'; stage.mkdir()
        owned = unittest.mock.Mock(pid=12345)
        owned.poll.return_value = None
        owned.wait.return_value = -15
        with patch.object(backend.subprocess, 'Popen', return_value=owned) as launch, \
             patch.object(backend.subprocess, 'run', side_effect=backend.subprocess.TimeoutExpired('java', 1)), \
             patch.object(backend.Path, 'glob', return_value=[Path('/tmp/owned-test-pipe')]), \
             patch.object(backend.os, 'killpg') as cleanup:
            with self.assertRaises(backend.subprocess.TimeoutExpired):
                backend._run_native(rt, stage, self.source, self.digest, 1)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        cleanup.assert_called_once_with(12345, backend.signal.SIGTERM)
        log = json.loads((stage / 'execution.json').read_text())
        self.assertFalse(log['native_execution_completed'])
        self.assertEqual(log['error_type'], 'TimeoutExpired')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_external_relationship_is_rejected_before_native_load(self):
        def external(root):
            node = etree.SubElement(root, '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')
            node.set('Id', 'rIdExternalTest'); node.set('TargetMode', 'External')
            node.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink')
            node.set('Target', 'https://example.invalid/no-network-test')
        path = rewritten(FIX/'source.docx', self.root/'external.docx', 'word/_rels/document.xml.rels', external)
        with self.assertRaisesRegex(ValueError, 'external document relationship'): evidence.preflight(path, path)
        self.process.assert_not_called()

    def test_partial_heading_bookmark_is_not_verified_by_whole_heading_destination(self):
        doc = Document(self.source)
        paragraph = doc.paragraphs[14]
        node = next(n for n in paragraph._p.iter(W+'t') if '[H01]' in (n.text or ''))
        # Preserve all heading text, but move its last token into its own run.
        original = node.text
        self.assertTrue(original.endswith('[H01]'))
        node.text = original[:-5]
        start = etree.SubElement(paragraph._p, W+'bookmarkStart')
        start.set(W+'name', 'PartialHeadingToken'); start.set(W+'id', '987')
        paragraph.add_run('[H01]')
        end = etree.SubElement(paragraph._p, W+'bookmarkEnd'); end.set(W+'id', '987')
        link = next(doc._element.body.iter(W+'hyperlink'))
        link.set(W+'anchor', 'PartialHeadingToken')
        changed = self.root/'partial-heading.docx'; doc.save(changed)
        before = changed.read_bytes()
        with self.assertRaisesRegex(ValueError, 'partial heading bookmark'):
            evidence.preflight(changed, changed)
        self.assertEqual(changed.read_bytes(), before)
        self.process.assert_not_called()

    def test_implementation_identity_is_captured_and_checked_around_native_execution(self):
        # Change a scratch stand-in, never a real source module.
        implementation = self.root/'implementation.py'; implementation.write_text('before')
        def fake(*args):
            native = replay_native(*args)
            implementation.write_text('after')
            return native
        with patch.object(backend, 'IMPLEMENTATION', (implementation,)), patch.object(backend, '_run_native', side_effect=fake):
            with self.assertRaisesRegex(ValueError, 'protected input/runtime changed'): self.export()
        self.assert_no_accepted()


class CliTests(unittest.TestCase):
    def test_default_and_explicit_legacy_route_unchanged(self):
        for flags in ([], ['--backend', 'pdf-cache']):
            with patch.object(cli, 'materialize', return_value={'legacy': True}) as old, patch.object(backend, 'export') as new, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['source.docx','baseline.docx','out.docx','--work-dir','new']+flags), 0)
                self.assertEqual(old.call_count, 1); new.assert_not_called()
                self.assertEqual(old.call_args.kwargs, {'max_iterations': 8, 'timeout': 900})

    def test_uno_requires_explicit_profile_hashes_and_no_docx_output(self):
        for args in (['s','b','--backend','libreoffice-uno','--work-dir','new'],
                     ['s','b','out','--backend','libreoffice-uno','--work-dir','new','--runtime-profile','r','--source-sha256','0'*64,'--baseline-sha256','0'*64],
                     ['s','b','out','--work-dir','new','--source-sha256','0'*64]):
            with self.subTest(args=args), patch.object(backend, 'export') as new, patch.object(cli, 'materialize') as old, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error: cli.main(args)
                self.assertEqual(error.exception.code, 2); new.assert_not_called(); old.assert_not_called()

    def test_explicit_uno_calls_only_optional_backend_and_propagates_failure(self):
        args = ['s','b','--backend','libreoffice-uno','--work-dir','new','--runtime-profile','r',
                '--source-sha256','0'*64,'--baseline-sha256','1'*64]
        for fail in (False, True):
            with self.subTest(fail=fail), patch.object(backend, 'export', **({'side_effect': ValueError('blocked')} if fail else {'return_value': {'optional': True}})) as new, patch.object(cli, 'materialize') as old, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 2 if fail else 0)
                self.assertEqual(new.call_count, 1); old.assert_not_called()


if __name__ == '__main__': unittest.main(verbosity=2)
