"""Read-only formatting evidence for caller-bound, real DOCX paragraphs.

This module does not discover roles or use a role's configured style as evidence.
Callers must supply *all* relevant paragraphs from the serialized document. A
missing binding can be represented by None and cannot produce a passing receipt.
Mixed values remain list-valued leaves, compatible with property_receipts.flatten.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from docx.oxml.ns import qn
from lxml import etree

METHOD = 'serialized_docx_visible_instances'
FONT_KEYS = ('latin', 'cjk', 'size_pt', 'bold', 'italic')
PARAGRAPH_KEYS = (
    'alignment', 'first_line_indent_pt', 'first_line_indent_chars',
    'left_indent_pt', 'right_indent_pt', 'space_before_pt', 'space_after_pt',
    'space_before_lines', 'space_after_lines', 'outline_level',
    'page_break_before', 'keep_with_next',
)
UNSUPPORTED_VISIBLE_TAGS = {qn('w:' + tag) for tag in (
    'sym', 'drawing', 'pict', 'object', 'footnoteReference', 'endnoteReference',
    'commentReference', 'footnoteRef', 'endnoteRef', 'annotationRef',
    'separator', 'continuationSeparator', 'pgNum', 'noBreakHyphen', 'softHyphen',
)}

DEFAULT_PATHS = tuple('font.' + key for key in FONT_KEYS) + tuple(
    'paragraph.' + key for key in PARAGRAPH_KEYS) + tuple(
    'paragraph.line_spacing.' + key for key in ('type', 'value', 'unit'))


def _child(node, name):
    return node.find(qn('w:' + name)) if node is not None else None


def _attr(node, name):
    return node.get(qn('w:' + name)) if node is not None else None


def _number(raw, divisor=1):
    try:
        return round(float(raw) / divisor, 3)
    except (TypeError, ValueError, OverflowError):
        return None


def _onoff(raw):
    if raw in (None, 'true', '1', 'on'):
        return True
    if raw in ('false', '0', 'off'):
        return False
    return None


def _unique(values):
    result = []
    for value in values:
        if not any(type(value) is type(prior) and value == prior for prior in result):
            result.append(value)
    return result


def _aggregate(values):
    values = _unique([value for item in values for value in
                      (item if isinstance(item, list) else [item])])
    return values[0] if len(values) == 1 else values or None


def _set_path(target, path, value):
    keys = path.split('.')
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value


def _cjk(char):
    code = ord(char)
    return (0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7FF
            or 0xF900 <= code <= 0xFAFF or 0xFE30 <= code <= 0xFE4F
            or 0xFF00 <= code <= 0xFFEF or 0x20000 <= code <= 0x323AF)


def _locator(paragraph, element=None):
    element = paragraph._p if element is None else element
    return str(paragraph.part.partname) + '#' + element.getroottree().getpath(element)


class _Reader:
    def __init__(self, doc):
        self.doc = doc
        self.styles = {_attr(style, 'styleId'): style for style in doc.styles.element
                       if style.tag == qn('w:style')}
        self.defaults = _child(doc.styles.element, 'docDefaults')
        self.default_rpr = _child(_child(self.defaults, 'rPrDefault'), 'rPr')
        self.default_ppr = _child(_child(self.defaults, 'pPrDefault'), 'pPr')

    def _style_chain(self, identifier, kind):
        if identifier is None:
            identifier = next((key for key, style in self.styles.items()
                               if _attr(style, 'type') == kind
                               and _attr(style, 'default') in ('1', 'true')), None)
        chain, seen = [], set()
        while identifier:
            if identifier in seen or identifier not in self.styles:
                return chain, False
            style = self.styles[identifier]
            if _attr(style, 'type') != kind:
                return chain, False
            seen.add(identifier)
            chain.append(style)
            identifier = _attr(_child(style, 'basedOn'), 'val')
        return chain, True

    def _theme_font(self, token, slot):
        scheme = 'majorFont' if token.startswith('major') else (
            'minorFont' if token.startswith('minor') else None)
        if scheme is None:
            return None
        tag = 'ea' if slot == 'eastAsia' else 'latin'
        ns = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
        for relationship in self.doc.part.rels.values():
            if not relationship.reltype.endswith('/theme') or relationship.is_external:
                continue
            try:
                root = etree.fromstring(relationship.target_part.blob)
                node = root.find('.//' + ns + scheme + '/' + ns + tag)
                return node.get('typeface') or None if node is not None else None
            except (etree.XMLSyntaxError, AttributeError, KeyError, TypeError):
                return None
        return None

    def _font_slot(self, layers, slot):
        for layer in layers:
            fonts = _child(layer, 'rFonts')
            theme = _attr(fonts, slot + 'Theme')
            if theme is not None:
                return self._theme_font(theme, slot)
            direct = _attr(fonts, slot)
            if direct is not None:
                return direct or None
        return None

    @staticmethod
    def _value(layers, child, attribute='val', default=None):
        for layer in layers:
            node = _child(layer, child)
            value = _attr(node, attribute)
            if value is not None:
                return value
        return default

    @staticmethod
    def _toggle(direct, style_layers, defaults, name):
        node = _child(direct, name)
        if node is not None:
            return _onoff(_attr(node, 'val'))
        node = _child(defaults, name)
        value = _onoff(_attr(node, 'val')) if node is not None else False
        for layer in reversed(style_layers):
            node = _child(layer, name)
            if node is None:
                continue
            toggle = _onoff(_attr(node, 'val'))
            if toggle is None:
                return None
            if toggle and value is not None:
                value = not value
        return value

    def _run_font(self, run, text, paragraph_styles, paragraph_chain_valid, in_table=False):
        direct = _child(run, 'rPr')
        char_styles, valid = self._style_chain(_attr(_child(direct, 'rStyle'), 'val'), 'character')
        style_layers = [_child(style, 'rPr') for style in char_styles + paragraph_styles]
        # Conditional table formatting sits above document defaults. Its
        # unresolved values must not be replaced by a known default font.
        layers = [direct] + style_layers + ([] if in_table else [self.default_rpr])
        # Broken style chains may hide a higher-precedence definition. Never
        # silently fall back to document defaults in that situation.
        if not valid or not paragraph_chain_valid:
            layers = [direct]
        complex_nodes = [next((_child(layer, name) for layer in layers
                              if _child(layer, name) is not None), None)
                         for name in ('cs', 'rtl')]
        complex_script = any(node is not None and _onoff(_attr(node, 'val')) is not False
                             for node in complex_nodes) or any(
            0x0590 <= ord(char) <= 0x109F or 0x1780 <= ord(char) <= 0x18AF
            or 0xFB1D <= ord(char) <= 0xFDFF or 0xFE70 <= ord(char) <= 0xFEFF
            for char in text)
        latin_slots = set('ascii' if ord(char) < 128 else 'hAnsi'
                          for char in text if not _cjk(char) and not char.isspace())
        font = {
            'latin': _aggregate([self._font_slot(layers, slot) for slot in sorted(latin_slots)]),
            'cjk': self._font_slot(layers, 'eastAsia') if any(_cjk(char) for char in text) else None,
            'size_pt': _number(self._value(layers, 'sz'), 2),
        }
        for key, tag in (('bold', 'b'), ('italic', 'i')):
            font[key] = (self._toggle(direct, style_layers, self.default_rpr, tag)
                         if valid and paragraph_chain_valid and not in_table else
                         _onoff(_attr(_child(direct, tag), 'val'))
                         if _child(direct, tag) is not None else None)
        if complex_script:
            # Script shaping and bidi font-slot selection need an application
            # renderer; do not certify western font properties for these runs.
            font = {key: None for key in FONT_KEYS}
        hidden = self._toggle(direct, style_layers, self.default_rpr, 'vanish')
        return font, {'latin': bool(latin_slots), 'cjk': any(_cjk(char) for char in text)}, hidden

    def _paragraph(self, paragraph, styles, valid, in_table=False):
        direct = _child(paragraph._p, 'pPr')
        layers = [direct] + ([_child(style, 'pPr') for style in styles]
                             + ([] if in_table else [self.default_ppr]) if valid else [])
        numbering_present = False
        for layer in layers:
            numbering = _child(layer, 'numPr')
            if numbering is None:
                continue
            numbering_present = True
            identifier = _attr(_child(numbering, 'numId'), 'val')
            if identifier is not None:
                numbering_present = identifier != '0'
                break
        # Numbering levels can supply paragraph properties outside styles.xml.
        # Until that cascade is resolved, only direct paragraph values are
        # independently observable; numId=0 explicitly cancels numbering.
        if numbering_present:
            layers = [direct]
        known_defaults = valid and not in_table and not numbering_present
        def value(tag, attribute='val', default=None):
            return self._value(layers, tag, attribute, default if known_defaults else None)
        result = {'alignment': {'both': 'justify', 'distribute': 'distributed'}.get(
            value('jc', default='left'), value('jc', default='left'))}
        for key, tag in (('page_break_before', 'pageBreakBefore'), ('keep_with_next', 'keepNext')):
            node = next((_child(layer, tag) for layer in layers if _child(layer, tag) is not None), None)
            result[key] = _onoff(_attr(node, 'val')) if node is not None else False if known_defaults else None
        level = _number(value('outlineLvl', default='9'))
        result['outline_level'] = int(level) + 1 if level is not None and level.is_integer() else None
        for side in ('left', 'right'):
            result[side + '_indent_pt'] = _number(value('ind', side, '0'), 20)
            # Character-relative indentation cannot be guessed in points.
            if value('ind', side + 'Chars') is not None:
                result[side + '_indent_pt'] = None
        first = next((_child(layer, 'ind') for layer in layers
                      if any(_attr(_child(layer, 'ind'), key) is not None for key in
                             ('firstLine', 'hanging', 'firstLineChars', 'hangingChars'))), None)
        chars = _attr(first, 'hangingChars') or _attr(first, 'firstLineChars')
        points = _attr(first, 'hanging') or _attr(first, 'firstLine')
        sign = -1 if _attr(first, 'hangingChars' if chars is not None else 'hanging') is not None else 1
        result['first_line_indent_chars'] = _number(chars, 100)
        result['first_line_indent_pt'] = None if chars is not None else _number(points if points is not None else '0' if known_defaults else None, 20)
        for key in ('first_line_indent_chars', 'first_line_indent_pt'):
            if result[key] is not None:
                result[key] *= sign
        for side in ('before', 'after'):
            relative = value('spacing', side + 'Lines')
            result['space_' + side + '_lines'] = _number(relative, 100)
            result['space_' + side + '_pt'] = None if relative is not None else _number(value('spacing', side, '0'), 20)
            if value('spacing', side + 'Autospacing') in ('1', 'true', 'on'):
                result['space_' + side + '_pt'] = None
                result['space_' + side + '_lines'] = None
        line_rule = value('spacing', 'lineRule', 'auto')
        line = _number(value('spacing', 'line', '240'))
        if line is None or line_rule not in ('auto', 'exact', 'atLeast'):
            result['line_spacing'] = {'type': None, 'value': None, 'unit': None}
        elif line_rule == 'auto':
            multiple = round(line / 240, 3)
            result['line_spacing'] = {'type': {1.0: 'single', 1.5: 'one_point_five', 2.0: 'double'}.get(multiple, 'multiple'),
                                      'value': multiple, 'unit': 'multiple'}
        else:
            result['line_spacing'] = {'type': 'exact' if line_rule == 'exact' else 'at_least',
                                      'value': round(line / 20, 3), 'unit': 'pt'}
        return result

    def instance(self, binding):
        paragraph = binding.get('paragraph') if isinstance(binding, Mapping) else binding
        if paragraph is None or not hasattr(paragraph, '_p'):
            return {'node_locator': binding.get('node_locator') if isinstance(binding, Mapping) else None,
                    'text': '', 'actual': {}, 'properties': {}, 'runs': [],
                    'reason': 'bound_paragraph_missing'}
        locator = binding.get('node_locator') if isinstance(binding, Mapping) else None
        locator = deepcopy(locator) if locator is not None else _locator(paragraph)
        ppr = _child(paragraph._p, 'pPr')
        styles, valid = self._style_chain(_attr(_child(ppr, 'pStyle'), 'val'), 'paragraph')
        in_table = any(parent.tag == qn('w:tc') for parent in paragraph._p.iterancestors())
        runs = []
        unsupported_visible = []
        for math in paragraph._p.iter(qn('m:oMath')):
            ancestors = list(math.iterancestors())
            if (not any(parent.tag in (qn('w:del'), qn('w:moveFrom')) for parent in ancestors)
                    and next((parent for parent in ancestors if parent.tag == qn('w:p')), None) is paragraph._p):
                unsupported_visible.append({'node_locator': _locator(paragraph, math),
                                            'tag': math.tag})
        field_stack, fields = [], []
        incomplete_fields = any(
            not any((text.text or '').strip() for text in field.iter(qn('w:t')))
            for field in paragraph._p.iter(qn('w:fldSimple')))
        for node in paragraph._p.iter(qn('w:r')):
            # Text in deleted revisions and nested text boxes is not a run of
            # this paragraph. Instruction text is never a displayed field result.
            ancestors = list(node.iterancestors())
            if any(parent.tag in (qn('w:del'), qn('w:moveFrom')) for parent in ancestors):
                continue
            if next((parent for parent in ancestors if parent.tag == qn('w:p')), None) is not paragraph._p:
                continue
            text_parts = []
            for child in node:
                if child.tag == qn('w:fldChar'):
                    kind = _attr(child, 'fldCharType')
                    if kind == 'begin':
                        field = {'result': False, 'text': ''}
                        field_stack.append(field)
                        fields.append(field)
                    elif kind == 'separate' and field_stack:
                        field_stack[-1]['result'] = True
                    elif kind == 'end' and field_stack:
                        field_stack.pop()
                    else:
                        incomplete_fields = True
                elif child.tag == qn('w:t') and all(field['result'] for field in field_stack):
                    text_parts.append(child.text or '')
                    for field in field_stack:
                        field['text'] += child.text or ''
            text = ''.join(text_parts)
            font, applicability, hidden = self._run_font(node, text, styles, valid, in_table)
            if hidden is True:
                continue
            if hidden is None:
                incomplete_fields = True
            if all(field['result'] for field in field_stack):
                unsupported_visible.extend(
                    {'node_locator': _locator(paragraph, child), 'tag': child.tag}
                    for child in node.iter() if child.tag in UNSUPPORTED_VISIBLE_TAGS)
            if not text:
                continue
            runs.append({'node_locator': _locator(paragraph, node), 'text': text,
                         'font': font, 'applicability': applicability})
        incomplete_fields = incomplete_fields or bool(field_stack) or any(
            not field['text'].strip() for field in fields)
        result = {'node_locator': locator, 'text': ''.join(run['text'] for run in runs),
                  'actual': {}, 'properties': {}, 'runs': runs,
                  'unsupported_visible_nodes': unsupported_visible}
        para = self._paragraph(paragraph, styles, valid, in_table)
        scoped_font = isinstance(binding, Mapping) and "font_text_span" in binding
        span = binding.get("font_text_span") if scoped_font else None
        scope_valid = (not scoped_font or (isinstance(span, list) and len(span) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in span)
            and 0 <= span[0] < span[1] <= len(result["text"])
            and binding.get("font_scope_text") == result["text"]))
        if scoped_font:
            result["font_text_span"] = deepcopy(span)
            offset = 0
            for run in runs:
                end = offset + len(run["text"])
                selected = run["text"][max(0, span[0] - offset):max(0, min(end, span[1]) - offset)] if scope_valid else ""
                run["font_scope_text"] = selected
                run["applicability"].update({key: bool(selected.strip()) for key in FONT_KEYS})
                run["applicability"]["cjk"] = any(_cjk(char) for char in selected)
                run["applicability"]["latin"] = any(not _cjk(char) and not char.isspace() for char in selected)
                offset = end
        for path in DEFAULT_PATHS:
            family, key = path.split('.', 1)
            applicable = True
            if family == 'font':
                relevant = [run for run in runs if run['applicability'].get(key, True)]
                applicable = bool(relevant)
                values = [run['font'].get(key) for run in relevant]
                value = _aggregate(values)
                observed = bool(values) and all(v is not None and
                    (not isinstance(v, list) or None not in v) for v in values)
            else:
                value = para
                for component in key.split('.'):
                    value = value.get(component) if isinstance(value, dict) else None
                observed = bool(runs) and value is not None
            if incomplete_fields or (family == 'font' and (unsupported_visible or not scope_valid)):
                observed = False
                applicable = True
            _set_path(result['actual'], path, value)
            result['properties'][path] = {'observed': observed, 'applicable': applicable,
                                          'value': value}
        if incomplete_fields:
            result['reason'] = 'bound_paragraph_field_result_incomplete'
        elif unsupported_visible:
            result['reason'] = 'bound_paragraph_unsupported_visible_content'
        elif not runs:
            result['reason'] = 'bound_paragraph_has_no_visible_text_runs'
        return result


def collect_visible_instance_format(
    doc, bound_by_role: Mapping[str, Iterable[Any]], *,
    property_paths_by_role: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Collect receipt-compatible evidence without modifying document XML.

    Bindings are Paragraph objects or mappings with ``paragraph`` and an optional
    ``node_locator``. An optional ``font_text_span`` [start, end] binds font
    evidence to that exact visible-text interval, authenticated by ``font_scope_text``;
    paragraph properties still cover the whole paragraph. Invalid/missing spans
    remain unverified and all run evidence is retained. Missing/empty bindings are unverified. Paths default to all
    supported font and paragraph leaves; callers may request a subset, including
    unsupported leaves, which remain explicitly unverified. ``instance_evidence``
    retains every binding, paragraph locator and concrete visible run observation.
    """
    reader = _Reader(doc)
    actual, observability, evidence = {}, {}, {}
    requested = property_paths_by_role or {}
    for role in dict.fromkeys([*bound_by_role, *requested]):
        instances = [reader.instance(binding) for binding in bound_by_role.get(role, [])]
        evidence[role] = instances
        actual[role], observability[role] = {}, {}
        for path in requested.get(role, DEFAULT_PATHS):
            relevant = [instance for instance in instances
                        if instance.get('reason') or
                        instance['properties'].get(path, {}).get('applicable', True)]
            observations = [instance['properties'].get(path, {}) for instance in relevant]
            values = [item.get('value') for item in observations]
            complete = bool(observations) and all(item.get('observed') is True for item in observations)
            value = _aggregate(values)
            _set_path(actual[role], path, value)
            observation = {'observed': complete, 'verification_method': METHOD,
                           'instance_count': len(instances), 'observed_instance_count': sum(
                               item.get('observed') is True for item in observations),
                           'target_locators': [item['node_locator'] for item in relevant]}
            if not complete:
                observation['reason'] = ('no_bound_visible_instances' if not instances else
                                         'no_applicable_visible_text' if not relevant else
                                         'visible_instance_property_incomplete')
            elif isinstance(value, list):
                observation['reason'] = 'mixed_visible_instance_values'
            observability[role][path] = observation
    return {'actual_by_role': actual, 'role_observability': observability,
            'instance_evidence': evidence}
