"""Scoped synthetic protocol: real DOCX/PDF bytes, no operational campaign."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile
import fitz
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from current_session import prepare_session
from semantic_contract import sha256_file
from session_review import issue_review, render_inventory_sha256
from validation_budget import initialize_campaign, reserve_operation, record_operation, atomic_json

def hash_tree(base):
    return {p.relative_to(base).as_posix():sha256_file(p) for p in base.rglob('*') if p.is_file()}

class GuardedLeaf(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.work = self.root/'work'
        self.campaign = self.root/'fixture-campaign'
        initialize_campaign(self.campaign)
        req=self.root/'requirements.docx'; reqdoc=Document()
        reqdoc.add_paragraph('正文段落字体为宋体，小四号。'); reqdoc.save(req)
        source=self.root/'source.docx'; doc=Document()
        doc.add_paragraph('合成正文')
        doc.add_heading('参考文献',1)
        doc.add_paragraph('[1] 合成书目 A')
        self.empty_index=len(doc._body._element)-1
        doc.add_paragraph().paragraph_format.page_break_before=False
        doc.add_section(WD_SECTION.NEW_PAGE)
        doc.add_heading('附录',1).paragraph_format.page_break_before=True
        doc.add_paragraph('合成附录正文'); doc.save(source)
        prep=prepare_session(req,source,self.work,self.campaign,author_task_id='synthetic-fixture-author')
        self.assertFalse(prep['blocked_reasons'])
        # Deliberately a local protocol fixture, not a production generation.
        self.docx=self.work/'fixture.docx'; self.docx.write_bytes(source.read_bytes())
        manifest_file=self.work/'pipeline-manifest.json'; m=json.loads(manifest_file.read_bytes())
        m.update(output=str(self.docx),output_artifact={'sha256':sha256_file(self.docx)})
        atomic_json(manifest_file,m)
        self.run_id=m['run_id']

    def record_visual(self, *, ordinary=False):
        rd=self.work/'render'; rd.mkdir()
        f=fitz.open()
        prior=f.new_page(width=595,height=842); prior.insert_text((60,220),'[1] A synthetic citation')
        leaf=f.new_page(width=595,height=842)
        leaf.insert_text((60,35),'参考文献',fontname='china-s')
        leaf.insert_text((320,797),'2')
        if ordinary: leaf.insert_text((60,205),'Ordinary Body')
        appendix=f.new_page(width=595,height=842)
        appendix.insert_text((60,200),'附录',fontname='china-s')
        pdf=rd/'fixture.pdf'; f.save(pdf)
        pages=[]
        for i,p in enumerate(f,1):
            im=rd/f'page-{i:04}.png'; p.get_pixmap().save(im)
            pages.append({'page':i,'path':im.relative_to(self.work).as_posix(),'sha256':sha256_file(im)})
        f.close()
        inv={'candidate_sha256':sha256_file(self.docx),'operation_id':'isolated-protocol-fixture-render',
             'pages':pages,'pdf':{'path':pdf.relative_to(self.work).as_posix(),'sha256':sha256_file(pdf)}}
        self.inventory_sha=render_inventory_sha256(self.work,inv)
        atomic_json(rd/'render-binding.json',inv)
        self.assertTrue(reserve_operation(self.campaign,self.run_id,'candidate','isolated-protocol-fixture-candidate')['can_execute'])
        self.assertTrue(reserve_operation(self.campaign,self.run_id,'full_render',inv['operation_id'])['can_execute'])
        record_operation(self.campaign,inv['operation_id'],'completed',{'evidence_refs':['test-fixture-only'], 'candidate_sha256':inv['candidate_sha256'],'pdf_sha256':inv['pdf']['sha256'],'page_inventory_sha256':self.inventory_sha})
        self.task=issue_review(self.work,'visual')
        with ZipFile(self.docx) as z:
            body=etree.fromstring(z.read('word/document.xml')).find(qn('w:body'))
        paragraph=body[self.empty_index]
        self.approval={'schema':'thesis-reference-leaf-diagnostic-scope/v1','run_id':self.run_id,
          'candidate_sha256':inv['candidate_sha256'],'pdf_sha256':inv['pdf']['sha256'],
          'page_inventory_sha256':self.inventory_sha,'visual_review_task_id':self.task['task_id'],
          'visual_artifact_manifest_sha256':self.task['artifact_manifest_sha256'],
          'physical_page':2,'terminal_reference':1,'body_child_index':self.empty_index,
          'paragraph_c14n_sha256':sha256(etree.tostring(paragraph,method='c14n',exclusive=True,with_comments=True)).hexdigest()}

    def command(self,*flags):
        return subprocess.run([sys.executable,'-B',str(ROOT/'scripts/guarded_reference_leaf.py'),*flags,'--work',str(self.work)],capture_output=True,text=True)


    def test_exact_frozen_protocol_yields_a_read_only_hint_never_renderer_attestation(self):
        self.record_visual()
        before=hash_tree(self.work); ledger=hash_tree(self.campaign)
        result=self.command('diagnose'); self.assertEqual(result.returncode,0,result.stderr)
        hint=json.loads(result.stdout)
        self.assertEqual(hint['status'],'human_review')
        self.assertEqual(hint['diagnostic_scope'],self.approval)
        self.assertFalse(hint['renderer_provenance_verified'])
        self.assertFalse(hint['repair_authorized'])
        self.assertFalse(hint['submission_ready'])
        self.assertEqual(before,hash_tree(self.work)); self.assertEqual(ledger,hash_tree(self.campaign))

    def test_fabricated_fitz_pdf_and_valid_looking_ledger_cannot_launch_correction(self):
        # This exact fixture previously wrote a DOCX under the RC1 correction.
        self.record_visual()
        fake=self.root/'untrusted-approval.json'; atomic_json(fake,self.approval)
        output=self.root/'must-not-exist'
        before=hash_tree(self.work); ledger=hash_tree(self.campaign)
        r=self.command('correct','--exact-approval',str(fake),'--separate-output',str(output))
        self.assertNotEqual(r.returncode,0)
        self.assertIn('invalid choice',r.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(before,hash_tree(self.work)); self.assertEqual(ledger,hash_tree(self.campaign))

    def test_body_present_negative_has_no_scope_or_authority(self):
        self.record_visual(ordinary=True)
        result=self.command('diagnose'); self.assertEqual(result.returncode,0,result.stderr)
        hint=json.loads(result.stdout); self.assertEqual(hint['status'],'no_specific_finding')
        self.assertNotIn('diagnostic_scope',hint)

    def test_tampered_visual_page_is_refused_without_renewal(self):
        self.record_visual()
        ri=self.work/'session-reviews'; active=(ri/'active.json').read_bytes()
        page=ri/self.task['task_id']/'pages/page-0002.png'
        page.write_bytes(page.read_bytes()+b'extra')
        result=self.command('diagnose'); self.assertNotEqual(result.returncode,0)
        self.assertIn('artifact bytes',result.stderr)
        self.assertEqual((ri/'active.json').read_bytes(),active)

    def test_redirected_visual_artifact_is_refused(self):
        self.record_visual()
        external=self.root/'external.pdf'; page=self.work/'session-reviews'/self.task['task_id']/'render/document.pdf'
        external.write_bytes(page.read_bytes()); page.unlink(); page.symlink_to(external)
        result=self.command('diagnose'); self.assertNotEqual(result.returncode,0)
        self.assertIn('redirected review path',result.stderr)
