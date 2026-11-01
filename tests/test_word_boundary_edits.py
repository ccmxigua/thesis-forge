"""Boundary edit/range-consumer proofs and source-bound additive navigation."""
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from lxml import etree

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import scoped_template_profile as scoped
import toc_navigation as nav
from word_boundary_edits import apply_text_records
from word_reference_inventory import inspect_references
import tests.test_scoped_template_profile as support

W=scoped.W; MC='{http://schemas.openxmlformats.org/markup-compatibility/2006}'


def marker(p,kind,sid,name=None):
    n=OxmlElement('w:'+kind);n.set(W+'id',sid)
    if name:n.set(W+'name',name)
    p._p.append(n);return n


def wide(value='Scope',kind='bookmark',local=False):
    d=Document();a=d.add_paragraph('Before');p=d.add_paragraph(value);b=d.add_paragraph('After')
    if local:a=p;b=p
    s=marker(a,kind+'Start','7','wide'if kind=='bookmark'else None)
    if local:p._p.remove(s);p._p.insert(1 if p._p.pPr is not None else 0,s)
    marker(b,kind+'End','7')
    return d


def boundary(value,role='thesis_subtitle_en',**props):
    return support.rule(value,role,boundary_edit_policy='navigation_only',**props)


def xmlstate(d):return d._element.xml,d.settings.element.xml


class BoundaryAndReferenceTests(unittest.TestCase):
    def setUp(self):self.h=support.ScopedProfileTests();self.h.setUp()
    def tearDown(self):self.h.tearDown()
    def apply(self,*args,**kw):return self.h.apply(*args,**kw)

    def test_wide_bookmark_prefix_comment_reference_preserved_and_inverse(self):
        d=wide();p=d.paragraphs[1];n=marker(p,'commentReference','12');p._p.remove(n);p.add_run()._r.append(n);before=xmlstate(d)
        q,r=self.apply(d,[boundary('Scope',display_prefix='——')])
        self.assertEqual(q.paragraphs[1].text,'——Scope');self.assertEqual(r['boundary_text_edit_count'],1)
        self.assertTrue(r['boundary_inverse']['valid']);self.assertEqual(len(q._element.xpath('.//w:commentReference')),1)
        records=r['rules'][0]['boundary_text_edits'];apply_text_records(q,records,inverse=True)
        self.assertEqual(xmlstate(q),before)

    def test_inline_bookmark_end_offsets_follow_exact_authorized_edit(self):
        d=wide(local=True);q,r=self.apply(d,[boundary('Scope',display_prefix='——')])
        self.assertTrue(r['boundary_inverse']['valid']);self.assertEqual(q.paragraphs[1].text,'——Scope')
        self.assertEqual(len(q._element.xpath('.//w:bookmarkStart')),1)

    def test_body_level_bookmark_end_is_paired(self):
        d=wide();end=d.paragraphs[-1]._p.find(W+'bookmarkEnd');end.getparent().remove(end);d._element.body.insert(len(d._element.body)-1,end)
        q,r=self.apply(d,[boundary('Scope',display_prefix='——')]);self.assertEqual(r['rules'][0]['status'],'applied')

    def test_terminal_changes_only_final_full_stop_and_url_doi_protected(self):
        for value,expected in (('[1] A. B。','[1] A. B.'),('[2] https://example.org/a。',None),('[3] DOI:10.1000/a。',None)):
            with self.subTest(value=value):
                d=wide(value);cfg=boundary(value,'bibliography_entry',terminal={'accepted':['。','.'],'value':'.'})
                q,r=self.apply(d,[cfg]);self.assertEqual(q.paragraphs[1].text,expected or value)
                self.assertEqual(r['boundary_text_edit_count'],int(expected is not None))

    def test_local_and_other_story_text_consumers_reject(self):
        for mode in ('REF','NOTEREF','simple','split','footer','uppercase'):
            d=wide();p=d.add_paragraph('Reference ')
            if mode=='footer':p=d.sections[0].footer.paragraphs[0]
            if mode=='simple':
                n=OxmlElement('w:fldSimple');n.set(W+'instr',' REF wide ');p._p.append(n)
            else:
                support.field(p,' '+('NOTEREF'if mode=='NOTEREF'else'REF')+' '+('WIDE'if mode=='uppercase'else'wide')+' ')
                if mode=='split':
                    n=next(p._p.iter(W+'instrText'));v=n.text;n.text=v[:3];more=copy.deepcopy(n);more.text=v[3:];n.addnext(more)
            q,r=self.apply(d,[boundary('Scope',display_prefix='——')])
            self.assertEqual(r['rules'][0]['status'],'unhandled_preserved',mode)

    def test_navigation_only_consumers_allow_but_external_and_unknown_reject(self):
        for instruction,allowed in [(' PAGEREF wide \\h ',True),(' HYPERLINK \\l wide ',True),
                                    (' TOC \\b wide ',False),(' HYPERLINK "other.docx" \\l wide ',False),
                                    (' IF 1 = 1 "a" "b" ',False)]:
            d=wide();support.field(d.add_paragraph(),instruction)
            q,r=self.apply(d,[boundary('Scope',display_prefix='——')]);self.assertEqual(r['boundary_text_edit_count'],int(allowed),instruction)
        d=wide();n=OxmlElement('w:hyperlink');n.set(W+'anchor','wide');d.add_paragraph()._p.append(n)
        q,r=self.apply(d,[boundary('Scope',display_prefix='——')]);self.assertEqual(r['boundary_text_edit_count'],1)

    def test_external_markup_anchor_is_not_a_local_navigation_proof(self):
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        d=wide();rid=d.part.relate_to('https://example.org/other.docx',RT.HYPERLINK,is_external=True)
        n=OxmlElement('w:hyperlink');n.set(W+'anchor','wide');n.set('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id',rid)
        d.add_paragraph()._p.append(n)
        q,r=self.apply(d,[boundary('Scope',display_prefix='——')]);self.assertEqual(r['boundary_text_edit_count'],0)

    def test_dynamic_instruction_field_cannot_hide_text_consumer(self):
        d=wide();d.paragraphs[0]._p.find(W+'bookmarkStart').set(W+'name','wide2')
        p=d.add_paragraph();n=OxmlElement('w:fldChar');n.set(W+'fldCharType','begin');p.add_run()._r.append(n)
        n=OxmlElement('w:instrText');n.text=' REF wide';p.add_run()._r.append(n)
        support.field(p,' PAGE ','2')
        n=OxmlElement('w:fldChar');n.set(W+'fldCharType','separate');p.add_run()._r.append(n)
        p.add_run('result');n=OxmlElement('w:fldChar');n.set(W+'fldCharType','end');p.add_run()._r.append(n)
        self.assertFalse(inspect_references(d)['valid'])
        q,r=self.apply(d,[boundary('Scope',display_prefix='——')]);self.assertEqual(r['boundary_text_edit_count'],0)

    def mc_pages(self,bad=None):
        d=wide();root=d.sections[0].footer._element;ac=etree.SubElement(root,MC+'AlternateContent')
        for tag in ('Choice','Fallback'):
            branch=etree.SubElement(ac,MC+tag);p=etree.SubElement(branch,W+'p')
            # Typed paragraph wrapper only for the fixture field constructor.
            from docx.text.paragraph import Paragraph
            pp=Paragraph(p,SimpleNamespace(part=d.part));support.field(pp,' REF wide 'if bad=='REF'and tag=='Fallback'else' PAGE ')
        if bad=='cross':
            sep=next(ac.iter(W+'fldChar'))
            sep.getparent().remove(sep);ac.find(MC+'Fallback').append(sep)
        return d

    def test_mc_non_text_consumer_union_keeps_native_selection_unresolved(self):
        d=self.mc_pages();i=inspect_references(d);self.assertTrue(i['valid'],i['findings'])
        self.assertEqual(sum(r.get('display_selection_unresolved',False)for r in i['references']),2)
        q,r=self.apply(d,[boundary('Scope',display_prefix='——')]);self.assertEqual(r['boundary_text_edit_count'],1)
        self.assertFalse(r['native_verified'])

    def test_mc_ref_or_cross_branch_field_is_unresolved(self):
        for bad in ('REF','cross'):
            d=self.mc_pages(bad);self.assertFalse(inspect_references(d)['valid'])

    def test_wide_comment_permission_move_ranges_never_authorize_text_edits(self):
        for kind in ('commentRange','perm','moveFromRange','moveToRange'):
            d=wide(kind=kind);q,r=self.apply(d,[boundary('Scope',display_prefix='——')])
            self.assertEqual(r['rules'][0]['status'],'unhandled_preserved',kind)

    def test_word_story_boundaries_cannot_pair_fields(self):
        # Exercise the actual inventory with a malformed Word part, without
        # launching Office or creating an external artifact.
        d=wide();root=etree.Element(W+'footnotes',nsmap={'w':W[1:-1]})
        a=etree.SubElement(root,W+'footnote');b=etree.SubElement(root,W+'footnote')
        p=etree.SubElement(a,W+'p');r=etree.SubElement(p,W+'r')
        n=etree.SubElement(r,W+'fldChar');n.set(W+'fldCharType','begin');etree.SubElement(r,W+'instrText').text=' PAGE '
        n=etree.SubElement(r,W+'fldChar');n.set(W+'fldCharType','separate')
        n=etree.SubElement(etree.SubElement(etree.SubElement(b,W+'p'),W+'r'),W+'fldChar');n.set(W+'fldCharType','end')
        import word_reference_inventory as refs
        original=refs.roots
        with patch.object(refs,'roots',side_effect=lambda doc:list(original(doc))+[('/word/footnotes.xml',root)]):
            self.assertFalse(refs.inspect_references(d)['valid'])

    def test_complete_candidate_rejects_extra_ordinary_text_not_in_ledger(self):
        d=wide();original=scoped._copy_contents
        def corrupt(target,source):
            original(target,source)
            if scoped.text(target).startswith('——Scope'):
                next(target.iter(W+'t')).text+=' UNAUTHORIZED'
        with patch.object(scoped,'_copy_contents',side_effect=corrupt):
            with self.assertRaises(ValueError):self.apply(d,[boundary('Scope',display_prefix='——')])
        self.assertFalse(self.h.output.exists());self.assertFalse(self.h.report.exists())

    def test_prefix_and_terminal_repeat_is_noop_after_explicit_rebinding(self):
        d=wide();cfg=boundary('Scope',display_prefix='——');q,r=self.apply(d,[cfg]);cfg['selector']['text_sha256']=[support.digest('Scope'),support.digest('——Scope')]
        q,r=self.apply(q,[cfg]);self.assertEqual(r['boundary_text_edit_count'],0);self.assertEqual(q.paragraphs[1].text,'——Scope')

    def test_spacer_before_boundary_does_not_invalidate_inverse_locator(self):
        d=wide();p=d.add_paragraph('Spacer');p._p.getparent().remove(p._p);d._element.body.insert(0,p._p)
        cfg=support.rule('Spacer','heading_publications',spacer={'count':1,'paragraph':{}})
        q,r=self.apply(d,[cfg,boundary('Scope',display_prefix='——')]);self.assertTrue(r['boundary_inverse']['valid'])
        self.assertEqual(len(q.paragraphs),5)


class BoundAdditiveTests(unittest.TestCase):
    def setUp(self):self.h=support.ScopedProfileTests();self.h.setUp()
    def tearDown(self):self.h.tearDown()
    def doc(self):
        d=self.h.nav_doc(partial=True)
        # The broken old target is complete but belongs to a different heading.
        p=d.paragraphs[-3];a=marker(p,'bookmarkStart','40','missing');p._p.remove(a);p._p.insert(0,a);marker(p,'bookmarkEnd','40')
        return d
    def repair(self,d):
        return self.h.apply(d,[support.rule('Chapter One','heading_1')],repair_toc_navigation=True,navigation_target_policy='bound_heading_additive')

    def test_unique_bound_target_adds_pair_preserves_old_ranges_and_exact_inverse(self):
        d=self.doc();before=xmlstate(d);old=nav._book_records(d);q,r=self.repair(d)
        self.assertEqual(len(r['navigation']['changes']),1,r['navigation']['findings'])
        for n in old:self.assertIn(n,nav._book_records(q))
        self.assertEqual(len(nav._book_records(q)),len(old)) # Existing whole source pair may be reused.
        self.assertTrue(nav.restore_additive_navigation(q,r['navigation'])['restored']);self.assertEqual(xmlstate(q),before)

    def test_new_pair_when_no_whole_range_and_idempotence(self):
        d=self.doc();p=d.paragraphs[-2]
        for n in list(p._p):
            if n.tag in scoped.BOOKS and n.get(W+'id')=='7':p._p.remove(n)
        old=nav._book_records(d);q,r=self.repair(d)
        self.assertEqual(len(r['navigation']['changes']),1);self.assertEqual(len(nav._book_records(q)),len(old)+2)
        q,again=self.repair(q);self.assertFalse(again['navigation']['changes']);self.assertEqual(len(nav._book_records(q)),len(old)+2)

    def test_ref_case_variants_and_other_toc_consumer_block(self):
        for instruction in (' REF missing ',' REF MISSING '):
            d=self.doc();support.field(d.add_paragraph(),instruction);q,r=self.repair(d)
            self.assertFalse(r['navigation']['changes'])

    def test_non_toc_navigation_consumer_blocks_retargeting(self):
        d=self.doc();support.field(d.add_paragraph(),' PAGEREF missing \\h ')
        q,r=self.repair(d);self.assertFalse(r['navigation']['changes'])

    def test_row_in_wide_comment_range_blocks(self):
        d=self.doc();start=marker(d.paragraphs[0],'commentRangeStart','101')
        d.paragraphs[0]._p.remove(start);d.paragraphs[0]._p.insert(0,start)
        marker(d.paragraphs[3],'commentRangeEnd','101')
        q,r=self.repair(d);self.assertFalse(r['navigation']['changes'])

    def test_point_bookmark_or_duplicate_title_blocks(self):
        for mode in ('point','duplicate','table'):
            d=self.doc();p=d.paragraphs[-2]
            if mode=='duplicate':d.add_paragraph('Chapter One',style='Heading 1')
            elif mode=='table':d.add_table(rows=1,cols=1).cell(0,0).text='Chapter One'
            else:
                a=p._p.find(W+'bookmarkStart');b=p._p.find(W+'bookmarkEnd');p._p.remove(b);a.addnext(b)
            q,r=self.repair(d);self.assertFalse(r['navigation']['changes'])

    def test_wide_comment_permission_move_target_blocks(self):
        for kind in ('commentRange','perm','moveFromRange'):
            d=self.doc();marker(d.paragraphs[-3],kind+'Start','101');marker(d.paragraphs[-1],kind+'End','101')
            q,r=self.repair(d);self.assertFalse(r['navigation']['changes'],kind)

    def test_inline_comment_enclosed_outside_both_write_positions_preserved(self):
        d=self.doc();p=d.paragraphs[-2]
        for n in list(p._p):
            if n.tag in scoped.BOOKS and n.get(W+'id')=='7':p._p.remove(n)
        start=marker(p,'commentRangeStart','12');p._p.remove(start);p._p.insert(1,start);marker(p,'commentRangeEnd','12')
        before=nav._control_markers(d);q,r=self.repair(d);self.assertEqual(len(r['navigation']['changes']),1)
        self.assertEqual(nav._control_markers(q),before)

    def test_field_pair_inconsistency_never_uses_additive_to_guess(self):
        d=self.doc()
        for n in d.paragraphs[1]._p.iter(W+'instrText'):
            if 'PAGEREF'in(n.text or ''):n.text=n.text.replace('missing','source_heading')
        q,r=self.repair(d);self.assertFalse(r['navigation']['changes'])

    def test_mutate_then_raise_rolls_back_new_endpoints_and_fields(self):
        d=self.doc();p=d.paragraphs[-2]
        for n in list(p._p):
            if n.tag in scoped.BOOKS and n.get(W+'id')=='7':p._p.remove(n)
        before=xmlstate(d);original=nav._write_reference
        def fail(ref,value):original(ref,value);raise RuntimeError('injected field write failure')
        with patch.object(nav,'_write_reference',side_effect=fail):q,r=self.repair(d)
        self.assertEqual(r['navigation']['transaction_status'],'rolled_back');self.assertEqual(xmlstate(q),before)

    def test_new_bookmark_end_append_fault_leaves_no_orphan(self):
        d=self.doc();p=d.paragraphs[-2]
        for n in list(p._p):
            if n.tag in scoped.BOOKS and n.get(W+'id')=='7':p._p.remove(n)
        before=xmlstate(d);kind=type(p._p);original=kind.append;failed=[]
        def fail(node,child):
            result=original(node,child)
            if child.tag==W+'bookmarkEnd'and child.get(W+'id')not in {'7','40','99'}and not failed:
                failed.append(True);raise RuntimeError('injected append-after-mutation')
            return result
        with patch.object(kind,'append',new=fail):q,r=self.repair(d)
        self.assertTrue(failed);self.assertEqual(r['navigation']['transaction_status'],'rolled_back');self.assertEqual(xmlstate(q),before)

    def test_full_boundary_then_additive_inverse_restores_exact_control(self):
        d=self.doc();d.add_paragraph('Subtitle');cfg=boundary('Subtitle',display_prefix='——')
        q,r=self.h.apply(d,[support.rule('Chapter One','heading_1'),cfg],repair_toc_navigation=True,navigation_target_policy='bound_heading_additive')
        self.assertTrue(r['boundary_inverse']['valid']);self.assertTrue(r['navigation']['changes'])
