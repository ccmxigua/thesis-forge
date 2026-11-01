"""Resource binding tests: in-memory parser fixtures and unchanged actual PDFs.

No Office conversion or native document update. Parser fonts are deliberately
small sfnt records, not claims of visually rendered counterexample letters.
"""
from pathlib import Path
from io import BytesIO
import hashlib,json,struct,sys,tempfile,unittest
from unittest import mock
import fitz
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,NumberObject,ArrayObject,DecodedStreamObject

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from pdf_glyph_integrity import audit_pdf_glyph_integrity,_sfnt_tables
from test_pdf_glyph_integrity import font_program,span

FIXTURES=ROOT/'tests/fixtures/pdf-font-resources'

def simple_font(records,*,gid=1,name='FamilyPS'):
    data=font_program(records=records,name_records=[(3,1,1033,6,name.encode('utf-16-be'))])
    tables=list(_sfnt_tables(data).items())
    mapping=bytearray(256);mapping[1]=gid
    tables.append((b'cmap',struct.pack('>HHHHI',0,1,1,0,12)+struct.pack('>HHH',0,262,0)+mapping))
    offset=12+16*len(tables);directory=[];payload=[]
    for tag,value in tables:
        directory.append(struct.pack('>4sIII',tag,0,offset,len(value)));payload.append(value);offset+=len(value)
    return b'\0\1\0\0'+struct.pack('>HHHH',len(tables),0,0,0)+b''.join(directory+payload)

def stream(data):
    result=DecodedStreamObject();result.set_data(data);return result

def cmap():
    return b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n1 begincodespacerange <00> <FF> endcodespacerange\n1 beginbfchar <01> <0041> endbfchar\nendcmap end end'

class MemoryPage:
    def __init__(self,resources,traces,content):self.resources,self.traces,self.content=resources,traces,content
    def get_fonts(self,full=False):return self.resources
    def get_texttrace(self):return self.traces
    def get_bboxlog(self):return [('fill-text',(0,0,100,100)) for _ in range(self.content.count(b'BT'))]
    def read_contents(self):return self.content

class MemoryPDF:
    def __init__(self,programs,page_specs,*,font_attributes=None,unicode_data=None,form_resources=False):
        writer=PdfWriter();self.programs={};refs=[];self.pages=[]
        for index,program in enumerate(programs):
            file_ref=writer._add_object(stream(program))
            descriptor=writer._add_object(DictionaryObject({NameObject('/Flags'):NumberObject(4),NameObject('/FontFile2'):file_ref}))
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/TrueType'),
                NameObject('/BaseFont'):NameObject('/'+chr(65+index)*6+'+FamilyPS'),NameObject('/FontDescriptor'):descriptor,
                NameObject('/ToUnicode'):writer._add_object(stream(unicode_data if unicode_data is not None else cmap()))})
            font.update((font_attributes or {}).get(index,{}))
            ref=writer._add_object(font);refs.append(ref);self.programs[ref.idnum]=program
        self.refs=[ref.idnum for ref in refs]
        for keys,traces,content in page_specs:
            p=writer.add_blank_page(width=200,height=200)
            fonts=DictionaryObject({NameObject('/'+key):refs[index] for key,index in keys.items()})
            p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):fonts})
            if form_resources:
                p['/Resources'][NameObject('/XObject')]=DictionaryObject({NameObject('/Form1'):writer._add_object(stream(b'BT ET'))})
            p[NameObject('/Contents')]=writer._add_object(stream(content))
            resources=[(refs[index].idnum,'ttf','TrueType',chr(65+index)*6+'+FamilyPS',key,'',0) for key,index in keys.items()]
            self.pages.append(MemoryPage(resources,traces,content))
        out=BytesIO();writer.write(out);self.data=out.getvalue()
    def __iter__(self):return iter(self.pages)
    def __len__(self):return len(self.pages)
    def tobytes(self,garbage=0):return self.data
    def extract_font(self,xref):return 'FamilyPS','ttf','TrueType',self.programs[xref]

def trace(gid=1,seqno=0):
    result=span(ord('A'),gid,font='FamilyPS');result['seqno']=seqno;return result

def pair(first,second,*,selected=0,gid=1,content=None,traces=None):
    return MemoryPDF([first,second],[({'F1':selected,'F2':1-selected},traces or [trace(gid)],
                                    content or b'BT /F1 12 Tf <01> Tj ET')])

class PdfResourceBindingTests(unittest.TestCase):
    def test_active_good_subset_does_not_inherit_other_subset_notdef(self):
        pdf=pair(simple_font([b'notdef',b'letter']),simple_font([b'notdef',b'notdef']))
        result=audit_pdf_glyph_integrity(pdf)
        self.assertEqual(result['findings'],[])
        self.assertEqual(result['resource_binding'][0]['bindings'][0]['font_xref'],pdf.refs[0])
        self.assertEqual(result['resource_binding'][0]['bindings'][0]['resource'],'/F1')
        self.assertEqual(result['record_coverage']['covered_unicode_characters'],1)
        self.assertFalse(result['specified_typeface_verified'])
        self.assertFalse(result['all_unicode_glyph_shapes_verified'])

    def test_active_notdef_cannot_borrow_the_other_subsets_letter(self):
        pdf=pair(simple_font([b'notdef',b'notdef']),simple_font([b'notdef',b'letter']))
        result=audit_pdf_glyph_integrity(pdf)
        self.assertEqual([f['code'] for f in result['findings']],['pdf_glyph_notdef_record'])
        self.assertEqual(result['findings'][0]['font_xrefs'],[pdf.refs[0]])

    def test_empty_glyph_blocks_even_when_notdef_is_nonempty(self):
        pdf=pair(simple_font([b'notdef',b'']),simple_font([b'notdef',b'letter']))
        self.assertEqual(audit_pdf_glyph_integrity(pdf)['findings'][0]['code'],'pdf_glyph_empty_record')

    def test_empty_notdef_and_renumbered_empty_copy_still_block(self):
        pdf=pair(simple_font([b'',b'']),simple_font([b'',b'letter']))
        self.assertEqual(audit_pdf_glyph_integrity(pdf)['findings'][0]['code'],'pdf_glyph_notdef_record')

    def test_same_resource_name_on_another_page_uses_that_pages_object(self):
        a=simple_font([b'notdef',b'letter']);b=simple_font([b'notdef',b'notdef'])
        content=b'BT /F1 12 Tf <01> Tj ET'
        pdf=MemoryPDF([a,b],[({'F1':0,'F2':1},[trace()],content),({'F1':1,'F2':0},[trace()],content)])
        result=audit_pdf_glyph_integrity(pdf)
        self.assertEqual([(x['page'],x['code']) for x in result['findings']],[(2,'pdf_glyph_notdef_record')])
        self.assertEqual([p['bindings'][0]['font_xref'] for p in result['resource_binding']],pdf.refs)

    def test_equal_font_names_and_equal_indices_do_not_pick_an_arbitrary_object(self):
        a=simple_font([b'notdef',b'letter'])
        content=b'BT /F1 12 Tf <01> Tj /F2 12 Tf <01> Tj ET'
        result=audit_pdf_glyph_integrity(pair(a,a,content=content,traces=[trace(),trace()]))
        self.assertEqual(result['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')
        self.assertEqual(result['findings'][0]['count'],2)
        self.assertEqual(result['resource_binding'][0]['bindings'],[])

    def test_changed_unicode_or_glyph_sequence_is_not_a_binding(self):
        a=simple_font([b'notdef',b'letter',b'other'])
        for changed in ('unicode','glyph','seqno'):
            pdf=pair(a,a)
            if changed=='unicode':pdf.pages[0].traces[0]['chars'][0]=(ord('B'),1,(10,20),(10,10,20,25))
            elif changed=='glyph':pdf.pages[0].traces[0]['chars'][0]=(ord('A'),2,(10,20),(10,10,20,25))
            else:pdf.pages[0].traces[0]['seqno']=10
            with self.subTest(changed=changed):
                self.assertEqual(audit_pdf_glyph_integrity(pdf)['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_resource_name_or_form_scope_mismatch_stays_unknown(self):
        a=simple_font([b'notdef',b'letter'])
        for changed in ('duplicate_name','referencer','different_name'):
            pdf=pair(a,a);row=list(pdf.pages[0].resources[0])
            if changed=='duplicate_name':
                other=list(pdf.pages[0].resources[1]);other[4]='F1';pdf.pages[0].resources[1]=tuple(other)
            elif changed=='referencer':row[6]=99;pdf.pages[0].resources[0]=tuple(row)
            else:row[4]='OtherName';pdf.pages[0].resources[0]=tuple(row)
            with self.subTest(changed=changed):
                self.assertEqual(audit_pdf_glyph_integrity(pdf)['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_snapshot_font_bytes_must_match_the_inspected_object(self):
        good=simple_font([b'notdef',b'letter']);bad=simple_font([b'notdef',b'notdef'])
        pdf=pair(good,good);pdf.programs[pdf.refs[0]]=bad
        self.assertEqual(audit_pdf_glyph_integrity(pdf)['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_nested_graphics_state_and_unsupported_operators_are_not_guessed(self):
        a=simple_font([b'notdef',b'letter'])
        for content in (b'BT /F1 12 Tf <01> Tj q Q ET',b'BT /F1 12 Tf <01> Tj (A) \' ET'):
            with self.subTest(content=content):
                self.assertEqual(audit_pdf_glyph_integrity(pair(a,a,content=content))['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_bad_snapshot_or_optional_reader_failure_stays_unknown(self):
        a=simple_font([b'notdef',b'letter'])
        pdf=pair(a,a);pdf.data=b'not a PDF'
        self.assertEqual(audit_pdf_glyph_integrity(pdf)['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')
        with mock.patch('pdf_glyph_integrity._snapshot_reader',side_effect=ImportError('optional reader absent')):
            self.assertEqual(audit_pdf_glyph_integrity(pair(a,a))['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_type1_is_uninspected_not_a_complete_font_or_shape_pass(self):
        pdf=MemoryPDF([b'%!PS unsupported Type1'],[({'F1':0},[trace()],b'BT /F1 12 Tf <01> Tj ET')])
        result=audit_pdf_glyph_integrity(pdf)
        self.assertEqual(result['font_programs'][0]['status'],'unsupported_font_program')
        self.assertEqual(result['truetype_record_characters_inspected'],0)
        self.assertEqual(result['record_coverage']['uninspected_unicode_characters'],1)
        self.assertFalse(result['record_coverage']['complete'])
        self.assertFalse(result['specified_typeface_verified'])
        self.assertFalse(result['all_unicode_glyph_shapes_verified'])

    def test_form_resources_or_explicit_font_encoding_remain_unknown(self):
        a=simple_font([b'notdef',b'letter']);specs=[({'F1':0,'F2':1},[trace()],b'BT /F1 12 Tf <01> Tj ET')]
        cases=[{'form_resources':True},
               {'font_attributes':{0:{NameObject('/Encoding'):NameObject('/WinAnsiEncoding')}}},
               {'font_attributes':{0:{NameObject('/Subtype'):NameObject('/Type0')}}}]
        for options in cases:
            with self.subTest(options=options):
                result=audit_pdf_glyph_integrity(MemoryPDF([a,a],specs,**options))
                self.assertEqual(result['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_unproven_font_cmap_or_tounicode_cannot_remove_ambiguity(self):
        good=simple_font([b'notdef',b'letter']);bad=bytearray(good)
        # Change the one embedded cmap's format from0 to6 without touching the
        # actual resource identity: the resolver must decline this encoding.
        count=struct.unpack_from('>H',bad,4)[0]
        offset=next(struct.unpack_from('>I',bad,12+16*i+8)[0] for i in range(count) if bad[12+16*i:16+16*i]==b'cmap')
        struct.pack_into('>H',bad,offset+12,6)
        self.assertEqual(audit_pdf_glyph_integrity(pair(bytes(bad),good))['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')
        specs=[({'F1':0,'F2':1},[trace()],b'BT /F1 12 Tf <01> Tj ET')]
        for data in (cmap().replace(b'1 beginbfchar',b'2 beginbfchar'),cmap().replace(b'beginbfchar',b'beginbfrange')):
            with self.subTest(data=data):
                result=audit_pdf_glyph_integrity(MemoryPDF([good,good],specs,unicode_data=data))
                self.assertEqual(result['findings'][0]['code'],'pdf_glyph_font_mapping_unresolved')

    def test_repeated_readonly_binding_is_deterministic(self):
        a=simple_font([b'notdef',b'letter']);pdf=pair(a,a);raw=pdf.data
        first=audit_pdf_glyph_integrity(pdf)
        self.assertEqual(first,audit_pdf_glyph_integrity(pdf))
        self.assertEqual(raw,pdf.data)

    def test_both_actual_pdfs_use_object_binding_and_disclose_type1_coverage(self):
        expected={'render-1.pdf':'e82e4fba38875cdf15304ee8e2b6b6b4b009a2090751011c432a577f0f062b7b',
                  'render-2.pdf':'b17cbd907b2e834a0eccc4349b7528e5f55aac6c24824c4149df5544df8c4d63'}
        observed=[]
        for name,digest in expected.items():
            raw=(FIXTURES/name).read_bytes();self.assertEqual(hashlib.sha256(raw).hexdigest(),digest)
            with fitz.open(stream=raw,filetype='pdf') as pdf:report=audit_pdf_glyph_integrity(pdf)
            self.assertEqual(report['findings'],[])
            self.assertEqual(report['visible_trace_characters'],1293)
            self.assertEqual(report['truetype_record_characters_inspected'],972)
            self.assertEqual(report['record_coverage']['uninspected_unicode_characters'],321)
            self.assertFalse(report['record_coverage']['complete'])
            self.assertFalse(report['specified_typeface_verified'])
            self.assertFalse(report['all_unicode_glyph_shapes_verified'])
            observed.append(report['resource_binding'][0]['bindings'][0]['font_xref'])
            self.assertEqual((FIXTURES/name).read_bytes(),raw)
        self.assertNotEqual(observed[0],observed[1])

    def test_both_formal_audit_paths_call_the_new_shared_binding(self):
        import audit_equation_export,rendered_format_audit
        source=FIXTURES/'source.docx';pdf=FIXTURES/'render-1.pdf'
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,pdf)}
        with tempfile.TemporaryDirectory() as directory:
            spec=Path(directory)/'spec.json';mapping=Path(directory)/'map.json'
            spec.write_text(json.dumps({'roles':{}}));mapping.write_text('{}')
            equation=audit_equation_export.audit(source,pdf)
            rendered=rendered_format_audit.audit(source,source,pdf,spec,style_map_path=mapping)
        for result in (equation,rendered):
            self.assertEqual(result['glyph_integrity']['findings'],[])
            self.assertTrue(result['glyph_integrity']['resource_binding'][0]['bindings'])
            self.assertEqual(result['glyph_integrity']['record_coverage']['uninspected_unicode_characters'],321)
            self.assertFalse(result['glyph_integrity']['all_unicode_glyph_shapes_verified'])
        self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_both_formal_audits_block_an_empty_record_distinct_from_notdef(self):
        import audit_equation_export,rendered_format_audit
        source=FIXTURES/'source.docx';pdf=FIXTURES/'render-1.pdf'
        program=font_program(records=[b'nonempty-notdef',b'',b'letter'])
        observed=span(ord('A'),1,font='Helvetica')
        with tempfile.TemporaryDirectory() as directory:
            spec=Path(directory)/'spec.json';mapping=Path(directory)/'map.json'
            spec.write_text(json.dumps({'roles':{}}));mapping.write_text('{}')
            with mock.patch.object(fitz.Document,'extract_font',return_value=('Helvetica','ttf','TrueType',program)), \
                 mock.patch.object(fitz.Page,'get_texttrace',return_value=[observed]), \
                 mock.patch.object(fitz.Page,'get_fonts',return_value=[(1,'ttf','TrueType','Helvetica','F1','',0)]):
                equation=audit_equation_export.audit(source,pdf)
                rendered=rendered_format_audit.audit(source,source,pdf,spec,style_map_path=mapping)
        self.assertFalse(equation['export_checks_passed'])
        self.assertEqual(rendered['status'],'issues_found')
        for result in (equation,rendered):
            self.assertIn('pdf_glyph_empty_record',{f['code'] for f in result['findings']})

if __name__=='__main__':unittest.main()
