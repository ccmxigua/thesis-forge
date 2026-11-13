"""Read-only, pre-mutation paragraph role bindings for broad style application.

Build this inventory before changing any source style. Membership is keyed by
the original XML elements, not paragraph wrapper identity or mutable styles.
Unknown specialized content is protected rather than relabelled as body text.
The inventory does not authorize mutation of shared style definitions.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from docx.oxml.ns import qn
from docx.table import Table

try:
    from .docx_semantics import (has_drawing, has_toc_field, has_visible_or_object_content,
                                 iter_document_nodes, matches_structural_detector)
    from .role_registry import normalize_style_name, role_names, structural_detector, style_matches
    from .table_roles import classify as classify_table
    from .toc_refresh import toc_result_paragraphs
except ImportError:
    from docx_semantics import (has_drawing, has_toc_field, has_visible_or_object_content,
                                iter_document_nodes, matches_structural_detector)
    from role_registry import normalize_style_name, role_names, structural_detector, style_matches
    from table_roles import classify as classify_table
    from toc_refresh import toc_result_paragraphs


_HEADINGS = {f"heading_{level}" for level in range(1, 5)}
_REFERENCES = {"heading_references", "bibliography_heading"}
_BACKMATTER = {"heading_acknowledgments", "heading_appendix", "heading_publications"}
_COVER = {"cover_field_label", "cover_field_value", "thesis_title_zh", "thesis_title_en",
          "thesis_type_zh", "thesis_type_en", "thesis_author", "thesis_author_en",
          "thesis_affiliation_date", "thesis_classified_index"}
_DECLARATION = {"declaration_heading", "declaration_body"}
_SIGNATURE = {"signature_line_author", "signature_line_supervisor", "signature_date_line"}
_FRONT = _COVER | _DECLARATION | _SIGNATURE | {
    "abstract_title_zh", "abstract_title_en", "abstract_body_zh", "abstract_body_en",
    "keywords_zh", "keywords_en", "toc", "toc_title"}
_ANNOTATION = re.compile(
    r"^(?:单位|units?|注|注释|备注|notes?|资料来源|数据来源|来源|sources?)\s*[:：]\s*\S", re.I)
_CAPTION_ROLES = {"figure_caption", "table_caption"}
_SUBFIGURE_LABEL = re.compile(r"^[（(]\s*[a-z]\s*[）)]\s*\S", re.I)
_CONTINUED_TABLE = re.compile(r"^续\s*表\s*\d+(?:[.．\-－]\d+)*\s{2,}\S")


@dataclass(frozen=True)
class SourceRoleBindings:
    """An in-memory source snapshot with separately serializable diagnostics."""

    roles_by_element: dict[Any, frozenset[str]]
    reasons_by_element: dict[Any, tuple[str, ...]]
    records: list[dict[str, Any]]
    findings: list[dict[str, Any]]

    def bound(self, role: str, paragraph: Any) -> bool:
        return role in self.roles_by_element.get(paragraph._p, ())

    def body_allowed(self, paragraph: Any) -> bool:
        return self.bound("body_text", paragraph)

    def table_allowed(self, paragraph: Any) -> bool:
        return self.bound("table_text", paragraph)

    def reason(self, paragraph: Any) -> tuple[str, ...]:
        return self.reasons_by_element.get(paragraph._p, ("not_in_source_inventory",))


def _style_matches(role: str, style: Any, explicit: dict[str, str]) -> bool:
    mapped = explicit.get(role)
    if (style_matches(role, style.style_id, style.name)
            or (isinstance(mapped, str) and bool(mapped) and normalize_style_name(mapped) in {
                normalize_style_name(style.style_id), normalize_style_name(style.name)})):
        return True
    seen = set()
    while style is not None and style.element not in seen:
        seen.add(style.element)
        for value in style.element.xpath("./w:aliases/@w:val"):
            for alias in value.split(","):
                # Normal is a shared layout default, not positive body intent.
                if role == "body_text" and normalize_style_name(alias) == "normal":
                    continue
                if style_matches(role, style_name=alias):
                    return True
        style = style.base_style
    return False


def _body_style_matches(style: Any, explicit: dict[str, str]) -> bool:
    if _style_matches("body_text", style, explicit):
        return True
    seen = set()
    while style is not None and style.element not in seen:
        seen.add(style.element)
        # This is the executor's exact school-neutral isolated body style.
        # An arbitrary Normal-derived custom style is not equivalent evidence.
        names = {normalize_style_name(style.name), normalize_style_name(style.style_id)}
        if "thesisbodytext" in names:
            return True
        if "normal" not in names and _style_matches("body_text", style, explicit):
            return True
        style = style.base_style
    return False


def _outline_role(values: list[Any], paragraph: Any, reason: str) -> tuple[set[str], list[str]]:
    try:
        level = int(values[0])
    except (ValueError, TypeError):
        return set(), ["source_outline_level_unresolved"]
    if 0 <= level <= 3 and paragraph.text.strip():
        return {f"heading_{level + 1}"}, [reason]
    if level != 9:
        return set(), ["source_outline_level_unresolved"]
    # Explicit body outline overrides a heading name or inherited outline.
    return set(), []


def _source_roles(paragraph: Any, *, cached: set[Any],
                  explicit: dict[str, str]) -> tuple[set[str], list[str]]:
    style = paragraph.style
    if any(_style_matches(role, style, explicit)
           and not paragraph._p.xpath(f"ancestor::w:{story}")
           for role, story in (("header", "hdr"), ("footer", "ftr"))):
        return set(), ["story_specific_style_outside_story"]
    if paragraph._p in cached:
        return ({"toc"} if has_toc_field(paragraph) else set()), ["toc_cached_result"]
    if has_toc_field(paragraph):
        return {"toc"}, ["toc_field"]
    found = set()
    for role in role_names():
        if role in {"body_text", "table_text", "toc"} | _HEADINGS:
            continue
        if (_style_matches(role, style, explicit)
                or matches_structural_detector(paragraph, structural_detector(role))):
            found.add(role)
    # A special heading's semantics override its generic source outline level.
    if found:
        return found, ["registered_source_semantics"]
    values = paragraph._p.xpath("./w:pPr/w:outlineLvl/@w:val")
    if values:
        return _outline_role(values, paragraph, "direct_source_outline")
    for role in sorted(_HEADINGS & explicit.keys()):
        mapped = explicit[role]
        if isinstance(mapped, str) and normalize_style_name(mapped) in {
                normalize_style_name(style.style_id), normalize_style_name(style.name)}:
            return {role}, ["explicit_source_style_mapping"]
    seen = set()
    while style is not None:
        if style.element in seen:
            return set(), ["source_style_inheritance_unresolved"]
        seen.add(style.element)
        values = style.element.xpath("./w:pPr/w:outlineLvl/@w:val")
        if values:
            return _outline_role(values, paragraph, "inherited_source_outline")
        for role in sorted(_HEADINGS):
            if _style_matches(role, style, {}):
                return {role}, ["registered_heading_style"]
        style = style.base_style
    return set(), []


def _top_level(element: Any, body: Any) -> Any:
    while element.getparent() is not None and element.getparent() is not body:
        element = element.getparent()
    return element


def _annotation_elements(children: list[Any], nodes_by_top: dict[Any, list[Any]],
                         raw_roles: dict[Any, set[str]]) -> set[Any]:
    """Only annotation-shaped paragraphs adjoining a table/figure cluster."""
    annotated = set()
    anchors = set()
    for index, child in enumerate(children):
        nodes = nodes_by_top.get(child, [])
        if (child.tag == qn("w:tbl") or any(
                (raw_roles[n.paragraph._p] & _CAPTION_ROLES
                 and not _ANNOTATION.match(n.paragraph.text.strip()))
                or has_drawing(n.paragraph) for n in nodes)):
            anchors.add(index)
    for anchor in anchors:
        for direction in (-1, 1):
            index = anchor + direction
            while 0 <= index < len(children):
                nodes = nodes_by_top.get(children[index], [])
                if children[index].tag != qn("w:p") or len(nodes) != 1:
                    break
                paragraph = nodes[0].paragraph
                roles = raw_roles[paragraph._p]
                if paragraph._p.xpath("./w:pPr/w:sectPr|.//w:br[@w:type='page']"):
                    break
                # An annotation's content and object context outrank an
                # incorrect caption-style map. Such a line is not an anchor.
                if roles <= _CAPTION_ROLES and _ANNOTATION.match(paragraph.text.strip()):
                    annotated.add(paragraph._p)
                    index += direction
                    continue
                if roles & _CAPTION_ROLES:
                    index += direction
                    continue
                if roles:
                    break
                if paragraph.text.strip():
                    if not _ANNOTATION.match(paragraph.text.strip()):
                        break
                    annotated.add(paragraph._p)
                index += direction
    return annotated


def _specialized_caption_elements(children: list[Any], nodes_by_top: dict[Any, list[Any]],
                                  raw_roles: dict[Any, set[str]]) -> dict[Any, str]:
    """Preserve unsupported subfigure/continuation labels with object evidence.

    A lettered list alone is not a subcaption. Require a preceding drawing and
    a connected genuine main figure caption. Continued table labels must be
    caption-shaped and adjacent to a real table, never just a prose reference.
    """
    def paragraph_at(index):
        members = nodes_by_top.get(children[index], [])
        return members[0].paragraph if children[index].tag == qn("w:p") and len(members) == 1 else None

    def boundary(paragraph):
        return bool(paragraph._p.xpath("./w:pPr/w:sectPr|.//w:br[@w:type='page']"))

    def neighbor(index, direction):
        index += direction
        while 0 <= index < len(children):
            paragraph = paragraph_at(index)
            if paragraph is None or boundary(paragraph):
                return index
            if has_visible_or_object_content(paragraph) or raw_roles[paragraph._p]:
                return index
            index += direction
        return None

    protected = {}
    for index, child in enumerate(children):
        paragraph = paragraph_at(index)
        if paragraph is None or boundary(paragraph) or not raw_roles[paragraph._p] <= _CAPTION_ROLES:
            continue
        text = paragraph.text.strip()
        if _CONTINUED_TABLE.match(text):
            adjacent = [neighbor(index, direction) for direction in (-1, 1)]
            if any(other is not None and children[other].tag == qn("w:tbl") for other in adjacent):
                protected[child] = "continued_table_caption_role_unresolved"
            continue
        if not _SUBFIGURE_LABEL.match(text):
            continue
        previous = neighbor(index, -1)
        before = paragraph_at(previous) if previous is not None else None
        if before is None or boundary(before) or not has_drawing(before):
            continue
        for following in range(index + 1, len(children)):
            candidate = paragraph_at(following)
            if candidate is None or boundary(candidate):
                break
            roles = raw_roles[candidate._p]
            if roles - {"figure_caption"}:
                break
            if matches_structural_detector(candidate, "figure_caption"):
                protected[child] = "subfigure_caption_role_unresolved"
                break
            content = candidate.text.strip()
            if (not content or has_drawing(candidate) or _SUBFIGURE_LABEL.match(content)
                    or _ANNOTATION.match(content)):
                continue
            break
    return protected


def _is_reference_entry(paragraph: Any, explicit: dict[str, str]) -> bool:
    style = paragraph.style
    if _style_matches("bibliography_entry", style, explicit):
        return True
    if not _body_style_matches(style, explicit):
        return False
    text = paragraph.text.strip()
    return bool(re.match(r"^(?:\[\d+\]|［\d+］|【\d+】|\d+[.)、])\s*\S", text)
                or paragraph._p.xpath("./w:pPr/w:numPr")
                or (re.search(r"\b(?:1[89]|20)\d{2}[a-z]?\b", text)
                    and re.search(r"[.．,，()（）]", text)))


def build_source_role_bindings(doc: Any, *,
                               explicit_mappings: dict[str, str] | None = None) -> SourceRoleBindings:
    """Bind provable source roles and fence broad body/table selectors.

    Frontmatter ends at the first genuine source heading, excluding TOC cached
    entries. References and other backmatter form an upper bound: unknown
    later headings cannot silently reopen the broad body region. A document
    with no section markers is allowed to contain ordinary standalone prose.
    ``explicit_mappings`` adds caller-declared role-to-style-name/ID evidence;
    it never bypasses TOC, special-content or broad-role scope protection.
    """
    nodes = list(iter_document_nodes(doc))
    body = doc._body._element
    children = list(body.iterchildren())
    cached = toc_result_paragraphs(doc)
    explicit = dict(explicit_mappings or {})
    raw_roles = {}
    reasons = {}
    nodes_by_top: dict[Any, list[Any]] = {}
    for node in nodes:
        paragraph = node.paragraph
        raw_roles[paragraph._p], reasons[paragraph._p] = _source_roles(
            paragraph, cached=cached, explicit=explicit)
        if node.container == "table_cell" and raw_roles[paragraph._p] & _HEADINGS:
            raw_roles[paragraph._p] -= _HEADINGS
            reasons[paragraph._p].append("table_cell_outline_preserved")
        nodes_by_top.setdefault(_top_level(paragraph._p, body), []).append(node)
    annotations = _annotation_elements(children, nodes_by_top, raw_roles)
    annotations.update(n.paragraph._p for n in nodes if n.container == "table_cell"
                       and _ANNOTATION.match(n.paragraph.text.strip()))
    specialized_captions = _specialized_caption_elements(children, nodes_by_top, raw_roles)
    for element in annotations | specialized_captions.keys():
        raw_roles[element] -= _CAPTION_ROLES
    for element, reason in specialized_captions.items():
        reasons[element].append(reason)
    has_start = any(raw_roles[n.paragraph._p] & ((_HEADINGS | _FRONT)
                    - {"keywords_zh", "keywords_en"})
                    for n in nodes if n.container == "body")
    scope = "frontmatter" if has_start else "body"
    body_scope_evidence = False
    terminal_backmatter = False
    result = {}
    records = []
    findings = []
    for child in children:
        members = nodes_by_top.get(child, [])
        if not members:
            continue
        if child.tag == qn("w:p"):
            roles = raw_roles[child]
            if roles & _REFERENCES:
                scope = "references"
                terminal_backmatter = True
            elif roles & _BACKMATTER:
                scope = "backmatter"
                terminal_backmatter = True
            elif roles & _DECLARATION:
                scope = "declaration"
            elif roles & _COVER:
                scope = "cover"
            elif roles & _SIGNATURE:
                scope = "signature"
            elif roles & {"abstract_title_zh", "abstract_body_zh"}:
                scope = "abstract_zh"
            elif roles & {"abstract_title_en", "abstract_body_en"}:
                scope = "abstract_en"
            elif roles & {"toc", "toc_title"}:
                scope = "toc"
            elif roles & (_HEADINGS | {"heading_conclusion"}):
                scope = "backmatter" if terminal_backmatter else "body"
        if scope != "body":
            body_scope_evidence = False
        table_reason = None
        if child.tag == qn("w:tbl"):
            semantics = set().union(*(raw_roles[n.paragraph._p] for n in members))
            if semantics & (_COVER | _DECLARATION | _SIGNATURE):
                table_reason = "protected_administrative_table"
            elif scope != "body":
                table_reason = "table_outside_body_scope"
            else:
                decision = classify_table(Table(child, doc._body))
                if decision["role"] == "form":
                    table_reason = "protected_form_table"
                elif decision["reasons"] != ["no_bound_data_table_role"]:
                    # Border geometry requires at least two columns; text
                    # typography does not. An unmerged one-column table after
                    # proven body content remains eligible, while forms,
                    # controls, nesting and unanchored layouts stay protected.
                    rows = child.findall(qn("w:tr"))
                    ordinary_single_column = (
                        body_scope_evidence and bool(rows) and child.find(qn("w:tblPr")) is not None
                        and decision["reasons"] == ["tabular_structure_unresolved"]
                        and not decision["embedded_drawing_count"]
                        and not any(re.match(r"^[^:：\r\n]{1,80}[:：]", n.paragraph.text.strip())
                                    for n in members)
                        and all(len(row.findall(qn("w:tc"))) == 1 for row in rows))
                    if not ordinary_single_column:
                        table_reason = "table_layout_role_unresolved"
        for node in members:
            paragraph = node.paragraph
            element = paragraph._p
            roles = set(raw_roles[element])
            why = reasons[element]
            visible = has_visible_or_object_content(paragraph)
            style = paragraph.style
            generic = _body_style_matches(style, explicit)
            specialized_object = bool(element.xpath(".//w:drawing|.//w:pict|.//w:object|.//m:oMath|.//m:oMathPara"))
            if element in annotations:
                why.append("table_annotation_role_unresolved")
            if node.container == "table_cell":
                if table_reason:
                    why.append(table_reason)
                elif (visible and not roles and not specialized_object
                      and (generic or _style_matches("table_text", style, explicit)
                           or style.name == "Compact") and not why):
                    roles.add("table_text")
                    why.append("ordinary_body_table_cell")
            elif (scope == "references" and visible and not roles and not why
                  and not specialized_object and _is_reference_entry(paragraph, explicit)):
                roles.add("bibliography_entry")
                why.append("reference_entry_in_source_section")
            elif (scope == "body" and visible and generic and not roles and not why
                  and not specialized_object):
                roles.add("body_text")
                why.append("ordinary_source_body_prose")
            if not roles and visible:
                if not why:
                    why.append("source_object_preserved" if specialized_object else
                               "specialized_source_style_unresolved" if not generic else
                               "source_region_preserved")
                findings.append({"code": next((r for r in why if r.endswith("unresolved")), why[-1]),
                                 "node_id": node.node_id, "scope": scope,
                                 "disposition": "preserved", "reasons": list(why)})
            result[element] = frozenset(roles)
            if scope == "body" and node.container == "body" and roles & (
                    _HEADINGS | {"body_text", "heading_conclusion"}):
                body_scope_evidence = True
            records.append({"node_id": node.node_id, "xml_order": node.xml_order,
                            "container": node.container, "source_style_name": style.name,
                            "source_style_id": style.style_id, "scope": scope,
                            "roles": sorted(roles), "reasons": list(why),
                            "disposition": "bound" if roles else "preserved"})
    return SourceRoleBindings(result, {p: tuple(r) for p, r in reasons.items()}, records, findings)
