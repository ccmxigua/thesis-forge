"""Source-owned document-wide Latin fonts; role coverage is not run coverage."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import zipfile
from typing import Any

from lxml import etree

TEXT_FONT_ROLES = (
    "thesis_title_zh", "thesis_title_en", "heading_1", "heading_2", "heading_3", "heading_4",
    "heading_acknowledgments", "heading_appendix", "heading_conclusion",
    "heading_publications", "heading_references", "body_text", "figure_caption", "table_caption",
    "table_text", "abstract_title_zh", "abstract_body_zh", "abstract_title_en", "abstract_body_en",
    "keywords_zh", "keywords_en", "bibliography_heading", "bibliography_entry", "equation",
    "footnote", "header", "footer", "toc", "toc_title", "thesis_type_zh", "thesis_type_en",
    "thesis_author", "thesis_author_en", "thesis_affiliation_date", "thesis_classified_index",
)
_GLOBAL_RULE = re.compile(
    r"(?:论文中出现英文时|论文中的?英文|论文中的?西文|全文的?(?:英文|西文)|所有英文)"
    r"(?:需要|必须|应当|应|须)?(?:使用|采用)"
    r"(?P<font>[A-Za-z][A-Za-z0-9 -]{0,60}?)(?:字体)?[。.]?\Z", re.I,
)
_FONT_NAMES = {name.casefold(): name for name in
               ("Times New Roman", "Arial", "Calibri", "Cambria", "Helvetica")}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def compile_document_latin_font(source: Any) -> str | None:
    if not isinstance(source, str):
        return None
    match = _GLOBAL_RULE.fullmatch(source.strip())
    value = re.sub(r"\s+", " ", match["font"].strip()) if match else ""
    return _FONT_NAMES.get(value.casefold())


def compile_document_font_applicability(source: Any) -> dict | None:
    """Compile only the recognized source's English-presence condition.

    Callers must first establish compile_document_latin_font(source); None
    alone is not proof that an arbitrary source is unconditional.
    """
    if compile_document_latin_font(source) and source.strip().startswith("论文中出现英文时"):
        return {"status": "conditional", "conditions": [{
            "fact": "source_inventory.english_text", "operator": "present", "value": None,
        }], "exceptions": []}
    return None


def _source_applicability_matches(source: str, declaration: Any) -> bool:
    expected = compile_document_font_applicability(source)
    if declaration is None:
        return expected is None
    if declaration == {}:
        return expected is None
    if not isinstance(declaration, dict) or set(declaration) - {"status", "conditions", "exceptions"}:
        return False
    if declaration.get("exceptions") not in (None, []):
        return False
    if expected is None:
        return declaration.get("status") == "always" and declaration.get("conditions") in (None, [])
    return (declaration.get("status") == expected["status"]
            and declaration.get("conditions") == expected["conditions"])


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _latin_font(requirement: Any) -> Any:
    props = requirement.get("properties") if isinstance(requirement, dict) else None
    fonts = props.get("font") if isinstance(props, dict) else None
    return fonts.get("latin") if isinstance(fonts, dict) else None


def _items(value: Any) -> list:
    return value if isinstance(value, list) else []


def document_font_safe_roles(spec: dict, roles: dict | None = None) -> dict:
    """Keep English-only source fonts out of general run/style operations.

    Requirements remain intact. A separate unconditional source requirement
    may authorize the same Latin font as a general role default; the global
    English-only sentence by itself cannot authorize numbers or symbols.
    """
    result = copy.deepcopy(spec.get("roles", {}) if roles is None else roles)
    requirements = [r for r in _items(spec.get("requirements")) if isinstance(r, dict)]
    def unconditional(declaration: Any) -> bool:
        return declaration is None or declaration == {} or (
            isinstance(declaration, dict)
            and not set(declaration) - {"status", "conditions", "exceptions"}
            and declaration.get("status") == "always"
            and declaration.get("conditions", []) == []
            and declaration.get("exceptions", []) == [])
    for role, props in list(result.items()):
        if not isinstance(props, dict) or not isinstance(props.get("font"), dict):
            continue
        latin = props["font"].get("latin")
        global_sources = [r for r in requirements if r.get("role") == role
                          and compile_document_latin_font(r.get("source_text")) == latin
                          and latin is not None]
        if not global_sources:
            continue
        independent = [r for r in requirements if r.get("role") == role
                       and not compile_document_latin_font(r.get("source_text"))
                       and _latin_font(r) == latin
                       and unconditional(r.get("applicability"))]
        if independent:
            continue
        props["font"].pop("latin", None)
        if not props["font"]:
            props.pop("font")
        if not props:
            result.pop(role)
    return result


def materialize_document_font_references(response: Any, chunk: dict) -> tuple[Any, list[dict]]:
    """Complete only exact, current-source catalog edges, never synthesize values.

    A missing/inconsistent catalog, conflicting value or condition, or
    non-executable classification is not permission to guess. The single
    source-compiled English-presence condition is retained on every edge.
    """
    from source_obligation_compiler import verified_current_source_span

    projected = copy.deepcopy(response)
    if not isinstance(projected, dict) or projected.get("contract_version") != "3.0":
        return projected, []
    requirements, reviews = projected.get("requirements"), projected.get("clause_reviews")
    if not isinstance(requirements, list) or not isinstance(reviews, list):
        return projected, []
    rule_spec = chunk.get("rule_spec")
    catalog = _items(rule_spec.get("requirements")) if isinstance(rule_spec, dict) else []
    audits = []
    for clause in _items(chunk.get("clauses")):
        if not isinstance(clause, dict):
            continue
        binding = verified_current_source_span(clause, chunk.get("evidence_context"))
        if binding is None or not (font := compile_document_latin_font(binding[1])):
            continue
        cid, eid = clause["id"], binding[0]
        matching_reviews = [r for r in reviews if isinstance(r, dict) and r.get("clause_id") == cid]
        if len(matching_reviews) != 1 or matching_reviews[0].get("classification") not in {"covered", "executable"}:
            continue
        eligible = [r for r in catalog if isinstance(r, dict)
                    and r.get("clause_ids") == [cid] and eid in _items(r.get("evidence_ids"))
                    and r.get("source_text") == binding[1]]
        if (len(eligible) != len(TEXT_FONT_ROLES)
                or {r.get("role") for r in eligible} != set(TEXT_FONT_ROLES)
                or len({r.get("id") for r in eligible}) != len(eligible)
                or any(not isinstance(r.get("id"), str)
                       or r.get("properties") != {"font": {"latin": font}}
                       or not _source_applicability_matches(binding[1], r.get("applicability"))
                       or r.get("input_prerequisites")
                       for r in eligible)):
            continue
        by_id = {r["id"]: r for r in eligible}
        existing = [r for r in requirements if isinstance(r, dict) and cid in _items(r.get("clause_ids"))]
        if any(r.get("existing_requirement_id") not in by_id
               or r.get("clause_ids") != [cid] or r.get("evidence_ids") != clause.get("evidence_ids")
               or not _source_applicability_matches(binding[1], r.get("applicability"))
               or r.get("input_prerequisites")
               or (r.get("properties") is not None
                   and r["properties"] != by_id[r["existing_requirement_id"]]["properties"])
               or (r.get("role") is not None and r["role"] != by_id[r["existing_requirement_id"]]["role"])
               for r in existing):
            continue
        if len({r["existing_requirement_id"] for r in existing}) != len(existing):
            continue
        selected = {r["existing_requirement_id"] for r in existing}
        missing = [r for r in eligible if r["id"] not in selected]
        if not missing:
            continue
        before = _digest(projected)
        for rule in missing:
            added = {"existing_requirement_id": rule["id"], "role": rule["role"],
                     "properties": copy.deepcopy(rule["properties"]),
                     "clause_ids": [cid], "evidence_ids": copy.deepcopy(clause["evidence_ids"]),
                     "confidence": 1.0, "reason": "Code-bound complete document Latin-font scope."}
            if rule.get("applicability") is not None:
                added["applicability"] = copy.deepcopy(rule["applicability"])
            requirements.append(added)
        audits.append({"policy": "current_source_document_font_catalog_completion_v2",
                       "clause_id": cid, "evidence_id": eid, "font": font,
                       "applicability": compile_document_font_applicability(binding[1]),
                       "source_span": copy.deepcopy(clause["source_span"]),
                       "source_applicability": compile_document_font_applicability(binding[1]),
                       "source_applicability_sha256": _digest(compile_document_font_applicability(binding[1])),
                       "catalog_sha256": _digest(eligible), "added_existing_requirement_ids": [r["id"] for r in missing],
                       "response_before_sha256": before, "response_after_sha256": _digest(projected),
                       "provenance": copy.deepcopy(chunk.get("provenance")), "submission_ready": False})
    return projected, audits


def document_font_policy(spec: dict) -> str | None:
    groups: dict[str, list[dict]] = {}
    for requirement in _items(spec.get("requirements")):
        if isinstance(requirement, dict) and (font := compile_document_latin_font(requirement.get("source_text"))):
            groups.setdefault(font, []).append(requirement)
    if not groups:
        return None
    if len(groups) != 1:
        raise ValueError("conflicting document-wide Latin-font sources")
    font, rules = next(iter(groups.items()))
    source_groups: dict[tuple, list[dict]] = {}
    for rule in rules:
        source_groups.setdefault((tuple(rule.get("clause_ids", [])), tuple(rule.get("evidence_ids", []))), []).append(rule)
    if (not set(TEXT_FONT_ROLES) <= {r.get("role") for r in rules}
            or any(not key[0] or not key[1] or not set(TEXT_FONT_ROLES) <= {r.get("role") for r in group}
                   for key, group in source_groups.items())
            or any(r.get("properties") != {"font": {"latin": font}}
                   or not _source_applicability_matches(r["source_text"], r.get("applicability"))
                   or r.get("input_prerequisites") for r in rules)):
        raise ValueError("incomplete document-wide Latin-font scope")
    # Role-specific exceptions must be adjudicated, not silently overwritten.
    for requirement in _items(spec.get("requirements")):
        other = _latin_font(requirement)
        if other is not None and other != font:
            raise ValueError("document-wide and local Latin-font requirements conflict")
    return font


def document_font_scope_errors(response: dict, chunk: dict) -> list[str]:
    from source_obligation_compiler import verified_current_source_span

    errors = []
    for index, review in enumerate(_items(response.get("clause_reviews"))):
        if not isinstance(review, dict) or review.get("classification") not in {"covered", "executable"}:
            continue
        clause = next((c for c in _items(chunk.get("clauses"))
                       if isinstance(c, dict) and c.get("id") == review.get("clause_id")), None)
        binding = verified_current_source_span(clause, chunk.get("evidence_context"))
        if binding is None or not (font := compile_document_latin_font(binding[1])):
            continue
        rules = [r for r in _items(response.get("requirements")) if isinstance(r, dict)
                 and review["clause_id"] in _items(r.get("clause_ids"))]
        roles = {r.get("role") for r in rules if r.get("properties") == {"font": {"latin": font}}
                 and _source_applicability_matches(binding[1], r.get("applicability"))
                 and not r.get("input_prerequisites")}
        if not set(TEXT_FONT_ROLES) <= roles:
            errors.append(f"$.clause_reviews[{index}]: document_latin_font_scope_incomplete")
        if any(_latin_font(r) not in (None, font) for r in rules):
            errors.append(f"$.clause_reviews[{index}]: document_latin_font_conflict")
        if any(not _source_applicability_matches(binding[1], r.get("applicability")) for r in rules):
            errors.append(f"$.clause_reviews[{index}]: document_latin_font_condition_mismatch")
        if any(r.get("properties") != {"font": {"latin": font}} for r in rules):
            errors.append(f"$.clause_reviews[{index}]: document_latin_font_payload_mismatch")
    return errors


def _latin(text: str) -> bool:
    return any("LATIN" in unicodedata.name(char, "") for char in text)


def apply_document_font(doc: Any, spec: dict) -> dict:
    # Each touched run supplies direct evidence of Latin text; unknown global
    # inventory facts are never defaulted to true. Other conditions are
    # rejected before mutation. Capability preflight retains its independent
    # three-valued source-inventory applicability gate.
    font = document_font_policy(spec)
    count = 0
    if font:
        for part in doc.part.package.parts:
            if not str(part.partname).startswith("/word/") or not part.content_type.endswith("xml"):
                continue
            root = getattr(part, "element", None)
            generic = root is None
            if generic:
                root = etree.fromstring(part.blob, parser=etree.XMLParser(resolve_entities=False, no_network=True))
            touched = False
            for run in root.iter(f"{{{W}}}r"):
                if not _latin("".join(t.text or "" for t in run.findall(f"{{{W}}}t"))):
                    continue
                props = run.find(f"{{{W}}}rPr")
                if props is None:
                    props = etree.Element(f"{{{W}}}rPr"); run.insert(0, props)
                fonts = props.find(f"{{{W}}}rFonts")
                if fonts is None:
                    fonts = etree.Element(f"{{{W}}}rFonts"); props.insert(0, fonts)
                for slot in ("ascii", "hAnsi"):
                    fonts.set(f"{{{W}}}{slot}", font)
                    fonts.attrib.pop(f"{{{W}}}{slot}Theme", None)
                count += 1; touched = True
            if generic and touched:
                part._blob = etree.tostring(root, encoding="UTF-8", xml_declaration=True)
    return {"policy": "document_wordprocessingml_latin_font_v1", "font": font, "formatted_run_count": count,
            "applicability_evidence": {"mode": "observed_wordprocessingml_latin_runs",
                                       "observed_run_count": count,
                                       "global_source_inventory_inferred": False}}


def audit_document_font(path: Any, spec: dict) -> list[dict]:
    try:
        font = document_font_policy(spec)
    except ValueError as error:
        return [{"role": "all_text", "property": "font.latin.scope", "failure_type": "document_font_scope",
                 "reason": str(error)}]
    if font is None:
        return []
    findings = []
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            root = etree.fromstring(package.read(name), parser=etree.XMLParser(resolve_entities=False, no_network=True))
            for index, run in enumerate(root.iter(f"{{{W}}}r")):
                if not _latin("".join(t.text or "" for t in run.findall(f"{{{W}}}t"))):
                    continue
                fonts = run.find(f"{{{W}}}rPr/{{{W}}}rFonts")
                actual = {slot: fonts.get(f"{{{W}}}{slot}") if fonts is not None else None for slot in ("ascii", "hAnsi")}
                if any(value != font for value in actual.values()) or (fonts is not None and any(
                        fonts.get(f"{{{W}}}{slot}Theme") for slot in ("ascii", "hAnsi"))):
                    findings.append({"role": "all_text", "property": "font.latin.run", "part": name,
                                     "run_index": index, "template_value": actual, "required_value": font,
                                     "failure_type": "document_font_run_mismatch"})
            for tag in (f"{{{M}}}t", f"{{{A}}}t"):
                if any(_latin(node.text or "") for node in root.iter(tag)):
                    findings.append({"role": "all_text", "property": "font.latin.non_word_text", "part": name,
                                     "failure_type": "document_font_not_observable",
                                     "reason": "Math/drawing Latin text needs a separate font executor and verifier."})
    return findings
