"""Script-scoped OOXML font declarations; never infer installed substitutions."""
from __future__ import annotations
from typing import Any
import unicodedata
from docx.oxml.ns import qn
from docx.text.run import Run

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
THEME_ATTRIBUTE = {'ascii': 'asciiTheme', 'hAnsi': 'hAnsiTheme',
                   'eastAsia': 'eastAsiaTheme', 'cs': 'cstheme'}


def font_comparison_key(name: str) -> str:
    """Keep Unicode family names distinct; aliases never substitute a face."""
    key = name.strip().casefold()
    return {'宋体': 'simsun', '仿宋': 'fangsong',
            '黑体': 'simhei', '楷体': 'kaiti'}.get(key, key)


def cjk_character(character: str) -> bool:
    code = ord(character)
    if unicodedata.category(character) == 'Cn':
        return False
    if unicodedata.name(character, '').startswith(('CJK ', 'IDEOGRAPHIC ', 'HIRAGANA ', 'KATAKANA ', 'HANGUL ', 'BOPOMOFO ', 'KANGXI ')):
        return True
    return any(low <= code <= high for low, high in (
        (0x2E80,0x30FF),(0x3100,0x31FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),
        (0x1100,0x11FF),(0xA960,0xA97F),(0xAC00,0xD7FF),(0xF900,0xFAFF),
        (0xFE30,0xFE4F),(0xFF00,0xFFEF),(0x20000,0x323AF)))


def cjk_text_status(text: str, before: str = '', after: str = '') -> str:
    """Applicability is distinct from knowing a run's font family."""
    combined = before + text + after
    known = False
    for offset, c in enumerate(text, len(before)):
        if c.isspace() or ord(c) < 128 or latin_letter(c):
            continue
        if cjk_character(c):
            known = True; continue
        # Only a demonstrated word-internal apostrophe or a mark attached to
        # a Latin base is non-CJK. An unrelated Latin letter never licenses
        # dropping an emoji, mathematical symbol or unknown character.
        if c in {'\u2019', '\u02bc'} and 0 < offset < len(combined)-1:
            if latin_letter(combined[offset-1]) and latin_letter(combined[offset+1]):
                continue
        if unicodedata.category(c).startswith('M'):
            base = offset-1
            while base >= 0 and unicodedata.category(combined[base]).startswith('M'):
                base -= 1
            if base >= 0 and latin_letter(combined[base]):
                continue
        return 'unknown'
    return 'applicable' if known else 'not_applicable'


def latin_letter(character: str) -> bool:
    return ('A' <= character <= 'Z' or 'a' <= character <= 'z'
            or 'LATIN' in unicodedata.name(character, '') and character.isalpha())


def set_explicit_faces(fonts: Any, *, cjk: str | None = None,
                       latin: str | None = None) -> None:
    """Replace only authorized script slots and their competing theme tokens."""
    replacements = {}
    if cjk:
        replacements['eastAsia'] = cjk
    if latin:
        replacements.update({slot: latin for slot in ('ascii', 'hAnsi', 'cs')})
    for slot, face in replacements.items():
        fonts.set(W + slot, face)
        fonts.attrib.pop(W + THEME_ATTRIBUTE[slot], None)


def declared_slot(fonts: Any, slot: str) -> tuple[bool, str | None]:
    """A theme declaration blocks concrete fallback at the same/lower level.

    CJK themes depend on language/script and theme context. Until that context
    is resolved, reporting the competing concrete name as observed is unsafe.
    """
    if fonts is None:
        return False, None
    if W + THEME_ATTRIBUTE[slot] in fonts.attrib:
        return True, None
    if W + slot in fonts.attrib:
        value = fonts.get(W + slot)
        return True, value if value and value.strip() else None
    return False, None


def style_slot(style: Any, slot: str, resolve: Any = None) -> tuple[bool, str | None]:
    """Resolve one slot, distinguishing an absent value from a broken chain."""
    resolve = resolve or declared_slot
    seen = set()
    while style is not None:
        if not style.style_id or style.style_id in seen:
            return True, None
        seen.add(style.style_id)
        rpr = style.element.find(W + 'rPr')
        fonts = rpr.find(W + 'rFonts') if rpr is not None else None
        declared, face = resolve(fonts, slot)
        if declared:
            return True, face
        references = style.element.findall(W + 'basedOn')
        if not references:
            return False, None
        if len(references) != 1 or not references[0].get(W + 'val'):
            return True, None
        parent = style.base_style
        if (parent is None or parent.style_id != references[0].get(W + 'val')
                or parent.type != style.type):
            return True, None
        style = parent
    return False, None


def referenced_style_slot(properties: Any, reference: str, style: Any, slot: str,
                          resolve: Any = None) -> tuple[bool, str | None]:
    """Do not let python-docx silently replace an explicit bad style reference."""
    references = properties.findall(W + reference) if properties is not None else []
    if references and (len(references) != 1 or not references[0].get(W + 'val')
                       or style is None or style.style_id != references[0].get(W + 'val')):
        return True, None
    return style_slot(style, slot, resolve)


def uniform_direct_face(fonts: Any) -> str | None:
    """The established table boundary: every script slot must agree directly."""
    slots = [declared_slot(fonts, slot) for slot in THEME_ATTRIBUTE]
    faces = [face for declared, face in slots if declared and face]
    return faces[0] if len(faces) == len(slots) and len(set(faces)) == 1 else None


def run_belongs_to_paragraph(run: Any, paragraph: Any) -> bool:
    return next(run._r.iterancestors(W + 'p'), None) is paragraph._p


def script_selection_unresolved(run: Any, paragraph: Any, doc: Any) -> bool:
    """Do not assume normal script slots with forced/unknown cs or rtl.

    This is a conservative gate, not a complete Word script classifier. Direct
    off values override lower switches. Unparsed table conditions and broken
    style references cannot establish that a switch is off.
    """
    direct = run._r.find(W + 'rPr')
    for flag in ('cs', 'rtl'):
        values = direct.findall(W + flag) if direct is not None else []
        if values:
            if len(values) != 1 or values[0].get(W + 'val', 'true') not in {'0', 'false', 'off'}:
                return True
            continue
        if next(paragraph._p.iterancestors(W + 'tbl'), None) is not None:
            return True  # Conditional table run properties are not evaluated.
        for properties, reference, style in ((direct, 'rStyle', run.style),
                (paragraph._p.find(W + 'pPr'), 'pStyle', paragraph.style)):
            refs = properties.findall(W + reference) if properties is not None else []
            if refs and (len(refs) != 1 or style is None or refs[0].get(W + 'val') != style.style_id):
                return True
            seen = set()
            while style is not None:
                if not style.style_id or style.style_id in seen:
                    return True
                seen.add(style.style_id)
                rpr = style.element.find(W + 'rPr')
                values = rpr.findall(W + flag) if rpr is not None else []
                if len(values) > 1 or any(node.get(W + 'val', 'true') not in {'0', 'false', 'off'} for node in values):
                    return True
                refs = style.element.findall(W + 'basedOn')
                if not refs:
                    break
                parent = style.base_style
                if len(refs) != 1 or parent is None or refs[0].get(W + 'val') != parent.style_id or parent.type != style.type:
                    return True
                style = parent
        defaults = doc.styles.element.find(W + 'docDefaults/' + W + 'rPrDefault/' + W + 'rPr')
        values = defaults.findall(W + flag) if defaults is not None else []
        if len(values) > 1 or any(node.get(W + 'val', 'true') not in {'0', 'false', 'off'} for node in values):
            return True
    return False


def direct_ascii_face(fonts: Any) -> str | None:
    """Actual ASCII letters: direct normal and complex slots agree.

    eastAsia may legitimately differ in bilingual runs. This does not resolve
    arbitrary Unicode Latin, CJK or an installed font substitution.
    """
    values = [declared_slot(fonts, slot) for slot in ('ascii', 'hAnsi', 'cs')]
    faces = [face for declared, face in values if declared and face]
    return faces[0] if len(faces) == 3 and len(set(faces)) == 1 else None


def run_cjk_face(run: Any, paragraph: Any, doc: Any) -> str | None:
    if not run_belongs_to_paragraph(run, paragraph):
        return None
    rpr = run._r.find(W + 'rPr')
    fonts = rpr.find(W + 'rFonts') if rpr is not None else None
    independent = uniform_direct_face(fonts)
    if independent is not None:
        return independent
    if script_selection_unresolved(run, paragraph, doc):
        return None
    if next(paragraph._p.iterancestors(W + 'tbl'), None) is not None:
        # Table and conditional table styles are another cascade layer, and
        # script selection can use more than eastAsia. Until that complete
        # context is resolved, neither paragraph styles nor docDefaults prove
        # the effective CJK face. Only identical concrete direct declarations
        # in every script slot make the result independent of those choices.
        return declared_slot(fonts, 'eastAsia')[1]
    declared, face = declared_slot(fonts, 'eastAsia')
    if declared:
        return face
    declared, face = referenced_style_slot(rpr, 'rStyle', run.style, 'eastAsia')
    if declared:
        return face
    declared, face = referenced_style_slot(paragraph._p.find(W + 'pPr'), 'pStyle',
                                          paragraph.style, 'eastAsia')
    if declared:
        return face
    defaults = doc.styles.element.find(W + 'docDefaults/' + W + 'rPrDefault/' + W + 'rPr/' + W + 'rFonts')
    return declared_slot(defaults, 'eastAsia')[1]


def word_run_visibility_issue(run: Any, paragraph: Any) -> str | None:
    """Unknown display flags are not permission to choose a view or drop text."""
    def flag_value(node):
        value = node.get(qn("w:val"), "true")
        if value in {"0", "false", "off"}:
            return False
        if value in {"1", "true", "on"}:
            return True
        return None

    rpr = run._r.rPr
    for flag in ("vanish", "webHidden"):
        direct = rpr.findall(qn("w:" + flag)) if rpr is not None else []
        if direct:
            if len(direct) != 1 or flag_value(direct[0]) is not False:
                return "hidden_display_scope_unresolved"
            continue  # A direct off value overrides lower visibility layers.
        for properties, reference, style in (
            (rpr, "rStyle", run.style), (paragraph._p.pPr, "pStyle", paragraph.style),
        ):
            references = properties.findall(qn("w:" + reference)) if properties is not None else []
            if references and (len(references) != 1 or not references[0].get(qn("w:val"))
                               or style is None or style.style_id != references[0].get(qn("w:val"))):
                return "visibility_style_reference_unresolved"
            seen = set()
            while style is not None:
                if not style.style_id or style.style_id in seen:
                    return "visibility_style_chain_unresolved"
                seen.add(style.style_id)
                props = style.element.rPr
                values = props.findall(qn("w:" + flag)) if props is not None else []
                if len(values) > 1 or any(flag_value(node) is not False for node in values):
                    return "inherited_hidden_display_scope_unresolved"
                parents = style.element.findall(qn("w:basedOn"))
                if not parents:
                    break
                parent = style.base_style
                if (len(parents) != 1 or not parents[0].get(qn("w:val")) or parent is None
                        or parent.style_id != parents[0].get(qn("w:val")) or parent.type != style.type):
                    return "visibility_style_chain_unresolved"
                style = parent
        defaults = paragraph.part.package.main_document_part.styles.element.find(
            qn("w:docDefaults") + "/" + qn("w:rPrDefault") + "/" + qn("w:rPr")
        )
        values = defaults.findall(qn("w:" + flag)) if defaults is not None else []
        if len(values) > 1 or any(flag_value(node) is not False for node in values):
            return "default_hidden_display_scope_unresolved"
    return None


def word_run_display_issue(node: Any, paragraph: Any) -> str | None:
    """One display-scope gate for Word text and opaque object runs."""
    mc = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
    owner = next(node.iterancestors(qn("w:p")), None)
    if owner is not paragraph._p:
        return "nested_paragraph_scope_unresolved"
    if next(node.iter(qn("w:p")), None) is not None:
        return "nested_paragraph_container_scope_unresolved"
    ancestors = list(node.iterancestors())
    if any(ancestor.tag in {mc + name for name in ("AlternateContent", "Choice", "Fallback")} for ancestor in ancestors):
        return "alternate_content_display_scope_unresolved"
    if (any(ancestor.tag in {qn("w:" + name) for name in ("ins", "del", "moveFrom", "moveTo")} for ancestor in ancestors)
            or node.find(qn("w:delText")) is not None):
        return "revision_display_scope_unresolved"
    for ancestor in ancestors:
        if ancestor is paragraph._p:
            break
        if ancestor.tag not in {qn("w:hyperlink"), qn("w:fldSimple")}:
            return "inline_container_display_scope_unresolved"
    return word_run_visibility_issue(Run(node, paragraph), paragraph)
