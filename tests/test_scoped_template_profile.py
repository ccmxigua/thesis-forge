"""Local source-preserving scope integration; no models or office processes."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.shared import Pt
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import scoped_template_profile as scoped
import apply_format_spec as main

W = scoped.W


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def selector(value, **kw):
    return dict(text_sha256=digest(value), **kw)


def rule(value, role='appendix_body', **props):
    return dict(id=digest(value)[:10], role=role, selector=selector(value),
                evidence=['Synthetic explicit applicable rule; not a real university norm'], **props)


def field(p, instruction, result='1'):
    a=OxmlElement('w:fldChar'); a.set(W+'fldCharType','begin');p.add_run()._r.append(a)
    i=OxmlElement('w:instrText');i.text=instruction;p.add_run()._r.append(i)
    a=OxmlElement('w:fldChar');a.set(W+'fldCharType','separate');p.add_run()._r.append(a)
    p.add_run(result)
    a=OxmlElement('w:fldChar');a.set(W+'fldCharType','end');p.add_run()._r.append(a)


class ScopedProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'source.docx';self.profile=self.root/'profile.json'
        self.output=self.root/'candidate.docx';self.report=self.root/'report.json'

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, doc, rules, **options):
        doc.save(self.source)
        p=dict(schema_version='1.0',profile_id='synthetic-A',authority='caller_declared',
               input_sha256=digest(self.source.read_bytes()),rules=rules,**options)
        self.profile.write_text(json.dumps(p))
        before=self.source.read_bytes()
        result=scoped.apply_profile(self.source,self.profile,self.output,self.report)
        self.assertEqual(before,self.source.read_bytes())
        self.assertFalse(result['submission_ready']);self.assertFalse(result['native_verified'])
        self.assertEqual(result['output_sha256'],digest(self.output.read_bytes()))
        return Document(self.output),result

    def test_partial_appendix_preserves_source_style_chain_and_slots(self):
        d=Document();base=d.styles.add_style('Source Parent',WD_STYLE_TYPE.PARAGRAPH)
        base.font.size=Pt(19);base.element.get_or_add_rPr().get_or_add_rFonts().set(W+'eastAsia','FangSong')
        child=d.styles.add_style('Source Child',WD_STYLE_TYPE.PARAGRAPH);child.base_style=base
        p=d.add_paragraph('附录正文',style=child);p.alignment=1
        out,r=self.apply(d,[rule(p.text,paragraph={'alignment':'justify','first_line_indent_chars':2})])
        q=out.paragraphs[0];self.assertEqual(q.style.name,'Source Child')
        self.assertEqual(q.paragraph_format.first_line_indent.pt,38)
        self.assertEqual(q._p.pPr.find(W+'ind').get(W+'firstLineChars'),'200')
        self.assertEqual(out.styles['Source Parent'].font.size.pt,19)
        self.assertEqual(out.styles['Source Parent'].element.rPr.rFonts.get(W+'eastAsia'),'FangSong')
        self.assertEqual(r['rules'][0]['status'],'applied')

    def test_relative_spacing_uses_explicit_current_size_not_style(self):
        d=Document();d.styles['Normal'].font.size=Pt(10.5);d.add_paragraph('References')
        q,_=self.apply(d,[rule('References','bibliography_heading',font={'size_pt':15},paragraph={'space_before_lines':2,'space_after_lines':2})])
        self.assertEqual(q.paragraphs[0].paragraph_format.space_before.pt,30)
        self.assertEqual(q.paragraphs[0].paragraph_format.space_after.pt,30)

    def test_mixed_size_relative_rule_preserved_atomically(self):
        d=Document();p=d.add_paragraph();p.add_run('ab').font.size=Pt(12);p.add_run('cd').font.size=Pt(20)
        before=p._p.xml
        q,r=self.apply(d,[rule('abcd',font={'bold':True},paragraph={'first_line_indent_chars':2})])
        self.assertEqual(q.paragraphs[0]._p.xml,before)
        self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_subtitle_formats_prefix_without_changing_title(self):
        d=Document();p=d.add_paragraph();p.add_run('——').font.size=Pt(15);p.add_run('Scope').font.size=Pt(18)
        d.add_paragraph('Main Title')
        q,_=self.apply(d,[rule('——Scope','thesis_subtitle_zh',font={'size_pt':18})])
        self.assertEqual([r.font.size.pt for r in q.paragraphs[0].runs],[18,18])
        self.assertEqual(q.paragraphs[1].text,'Main Title')

    def test_prefix_explicit_idempotent_and_other_profile(self):
        d=Document();d.add_paragraph('Scope')
        cfg=rule('Scope','thesis_subtitle_en',display_prefix='——')
        q,r=self.apply(d,[cfg]);self.assertEqual(q.paragraphs[0].text,'——Scope')
        cfg['selector']['text_sha256']=[digest('Scope'),digest('——Scope')]
        q,r=self.apply(q,[cfg]);self.assertEqual(q.paragraphs[0].text,'——Scope')
        self.assertEqual(r['rules'][0]['status'],'already_satisfied')
        q,r=self.apply(d,[rule('Scope','thesis_subtitle_en',font={'size_pt':11})])
        self.assertEqual(q.paragraphs[0].text,'Scope')

    def test_keywords_label_value_boundary_within_old_run(self):
        d=Document();p=d.add_paragraph();p.add_run('Key Wor').bold=True;p.add_run('ds: Digital T').bold=True;p.add_run('ransformation').bold=False
        q,r=self.apply(d,[rule(p.text,'keywords_en',keyword={'label':'Key Words:','label_font':{'bold':True},'value_font':{'bold':False}})])
        actual=[(c,run.bold) for run in q.paragraphs[0].runs for c in run.text]
        self.assertTrue(all(b for c,b in actual[:10]));self.assertTrue(all(b is False for c,b in actual[10:]))
        self.assertEqual(q.paragraphs[0].text,p.text)

    def test_label_different_language_and_colon_in_value(self):
        d=Document();p=d.add_paragraph('关键词：A:B；乙')
        q,_=self.apply(d,[rule(p.text,'keywords_zh',keyword={'label':'关键词：','label_font':{'bold':True}})])
        self.assertEqual(q.paragraphs[0].text,p.text)

    def test_script_slot_writer_removes_only_authorized_themes(self):
        d=Document();p=d.add_paragraph('Abc中文123');f=p.runs[0]._r.get_or_add_rPr().get_or_add_rFonts()
        for key,value in {'eastAsia':'FangSong','eastAsiaTheme':'minorEastAsia','asciiTheme':'majorAscii','hAnsiTheme':'majorHAnsi','cs':'ArabicSource','cstheme':'minorBidi'}.items():f.set(W+key,value)
        q,_=self.apply(d,[rule(p.text,font={'latin':'Times New Roman'})]);f=q.paragraphs[0].runs[0]._r.rPr.rFonts
        self.assertEqual(f.get(W+'eastAsia'),'FangSong');self.assertEqual(f.get(W+'eastAsiaTheme'),'minorEastAsia')
        self.assertEqual(f.get(W+'cs'),'ArabicSource');self.assertEqual(f.get(W+'cstheme'),'minorBidi')
        self.assertIsNone(f.get(W+'asciiTheme'));self.assertEqual(f.get(W+'hAnsi'),'Times New Roman')

    def test_cjk_does_not_replace_latin_or_valid_fangsong_elsewhere(self):
        d=Document();d.add_paragraph('Chinese');d.add_paragraph('Other')
        for p in d.paragraphs:
            f=p.runs[0]._r.get_or_add_rPr().get_or_add_rFonts();f.set(W+'eastAsia','FangSong');f.set(W+'ascii','Courier New')
        q,_=self.apply(d,[rule('Chinese',font={'cjk':'SimSun'})])
        self.assertEqual(q.paragraphs[0].runs[0]._r.rPr.rFonts.get(W+'ascii'),'Courier New')
        self.assertEqual(q.paragraphs[1].runs[0]._r.rPr.rFonts.get(W+'eastAsia'),'FangSong')

    def test_caption_exact_line_spacing_is_local(self):
        d=Document();d.add_paragraph('Figure 2 Example');d.add_paragraph('Body mentions Figure 2')
        q,_=self.apply(d,[rule('Figure 2 Example','figure_caption',paragraph={'line_spacing':{'type':'exact','value':20}})])
        self.assertEqual(q.paragraphs[0].paragraph_format.line_spacing.pt,20)
        self.assertIsNone(q.paragraphs[1].paragraph_format.line_spacing)

    def table_doc(self,signature=False,picture=False,form=False,merged=False):
        d=Document();d.add_paragraph('Table 1 Data');t=d.add_table(rows=2,cols=2)
        for row,values in zip(t.rows,([['Name','Ada'],['Signature','sign']] if form else [['Signature method' if signature else 'Method','N'],['A','12']])):
            for cell,value in zip(row.cells,values):cell.text=value
        if picture:
            t.cell(1,0).paragraphs[0].add_run()._r.append(OxmlElement('w:drawing'))
        if merged:t.cell(0,0).merge(t.cell(0,1))
        t.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        return d

    def table_cfg(self):
        return rule('Table 1 Data','table_caption',table={'style':'three_line','top_border_pt':0.75,'header_border_pt':0.75,'bottom_border_pt':0.75})

    def test_three_line_real_data_signature_algorithm_not_form(self):
        q,r=self.apply(self.table_doc(signature=True),[self.table_cfg()])
        self.assertEqual(r['rules'][0]['status'],'applied')
        self.assertEqual(q.tables[0].cell(0,0)._tc.tcPr.find(W+'tcBorders/'+W+'bottom').get(W+'sz'),'6')
        self.assertTrue(q.tables[0].rows[0]._tr.xpath('./w:trPr/w:cantSplit'))

    def test_three_line_data_thumbnail_keeps_object(self):
        q,r=self.apply(self.table_doc(picture=True),[self.table_cfg()])
        self.assertEqual(r['rules'][0]['status'],'applied');self.assertEqual(len(q._element.xpath('.//w:drawing')),1)

    def test_identity_form_and_merged_tables_retained(self):
        for kw in ({'form':True},{'merged':True}):
            with self.subTest(kw=kw):
                d=self.table_doc(**kw);before=d.tables[0]._tbl.xml
                q,r=self.apply(d,[self.table_cfg()]);self.assertEqual(q.tables[0]._tbl.xml,before)
                self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_table_conditional_inherited_borders_unknown(self):
        d=self.table_doc();s=d.styles['Table Grid'];pr=OxmlElement('w:tblStylePr');pr.set(W+'type','firstRow')
        tc=OxmlElement('w:tcPr');b=OxmlElement('w:tcBorders');b.append(OxmlElement('w:bottom'));tc.append(b);pr.append(tc);s.element.append(pr)
        d.tables[0].style=s
        q,r=self.apply(d,[self.table_cfg()]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_reference_terminal_only(self):
        d=Document();d.add_paragraph('[1] Author. Title。');d.add_paragraph('Prose。')
        q,_=self.apply(d,[rule('[1] Author. Title。','bibliography_entry',terminal={'accepted':['。','.'],'value':'.'})])
        self.assertEqual(q.paragraphs[0].text,'[1] Author. Title.');self.assertEqual(q.paragraphs[1].text,'Prose。')

    def test_reference_url_doi_retained(self):
        for v in ('[1] https://example.org/a.','[2] DOI:10.1000/x。'):
            d=Document();d.add_paragraph(v)
            q,r=self.apply(d,[rule(v,'bibliography_entry',terminal={'accepted':['。','.'],'value':'.'})])
            self.assertEqual(q.paragraphs[0].text,v);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_spacers_only_insert_missing_not_delete_semantics(self):
        d=Document();d.add_paragraph('Achievements');d.add_paragraph('');d.add_paragraph('Author content')
        cfg=rule('Achievements','heading_publications',spacer={'count':2,'paragraph':{'line_spacing':{'type':'exact','value':17}}})
        q,r=self.apply(d,[cfg]);self.assertEqual([p.text for p in q.paragraphs],['Achievements','','','Author content'])
        q,r=self.apply(q,[cfg]);self.assertEqual(r['rules'][0]['inserted_spacers'],0)

    def test_blank_looking_bookmark_is_not_spacer(self):
        d=Document();d.add_paragraph('Achievements');p=d.add_paragraph()
        a=OxmlElement('w:bookmarkStart');a.set(W+'id','1');a.set(W+'name','empty');p._p.append(a)
        b=OxmlElement('w:bookmarkEnd');b.set(W+'id','1');p._p.append(b);d.add_paragraph('Content')
        q,r=self.apply(d,[rule('Achievements','heading_publications',spacer={'count':2,'paragraph':{}})])
        self.assertEqual(len(q.paragraphs),3);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_cross_paragraph_field_result_retained(self):
        d=Document();a=d.add_paragraph();n=OxmlElement('w:fldChar');n.set(W+'fldCharType','begin');a.add_run()._r.append(n)
        n=OxmlElement('w:instrText');n.text=' TOC ';a.add_run()._r.append(n)
        p=d.add_paragraph('Cached content');b=d.add_paragraph();n=OxmlElement('w:fldChar');n.set(W+'fldCharType','end');b.add_run()._r.append(n)
        q,r=self.apply(d,[rule(p.text,font={'bold':True})]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_text_edits_inside_cross_paragraph_bookmark_retained(self):
        d=Document();a=d.add_paragraph('start');n=OxmlElement('w:bookmarkStart');n.set(W+'id','1');n.set(W+'name','author');a._p.append(n)
        d.add_paragraph('Scope');d.add_paragraph('Body');b=d.add_paragraph('end');n=OxmlElement('w:bookmarkEnd');n.set(W+'id','1');b._p.append(n)
        for cfg in (rule('Scope','thesis_subtitle_en',display_prefix='—'),rule('Scope','heading_publications',spacer={'count':1,'paragraph':{}})):
            q,r=self.apply(d,[cfg]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved');self.assertEqual(len(q.paragraphs),4)

    def test_keyword_split_preserves_bookmark_range_offsets(self):
        d=Document();p=d.add_paragraph();a=OxmlElement('w:bookmarkStart');a.set(W+'id','5');a.set(W+'name','author');p._p.append(a)
        p.add_run('Key Words: value');b=OxmlElement('w:bookmarkEnd');b.set(W+'id','5');p._p.append(b)
        q,r=self.apply(d,[rule(p.text,'keywords_en',keyword={'label':'Key Words:','label_font':{'bold':True},'value_font':{'bold':False}})])
        self.assertEqual(r['rules'][0]['status'],'applied');self.assertEqual(q.paragraphs[0].text,p.text)
        self.assertEqual(len(q._element.xpath('.//w:bookmarkStart')),1)

    def test_objects_math_fields_and_unsafe_wrappers_preserved(self):
        for tag in ('w:object','m:oMath','w:fldSimple','w:rPrChange','w:txbxContent'):
            with self.subTest(tag=tag):
                d=Document();p=d.add_paragraph('Content');p.runs[0]._r.append(OxmlElement(tag));before=p._p.xml
                q,r=self.apply(d,[rule(p.text,font={'bold':True},paragraph={'alignment':'justify'})]);self.assertEqual(q.paragraphs[0]._p.xml,before)
                self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_style_revision_lock_and_unknown_base_preserved(self):
        for tag in ('w:locked','w:rPrChange','w:basedOn'):
            d=Document();s=d.styles.add_style('Protected',WD_STYLE_TYPE.PARAGRAPH)
            n=OxmlElement(tag)
            if tag=='w:basedOn':n.set(W+'val','Missing')
            s.element.append(n);p=d.add_paragraph('Content',s)
            q,r=self.apply(d,[rule(p.text,font={'bold':True})]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_hidden_and_complex_script_retained(self):
        for tag in ('w:vanish','w:rtl','w:cs'):
            d=Document();p=d.add_paragraph('Content');p.runs[0]._r.get_or_add_rPr().append(OxmlElement(tag))
            q,r=self.apply(d,[rule(p.text,font={'latin':'Arial'})]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_ambiguous_selector_and_wrong_keyword_boundary_preserved(self):
        d=Document();d.add_paragraph('Repeated');d.add_paragraph('Repeated')
        q,r=self.apply(d,[rule('Repeated',font={'bold':True})]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')
        d=Document();d.add_paragraph('words: value')
        q,r=self.apply(d,[rule('words: value','keywords_en',keyword={'label':'Key Words:'})]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_region_disambiguates_without_body_indices(self):
        d=Document();[d.add_paragraph(t) for t in ('Repeat','Start','Repeat','End')]
        cfg=rule('Repeat',font={'bold':True});cfg['selector']['region']={'after':digest('Start'),'before':digest('End')}
        q,r=self.apply(d,[cfg]);self.assertIsNone(q.paragraphs[0].runs[0].bold);self.assertTrue(q.paragraphs[2].runs[0].bold)

    def header_doc(self,shared=False):
        d=Document();d.add_paragraph('Chapter One');d.sections[0].header.paragraphs[0].text='Old Chapter'
        if shared:d.add_section(WD_SECTION.NEW_PAGE);d.add_paragraph('Chapter Two')
        return d

    def test_unshared_section_header_sync_and_slots(self):
        cfg=rule('Chapter One','heading_1',header={'side':'default','expected_text_sha256':digest('Old Chapter'),'synchronize_heading':True},font={'latin':'Times New Roman'})
        q,r=self.apply(self.header_doc(),[cfg]);self.assertEqual(q.sections[0].header.paragraphs[0].text,'Chapter One')
        self.assertEqual(q.paragraphs[0].text,'Chapter One');self.assertIsNone(q.paragraphs[0].runs[0]._r.rPr)
        self.assertEqual(r['rules'][0]['status'],'applied')

    def test_shared_header_part_is_not_globally_replaced(self):
        cfg=rule('Chapter One','heading_1',header={'side':'default','expected_text_sha256':digest('Old Chapter'),'synchronize_heading':True})
        q,r=self.apply(self.header_doc(shared=True),[cfg]);self.assertEqual(q.sections[0].header.paragraphs[0].text,'Old Chapter')
        self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_opc_unmodified_payloads_exact_and_repeat_byte_stable(self):
        d=Document();d.add_paragraph('Text');self.apply(d,[rule('Text',font={'bold':True})])
        first=self.output.read_bytes()
        scoped.apply_profile(self.source,self.profile,self.output,self.report)
        self.assertEqual(first,self.output.read_bytes())
        with zipfile.ZipFile(self.source) as a,zipfile.ZipFile(self.output) as b:
            self.assertEqual(a.namelist(),b.namelist())
            for name in a.namelist():
                if name!='word/document.xml':self.assertEqual(a.read(name),b.read(name),name)

    def test_input_hash_unknown_schema_and_conflicts_publish_nothing(self):
        d=Document();d.add_paragraph('Text');self.apply(d,[])
        old=self.output.read_bytes();old_report=self.report.read_bytes();p=json.loads(self.profile.read_text())
        for change in ({'input_sha256':'0'*64},{'unknown':1},{'rules':[rule('Text',paragraph={'first_line_indent_pt':2,'first_line_indent_chars':2})]}):
            x=copy.deepcopy(p);x.update(change);self.profile.write_text(json.dumps(x))
            with self.assertRaises(ValueError):scoped.apply_profile(self.source,self.profile,self.output,self.report)
            self.assertEqual(self.output.read_bytes(),old);self.assertEqual(self.report.read_bytes(),old_report)

    def test_all_input_and_output_aliases_protected_even_unused_sources(self):
        d=Document();d.add_paragraph('Text');self.apply(d,[])
        evidence=self.root/'source.tex';evidence.write_text('author content');p=json.loads(self.profile.read_text())
        p['sources']=[{'path':'source.tex','sha256':digest(evidence.read_bytes())}];self.profile.write_text(json.dumps(p))
        import os
        link=self.root/'alias.tex';os.link(evidence,link)
        sym=self.root/'symlink.tex';sym.symlink_to(evidence)
        for alias in (evidence,link,sym,self.source,self.profile):
            for output,report in ((alias,self.report),(self.output,alias)):
                with self.assertRaises(ValueError):scoped.apply_profile(self.source,self.profile,output,report)
        self.assertEqual(evidence.read_text(),'author content')
        with self.assertRaises(ValueError):scoped.apply_profile(self.source,self.profile,self.output,self.output)

    def test_existing_directory_destination_retains_old_receipt(self):
        d=Document();d.add_paragraph('Text');self.apply(d,[]);old=self.report.read_bytes();directory=self.root/'directory';directory.mkdir()
        with self.assertRaises(ValueError):scoped.apply_profile(self.source,self.profile,directory,self.report)
        self.assertEqual(self.report.read_bytes(),old)

    def test_mid_publication_failure_rolls_back_both_outputs(self):
        d=Document();d.add_paragraph('Text');self.apply(d,[]);old=self.output.read_bytes();receipt=self.report.read_bytes()
        import artifact_io
        real=artifact_io.os.replace
        def fail_once(a,b):
            if Path(b)==self.report and str(a).endswith('.json'):
                raise OSError('synthetic receipt publish failure')
            return real(a,b)
        with patch.object(artifact_io.os,'replace',side_effect=fail_once):
            with self.assertRaises(OSError):scoped.apply_profile(self.source,self.profile,self.output,self.report)
        self.assertEqual(self.output.read_bytes(),old);self.assertEqual(self.report.read_bytes(),receipt)

    def test_cli_real_main_and_no_mixed_release_options(self):
        d=Document();d.add_paragraph('Content');self.apply(d,[rule('Content',font={'size_pt':17})])
        args=[str(self.source),str(self.profile),str(self.output),'--out-dir',str(self.root/'audit'),'--scoped-profile']
        self.assertEqual(main.main(args),0)
        result=json.loads((self.root/'audit/scoped-template-report.json').read_text())
        self.assertEqual(result['output_sha256'],digest(self.output.read_bytes()))
        with self.assertRaises(SystemExit):main.main(args+['--require-submission-ready'])
        with self.assertRaises(SystemExit):main.main(args+['--output-policy','submission'])

    def test_pending_evidence_never_native_pass(self):
        d=Document();d.add_paragraph('Text');_,r=self.apply(d,[])
        evidence=r['pending_native_evidence'];self.assertEqual(len(evidence['pending_categories']),8)
        self.assertFalse(evidence['native_verified']);self.assertEqual(evidence['status'],'native_evidence_required')

    def nav_doc(self,partial=False):
        d=Document();p=d.add_paragraph()
        n=OxmlElement('w:fldChar');n.set(W+'fldCharType','begin');p.add_run()._r.append(n)
        n=OxmlElement('w:instrText');n.text=' TOC \\o "1-3" \\h ';p.add_run()._r.append(n)
        n=OxmlElement('w:fldChar');n.set(W+'fldCharType','separate');p.add_run()._r.append(n)
        p=d.add_paragraph(style='TOC Heading')
        field(p,' HYPERLINK \\l missing ','Chapter One');p.add_run('\t');field(p,' PAGEREF missing \\h ','999')
        p=d.add_paragraph();n=OxmlElement('w:fldChar');n.set(W+'fldCharType','end');p.add_run()._r.append(n)
        p=d.add_paragraph('Earlier body')
        if partial:
            n=OxmlElement('w:bookmarkStart');n.set(W+'id','99');n.set(W+'name','author_range');p._p.append(n)
        p=d.add_paragraph('Chapter One',style='Heading 1')
        n=OxmlElement('w:bookmarkStart');n.set(W+'id','7');n.set(W+'name','source_heading');p._p.insert(1,n)
        n=OxmlElement('w:bookmarkEnd');n.set(W+'id','7');p._p.append(n)
        if partial:
            n=OxmlElement('w:bookmarkEnd');n.set(W+'id','99');p._p.append(n)
        p=d.add_paragraph('Body cross reference ');field(p,' REF author_unrelated ','untouched')
        return d

    def test_navigation_actual_main_and_final_sha_body_refs_preserved(self):
        d=self.nav_doc();body=d.paragraphs[-1]._p.xml
        q,r=self.apply(d,[rule('Chapter One','heading_1')],repair_toc_navigation=True)
        self.assertEqual(r['navigation']['status'],'references_repaired')
        self.assertTrue(r['navigation_audit']['valid']);self.assertTrue(r['navigation_audit']['navigation_references_verified'])
        self.assertEqual(r['navigation_audit']['output_sha256'],digest(self.output.read_bytes()))
        self.assertEqual(q.paragraphs[-1]._p.xml,body)
        self.assertIn('999',q.paragraphs[1].text)
        args=[str(self.source),str(self.profile),str(self.output),'--out-dir',str(self.root/'audit'),'--scoped-profile']
        self.assertEqual(main.main(args),0)
        q,r=self.apply(q,[rule('Chapter One','heading_1')],repair_toc_navigation=True)
        self.assertEqual(r['navigation']['status'],'references_verified');self.assertFalse(r['navigation']['changes'])

    def test_navigation_cross_range_guard_not_weakened(self):
        d=self.nav_doc(partial=True);before=d._element.xml
        q,r=self.apply(d,[rule('Chapter One','heading_1')],repair_toc_navigation=True)
        self.assertFalse(r['navigation']['changes']);self.assertTrue(r['navigation']['findings'])
        self.assertEqual(q._element.xml,before)
        self.assertIn('partial heading range',str(r['navigation']['findings']))

    def test_navigation_injected_orphan_failure_never_publishes_dirty_graph(self):
        import toc_navigation as nav
        d=self.nav_doc();p=d.paragraphs[-2]
        for n in list(p._p):
            if n.tag in scoped.BOOKS:p._p.remove(n)
        original=nav._write_reference
        def fail(ref,new):
            original(ref,new);raise RuntimeError('synthetic field update failure')
        with patch.object(nav,'_write_reference',side_effect=fail):
            q,r=self.apply(d,[rule('Chapter One','heading_1')],repair_toc_navigation=True)
        self.assertFalse(q._element.xpath('.//w:bookmarkStart'))
        self.assertEqual(r['navigation']['transaction_status'],'rolled_back')
        self.assertTrue(r['navigation_audit']['valid']);self.assertFalse(r['navigation_audit']['navigation_references_verified'])

    def test_figure_order_only_reports_never_reorders_or_writes_prose(self):
        d=Document();p=d.add_paragraph();p.add_run()._r.append(OxmlElement('w:drawing'))
        d.add_paragraph('Figure 2 Caption');d.add_paragraph('See Figure 2 for results.')
        before=d._element.xml
        q,r=self.apply(d,[],figure_checks=[{'caption':selector('Figure 2 Caption'),'reference_label':'Figure 2'}])
        self.assertEqual(r['figure_order'][0]['status'],'potential_reference_after_figure')
        self.assertFalse(r['figure_order'][0]['automatic_reordering']);self.assertEqual(q._element.xml,before)

    def test_figure_quote_or_unknown_object_group_is_unhandled(self):
        d=Document();d.add_paragraph('Figure 2 Caption');d.add_paragraph('See Figure 2')
        q,r=self.apply(d,[],figure_checks=[{'caption':selector('Figure 2 Caption'),'reference_label':'Figure 2'}])
        self.assertEqual(r['figure_order'][0]['status'],'unhandled_preserved')
        self.assertEqual(r['status'],'review_candidate_with_unhandled')

    def test_false_style_lock_is_allowed_but_enforced_doc_protection_not(self):
        d=Document();s=d.styles.add_style('Unlocked',WD_STYLE_TYPE.PARAGRAPH);n=OxmlElement('w:locked');n.set(W+'val','off');s.element.append(n)
        d.add_paragraph('Text',s);q,r=self.apply(d,[rule('Text',font={'bold':True})]);self.assertEqual(r['rules'][0]['status'],'applied')
        n=OxmlElement('w:documentProtection');n.set(W+'enforcement','1');n.set(W+'formatting','1');d.settings.element.append(n)
        old=self.output.read_bytes()
        with self.assertRaises(ValueError):self.apply(d,[rule('Text',font={'bold':True})])
        self.assertEqual(self.output.read_bytes(),old)

    def test_distinct_rids_same_header_part_still_shared(self):
        from docx.opc.rel import _Relationship
        d=self.header_doc(shared=True)
        ref=d.sections[0]._sectPr.find(W+'headerReference');rid=ref.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        rel=d.part.rels[rid];other='rId999';d.part.rels[other]=_Relationship(other,rel.reltype,rel.target_part,d.part.partname.baseURI)
        new=copy.deepcopy(ref);new.set('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id',other);d.sections[1]._sectPr.insert(0,new)
        cfg=rule('Chapter One','heading_1',header={'side':'default','expected_text_sha256':digest('Old Chapter'),'synchronize_heading':True})
        q,r=self.apply(d,[cfg]);self.assertEqual(q.sections[0].header.paragraphs[0].text,'Old Chapter')
        self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_table_caption_combination_applies_both_atomically(self):
        d=self.table_doc();cfg=self.table_cfg();cfg['paragraph']={'line_spacing':{'type':'exact','value':23}}
        q,r=self.apply(d,[cfg]);self.assertEqual(q.paragraphs[0].paragraph_format.line_spacing.pt,23)
        self.assertEqual(q.tables[0].cell(0,0)._tc.tcPr.find(W+'tcBorders/'+W+'bottom').get(W+'sz'),'6')
        d=self.table_doc(form=True);q,r=self.apply(d,[cfg]);self.assertIsNone(q.paragraphs[0].paragraph_format.line_spacing)
        self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_two_selectors_cannot_write_same_actual_table(self):
        d=self.table_doc();p=d.add_paragraph('单位：元');d._element.body.remove(p._p);d.paragraphs[0]._p.addnext(p._p)
        cfg=self.table_cfg();other=copy.deepcopy(cfg);other['id']='other';other['selector']=selector('单位：元')
        with self.assertRaisesRegex(ValueError,'effect target'):self.apply(d,[cfg,other])
        self.assertFalse(self.output.exists());self.assertFalse(self.report.exists())

    def test_conflicting_actions_are_contract_errors_not_silently_ignored(self):
        d=self.header_doc();cfg=rule('Chapter One','heading_1',header={'side':'default','expected_text_sha256':digest('Old Chapter')},terminal={'accepted':['.'],'value':'.'})
        with self.assertRaises(ValueError):self.apply(d,[cfg])
        self.assertFalse(self.output.exists())

    def test_competing_indent_char_units_cleared_only_when_authorized(self):
        d=Document();p=d.add_paragraph('Text');n=OxmlElement('w:ind')
        for k,v in {'leftChars':'200','rightChars':'300','firstLineChars':'200'}.items():n.set(W+k,v)
        p._p.get_or_add_pPr().append(n)
        q,r=self.apply(d,[rule('Text',paragraph={'left_indent_pt':0,'right_indent_pt':0})])
        ind=q.paragraphs[0]._p.pPr.find(W+'ind');self.assertIsNone(ind.get(W+'leftChars'));self.assertIsNone(ind.get(W+'rightChars'))
        self.assertEqual(ind.get(W+'firstLineChars'),'200')

    def test_unrepresentable_size_is_rejected_not_rounded_success(self):
        d=Document();d.add_paragraph('Text')
        for props in ({'font':{'size_pt':12.25}}, {'keyword':{'label':'Text','value_font':{'size_pt':12.25}}}):
            cfg=rule('Text','keywords_en',**props)
            with self.assertRaisesRegex(ValueError,'half-points'):self.apply(d,[cfg])
        self.assertFalse(self.output.exists());self.assertFalse(self.report.exists())

    def test_explicit_line_height_resolves_mixed_size_spacing(self):
        d=Document();p=d.add_paragraph();p.add_run('a').font.size=Pt(12);p.add_run('b').font.size=Pt(20)
        q,r=self.apply(d,[rule('ab',paragraph={'space_before_lines':2,'spacing_line_height_pt':13})])
        self.assertEqual(q.paragraphs[0].paragraph_format.space_before.pt,26)
        self.assertEqual([x.font.size.pt for x in q.paragraphs[0].runs],[12,20])

    def test_duplicate_opc_members_rejected_even_no_rules(self):
        d=Document();d.add_paragraph('Text');self.apply(d,[])
        before=self.output.read_bytes();receipt=self.report.read_bytes()
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(self.source,'a') as z:
                z.writestr('word/document.xml',z.read('word/document.xml').replace(b'Text',b'Other'))
        p=json.loads(self.profile.read_text());p['input_sha256']=digest(self.source.read_bytes());self.profile.write_text(json.dumps(p))
        with self.assertRaisesRegex(ValueError,'OPC member'):scoped.apply_profile(self.source,self.profile,self.output,self.report)
        self.assertEqual(self.output.read_bytes(),before);self.assertEqual(self.report.read_bytes(),receipt)

    def test_unhandled_only_output_is_byte_exact_source(self):
        d=Document();d.add_paragraph('Text');q,r=self.apply(d,[rule('Missing',font={'bold':True})])
        self.assertEqual(self.source.read_bytes(),self.output.read_bytes())

    def test_keyword_comment_reference_and_empty_runs_not_duplicated(self):
        d=Document();p=d.add_paragraph('Key Words: value');p.add_run()
        n=OxmlElement('w:commentReference');n.set(W+'id','1');p.add_run()._r.append(n)
        q,r=self.apply(d,[rule('Key Words: value','keywords_en',keyword={'label':'Key Words:','label_font':{'bold':True},'value_font':{'bold':False}})])
        self.assertEqual(r['rules'][0]['status'],'applied');self.assertEqual(len(q._element.xpath('.//w:commentReference')),1)

    def test_formatted_empty_paragraph_is_spacer_but_page_break_is_not(self):
        d=Document();d.add_paragraph('Head');p=d.add_paragraph();n=OxmlElement('w:widowControl');n.set(W+'val','0');p._p.get_or_add_pPr().append(n)
        n=OxmlElement('w:pageBreakBefore');n.set(W+'val','0');p._p.pPr.append(n);d.add_paragraph('Content')
        cfg=rule('Head','heading_publications',spacer={'count':2,'paragraph':{}})
        q,r=self.apply(d,[cfg]);self.assertEqual(r['rules'][0]['inserted_spacers'],1)
        n.set(W+'val','1');q,r=self.apply(d,[cfg]);self.assertEqual(r['rules'][0]['status'],'unhandled_preserved')

    def test_two_school_configs_do_not_share_values_or_mutate_normal(self):
        d=self.table_doc();d.add_paragraph('Appendix');d.styles['Normal'].font.size=Pt(19)
        for size,line,border in ((12,20,.75),(14,24,1.25)):
            cfg=self.table_cfg();cfg['table'].update(top_border_pt=border,header_border_pt=border,bottom_border_pt=border)
            q,r=self.apply(d,[cfg,rule('Appendix',font={'size_pt':size},paragraph={'line_spacing':{'type':'exact','value':line}})])
            self.assertEqual(q.paragraphs[-1].runs[0].font.size.pt,size);self.assertEqual(q.paragraphs[-1].paragraph_format.line_spacing.pt,line)
            self.assertEqual(q.styles['Normal'].font.size.pt,19)
            self.assertEqual(q.tables[0].cell(0,0)._tc.tcPr.find(W+'tcBorders/'+W+'bottom').get(W+'sz'),str(round(border*8)))


if __name__=='__main__':
    unittest.main()
