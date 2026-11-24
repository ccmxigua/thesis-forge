"""Regression for a live child process, not a trusted caller PDF."""
from hashlib import sha256
from pathlib import Path
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from zipfile import ZipFile
import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
SCRIPT = ROOT/'scripts/reference_leaf_correction.py'
CASE = os.environ.get('THESIS_EXACT_SYNTHETIC_REFLEAF')
CASE_SHA = '624f73fbade784668206ecb573443fee433827aed3b8d5137d7a8f9b716c759e'
FIX_SHA = 'bc81754203649fd871175045d3fbe757f71c2157fc82af0e83542fbe2e7a9881'

def h(raw):
    return sha256(raw).hexdigest()

class ReferenceLeafCorrection(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.tmp=Path(t.name)
        self.candidate=self.tmp/'separate'
    def invoke(self,source,extra=(),env=None):
        command=[sys.executable,'-B',str(SCRIPT),'--input',str(source),'--expect-input-sha256',CASE_SHA,
                 '--expect-page','4','--expect-terminal-reference','21',
                 '--output-dir',str(self.candidate)]
        return subprocess.run(command+list(extra),capture_output=True,text=True,timeout=160,env=env)
    def test_forged_prior_pdf_is_not_an_input_authority(self):
        fake=self.tmp/'fake.pdf'; fake.write_bytes(b'%PDF- pretend external ledger')
        r=self.invoke(self.tmp/'absent.docx',('--frozen-pdf',str(fake)))
        self.assertEqual(r.returncode,2,r.stderr)
        self.assertIn('unrecognized arguments',r.stderr)
        self.assertFalse(self.candidate.exists())
    def test_unbound_bytes_never_launch_a_renderer(self):
        raw=self.tmp/'fake.docx'; raw.write_bytes(b'not the approved digest')
        r=self.invoke(raw)
        self.assertEqual(r.returncode,2,r.stderr)
        self.assertIn('input SHA-256 differs',r.stderr)
        self.assertFalse(self.candidate.exists())
    def test_unrelated_footer_graphics_are_not_ignored_as_renumbering(self):
        from reference_leaf_correction import verify_layout_change
        def make(n, add_graphic):
            f=fitz.open()
            for i in range(n):
                page=f.new_page(width=595,height=842)
                if i==0: page.insert_text((45,200),'[1] Same source')
                if i==n-1: page.insert_text((60,200),'附录',fontname='china-s')
                page.insert_text((300,800),str(i+1))
                if i==n-1 and add_graphic:
                    page.draw_rect(fitz.Rect(65,780,88,802),color=(0,0,0),fill=(0,0,0))
            result=f.tobytes();f.close();return result
        before,after=make(3,False),make(2,True)
        with self.assertRaisesRegex(ValueError,'unrelated subsequent page'):
            verify_layout_change(before,after,2)

    def test_graphic_inside_footer_digit_region_not_masked(self):
        from reference_leaf_correction import verify_layout_change
        def make(count, add_graphic):
            d=fitz.open()
            for i in range(count):
                page=d.new_page(width=595,height=842)
                if i==count-1:page.insert_text((60,200),'附录',fontname='china-s')
                page.insert_text((300,800),str(i+1))
                if i==count-1 and add_graphic:
                    page.draw_rect(fitz.Rect(301,796,304,799),color=(1,0,0),fill=(1,0,0))
            raw=d.tobytes();d.close();return raw
        with self.assertRaisesRegex(ValueError,'footer number overlaps'):
            verify_layout_change(make(3,False),make(2,True),2)

    def test_padded_footer_mask_refuses_thin_adjacent_nontext_ink(self):
        from reference_leaf_correction import numeric_footer_box,verify_layout_change
        def make(n, ink):
            d=fitz.open()
            for idx in range(n):
                p=d.new_page(width=595,height=842)
                if idx==n-1:p.insert_text((60,200),'附录',fontname='china-s')
                label=str(idx+1);p.insert_text((300,800),label)
                if idx==n-1 and ink:
                    b=numeric_footer_box(p,label)
                    p.draw_line((b.x0+1,b.y0-.2),(b.x1-1,b.y0-.2),color=(1,0,0),width=.4)
            raw=d.tobytes();d.close();return raw
        with self.assertRaisesRegex(ValueError,'unrelated subsequent page'):
            verify_layout_change(make(3,False),make(2,True),2)

    @unittest.skipUnless(CASE,'retained authorized synthetic fixture required')
    def test_byte_identical_wrapper_in_writable_copy_is_not_trusted(self):
        projected = self.tmp/'bin'; projected.mkdir()
        shutil.copyfile(shutil.which('soffice'),projected/'soffice')
        (projected/'soffice').chmod(0o755)
        r=self.invoke(Path(CASE),env={**os.environ,'PATH':str(projected)})
        self.assertEqual(r.returncode,2,r.stderr)
        self.assertIn('writable location',r.stderr)
        self.assertFalse(self.candidate.exists())

    @unittest.skipUnless(CASE,'retained authorized synthetic fixture required')
    def test_same_shaped_wrong_main_content_type_rejected(self):
        from reference_leaf_correction import supported_docx
        from io import BytesIO
        raw=BytesIO()
        with ZipFile(Path(CASE)) as original,ZipFile(raw,'w') as rewritten:
            for name in original.namelist():
                entry=original.read(name)
                if name=='[Content_Types].xml':
                    entry=entry.replace(b'wordprocessingml.document.main+xml',b'macroEnabled.fixture.main+xml')
                rewritten.writestr(name,entry)
        with self.assertRaisesRegex(ValueError,'canonical supported DOCX'):
            supported_docx(raw.getvalue())

    @unittest.skipUnless(CASE,'retained authorized synthetic fixture required for real LibreOffice')
    def test_real_native_process_binds_both_pdfs_and_one_change(self):
        source=Path(CASE)
        self.assertEqual(h(source.read_bytes()),CASE_SHA)
        r=self.invoke(source)
        self.assertEqual(r.returncode,0,r.stderr)
        report=json.loads(r.stdout)
        self.assertEqual(report['page_counts'],[5,4])
        self.assertFalse(report['native_word_wps_verified']);self.assertFalse(report['submission_ready'])
        self.assertEqual(report['causal_proof']['terminal_reference'],21)
        self.assertEqual(report['output_docx_sha256'],FIX_SHA)
        self.assertEqual([p['input_sha256'] for p in report['renders']],[CASE_SHA,FIX_SHA])
        self.assertEqual(h((self.candidate/'before.pdf').read_bytes()),report['renders'][0]['pdf_sha256'])
        self.assertEqual(h((self.candidate/'after.pdf').read_bytes()),report['renders'][1]['pdf_sha256'])
        output=self.candidate/'reference-leaf-candidate.docx'
        self.assertEqual(h(output.read_bytes()),FIX_SHA)
        with ZipFile(output) as after,ZipFile(source) as before:
            self.assertEqual([n for n in before.namelist() if before.read(n)!=after.read(n)],['word/document.xml'])
        self.assertEqual(h(source.read_bytes()),CASE_SHA)
        repeated=self.invoke(source)
        self.assertEqual(repeated.returncode,2,repeated.stderr)
        self.assertIn('new separate output',repeated.stderr)
    @unittest.skipUnless(CASE,'retained authorized synthetic fixture required for HASH-gate attack probe')
    def test_untrusted_path_executable_is_not_invoked(self):
        path=self.tmp/'bin'; path.mkdir()
        fake=path/'soffice'; mark=self.tmp/'off-path-executed'
        fake.write_text('#!/bin/sh\ntouch '+str(mark)+'\nexit 0\n'); fake.chmod(0o755)
        r=self.invoke(Path(CASE),env={**os.environ,'PATH':str(path)})
        self.assertEqual(r.returncode,2,r.stderr)
        self.assertIn('not the pinned renderer',r.stderr)
        self.assertFalse(mark.exists());self.assertFalse(self.candidate.exists())
