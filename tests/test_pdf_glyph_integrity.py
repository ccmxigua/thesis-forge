from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

import fitz
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from pdf_glyph_integrity import _truetype_glyphs, audit_pdf_glyph_integrity


def font_program(*, short=False, records=None, name_records=None):
    # Deliberately small parser records, not a visual font fixture. The actual
    # reproduced office export is kept separately in the task evidence bundle.
    records = records or [b'notdef-record!', b'notdef-record!', b'letter-record!']
    records = [r + b'\0' * (len(r) % 2) for r in records]
    head = bytearray(54)
    struct.pack_into('>h', head, 50, 0 if short else 1)
    maxp = b'\0\x01\0\0' + struct.pack('>H', len(records))
    offsets = [0]
    for record in records:
        offsets.append(offsets[-1] + len(record))
    loca = b''.join(struct.pack('>H' if short else '>I', o // 2 if short else o) for o in offsets)
    tables = [(b'head', bytes(head)), (b'maxp', maxp), (b'loca', loca), (b'glyf', b''.join(records))]
    if name_records is not None:
        name_directory, strings = [], b''
        for platform, encoding, language, name_id, raw in name_records:
            name_directory.append(struct.pack('>6H', platform, encoding, language, name_id, len(raw), len(strings)))
            strings += raw
        tables.append((b'name', struct.pack('>3H', 0, len(name_records), 6 + 12 * len(name_records))
                       + b''.join(name_directory) + strings))
    offset = 12 + len(tables) * 16
    directory = []
    payload = []
    for name, raw in tables:
        directory.append(struct.pack('>4sIII', name, 0, offset, len(raw)))
        payload.append(raw)
        offset += len(raw)
    return b'\0\x01\0\0' + struct.pack('>HHHH', len(tables), 0, 0, 0) + b''.join(directory + payload)


def span(codepoint=ord('五'), glyph=1, *, kind=0, opacity=1, font='ArbitraryFont'):
    return {'font': font, 'type': kind, 'opacity': opacity, 'dir': (1, 0),
            'chars': [(codepoint, glyph, (10, 20), (10, 10, 20, 25))]}


class Page:
    def __init__(self, spans, resources=None):
        self.spans = spans
        self.resources = resources or [(1, 'ttf', 'TrueType', 'ABCDEF+ArbitraryFont', 'F1', '', 0)]

    def get_fonts(self, full=False):
        return self.resources

    def get_texttrace(self):
        return self.spans


class PDF:
    def __init__(self, pages, programs=None):
        self.pages = pages
        self.programs = programs or {1: font_program()}

    def __iter__(self):
        return iter(self.pages)

    def extract_font(self, xref):
        return ('ArbitraryFont', 'ttf', 'TrueType', self.programs[xref])


class PdfGlyphIntegrityTests(unittest.TestCase):
    def test_nonzero_renumbered_notdef_blocks_normal_unicode(self):
        report = audit_pdf_glyph_integrity(PDF([Page([span()])]))
        self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_notdef_record')
        self.assertEqual(report['findings'][0]['examples'][0]['glyph_id'], 1)
        self.assertNotEqual(report['findings'][0]['examples'][0]['unicode_from_trace'], 0xfffd)

    def test_different_variables_and_glyph_zero_use_the_same_rule(self):
        for char in ['a', 'q', 'β', '五', '=']:
            with self.subTest(char=char):
                report = audit_pdf_glyph_integrity(PDF([Page([span(ord(char), 0)])]))
                self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_notdef_record')

    def test_good_distinct_glyph_is_not_rejected(self):
        report = audit_pdf_glyph_integrity(PDF([Page([span(glyph=2)])]))
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['truetype_record_characters_inspected'], 1)

    def test_whitespace_and_format_controls_are_not_missing_letters(self):
        spans = [span(ord(c), 0) for c in [' ', '\t', '\u200d', '\u2061', '\ufe0f']]
        self.assertEqual(audit_pdf_glyph_integrity(PDF([Page(spans)]))['findings'], [])

    def test_hidden_or_transparent_text_does_not_claim_visible_failure(self):
        report = audit_pdf_glyph_integrity(PDF([Page([span(kind=3), span(opacity=0)])]))
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['visible_trace_characters'], 0)

    def test_stroked_visible_text_is_checked(self):
        self.assertEqual(audit_pdf_glyph_integrity(PDF([Page([span(kind=1)])]))['findings'][0]['code'],
                         'pdf_glyph_notdef_record')

    def test_replacement_unicode_remains_a_blocking_mapping_finding(self):
        report = audit_pdf_glyph_integrity(PDF([Page([span(0xfffd, 2)])]))
        self.assertEqual(report['findings'][0]['code'], 'pdf_unicode_mapping_unresolved')
        self.assertEqual(report['findings'][0]['glyph_ids'], [2])
        self.assertEqual(report['findings'][0]['count'], 1)

    def test_standard_unembedded_and_unsupported_fonts_are_disclosed(self):
        for data, status in [(b'', 'not_embedded'), (b'OTTO' + b'\0' * 12, 'unsupported_font_program')]:
            report = audit_pdf_glyph_integrity(PDF([Page([span()])], {1: data}))
            self.assertEqual(report['font_programs'][0]['status'], status)
            self.assertEqual(report['findings'], [])
            self.assertFalse(report['whole_document_visual_review'])

    def test_malformed_truetype_is_a_structured_failure(self):
        for data in [b'\0\x01\0\0', font_program()[:90]]:
            report = audit_pdf_glyph_integrity(PDF([Page([span()])], {1: data}))
            self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_inspection_failed')

    def test_ambiguous_same_named_fonts_never_claim_the_wrong_glyph(self):
        resources = [(1, 'ttf', 'TrueType', 'AAAAAA+ArbitraryFont', 'F1', '', 0),
                     (2, 'ttf', 'TrueType', 'BBBBBB+ArbitraryFont', 'F2', '', 0)]
        programs = {1: font_program(), 2: font_program(records=[b'missing', b'actual-letter', b'other'])}
        report = audit_pdf_glyph_integrity(PDF([Page([span()], resources)], programs))
        self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')

    def test_out_of_range_glyph_is_unknown_instead_of_an_exception(self):
        report = audit_pdf_glyph_integrity(PDF([Page([span(glyph=99)])]))
        self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')

    def test_zero_width_ligature_continuations_keep_the_painted_glyph_check(self):
        for letters in ['fi', 'ffi', 'ti']:
            with self.subTest(letters=letters):
                trace = span(ord(letters[0]), 2)
                trace['chars'].extend((ord(c), -1, (10, 20), (10, 10, 10, 25)) for c in letters[1:])
                report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
                self.assertEqual(report['findings'], [])
                self.assertEqual(report['compound_glyph_continuation_characters'], len(letters) - 1)
                trace['chars'][0] = (ord(letters[0]), 1, (10, 20), (10, 10, 20, 25))
                report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
                self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_notdef_record')

    def test_unbound_or_nonzero_width_negative_glyph_is_not_a_ligature_escape(self):
        for chars in [[(ord('i'), -1, (10, 20), (10, 10, 10, 25))],
                      [(ord('f'), 2, (10, 20), (10, 10, 20, 25)),
                       (ord('i'), -1, (11, 20), (11, 10, 11, 25))],
                      [(ord('f'), 2, (10, 20), (10, 10, 20, 25)),
                       (ord('i'), -1, (10, 20), (10, 10, 20, 25))]]:
            trace = span()
            trace['chars'] = chars
            report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
            self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')

    def test_short_and_long_locations_have_identical_record_semantics(self):
        self.assertEqual(_truetype_glyphs(font_program(short=True)), _truetype_glyphs(font_program()))

    def test_bad_offsets_duplicate_tables_and_invalid_formats_are_rejected(self):
        data = font_program()
        variants = []
        bad = bytearray(data)
        struct.pack_into('>I', bad, 20, len(data) + 1)
        variants.append(bytes(bad))
        bad = bytearray(data)
        bad[28:32] = b'head'
        variants.append(bytes(bad))
        for value in variants:
            with self.assertRaises(ValueError):
                _truetype_glyphs(value)

    def test_repeated_observations_are_deterministic_and_do_not_mutate_pdf(self):
        raw = font_program()
        pdf = PDF([Page([span(), span()])], {1: raw})
        a = audit_pdf_glyph_integrity(pdf)
        self.assertEqual(a, audit_pdf_glyph_integrity(pdf))
        self.assertEqual(a['findings'][0]['count'], 2)
        self.assertEqual(pdf.programs[1], raw)

    def test_both_export_entrypoints_block_the_shared_finding_without_writing(self):
        import audit_equation_export
        import rendered_format_audit
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.docx'
            Document().save(source)
            pdf_path = root / 'actual.pdf'
            pdf = fitz.open()
            pdf.new_page().insert_text((72, 72), 'A')
            pdf.save(pdf_path)
            pdf.close()
            spec = root / 'spec.json'
            spec.write_text(json.dumps({'roles': {}}))
            mapping = root / 'style-map.json'
            mapping.write_text('{}')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [source, pdf_path]}
            with mock.patch.object(fitz.Document, 'extract_font', return_value=('Helvetica', 'ttf', 'TrueType', font_program())), \
                 mock.patch.object(fitz.Page, 'get_texttrace', return_value=[span(ord('A'), 1, font='Helvetica')]):
                equation = audit_equation_export.audit(source, pdf_path)
                rendered = rendered_format_audit.audit(source, source, pdf_path, spec, style_map_path=mapping)
            self.assertFalse(equation['export_checks_passed'])
            self.assertEqual(rendered['status'], 'issues_found')
            for result in [equation, rendered]:
                self.assertIn('pdf_glyph_notdef_record', {f['code'] for f in result['findings']})
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_repair_code_identity_binds_the_shared_glyph_inspector(self):
        import derive_review_draft_repairs as repairs
        self.assertIn('scripts/pdf_glyph_integrity.py', repairs.CODE_FILES)

    def test_unbound_negative_glyphs_block_without_a_supported_font_program(self):
        programs = [('TrueType', font_program()), ('unembedded', b''),
                    ('CFF', b'OTTO' + b'\0' * 12), ('no_resource', None)]
        for label, data in programs:
            for glyph in [-1, -3]:
                for width in [0, 10]:
                    with self.subTest(program=label, glyph=glyph, width=width):
                        trace = span(ord('A'), glyph)
                        trace['chars'][0] = (ord('A'), glyph, (10, 20), (10, 10, 10 + width, 25))
                        page = Page([trace])
                        if label == 'no_resource':
                            page.resources = []
                        report = audit_pdf_glyph_integrity(PDF([page], {1: data or b''}))
                        self.assertTrue(report['findings'])
                        self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')

    def test_bound_ligatures_do_not_require_an_embedded_truetype_program(self):
        for data in [b'', b'OTTO' + b'\0' * 12]:
            trace = span(ord('f'), 2)
            trace['chars'].append((ord('i'), -1, (10, 20), (10, 10, 10, 25)))
            report = audit_pdf_glyph_integrity(PDF([Page([trace])], {1: data}))
            self.assertEqual(report['findings'], [])
            self.assertEqual(report['compound_glyph_continuation_characters'], 1)

    def test_known_empty_notdef_records_cannot_verify_printing_characters(self):
        program = font_program(records=[b'', b'', b'actual-letter'])
        for letter in ['A', '五']:
            for glyph in [0, 1]:
                with self.subTest(letter=letter, glyph=glyph):
                    report = audit_pdf_glyph_integrity(PDF([Page([span(ord(letter), glyph)])], {1: program}))
                    self.assertTrue(report['findings'])
                    self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_notdef_record')
                    self.assertTrue(report['findings'][0]['requires_manual_review'])

    def test_empty_notdef_keeps_real_glyphs_controls_spaces_and_ligatures(self):
        program = font_program(records=[b'', b'', b'actual-letter'])
        trace = span(ord('f'), 2)
        trace['chars'].append((ord('i'), -1, (10, 20), (10, 10, 10, 25)))
        page = Page([span(ord('A'), 2), span(ord(' '), 0), span(0x2061, 1), trace])
        report = audit_pdf_glyph_integrity(PDF([page], {1: program}))
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['compound_glyph_continuation_characters'], 1)

    def test_both_entrypoints_block_negative_and_empty_notdef_counterexamples(self):
        import audit_equation_export
        import rendered_format_audit
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, pdf_path = root / 'source.docx', root / 'actual.pdf'
            Document().save(source)
            pdf = fitz.open()
            pdf.new_page().insert_text((72, 72), 'A')
            pdf.save(pdf_path)
            pdf.close()
            spec, mapping = root / 'spec.json', root / 'style-map.json'
            spec.write_text(json.dumps({'roles': {}}))
            mapping.write_text('{}')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [source, pdf_path]}
            cases = [(b'', -1), (b'OTTO' + b'\0' * 12, -1),
                     (font_program(records=[b'', b'', b'actual-letter']), 1)]
            for program, glyph in cases:
                with self.subTest(glyph=glyph, embedded=bool(program)), \
                     mock.patch.object(fitz.Document, 'extract_font', return_value=('Helvetica', 'ttf', 'TrueType', program)), \
                     mock.patch.object(fitz.Page, 'get_texttrace', return_value=[span(ord('A'), glyph, font='Helvetica')]):
                    equation = audit_equation_export.audit(source, pdf_path)
                    rendered = rendered_format_audit.audit(source, source, pdf_path, spec, style_map_path=mapping)
                    self.assertFalse(equation['export_checks_passed'])
                    self.assertEqual(rendered['status'], 'issues_found')
                    self.assertTrue(equation['glyph_integrity']['findings'])
                    self.assertTrue(rendered['glyph_integrity']['findings'])
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_embedded_postscript_name_proves_the_subset_resource_binding(self):
        program = font_program(name_records=[(3, 1, 1033, 6, 'ProvenFacePS'.encode('utf-16-be'))])
        resources = [(1, 'ttf', 'Type0', 'ABCDEF+Proven Face Book', 'F1', '', 0)]
        report = audit_pdf_glyph_integrity(PDF([Page([span(ord('i'), 1, font='ProvenFacePS')], resources)], {1: program}))
        self.assertTrue(report['findings'])
        self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_notdef_record')
        self.assertEqual(report['truetype_record_characters_inspected'], 1)
        self.assertEqual(report['font_programs'][0].get('postscript_names'), ['ProvenFacePS'])
        report = audit_pdf_glyph_integrity(PDF([Page([span(ord('i'), 2, font='ProvenFacePS')], resources)], {1: program}))
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['truetype_record_characters_inspected'], 1)

    def test_family_full_names_and_case_folding_do_not_guess_postscript_identity(self):
        for records, trace_name in [
                ([(3, 1, 1033, 4, 'ProvenFacePS'.encode('utf-16-be'))], 'ProvenFacePS'),
                ([(3, 1, 1033, 6, 'ProvenFacePS'.encode('utf-16-be'))], 'provenfaceps'),
                ([], 'A Different Face')]:
            with self.subTest(trace_name=trace_name, name_ids=[r[3] for r in records]):
                program = font_program(name_records=records)
                report = audit_pdf_glyph_integrity(PDF([Page([span(ord('i'), 2, font=trace_name)])], {1: program}))
                self.assertTrue(report['findings'])
                self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')
                self.assertEqual(report['truetype_record_characters_inspected'], 0)

    def test_nonprinting_prefix_clusters_check_the_shared_painted_glyph(self):
        for prefix in [ord(' '), 0x200d]:
            for records in [[b'notdef', b'notdef', b'actual-letter'], [b'', b'', b'actual-letter']]:
                with self.subTest(prefix=prefix, empty=not records[0]):
                    program = font_program(records=records)
                    trace = span(prefix, 2)
                    trace['chars'].append((ord('i'), -1, (10, 20), (10, 10, 10, 25)))
                    report = audit_pdf_glyph_integrity(PDF([Page([trace])], {1: program}))
                    self.assertEqual(report['findings'], [])
                    self.assertEqual(report['truetype_record_characters_inspected'], 1)
                    self.assertEqual(report['compound_glyph_continuation_characters'], 1)
                    trace['chars'][0] = (prefix, 1, (10, 20), (10, 10, 20, 25))
                    report = audit_pdf_glyph_integrity(PDF([Page([trace])], {1: program}))
                    self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_notdef_record')

    def test_pure_nonprinting_clusters_keep_their_existing_exemption(self):
        trace = span(0x200d, 0)
        trace['chars'].append((0xfe0f, -1, (10, 20), (10, 10, 10, 25)))
        report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['visible_trace_characters'], 0)

    def test_nonprinting_prefix_does_not_exempt_unbound_negative_glyphs(self):
        for origin, box in [((11, 20), (11, 10, 11, 25)), ((10, 20), (10, 10, 20, 25))]:
            trace = span(ord(' '), 2)
            trace['chars'].append((ord('i'), -1, origin, box))
            report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
            self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')

    def test_postscript_name_windows_mac_and_malformed_encoding_boundaries(self):
        program = font_program(name_records=[(1, 0, 0, 6, b'ProvenFacePS'),
                                             (3, 1, 1033, 6, 'ProvenFacePS'.encode('utf-16-be'))])
        report = audit_pdf_glyph_integrity(PDF([Page([span()])], {1: program}))
        self.assertEqual(report['font_programs'][0].get('postscript_names'), ['ProvenFacePS'])
        bad = font_program(name_records=[(3, 1, 1033, 6, b'X')])
        report = audit_pdf_glyph_integrity(PDF([Page([span()])], {1: bad}))
        self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_inspection_failed')

    def test_rotated_and_oblique_compounds_follow_the_writing_direction(self):
        for angle in [0, 30, 45, 60, 90, 123, 180, 270, 315]:
            dx, dy = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
            nx, ny = -dy, dx
            points = [(10 - 8 * nx, 20 - 8 * ny), (10 + 3 * nx, 20 + 3 * ny)]
            box = (min(p[0] for p in points), min(p[1] for p in points),
                   max(p[0] for p in points), max(p[1] for p in points))
            for letters in ['fi', 'ffi', 'qi', ' i', '\u200di']:
                for glyph in [1, 2]:
                    with self.subTest(angle=angle, letters=letters, glyph=glyph):
                        trace = span(ord(letters[0]), glyph)
                        trace['dir'] = (dx, dy)
                        trace['chars'].extend((ord(c), -1, (10, 20), box) for c in letters[1:])
                        report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
                        self.assertEqual([f['code'] for f in report['findings']],
                                         ['pdf_glyph_notdef_record'] if glyph == 1 else [])
                        self.assertEqual(report['truetype_record_characters_inspected'], 1)
                        self.assertEqual(report['compound_glyph_continuation_characters'], len(letters) - 1)

    def test_rotated_negative_glyphs_need_proven_zero_advance_and_the_same_origin(self):
        for angle in [0, 45, 90, 270]:
            dx, dy = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
            nx, ny = -dy, dx
            cases = []
            # A genuine advancing character and a zero-advance line displaced
            # along the writing direction must not inherit the previous glyph.
            for advance in [6, -6]:
                points = [(10 + a * dx + b * nx, 20 + a * dy + b * ny)
                          for a in [0, advance] for b in [-8, 3]]
                cases.append(((10, 20), (min(p[0] for p in points), min(p[1] for p in points),
                                        max(p[0] for p in points), max(p[1] for p in points))))
            points = [(10 + dx + b * nx, 20 + dy + b * ny) for b in [-8, 3]]
            cases.append(((10, 20), (min(p[0] for p in points), min(p[1] for p in points),
                                    max(p[0] for p in points), max(p[1] for p in points))))
            cases.append(((11, 20), (11, 10, 11, 25)))
            for origin, box in cases:
                with self.subTest(angle=angle, origin=origin, box=box):
                    trace = span(ord('f'), 2)
                    trace['dir'] = (dx, dy)
                    trace['chars'].append((ord('i'), -1, origin, box))
                    report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
                    self.assertTrue(report['findings'])
                    self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')
        for direction in [None, (), (1,), (0, 0), (float('nan'), 0), (1, float('inf')), ('x', 0)]:
            trace = span(ord('f'), 2)
            trace['dir'] = direction
            trace['chars'].append((ord('i'), -1, (10, 20), (10, 10, 10, 25)))
            report = audit_pdf_glyph_integrity(PDF([Page([trace])]))
            self.assertTrue(report['findings'])
            self.assertEqual(report['findings'][0]['code'], 'pdf_glyph_font_mapping_unresolved')


if __name__ == '__main__':
    unittest.main()
