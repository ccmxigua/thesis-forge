"""Observe exported glyph records without changing source text or fonts.

A PDF can preserve ToUnicode while painting a renumbered .notdef glyph.
This narrow check detects exact TrueType glyph-record copies, not arbitrary
tofu shapes or all font substitutions. Unsupported programs stay disclosed.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import math
import re
import struct
import unicodedata
from typing import Any


def _mac_cmap0_gids(data: bytes) -> tuple[int, ...]:
    """The supported simple-font encoding, not a guessed Unicode cmap."""
    tables = _sfnt_tables(data)
    raw = tables.get(b'cmap', b'') if tables else b''
    if len(raw) < 4:
        raise ValueError('font_cmap_unavailable')
    version, count = struct.unpack_from('>HH', raw)
    if version != 0 or count != 1 or 4 + 8 * count > len(raw):
        raise ValueError('font_cmap_invalid')
    offsets = [offset for platform, encoding, offset in
               (struct.unpack_from('>HHI', raw, 4 + 8 * i) for i in range(count))
               if (platform, encoding) == (1, 0)]
    if len(offsets) != 1 or offsets[0] + 262 > len(raw):
        raise ValueError('font_cmap_scope_unhandled')
    offset = offsets[0]
    if struct.unpack_from('>HH', raw, offset) != (0, 262):
        raise ValueError('font_cmap_format_unhandled')
    return tuple(raw[offset + 6:offset + 262])


def _simple_tounicode(data: bytes) -> dict[int, int]:
    """Accept complete one-byte bfchar CMaps only; never execute PostScript."""
    if len(data) > 65536:
        raise ValueError('font_unicode_map_over_budget')
    text = data.decode('ascii')
    if 'beginbfrange' in text or 'usecmap' in text:
        raise ValueError('font_unicode_map_scope_unhandled')
    spaces = re.findall(r'\d+\s+begincodespacerange(.*?)endcodespacerange', text, re.S)
    if len(spaces) != 1 or re.findall(r'<([0-9A-Fa-f]+)>', spaces[0]) != ['00', 'FF']:
        raise ValueError('font_unicode_code_space_unhandled')
    result = {}
    blocks = re.findall(r'(\d+)\s+beginbfchar(.*?)endbfchar', text, re.S)
    for count, body in blocks:
        pairs = re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', body)
        if len(pairs) != int(count):
            raise ValueError('font_unicode_map_invalid')
        for source, target in pairs:
            code = bytes.fromhex(source)
            value = bytes.fromhex(target).decode('utf-16-be')
            if len(code) != 1 or len(value) != 1 or code[0] in result:
                raise ValueError('font_unicode_cluster_unhandled')
            result[code[0]] = ord(value)
    if not result:
        raise ValueError('font_unicode_map_unavailable')
    return result


def _snapshot_reader(document: Any):
    """Read the actual open PDF state; never reopen a stale path or save it."""
    from pypdf import PdfReader
    data = document.tobytes(garbage=0)
    if not 0 < len(data) <= 64 * 1024 * 1024:
        raise ValueError('resource_snapshot_over_budget')
    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted or len(reader.pages) != len(document):
        raise ValueError('resource_snapshot_scope_unhandled')
    return reader


def _page_resource_bindings(reader, document, page, page_index, spans, resources, programs):
    """Prove active simple-font objects for an intentionally bounded PDF subset.

    Unsupported Forms, encodings, clusters or paint correspondence stay unknown.
    A font name alone is never used to choose among same-name objects.
    """
    from pypdf.generic import ContentStream
    source = reader.pages[page_index]
    res = source['/Resources']
    if res.get('/XObject') or len(page.read_contents()) > 8 * 1024 * 1024:
        raise ValueError('resource_page_scope_unhandled')
    page_resources = {}
    for row in resources:
        if len(row) < 7 or row[6] != 0 or row[4] in page_resources:
            raise ValueError('resource_page_names_ambiguous')
        page_resources[row[4]] = row[0]
    operations = ContentStream(source.get_contents(), reader).operations
    if len(operations) > 200000:
        raise ValueError('resource_operations_over_budget')
    groups = []
    current = None
    active = None
    for operands, operator in operations:
        if operator == b'BT':
            if current is not None:
                raise ValueError('resource_text_boundary_invalid')
            current, active = {}, None
        elif operator == b'Tf':
            active = str(operands[0])
        elif operator in (b'Tj', b'TJ'):
            if current is None or active is None:
                raise ValueError('resource_text_font_unresolved')
            strings = [operands[0]] if operator == b'Tj' else [
                value for value in operands[0] if isinstance(value, (str, bytes))]
            for value in strings:
                data = bytes(value) if isinstance(value, bytes) else value.original_bytes
                current.setdefault(active, []).extend(data)
        elif operator == b'ET':
            if current is None:
                raise ValueError('resource_text_boundary_invalid')
            groups.append(current)
            current = None
        elif operator in (b"'", b'"', b'Do'):
            raise ValueError('resource_paint_operator_unhandled')
        elif current is not None and operator in (b'q', b'Q', b'gs', b'cm'):
            raise ValueError('resource_nested_graphics_state_unhandled')
    seqnos = sorted({span['seqno'] for span in spans})
    paint_seqnos = [i for i, item in enumerate(page.get_bboxlog())
                   if item[0] in ('fill-text', 'stroke-text', 'ignore-text')]
    if current is not None or len(groups) != len(seqnos) or seqnos != paint_seqnos:
        raise ValueError('resource_paint_correspondence_unresolved')
    by_seqno = dict(zip(seqnos, groups))
    bindings, evidence, cache = {}, [], {}
    for index, span in enumerate(spans):
        chars = [c for c in span.get('chars', []) if _printing_codepoint(c[0])]
        if not chars or any(type(c[1]) is not int or c[1] < 0 for c in chars):
            continue
        matches = []
        unresolved_active_resource = False
        for key, codes in by_seqno[span['seqno']].items():
            try:
                font_ref = res['/Font'][key]
                xref = font_ref.indirect_reference.idnum
                font = font_ref.get_object()
                program = programs[xref]
                aliases = [_font_key(str(font['/BaseFont']).lstrip('/')),
                           *program['postscript_names']]
                if (page_resources.get(key.lstrip('/')) != xref
                        or str(font['/Subtype']) != '/TrueType' or font.get('/Encoding') is not None
                        or program['status'] != 'truetype_records_inspected'):
                    raise ValueError('resource_active_font_unproven')
                flags = int(font['/FontDescriptor']['/Flags'])
                if not flags & 4 or flags & 32:
                    raise ValueError('resource_active_font_flags_unhandled')
                if key not in cache:
                    data = font['/FontDescriptor']['/FontFile2'].get_object().get_data()
                    if hashlib.sha256(data).hexdigest() != program['font_program_sha256']:
                        raise ValueError('resource_snapshot_font_mismatch')
                    cache[key] = (_simple_tounicode(font['/ToUnicode'].get_object().get_data()),
                                  _mac_cmap0_gids(data))
                unicode_map, gids = cache[key]
                visible = [cid for cid in codes if _printing_codepoint(unicode_map[cid])]
                if (span['font'] in aliases
                        and [unicode_map[cid] for cid in visible] == [c[0] for c in chars]
                        and [gids[cid] for cid in visible] == [c[1] for c in chars]):
                    matches.append((key, xref))
            except (KeyError, ValueError, IndexError, TypeError, AttributeError, struct.error):
                # An uninspected active resource has not been excluded. Dropping
                # it would make a different, supported match falsely unique.
                unresolved_active_resource = True
        if not unresolved_active_resource and len(matches) == 1:
            key, xref = matches[0]
            bindings[index] = xref
            evidence.append({'span': index, 'seqno': span['seqno'], 'resource': key,
                             'font_xref': xref, 'printing_characters': len(chars)})
    return bindings, evidence


def _sfnt_tables(data: bytes) -> dict[bytes, bytes] | None:
    if data[:4] not in {b'\x00\x01\x00\x00', b'true', b'OTTO'}:
        return None
    if len(data) < 12:
        raise ValueError('truncated sfnt header')
    count = struct.unpack_from('>H', data, 4)[0]
    if 12 + count * 16 > len(data):
        raise ValueError('truncated sfnt directory')
    tables = {}
    for index in range(count):
        tag, _, offset, length = struct.unpack_from('>4sIII', data, 12 + index * 16)
        if tag in tables or offset > len(data) or length > len(data) - offset:
            raise ValueError('invalid or duplicate sfnt table')
        tables[tag] = data[offset:offset + length]
    return tables


def _truetype_glyphs(data: bytes) -> tuple[bytes, ...] | None:
    """Read bounded glyf/loca records; CFF and other formats are not inferred."""
    if data[:4] not in {b'\x00\x01\x00\x00', b'true'}:
        return None
    tables = _sfnt_tables(data)
    if not {b'head', b'maxp', b'loca', b'glyf'} <= tables.keys():
        raise ValueError('TrueType glyph tables missing')
    if len(tables[b'head']) < 54 or len(tables[b'maxp']) < 6:
        raise ValueError('truncated TrueType metrics')
    glyph_count = struct.unpack_from('>H', tables[b'maxp'], 4)[0]
    location_format = struct.unpack_from('>h', tables[b'head'], 50)[0]
    if not glyph_count or location_format not in {0, 1}:
        raise ValueError('invalid TrueType location format or glyph count')
    width, scale, code = (2, 2, '>H') if location_format == 0 else (4, 1, '>I')
    locations = tables[b'loca']
    if len(locations) < (glyph_count + 1) * width:
        raise ValueError('truncated loca table')
    offsets = [struct.unpack_from(code, locations, i * width)[0] * scale
               for i in range(glyph_count + 1)]
    glyphs = tables[b'glyf']
    if offsets != sorted(offsets) or offsets[-1] > len(glyphs):
        raise ValueError('invalid glyph record offsets')
    return tuple(glyphs[a:b] for a, b in zip(offsets, offsets[1:]))


def _embedded_postscript_names(data: bytes) -> list[str]:
    """Use actual name-ID 6 records, never guessed family/full-name aliases."""
    tables = _sfnt_tables(data)
    if tables is None or b'name' not in tables:
        return []
    raw = tables[b'name']
    if len(raw) < 6:
        raise ValueError('truncated font name table')
    version, count, storage = struct.unpack_from('>3H', raw)
    if version not in {0, 1} or 6 + count * 12 > len(raw) or not 6 + count * 12 <= storage <= len(raw):
        raise ValueError('invalid font name table bounds')
    names = set()
    for index in range(count):
        platform, encoding, _, name_id, length, offset = struct.unpack_from('>6H', raw, 6 + index * 12)
        if storage + offset > len(raw) or length > len(raw) - storage - offset:
            raise ValueError('font name string outside table')
        if name_id != 6:
            continue
        value = raw[storage + offset:storage + offset + length]
        try:
            if platform in {0, 3}:
                name = value.decode('utf-16-be')
            elif platform == 1 and encoding == 0:
                name = value.decode('mac_roman')
            else:
                continue
        except UnicodeDecodeError as exc:
            raise ValueError('invalid PostScript name encoding') from exc
        # PostScript names are ASCII identifiers. Do not normalize spaces,
        # case, family names or localized full names to make a match.
        if name and all(33 <= ord(char) <= 126 and char not in '[](){}<>/%' for char in name):
            names.add(name)
    return sorted(names)


def _font_key(value: str) -> str:
    return re.sub(r'^[A-Z]{6}\+', '', value)


def _printing_codepoint(value: int) -> bool:
    if not 0 <= value <= 0x10ffff:
        return False
    char = chr(value)
    return (not char.isspace() and unicodedata.category(char) not in {'Cc', 'Cf'}
            and not 0xfe00 <= value <= 0xfe0f and not 0xe0100 <= value <= 0xe01ef)


def _zero_advance_component(char: Any, direction: Any) -> bool:
    """Recognize the trace's zero-advance rectangle in its writing direction.

    A rotated zero-advance character has a nonzero axis-aligned width. Its
    rectangle is the envelope of a line perpendicular to the writing direction
    through the origin. Both envelope dimensions and center must agree; a
    centered positive-advance box is not sufficient. Unknown geometry never
    grants the negative-glyph exemption.
    """
    try:
        dx, dy = direction
        ox, oy = char[2]
        x0, y0, x1, y1 = char[3]
        if not all(math.isfinite(v) for v in (dx, dy, ox, oy, x0, y0, x1, y1)):
            return False
        length = math.hypot(dx, dy)
        if not math.isfinite(length) or not length:
            return False
        dx, dy = dx / length, dy / length
        width, height = x1 - x0, y1 - y0
        if width < 0 or height < 0 or not all(map(math.isfinite, (width, height))):
            return False
        # MuPDF reports floating point axis-aligned envelopes. Allow only
        # numerical rounding at the glyph's scale, not at the page's scale.
        tolerance = 1e-5 * max(1, width, height)
        projection = (x0 + width / 2 - ox) * dx + (y0 + height / 2 - oy) * dy
        return (abs(width * abs(dx) - height * abs(dy)) <= tolerance
                and abs(projection) <= tolerance)
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


def _painted_clusters(chars: Any, direction: Any) -> list[list[Any]]:
    """Retain nonprinting prefixes until their painted glyph is resolved."""
    clusters: list[list[Any]] = []
    anchor = None
    for char in chars:
        glyph_id = char[1]
        if type(glyph_id) is int and glyph_id >= 0:
            anchor = [char]
            clusters.append(anchor)
        elif (type(glyph_id) is int and glyph_id == -1 and anchor is not None
              and tuple(char[2]) == tuple(anchor[0][2])
              and _zero_advance_component(char, direction)):
            anchor.append(char)
        else:
            anchor = None
            clusters.append([char])
    return clusters


def audit_pdf_glyph_integrity(document: Any) -> dict[str, Any]:
    programs: dict[int, dict[str, Any]] = {}
    groups: dict[tuple, dict[str, Any]] = {}
    traced_characters = 0
    inspected_characters = 0
    ligature_continuations = 0
    covered_characters = 0
    resource_reports = []
    reader = None
    reader_attempted = False
    reader_error = None

    def finding(code: str, page: int, font: str, sample: dict | None = None, **facts):
        key = (code, page, font, tuple(facts.get('font_xrefs', [])))
        row = groups.setdefault(key, {'code': code, 'requires_manual_review': True,
                                     'page': page, 'font': font, 'count': 0,
                                     'examples': [], **facts})
        row['count'] += 1
        if code == 'pdf_unicode_mapping_unresolved' and sample is not None:
            row['glyph_ids'] = sorted(set(row.get('glyph_ids', [])) | {sample['glyph_id']})
        if sample is not None and len(row['examples']) < 8:
            row['examples'].append(sample)

    for page_number, page in enumerate(document, 1):
        by_name: dict[str, list[int]] = {}
        for resource in page.get_fonts(full=True):
            xref = int(resource[0])
            if xref <= 0:
                continue
            if xref not in programs:
                row: dict[str, Any] = {'xref': xref, 'base_font': str(resource[3]), 'postscript_names': []}
                try:
                    _, extension, _, data = document.extract_font(xref)
                    row['extension'] = extension
                    if not data:
                        row['status'] = 'not_embedded'
                    else:
                        row['font_program_sha256'] = hashlib.sha256(data).hexdigest()
                        row['glyphs'] = _truetype_glyphs(data)
                        row['postscript_names'] = _embedded_postscript_names(data)
                        row['status'] = 'truetype_records_inspected' if row['glyphs'] is not None else 'unsupported_font_program'
                except (ValueError, RuntimeError, struct.error) as exc:
                    row.update(status='inspection_failed', reason=str(exc))
                programs[xref] = row
            for name in [str(resource[3]), *programs[xref]['postscript_names']]:
                key = _font_key(name)
                if xref not in by_name.setdefault(key, []):
                    by_name[key].append(xref)
        spans = page.get_texttrace()
        bindings, evidence = {}, []
        binding_error = None
        if any(len(by_name.get(_font_key(str(span.get('font', ''))), [])) > 1 for span in spans):
            if not reader_attempted:
                reader_attempted = True
                try:
                    reader = _snapshot_reader(document)
                except Exception as exc:
                    reader_error = 'resource_snapshot_unavailable:' + type(exc).__name__
            if reader is not None:
                try:
                    bindings, evidence = _page_resource_bindings(
                        reader, document, page, page_number - 1, spans, page.get_fonts(full=True), programs)
                except Exception as exc:
                    binding_error = 'resource_page_unhandled:' + type(exc).__name__
            else:
                binding_error = reader_error
            resource_reports.append({'page': page_number, 'method': 'PDF_Tf_object_and_unicode_gid_sequence',
                                     'bindings': evidence, 'unhandled_reason': binding_error})
        failed_on_page = set()
        for span_index, span in enumerate(spans):
            if span.get('type') == 3 or span.get('opacity', 1) <= 0:
                continue
            font = str(span.get('font', ''))
            candidates = by_name.get(_font_key(font), [])
            ambiguous = len(candidates) > 1
            if ambiguous and bindings.get(span_index) in candidates:
                candidates = [bindings[span_index]]
            for cluster in _painted_clusters(span.get('chars', []), span.get('dir')):
                printing = [char for char in cluster if _printing_codepoint(char[0])]
                if not printing:
                    continue
                painted = cluster[0]
                glyph_id = painted[1]
                traced_characters += len(printing)
                ligature_continuations += len(cluster) - 1
                sample = {'unicode_from_trace': printing[0][0], 'glyph_id': glyph_id,
                          'painted_unicode_from_trace': painted[0],
                          'unicode_cluster_from_trace': [char[0] for char in cluster],
                          'origin_pt': list(painted[2]), 'bbox_pt': list(painted[3])}
                for char in printing:
                    if char[0] == 0xfffd:
                        finding('pdf_unicode_mapping_unresolved', page_number, font,
                                {**sample, 'unicode_from_trace': char[0], 'glyph_id': char[1]})
                # A negative/unbound glyph is unresolved regardless of whether
                # its font program is embedded or a supported TrueType font.
                if type(glyph_id) is not int or glyph_id < 0:
                    finding('pdf_glyph_font_mapping_unresolved', page_number, font,
                            sample, font_xrefs=candidates)
                    continue
                if not candidates:
                    finding('pdf_glyph_font_mapping_unresolved', page_number, font,
                            sample, font_xrefs=[])
                    continue
                if ambiguous and span_index not in bindings:
                    finding('pdf_glyph_font_mapping_unresolved', page_number, font, sample,
                            font_xrefs=candidates, resource_binding='unproven')
                    continue
                matches = []
                invalid = []
                for xref in candidates:
                    program = programs[xref]
                    if program['status'] == 'inspection_failed':
                        if (font, xref) not in failed_on_page:
                            finding('pdf_glyph_font_inspection_failed', page_number, font,
                                    font_xrefs=[xref], reason=program['reason'])
                            failed_on_page.add((font, xref))
                        continue
                    glyphs = program.get('glyphs')
                    if glyphs is None:
                        continue
                    if glyph_id >= len(glyphs):
                        invalid.append(xref)
                    else:
                        # Printing characters cannot be verified by a known
                        # empty .notdef record either. Keep spaces, controls
                        # and bound ligature components on their paths above.
                        matches.append((xref, glyphs[glyph_id] == glyphs[0], not glyphs[glyph_id]))
                if matches:
                    inspected_characters += 1
                    covered_characters += len(printing)
                bad = [xref for xref, is_notdef, _ in matches if is_notdef]
                empty = [xref for xref, _, is_empty in matches if is_empty]
                if bad:
                    complete = len(matches) == len(candidates) and not invalid
                    code = ('pdf_glyph_notdef_record' if complete and len(bad) == len(matches)
                            else 'pdf_glyph_font_mapping_unresolved')
                    finding(code, page_number, font, sample, font_xrefs=candidates)
                elif empty:
                    finding('pdf_glyph_empty_record', page_number, font, sample, font_xrefs=candidates)
                elif invalid:
                    finding('pdf_glyph_font_mapping_unresolved', page_number, font,
                            sample, font_xrefs=candidates)
    return {
        'protocol': 'pdf_glyph_record_integrity_v1',
        'verification_scope': 'visible trace Unicode and exact embedded TrueType .notdef record copies',
        'visible_trace_characters': traced_characters,
        'truetype_record_characters_inspected': inspected_characters,
        'compound_glyph_continuation_characters': ligature_continuations,
        'font_programs': [{key: value for key, value in row.items() if key != 'glyphs'}
                          for row in programs.values()],
        'findings': list(groups.values()),
        'resource_binding': resource_reports,
        'record_coverage': {'covered_unicode_characters': covered_characters,
                            'uninspected_unicode_characters': traced_characters - covered_characters,
                            'complete': bool(traced_characters) and covered_characters == traced_characters},
        'specified_typeface_verified': False,
        'all_unicode_glyph_shapes_verified': False,
        'whole_document_visual_review': False,
        'native_editor_verified': False,
    }
