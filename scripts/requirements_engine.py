#!/usr/bin/env python3
"""Evidence-first parser for natural-language thesis formatting requirements.

The parser deliberately keeps deterministic extraction, semantic resolution and
DOCX execution separate.  It can analyse an unseen requirements DOCX without
allowing an LLM to modify a document directly.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET

from format_spec_validation import load_and_validate, validate_instance
from host_review_contract import validate_response as validate_host_review_response
from compliance import (
    ALLOWED_REVIEW_CLASSIFICATIONS,
    build_clause_records,
    classification_requires_requirement,
    report as compliance_report,
)
from semantic_contract import (
    HOST_AGENT_ORIGIN,
    attach_request_provenance,
    evidence_payload,
    request_body_sha256,
    request_envelope_sha256,
    sha256_file,
    sha256_json,
    strict_json_loads,
    validate_response_provenance,
)
from evidence_context_guards import (
    sample_content_guard,
    spine_clearance_external,
    SPINE_CLEARANCE_REASON,
)
from requirements_input import RequirementsInputError, normalize_requirements_input
from resource_registry import materialize_declaration_resources
from template_reconciliation import (
    extract_template_evidence,
    not_supplied_report,
    reconcile_template,
    reconciliation_markdown,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W}
Q = lambda name: f"{{{W}}}{name}"
MCQ = lambda name: f"{{{MC}}}{name}"

SIZE_PT = {
    "初号": 42, "小初": 36, "一号": 26, "小一": 24, "二号": 22,
    "小二": 18, "三号": 16, "小三": 15, "四号": 14, "小四": 12,
    "五号": 10.5, "小五": 9, "六号": 7.5, "小六": 6.5,
}
FONT_NAMES = {
    "宋体": "SimSun", "黑体": "SimHei", "楷体": "KaiTi", "仿宋": "FangSong",
    "微软雅黑": "Microsoft YaHei", "方正小标宋": "FZXiaoBiaoSong-B05",
}
ROLE_PATTERNS = [
    ("thesis_title_zh", r"(?:论文)?中文题目|中文标题|论文题目|封面标题"),
    ("thesis_title_en", r"英文题目|英文标题"),
    ("heading_1", r"一级标题|章标题|第一层次标题|标题[“\"]?附录[A-ZＡ-Ｚ一二三四五六七八九十\d]"),
    ("heading_2", r"二级标题|节标题|第二层次标题"),
    ("heading_3", r"三级标题|第三层次标题"),
    ("heading_4", r"四级标题|第四层次标题"),
    ("heading_acknowledgments", r"致谢标题|后记标题|致谢.*?(?:章标题|不编号标题)"),
    ("heading_appendix", r"附录标题|附录.*?(?:章标题|不编号标题)"),
    ("heading_conclusion", r"结论标题|结论.*?(?:章标题|不编号标题)"),
    ("heading_publications", r"(?:学术|研究|科研)成果标题|发表论文.*?标题|科研情况.*?标题"),
    ("heading_references", r"参考文献标题|参考文献.*?(?:章标题|不编号标题)"),
    ("abstract_title_zh", r"中文摘要标题|中文摘要.*?题头|摘要题头|题头为[“\"]?摘\s*要|摘\s*要\s*[（(].*?(?:居中|黑体)"),
    ("abstract_body_zh", r"中文摘要正文|中文摘要.*?内容|摘要正文|摘要内容.*?(?:宋体|字号|行距|对齐)"),
    ("abstract_title_en", r"英文摘要标题|英文摘要.*?题头|题头为[“\"]?ABSTRACT|ABSTRACT\s*[（(].*?(?:居中|加粗)"),
    ("abstract_body_en", r"英文摘要正文|英文摘要.*?内容|摘要内容.*?Times\s+New\s+Roman"),
    ("keywords_zh", r"中文关键词|关键词"),
    ("keywords_en", r"英文关键词|Key\s*Words?"),
    ("body_text", r"正文(?:中文|文字|内容)?|主体文字|论文段落的文字|除标题、图题、表题之外"),
    ("figure_table_title", r"图(?:、|和|与)?表(?:题|标题)|图表(?:题|标题)"),
    ("figure_caption", r"图题|图名|图注|插图标题"),
    ("table_caption", r"表题|表名|表注|表格标题"),
    ("table_text", r"表(?:格)?(?:内|中)(?:文字|内容|字体|用字)"),
    ("equation", r"公式|方程"),
    ("bibliography_heading", r"参考文献正文标题"),
    ("bibliography_entry", r"参考文献(?:条目|正文|内容)"),
    ("footnote", r"脚注|注释"),
    ("header", r"页眉"),
    ("footer", r"页脚"),
    ("toc_title", r"目录标题|目录两字|“目录”|\"目录\""),
    ("toc", r"目录(?:条目|正文|内容|层级|页码|缩进|前导符)"),
    ("thesis_type_zh", r"中文.*?(?:论文类型|学位类型)|学位论文类型"),
    ("thesis_type_en", r"英文.*?(?:论文类型|学位类型)"),
    ("thesis_author_en", r"英文作者|作者英文"),
    ("thesis_author", r"中文作者|作者姓名"),
    ("thesis_affiliation_date", r"单位.*?日期|培养单位.*?日期|论文定稿时间"),
    ("thesis_classified_index", r"分类号|中图分类号"),
    ("cover_field_label", r"作\s*者\s*[：:]|学\s*号\s*[：:]|导师姓名|学科专业|培养单位|提交日期|密\s*级|加密论文编号"),
]
AMBIGUOUS_ROLES = {
    "标题": ["thesis_title_zh", "heading_1"],
    "摘要": ["abstract_title_zh", "abstract_body_zh"],
    "参考文献": ["bibliography_heading", "bibliography_entry"],
}

ALL_TEXT_ROLES = (
    "thesis_title_zh", "thesis_title_en", "heading_1", "heading_2", "heading_3", "heading_4",
    "heading_acknowledgments", "heading_appendix", "heading_conclusion",
    "heading_publications", "heading_references",
    "body_text", "figure_caption", "table_caption", "table_text",
    "abstract_title_zh", "abstract_body_zh", "abstract_title_en", "abstract_body_en",
    "keywords_zh", "keywords_en", "bibliography_heading", "bibliography_entry",
    "equation", "footnote", "header", "footer", "toc", "toc_title",
    "thesis_type_zh", "thesis_type_en", "thesis_author", "thesis_author_en",
    "thesis_affiliation_date", "thesis_classified_index",
)

TOP_LEVEL_REQUIREMENT_ROLES = {
    "page", "table", "objects", "content_constraints", "conditional_constraints",
    "document_structure", "appendices", "equations", "cover", "declarations",
}
# Generic semantic roles may be understood before an execution adapter is
# available.  Keeping them in the interpretation contract lets capability
# preflight report a precise backend gap instead of collapsing the clause to an
# untyped string.  This list is backend-agnostic and contains no school IDs.
PENDING_SEMANTIC_ROLES = {
    "cover_field_label",
    "cover_field_value",
}
# Text is content, not a singleton style property.  This applies to cover
# labels/values as well as fixed headings, declaration paragraphs, thesis-type
# lines, and any other text role.  A school may therefore have many content
# instances while still sharing one role style definition.
CONTENT_INSTANCE_ROLES = set(ALL_TEXT_ROLES) | PENDING_SEMANTIC_ROLES
ALLOWED_REQUIREMENT_ROLES = set(ALL_TEXT_ROLES) | TOP_LEVEL_REQUIREMENT_ROLES | PENDING_SEMANTIC_ROLES


def _requires_external_artifact_verification(text: str) -> bool:
    """Return true for obligations a DOCX file cannot itself satisfy or prove."""
    # A menu recipe such as “点击文件→打印” is explanatory UI prose, not
    # evidence that a physical deliverable must exist.  External compliance
    # requires an actual material/production/delivery predicate.
    gui_instruction = bool(
        re.search(r"(?:Word|WPS|菜单|工具栏|功能区|选项卡|对话框|按钮|快捷键|鼠标)", text, re.I)
        and re.search(r"(?:点击|单击|双击|右键|选中|打开|选择|按下|依次)", text)
    )
    # ``以便装订`` is a purpose phrase attached to a page-margin rule, not
    # itself a physical-production obligation.  Binding is external only when
    # the clause describes a deliverable/production action (or an explicit
    # physical artifact), while ``装订线`` remains a DOCX page property.
    physical = re.search(
        r"(?:实体|纸质|硬质)(?:封皮|封面|论文|印刷本)|"
        r"封面(?:纸张|材质|颜色|底色)|书脊|防伪纸|"
        r"(?:双面|单面)?印刷|(?:胶装|精装|线装)|"
        r"(?:装订(?!线)).{0,4}(?:成册|本|册)|"
        r"(?:成册|本|册).{0,4}装订(?!线)|"
        r"(?:必须|须|应当|要求|统一|提交|上交|送交|制作|打印|印制).{0,12}装订(?!线)|"
        r"(?:提交|上交|送交|制作|打印|印制).{0,12}(?:纸质|实体|册|份)",
        text,
    )
    actual_signature = re.search(r"亲笔签名|实际签名|本人签字|导师签字|签字确认|完成签署|已签署", text)
    placeholder_only = re.search(r"签名栏|签字栏|空白签名|签名占位|签字占位", text)
    return bool((physical and not gui_instruction) or (actual_signature and not placeholder_only))


def _normalized_exact_text(text: Any) -> str:
    """Normalize presentation-only differences for auditable exact matching."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"\s+", "", value)
    return value.strip(" ：:。；;，,、.!！？?\t\r\n")


def _style_properties_for_role(role: str, props: dict[str, Any]) -> dict[str, Any]:
    """Return only shared style properties for a normalized requirement.

    ``text`` is deliberately removed for text-bearing roles.  It is retained
    in ``content_instances`` with clause/evidence provenance, so literal
    content is not accidentally treated as a style property or compared
    against a serialized style snapshot.
    """
    if role not in CONTENT_INSTANCE_ROLES:
        return copy.deepcopy(props)
    return {
        key: copy.deepcopy(value)
        for key, value in props.items()
        if key != "text"
    }


def _requirement_properties_for_role(role: str, props: dict[str, Any]) -> dict[str, Any]:
    """Keep every executable requirement non-empty after role projection.

    Literal text is not projected into the singleton ``roles`` style map, but
    it remains the executable property of the clause-scoped requirement and is
    also retained in ``content_instances``.  This prevents a valid exact-text
    requirement from degenerating into an empty-properties contract (the
    historic C00023 failure mode).
    """
    normalized = _style_properties_for_role(role, props)
    text = props.get("text") if isinstance(props, dict) else None
    if role in CONTENT_INSTANCE_ROLES and isinstance(text, str) and text.strip():
        normalized["text"] = text.strip()
    return normalized


def _sanitize_role_specs_for_backend(roles: dict[str, Any]) -> dict[str, Any]:
    """Keep role objects executable while retaining detail on requirements.

    ``numbering.depth`` is a declarative per-requirement hint.  The current
    application backend cannot execute a role-wide depth, so leaving it on a
    singleton role makes the otherwise valid format spec fail validation.  The
    requirement-level property remains intact for audit and future adapters.
    """
    normalized = copy.deepcopy(roles)
    for role, role_spec in normalized.items() if isinstance(normalized, dict) else []:
        if not isinstance(role_spec, dict):
            continue
        numbering = role_spec.get("numbering")
        if isinstance(numbering, dict) and "depth" in numbering:
            numbering.pop("depth", None)
            if not numbering:
                role_spec.pop("numbering", None)
    return normalized


def _cover_field_instance_id(role: str, field_key: str) -> str:
    digest = hashlib.sha256(f"{role}\0{field_key}".encode("utf-8")).hexdigest()[:16]
    return f"CFI-{digest}"


def _register_content_instance(
    instances: list[dict[str, Any]], item: dict[str, Any], role: str,
    props: dict[str, Any], clause_ids: set[str], cited_evidence: set[str],
    clause_map: dict[str, dict[str, Any]], reason: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Register one literal content value without merging it into a role.

    The optional ``field_key`` lets a host agent identify a field semantically
    (for example ``school_code``).  Existing responses do not need to provide
    it: a deterministic role-plus-normalized-text identity preserves backward
    compatibility and still keeps different labels/values independent.
    """
    if role not in CONTENT_INSTANCE_ROLES or "text" not in props:
        return None, None
    text = props.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, {"reason": "content_instance_text_must_be_nonempty"}
    explicit_key = item.get("field_key")
    if explicit_key is not None and (not isinstance(explicit_key, str) or not explicit_key.strip()):
        return None, {"reason": "field_key_must_be_nonempty_string"}
    field_key = (explicit_key.strip() if isinstance(explicit_key, str) and explicit_key.strip()
                 else f"{role}:{_normalized_exact_text(text)}")
    normalized_text = _normalized_exact_text(text)
    style_properties = _style_properties_for_role(role, props)
    for instance in instances:
        if instance.get("role") != role or instance.get("field_key") != field_key:
            continue
        existing_text = str(instance.get("text") or "")
        if _normalized_exact_text(existing_text) != normalized_text:
            return None, {
                "reason": "content_instance_identity_conflict",
                "field_key": field_key,
                "existing_text": existing_text,
                "new_text": text,
            }
        if style_properties:
            style_conflicts = _deep_merge(
                instance.setdefault("style_properties", {}), style_properties
            )
            if style_conflicts:
                property_name, old_value, new_value = style_conflicts[0]
                return None, {
                    "reason": "content_instance_style_conflict",
                    "field_key": field_key,
                    "property": property_name,
                    "existing_value": old_value,
                    "new_value": new_value,
                }
        instance["clause_ids"] = sorted(set(instance.get("clause_ids") or []) | clause_ids)
        instance["evidence_ids"] = sorted(set(instance.get("evidence_ids") or []) | cited_evidence)
        return str(instance["id"]), None

    instance_id = _cover_field_instance_id(role, field_key)
    source_text = " | ".join(
        clause_map[cid].get("text", "")
        for cid in sorted(clause_ids)
        if cid in clause_map
    )
    instance = {
        "id": instance_id,
        "field_key": field_key,
        "role": role,
        "text": text.strip(),
        "order": len(instances) + 1,
        "clause_ids": sorted(clause_ids),
        "evidence_ids": sorted(cited_evidence),
        "source_text": source_text,
        "reason": reason.strip(),
    }
    if style_properties:
        instance["style_properties"] = style_properties
    instances.append(instance)
    return instance_id, None


def _iter_text_nodes(el: ET.Element, *, include_textboxes: bool = False):
    """Yield text nodes without letting nested textboxes duplicate a paragraph.

    Word's ``mc:AlternateContent`` often stores the same textbox in both a
    ``mc:Choice`` and a ``mc:Fallback`` branch.  The outer paragraph is still
    the container for those nodes, so a descendant XPath would count the
    textbox once as outer paragraph text and again as textbox evidence.
    Explicit textbox extraction opts back in at the paragraph root.
    """
    def walk(node: ET.Element):
        if node is not el and node.tag == Q("txbxContent") and not include_textboxes:
            return
        if node.tag == Q("t"):
            yield node
        for child in list(node):
            yield from walk(child)

    yield from walk(el)


def _text(el: ET.Element, *, include_textboxes: bool = False) -> str:
    return "".join(t.text or "" for t in _iter_text_nodes(el, include_textboxes=include_textboxes)).strip()


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in list(parent)}


def _ancestor(parent_map: dict[ET.Element, ET.Element], node: ET.Element, tags: set[str]) -> ET.Element | None:
    current = parent_map.get(node)
    while current is not None:
        if current.tag in tags:
            return current
        current = parent_map.get(current)
    return None


def _run_format(el: ET.Element) -> dict[str, Any]:
    rpr = el.find("w:rPr", NS)
    if rpr is None:
        return {}
    out: dict[str, Any] = {}
    fonts = rpr.find("w:rFonts", NS)
    if fonts is not None:
        out["fonts"] = {k: fonts.get(Q(k)) for k in ("eastAsia", "ascii", "hAnsi") if fonts.get(Q(k))}
    sz = rpr.find("w:sz", NS)
    if sz is not None and sz.get(Q("val")):
        out["size_pt"] = int(sz.get(Q("val"))) / 2
    if rpr.find("w:b", NS) is not None:
        out["bold"] = rpr.find("w:b", NS).get(Q("val"), "1") != "0"
    if rpr.find("w:i", NS) is not None:
        out["italic"] = rpr.find("w:i", NS).get(Q("val"), "1") != "0"
    return out


def _paragraph_evidence(p: ET.Element, eid: str, kind: str, location: dict[str, Any]) -> dict[str, Any] | None:
    text = _text(p)
    if not text:
        return None
    ppr = p.find("w:pPr", NS)
    style = None
    if ppr is not None:
        ps = ppr.find("w:pStyle", NS)
        style = ps.get(Q("val")) if ps is not None else None
    runs = []
    for r in p.findall("w:r", NS):
        rt = _text(r)
        if rt:
            runs.append({"text": rt, "format": _run_format(r)})
    return {"id": eid, "kind": kind, "text": text, "style_id": style, "runs": runs, "location": location}


def extract_document_evidence(docx: Path) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    with zipfile.ZipFile(docx) as zf:
        names = set(zf.namelist())
        doc = ET.fromstring(zf.read("word/document.xml"))
        body = doc.find("w:body", NS)
        order = 0
        if body is not None:
            for child_index, child in enumerate(list(body)):
                if child.tag == Q("p"):
                    item = _paragraph_evidence(child, f"E{len(evidence)+1:05d}", "paragraph", {"part": "document", "child_index": child_index, "order": order})
                    if item:
                        evidence.append(item); order += 1
                elif child.tag == Q("tbl"):
                    for row_i, tr in enumerate(child.findall("w:tr", NS)):
                        for col_i, tc in enumerate(tr.findall("w:tc", NS)):
                            for para_i, p in enumerate(tc.findall("w:p", NS)):
                                item = _paragraph_evidence(p, f"E{len(evidence)+1:05d}", "table_cell", {"part": "document", "table_child_index": child_index, "row": row_i, "column": col_i, "paragraph": para_i, "order": order})
                                if item:
                                    evidence.append(item); order += 1
        # Text boxes are not represented by python-docx.  Capture them as
        # separate evidence, but do not count the same AlternateContent twice.
        # The outer paragraph extractor deliberately skips nested txbxContent;
        # here we prefer mc:Choice over mc:Fallback when both are present.
        known = {(e["text"], e["location"].get("part")) for e in evidence}
        for part in sorted(n for n in names if re.match(r"word/(header|footer)\d+\.xml$", n)):
            root = ET.fromstring(zf.read(part))
            part_parents = _parent_map(root)
            for i, p in enumerate(root.findall(".//w:p", NS)):
                if _ancestor(part_parents, p, {Q("txbxContent")}) is not None:
                    continue
                item = _paragraph_evidence(p, f"E{len(evidence)+1:05d}", "header_footer", {"part": part, "paragraph": i, "order": order})
                if item and (item["text"], part) not in known:
                    evidence.append(item); order += 1
        parent_map = _parent_map(doc)
        alternate_nodes = [node for node in doc.iter() if node.tag == MCQ("AlternateContent")]
        alternate_indexes = {id(node): index for index, node in enumerate(alternate_nodes)}
        for i, txbx in enumerate(doc.findall(".//w:txbxContent", NS)):
            alternate = _ancestor(parent_map, txbx, {MCQ("AlternateContent")})
            branch_node = _ancestor(parent_map, txbx, {MCQ("Choice"), MCQ("Fallback")})
            branch = branch_node.tag.rsplit("}", 1)[-1] if branch_node is not None else None
            if alternate is not None and branch == "Fallback":
                choice_has_textbox = any(
                    choice.find(".//w:txbxContent", NS) is not None
                    for choice in alternate.findall("mc:Choice", {"mc": MC})
                )
                if choice_has_textbox:
                    continue
            location_base = {
                "part": "document", "textbox": i, "paragraph": None, "order": order,
            }
            if alternate is not None:
                location_base["alternate_content_index"] = alternate_indexes[id(alternate)]
                location_base["alternate_branch"] = branch
            for j, p in enumerate(txbx.findall(".//w:p", NS)):
                location = dict(location_base)
                location["paragraph"] = j
                item = _paragraph_evidence(p, f"E{len(evidence)+1:05d}", "textbox", location)
                if item:
                    evidence.append(item); order += 1
        page = _extract_page(doc)
        structure = _extract_structure(zf, doc, names)
        style_names = structure.get("style_names", {})
    for i, item in enumerate(evidence):
        item["context_before"] = [evidence[j]["text"] for j in range(max(0, i-2), i)]
        item["context_after"] = [evidence[j]["text"] for j in range(i+1, min(len(evidence), i+3))]
        item["style_name"] = style_names.get(item.get("style_id"))
        item["xml_locator"] = {"part": item.get("location", {}).get("part"),
                               **{key: value for key, value in item.get("location", {}).items()
                                  if key != "part"}}
    return {"source_document": str(docx), "evidence": evidence, "page_evidence": page,
            "structure_evidence": structure}


def _extract_page(doc: ET.Element) -> dict[str, Any]:
    sect = doc.find(".//w:sectPr", NS)
    if sect is None:
        return {}
    out: dict[str, Any] = {}
    sz = sect.find("w:pgSz", NS)
    mar = sect.find("w:pgMar", NS)
    if sz is not None:
        out["width_twips"] = int(sz.get(Q("w"), "0")); out["height_twips"] = int(sz.get(Q("h"), "0"))
        out["orientation"] = sz.get(Q("orient"), "portrait")
    if mar is not None:
        out["margins_twips"] = {k: int(mar.get(Q(k))) for k in ("top", "bottom", "left", "right", "header", "footer", "gutter") if mar.get(Q(k))}
    return out


def _extract_structure(zf: zipfile.ZipFile, doc: ET.Element, names: set[str]) -> dict[str, Any]:
    """Extract auditable section and PAGE-field evidence for semantic review."""
    body = doc.find("w:body", NS)
    if body is None:
        return {"sections": [], "page_fields": []}
    style_names: dict[str, str] = {}
    if "word/styles.xml" in names:
        styles = ET.fromstring(zf.read("word/styles.xml"))
        for style in styles.findall("w:style", NS):
            style_id = style.get(Q("styleId"))
            name = style.find("w:name", NS)
            if style_id and name is not None and name.get(Q("val")):
                style_names[style_id] = name.get(Q("val"), style_id)
    sections: list[dict[str, Any]] = []
    current_texts: list[dict[str, Any]] = []
    section_index = 1

    def finish(sectpr: ET.Element | None) -> None:
        nonlocal current_texts, section_index
        if sectpr is None and not current_texts:
            return
        pg = sectpr.find("w:pgNumType", NS) if sectpr is not None else None
        refs = []
        if sectpr is not None:
            for ref in sectpr.findall("w:footerReference", NS):
                refs.append({"type": ref.get(Q("type"), "default"),
                             "relationship_id": ref.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")})
        sections.append({
            "section_index": section_index,
            "paragraph_count": len(current_texts),
            "first_paragraphs": current_texts[:8],
            "last_paragraphs": current_texts[-4:],
            "page_number_format": pg.get(Q("fmt")) if pg is not None else None,
            "page_number_start": int(pg.get(Q("start"))) if pg is not None and pg.get(Q("start")) else None,
            "footer_references": refs,
        })
        section_index += 1; current_texts = []

    final_sect = body.find("w:sectPr", NS)
    for child in list(body):
        if child.tag != Q("p"):
            continue
        text = _text(child)
        ppr = child.find("w:pPr", NS); style = None; num_id = None; level = None
        if ppr is not None:
            ps = ppr.find("w:pStyle", NS); style = ps.get(Q("val")) if ps is not None else None
            numpr = ppr.find("w:numPr", NS)
            if numpr is not None:
                num = numpr.find("w:numId", NS); ilvl = numpr.find("w:ilvl", NS)
                num_id = num.get(Q("val")) if num is not None else None
                level = int(ilvl.get(Q("val"))) if ilvl is not None and ilvl.get(Q("val")) else None
        # Empty styled/numbered paragraphs can be semantic heading placeholders
        # in Word templates.  Preserve them so a body-start boundary is not lost.
        if text or style or num_id:
            current_texts.append({"text": text[:240], "style_id": style,
                                  "style_name": style_names.get(style) if style else None,
                                  "numbering_id": num_id, "numbering_level": level,
                                  "is_empty": not bool(text)})
        boundary = ppr.find("w:sectPr", NS) if ppr is not None else None
        if boundary is not None:
            finish(boundary)
    finish(final_sect)

    page_fields = []
    for part in sorted(n for n in names if re.match(r"word/(header|footer)\d+\.xml$", n)):
        root = ET.fromstring(zf.read(part))
        simple = [node.get(Q("instr"), "") for node in root.findall(".//w:fldSimple", NS)
                  if "PAGE" in node.get(Q("instr"), "").upper()]
        complex_instr = [node.text or "" for node in root.findall(".//w:instrText", NS)
                         if "PAGE" in (node.text or "").upper()]
        if simple or complex_instr:
            page_fields.append({"part": part, "count": len(simple) + len(complex_instr),
                                "instructions": simple + complex_instr})
    return {"section_count": len(sections), "sections": sections, "page_fields": page_fields,
            "style_names": style_names}


def _split_role_segments(text: str) -> list[str]:
    """Split a sentence only when a later phrase starts a new explicit role."""
    # Role patterns overlap in ordinary Chinese phrases: ``图表题`` also
    # contains the shorter ``表题`` pattern. Treat every regex match as a
    # boundary would drop the leading ``图`` and silently change the role to
    # table-only, so select non-overlapping matches first.
    matches: list[tuple[int, int, int]] = []
    for pattern_index, (role, pattern) in enumerate(ROLE_PATTERNS):
        if role.startswith("heading_") and "页眉" in text and re.search(r"右(?:侧|边)?.*?(?:一级标题|章标题)|STYLEREF", text, re.I):
            continue
        matches.extend((match.start(), match.end(), pattern_index)
                       for match in re.finditer(pattern, text, re.I))
    selected: list[tuple[int, int, int]] = []
    for candidate in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]), item[2])):
        start, end, _ = candidate
        if any(start < existing_end and existing_start < end
               for existing_start, existing_end, _ in selected):
            continue
        selected.append(candidate)
    starts = sorted({start for start, _, _ in selected})
    if len(starts) <= 1:
        return [text]
    segments = []
    # Preserve substantive text before the first role marker.  Page clauses
    # commonly begin with A4/margins and only later mention header/footer;
    # dropping this prefix makes those values impossible to cite in contract
    # 2.1 even though they are present in the source evidence.
    if starts[0] > 0:
        prefix = text[:starts[0]].strip(" ，,、：:;；。和及")
        if prefix:
            segments.append(prefix)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        segment = text[start:end].strip(" ，,、：:;；。和及")
        if segment:
            segments.append(segment)
    return segments or [text]


def split_clauses(evidence_doc: dict[str, Any]) -> list[dict[str, Any]]:
    clauses = []
    for ev in evidence_doc["evidence"]:
        text = re.sub(r"\s+", " ", ev["text"]).strip()
        strong_parts = [p.strip(" ：:;；。") for p in re.split(r"[。；;\n]+", text) if p.strip(" ：:;；。")]
        parts = [segment for part in strong_parts for segment in _split_role_segments(part)]
        for part_i, part in enumerate(parts):
            if len(part) < 3:
                continue
            clauses.append({
                "id": f"C{len(clauses)+1:05d}", "text": part,
                "source_text_full": text,
                "evidence_ids": [ev["id"]], "source_kind": ev["kind"],
                "context_before": ev["context_before"], "context_after": ev["context_after"],
                "location": ev["location"], "part_index": part_i,
                "evidence_context": {
                    "id": ev["id"], "kind": ev["kind"], "style_id": ev.get("style_id"),
                    "style_name": ev.get("style_name"), "runs": copy.deepcopy(ev.get("runs", [])),
                    "location": copy.deepcopy(ev.get("location", {})),
                    "xml_locator": copy.deepcopy(ev.get("xml_locator", {})),
                    "context_before": copy.deepcopy(ev.get("context_before", [])),
                    "context_after": copy.deepcopy(ev.get("context_after", [])),
                },
            })
    return clauses


def identify_role(text: str) -> tuple[str | None, list[str]]:
    # Heading words inside a dynamic-header description are field references,
    # not a new heading-format requirement.
    if "页眉" in text:
        return "header", ["header"]
    # Formatting immediately describing a page number belongs to the footer,
    # not to an unknown paragraph role.
    if re.search(r"页码.*?(?:字体|字号|数字两侧|修饰线)", text):
        return "footer", ["footer"]
    for role, pattern in ROLE_PATTERNS:
        if re.search(pattern, text, re.I):
            return role, [role]
    # A standalone document-wide Latin-font statement is executable for every
    # textual role. Explicit roles above (for example 正文…英文…) take priority.
    if (re.search(r"(?:全文|论文中出现英文|论文中的?西文|所有英文|英文字母|阿拉伯数字).*(?:Times\s+New\s+Roman)", text, re.I)
            and not re.search(r"(?:英文题目|英文摘要|英文关键词|页码|页眉|页脚)", text, re.I)):
        return "all_text", ["all_text"]
    for token, roles in AMBIGUOUS_ROLES.items():
        if token in text:
            return None, roles
    return None, []


def infer_role_from_context(clause: dict[str, Any], text: str) -> str | None:
    """Resolve only high-confidence role omissions created by clause splitting.

    Word templates frequently put a semantic label in one paragraph/fragment
    and its formatting instruction in the next. These rules use nearby source
    evidence and explicit numbering syntax; they do not guess among arbitrary
    body styles.
    """
    before = " ".join(clause.get("context_before", [])[-2:])
    after = " ".join(clause.get("context_after", [])[:2])
    source_full = clause.get("source_text_full", "")
    context = f"{before} {source_full} {after}"
    if re.match(r"^第?[一二三四五六七八九十\d]+章(?:\s|　|题目)", text):
        return "heading_1"
    if re.match(r"^\d+\.\d+\.\d+(?:\s|　)", text):
        return "heading_3"
    if re.match(r"^\d+\.\d+(?:\s|　)", text):
        return "heading_2"
    if re.search(r"页码", before) and parse_properties(text):
        return "footer"
    if re.search(r"(?:英文摘要|ABSTRACT|Abstract)", context, re.I):
        if re.search(r"题目|标题|题头|居中", text):
            return "abstract_title_en"
        if re.search(r"摘要内容|Times\s+New\s+Roman|段落", text, re.I):
            return "abstract_body_en"
    if re.search(r"(?:中文摘要|摘\s*要)", context):
        if re.search(r"题目|标题|题头|居中", text):
            return "abstract_title_zh"
        if re.search(r"摘要内容|宋体|段落|首行缩进|行距", text):
            return "abstract_body_zh"
    if re.search(r"目录", context) and re.search(r"(?:其他内容|宋体|行距|段前|段后)", text):
        return "toc"
    if re.search(r"(?:论文段落的文字|正文|引言|绪论|附录正文)", context) and re.search(
            r"(?:宋体|首行缩进|行距|段前|段后|两端对齐)", text):
        return "body_text"
    if (clause.get("source_kind") == "textbox" and re.search(r"研究生姓名", context)
            and re.search(r"(?:仿宋|字号|磅|行距|段前|段后)", text)):
        return "cover_field_value"
    return None


def _non_page_text(text: str) -> str:
    """Return only fragments that may describe paragraph-role formatting.

    Page-number clauses often contain role-like words such as ``正文`` or
    ``摘要`` and generic formatting words such as ``居中``.  Feeding those
    fragments to the paragraph parser creates false body/abstract rules.  Work
    at comma-level granularity so a mixed sentence can still carry both a page
    rule and a real paragraph rule.
    """
    page_only = re.compile(
        r"(?:\bA4\b|A4纸|横向|纵向|"
        r"(?:上|下|左|右)(?:页)?边距|装订线|"
        r"页眉(?:距(?:页边|边界|顶端))|页脚(?:距(?:页边|边界|底端))|"
        r"首页不同|首页(?:页眉页脚)?另设|奇偶页不同|"
        r"页码|(?:前置|摘要|目录).*?(?:罗马数字|Roman)|"
        r"正文.*?(?:阿拉伯数字|decimal))",
        re.I,
    )
    fragments = [p.strip() for p in re.split(r"[，,；;]+", text) if p.strip()]
    return "，".join(fragment for fragment in fragments if not page_only.search(fragment))


def _set_nested(out: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = out
    for key in path[:-1]:
        cur = cur.setdefault(key, {})
    cur[path[-1]] = value


def parse_properties(text: str) -> dict[str, Any]:
    # Purely optional alternatives do not define a single required output
    # state. Keep them as source evidence instead of turning one allowed choice
    # into a mandatory document-wide style.
    if re.search(r"(?:内容|文字|字体|字形)?可用(?:宋体|黑体|楷体|仿宋|斜体|正体)", text) \
            and not re.search(r"(?:必须|应当|一律|统一|不得)", text):
        return {}
    out: dict[str, Any] = {}
    # Fonts: explicit script qualifiers win; otherwise Chinese named fonts are CJK.
    for zh, canonical in FONT_NAMES.items():
        if zh in text:
            _set_nested(out, ("font", "cjk"), canonical)
    latin = re.search(r"(Times\s+New\s+Roman|Arial|Calibri|Cambria|Courier\s+New)", text, re.I)
    if latin:
        _set_nested(out, ("font", "latin"), " ".join(w.capitalize() if w.lower() != "new" else "New" for w in latin.group(1).split()))
        if latin.group(1).lower().startswith("times"):
            out["font"]["latin"] = "Times New Roman"
    sizes = "|".join(sorted(map(re.escape, SIZE_PT), key=len, reverse=True))
    # When the text contains "标题" (title/heading), prefer the font size
    # after "标题" over the font size before "标题".  This handles combined
    # clauses such as "英文：Times New Roman，五号标题：黑体三号加粗居中"
    # where "五号" is the header font size and "三号" is the title font size.
    if "标题" in text:
        after_title = text.split("标题")[-1]
        m = re.search(rf"(?<![大小])({sizes})(?:字|字体|字号)?", after_title)
        if m is None:
            m = re.search(rf"(?<![大小])({sizes})(?:字|字体|字号)?", text)
    else:
        m = re.search(rf"(?<![大小])({sizes})(?:字|字体|字号)?", text)
    if m:
        # Chinese size names are ordinary language too (for example, 天宫二号).
        # Accept them only when the surrounding clause carries an explicit
        # formatting cue.  Named fonts count as a cue, so common requirements
        # such as “正文二号黑体” remain executable.
        size_context = re.compile(
            r"(?:字号|字体|字[体号]?|宋体|黑体|楷体|仿宋|微软雅黑|方正小标宋|"
            r"Times\s+New\s+Roman|Arial|Calibri|Cambria|Courier\s+New|"
            r"加粗|粗体|斜体|居中|对齐|行距|段前|段后|标题|正文|摘要|关键词|"
            r"页眉|页脚|目录|图题|图名|表题|表名|参考文献)",
            re.I,
        )
        if size_context.search(text):
            _set_nested(out, ("font", "size_pt"), SIZE_PT[m.group(1)])
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:磅|pt)(?:字|字体|字号)?", text, re.I)
        if (m and not re.search(r"(?:行距|段前|段后|页边距|边距|距).*?" + re.escape(m.group(0)), text)
                and not re.search(re.escape(m.group(0)) + r"\s*(?:文武线|武文线|线粗|边线|横线)", text)
                and not re.search(r"(?:线粗|边线|横线).*?" + re.escape(m.group(0)), text)):
            _set_nested(out, ("font", "size_pt"), float(m.group(1)))
    if re.search(r"不加粗|非粗体|无需加粗", text): _set_nested(out, ("font", "bold"), False)
    elif re.search(r"加粗|粗体", text): _set_nested(out, ("font", "bold"), True)
    # Quantity symbols, unit symbols, variables and similar mathematical tokens
    # are inline semantic conventions. They must not be promoted to a paragraph-
    # wide equation/body font style by the deterministic role formatter.
    inline_symbol_rule = re.search(r"(?:量|单位|变量|矢量|向量|矩阵|符号).*?(?:正体|斜体)", text)
    if not inline_symbol_rule:
        if re.search(r"正体|不倾斜|不斜体|非斜体", text): _set_nested(out, ("font", "italic"), False)
        elif re.search(r"斜体", text): _set_nested(out, ("font", "italic"), True)
    aligns = [("center", r"居中"), ("justify", r"两端对齐"), ("distributed", r"分散对齐"), ("left", r"左对齐|靠左"), ("right", r"右对齐|靠右")]
    for val, pat in aligns:
        if re.search(pat, text): _set_nested(out, ("paragraph", "alignment"), val); break
    m = re.search(r"首行缩进\s*(\d+(?:\.\d+)?)\s*(?:个)?(?:汉字|字符|字)", text)
    if m: _set_nested(out, ("paragraph", "first_line_indent_chars"), float(m.group(1)))
    m = re.search(r"首行缩进\s*(\d+(?:\.\d+)?)\s*(?:磅|pt)", text, re.I)
    if m: _set_nested(out, ("paragraph", "first_line_indent_pt"), float(m.group(1)))
    cn_num = {"一": 1.0, "二": 2.0, "半": .5}

    def line_count(token: str) -> float:
        return cn_num.get(token, float(token) if re.fullmatch(r"\d+(?:\.\d+)?", token) else 0.0)

    for label, key in (("段前", "space_before"), ("段后", "space_after")):
        m = re.search(label + r"(?:各|均)?(?:空|为|设置为)?\s*([一二半]|\d+(?:\.\d+)?)\s*行", text)
        if m: _set_nested(out, ("paragraph", key + "_lines"), line_count(m.group(1)))
        m = re.search(label + r"(?:各|均)?(?:空|为|设置为)?\s*(\d+(?:\.\d+)?)\s*(?:磅|pt)", text, re.I)
        if m: _set_nested(out, ("paragraph", key + "_pt"), float(m.group(1)))
    # Combined expression: 段前、段后各空一行
    m = re.search(r"段前[、,，和及]\s*段后(?:各|均)?空?\s*([一二半]|\d+(?:\.\d+)?)\s*行", text)
    if m:
        val = line_count(m.group(1))
        _set_nested(out, ("paragraph", "space_before_lines"), val); _set_nested(out, ("paragraph", "space_after_lines"), val)
    paragraph = out.get("paragraph", {})
    if {"space_before_lines", "space_after_lines"} & set(paragraph):
        size = out.get("font", {}).get("size_pt")
        if size is not None:
            paragraph["spacing_line_height_pt"] = float(size)
    # Some school guides omit the word “固定值” and write the direct form
    # “行距16磅”.  Keep this narrow so a font-size clause such as “正文14磅”
    # cannot be mistaken for paragraph line spacing.
    m = re.search(r"行距(?:为|采用|设置为)?\s*(\d+(?:\.\d+)?)\s*(?:磅|pt)", text, re.I)
    if m: _set_nested(out, ("paragraph", "line_spacing"), {"type": "exact", "value": float(m.group(1)), "unit": "pt"})
    m = re.search(r"(?:行距(?:为|采用|设置为)?\s*)?固定值\s*(\d+(?:\.\d+)?)\s*(?:磅|pt)", text, re.I)
    if m: _set_nested(out, ("paragraph", "line_spacing"), {"type": "exact", "value": float(m.group(1)), "unit": "pt"})
    m = re.search(r"(?:行距(?:为|采用|设置为)?\s*)?(\d+(?:\.\d+)?)\s*倍行距", text)
    if m: _set_nested(out, ("paragraph", "line_spacing"), {"type": "multiple", "value": float(m.group(1)), "unit": "multiple"})
    if re.search(r"单倍行距", text): _set_nested(out, ("paragraph", "line_spacing"), {"type": "single", "value": 1, "unit": "multiple"})
    if re.search(r"1\.5倍行距|一点五倍行距", text): _set_nested(out, ("paragraph", "line_spacing"), {"type": "one_point_five", "value": 1.5, "unit": "multiple"})
    if re.search(r"双倍行距|两倍行距", text): _set_nested(out, ("paragraph", "line_spacing"), {"type": "double", "value": 2, "unit": "multiple"})
    page_break = r"另起一页|(?<!部)分页|新页开始"
    if re.search(r"(?:无需|不|不得)(?:" + page_break + r")", text): _set_nested(out, ("paragraph", "page_break_before"), False)
    elif re.search(page_break, text): _set_nested(out, ("paragraph", "page_break_before"), True)
    if re.search(r"图(?:题|名|注).*?(?:图下|下方)|置于图下", text): out["position"] = "below"
    if re.search(r"图(?:题|名|注).*?(?:图上|上方)|置于图上", text): out["position"] = "above"
    if re.search(r"表(?:题|名|注).*?(?:表上|上方)|置于表上", text): out["position"] = "above"
    if re.search(r"表(?:题|名|注).*?(?:表下|下方)|置于表下", text): out["position"] = "below"
    left = re.search(r"(?:页眉)?左(?:侧|边)?(?:固定文字)?(?:为|是|：|:)\s*([^，,；;。]+)", text)
    if left:
        out.setdefault("header_content", {})["left_text"] = left.group(1).strip(" “\"”")
    if re.search(r"右(?:侧|边)?.*?(?:当前)?(?:一级标题|章标题)|STYLEREF", text, re.I):
        out.setdefault("header_content", {})["right_field"] = "styleref_heading_1"
    border = re.search(r"(\d+(?:\.\d+)?)\s*(?:磅|pt)?\s*(文武线|武文线)", text, re.I)
    if border:
        out["bottom_border"] = {"style": "thin_thick" if border.group(2) == "文武线" else "thick_thin",
                                "width_pt": float(border.group(1)), "space_pt": 0, "color": "000000"}
    # Require the explicit words 形式/格式. Generic prose such as “编号后跟
    # （续）” describes continuation tables, not a numbering template.
    m = re.search(r"编号(?:形式|格式)(?:为|采用)?[“\"]?([^”\"，。；;]+)[”\"]?", text)
    if m:
        example = m.group(1).strip()
        fmt = example
        if re.search(r"第[一二三四五六七八九十]+章", example): fmt = "第{chapter_cn}章"
        elif re.search(r"第\d+章", example): fmt = "第{chapter}章"
        _set_nested(out, ("numbering", "format"), fmt)
        _set_nested(out, ("numbering", "style"), "chinese" if "chapter_cn" in fmt else "decimal")
    return out


def parse_page_properties(text: str) -> dict[str, Any]:
    """Parse page/section properties that do not belong to a paragraph role."""
    out: dict[str, Any] = {}
    if re.search(r"\bA4\b|A4纸", text, re.I):
        out["size"] = "A4"
    if re.search(r"横向", text): out["orientation"] = "landscape"
    if re.search(r"纵向", text): out["orientation"] = "portrait"
    labels = {
        "top": r"上(?:页)?边距", "bottom": r"下(?:页)?边距",
        "left": r"左(?:页)?边距", "right": r"右(?:页)?边距",
        "header": r"页眉(?:距(?:页边|边界|顶端))?", "footer": r"页脚(?:距(?:页边|边界|底端))?",
        "gutter": r"装订线(?:宽度)?",
    }
    factors = {"cm": 72 / 2.54, "厘米": 72 / 2.54, "mm": 72 / 25.4, "毫米": 72 / 25.4,
               "pt": 1, "磅": 1, "inch": 72, "in": 72, "英寸": 72}
    for key, label in labels.items():
        m = re.search(label + r"(?:为|是|设置为|：|:)?\s*(\d+(?:\.\d+)?)\s*(cm|厘米|mm|毫米|pt|磅|inch|in|英寸)", text, re.I)
        if m:
            out.setdefault("margins_pt", {})[key] = round(float(m.group(1)) * factors[m.group(2).lower()], 3)
    if re.search(r"首页不同|首页(?:页眉页脚)?另设", text): out["different_first_page"] = True
    if re.search(r"奇偶页不同|奇数页.*偶数页.*不同", text): out["different_odd_even"] = True
    toc_listing = bool(re.search(r"目录.*?(?:标题与页码|页码右对齐|页码.*连接)", text))
    if not toc_listing and not re.search(r"页码.*(?:不|无需|不得)居中", text) and re.search(r"页码.*居中", text):
        out.setdefault("page_number", {})["alignment"] = "center"
    if not toc_listing and re.search(r"页码.*(?:右对齐|右侧)", text): out.setdefault("page_number", {})["alignment"] = "right"
    if not toc_listing and re.search(r"页码.*(?:左对齐|左侧)", text): out.setdefault("page_number", {})["alignment"] = "left"
    if re.search(r"前置|摘要|目录", text) and re.search(r"罗马数字|Roman", text, re.I):
        out.setdefault("page_number", {})["front_matter_format"] = "roman"
    if re.search(r"正文", text) and re.search(r"阿拉伯数字|decimal", text, re.I):
        out.setdefault("page_number", {})["body_format"] = "decimal"
    return out


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    conflicts = []
    for key, val in src.items():
        if key in dst and isinstance(dst[key], dict) and isinstance(val, dict):
            for sub, old, new in _deep_merge(dst[key], val): conflicts.append((f"{key}.{sub}", old, new))
        elif key in dst and dst[key] != val:
            conflicts.append((key, dst[key], val))
        else:
            dst[key] = copy.deepcopy(val)
    return conflicts


def _deep_overlay(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Overlay ``src`` on ``dst`` while preserving unrelated nested values."""
    for key, value in src.items():
        if key in dst and isinstance(dst[key], dict) and isinstance(value, dict):
            _deep_overlay(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)


def _merge_scoped_projection(
    dst: dict[str, Any], src: dict[str, Any], *, path: str = "",
    prefer_incoming: bool = False,
) -> list[tuple[str, Any, Any]]:
    """Merge clause-scoped top-level projections without list first-wins loss.

    ``prefer_incoming`` is used only when applying an accepted LLM projection
    over the deterministic baseline.  The in-memory LLM projection itself
    remains first-value-wins for scalar disagreements and records the
    disagreement as an audit finding; keyed collections are still unioned in
    both modes.
    """
    conflicts: list[tuple[str, Any, Any]] = []

    def list_key(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        for key in ("id", "object_type"):
            if isinstance(value.get(key), str) and value[key].strip():
                return key
        return None

    def merge_value(target: dict[str, Any], incoming: dict[str, Any], prefix: str) -> None:
        for key, value in incoming.items():
            current_path = f"{prefix}.{key}" if prefix else key
            if key not in target:
                target[key] = copy.deepcopy(value)
                continue
            old = target[key]
            if isinstance(old, dict) and isinstance(value, dict):
                merge_value(old, value, current_path)
                continue
            if isinstance(old, list) and isinstance(value, list):
                list_items = [*old, *value]
                if list_items and all(
                    isinstance(item, dict) and list_key(item) for item in list_items
                ):
                    key_name = next((list_key(item) for item in list_items), None)
                    index = {
                        item.get(key_name): item for item in old
                        if isinstance(item, dict) and key_name and item.get(key_name) is not None
                    }
                    for candidate in value:
                        candidate_key = candidate.get(key_name) if isinstance(candidate, dict) and key_name else None
                        if candidate_key is None or candidate_key not in index:
                            old.append(copy.deepcopy(candidate))
                        else:
                            merge_value(index[candidate_key], candidate, f"{current_path}[{candidate_key}]")
                    continue
                if old != value:
                    conflicts.append((current_path, copy.deepcopy(old), copy.deepcopy(value)))
                    if prefer_incoming:
                        target[key] = copy.deepcopy(value)
                        continue
                for candidate in value:
                    if candidate not in old:
                        old.append(copy.deepcopy(candidate))
                continue
            if old != value:
                # A neutral placeholder is not an authoritative institution
                # value.  A later evidence-bound concrete value may replace it.
                if current_path.endswith("institution") and str(old).strip() in {"——", "--"} and str(value).strip():
                    target[key] = copy.deepcopy(value)
                elif current_path.endswith("institution") and str(value).strip() in {"——", "--"}:
                    continue
                else:
                    conflicts.append((current_path, copy.deepcopy(old), copy.deepcopy(value)))
                    if prefer_incoming:
                        target[key] = copy.deepcopy(value)

    merge_value(dst, src, path)
    return conflicts


def _declaration_body_parts(item: dict[str, Any]) -> list[str]:
    """Return source-derived body paragraphs in their supplied order."""
    parts: list[str] = []
    body = item.get("body")
    if isinstance(body, str) and body.strip():
        parts.append(body.strip())
    body_parts = item.get("body_parts")
    if isinstance(body_parts, list):
        parts.extend(
            value.strip() for value in body_parts
            if isinstance(value, str) and value.strip()
        )
    return parts


def _declaration_has_fixed_text(item: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    props = item.get("properties") if item.get("role") == "declarations" else item
    if not isinstance(props, dict) or not isinstance(props.get("items"), list):
        return False
    return any(
        isinstance(candidate, dict)
        and (
            isinstance(candidate.get("heading"), str) and candidate.get("heading", "").strip()
            or _declaration_body_parts(candidate)
        )
        for candidate in props["items"]
    )


def _declaration_items_share_entity(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Recognize a later fragment of an already identified declaration item."""
    existing_evidence = set(existing.get("source_evidence_ids") or [])
    incoming_evidence = set(incoming.get("source_evidence_ids") or [])
    if not existing_evidence & incoming_evidence:
        return False
    existing_body = {_normalized_exact_text(value) for value in _declaration_body_parts(existing)}
    incoming_body = {_normalized_exact_text(value) for value in _declaration_body_parts(incoming)}
    if existing_body & incoming_body:
        return True
    return not incoming.get("heading") and not incoming_body


def _append_unique_source_text(target: list[str], values: list[str]) -> None:
    """Append exact source paragraphs while avoiding duplicate fragments."""
    existing = {_normalized_exact_text(value) for value in target}
    for value in values:
        normalized = _normalized_exact_text(value)
        if normalized and normalized not in existing:
            target.append(value.strip())
            existing.add(normalized)


def _merge_declaration_item(
    existing: dict[str, Any], incoming: dict[str, Any], item_id: str,
) -> list[tuple[str, Any, Any]]:
    """Merge complementary declaration fragments for one semantic item.

    Heading/body fragments may arrive in separate host chunks.  Paragraphs and
    evidence IDs are additive, while two different non-empty headings or two
    different scalar body values remain a blocking conflict.
    """
    merged = copy.deepcopy(existing)
    conflicts: list[tuple[str, Any, Any]] = []
    for field in ("heading",):
        old = merged.get(field)
        new = incoming.get(field)
        if new is None:
            continue
        if old and new and str(old).strip() != str(new).strip():
            conflicts.append((f"items[{item_id}].{field}", old, new))
        elif not old:
            merged[field] = copy.deepcopy(new)

    old_body = existing.get("body")
    new_body = incoming.get("body")
    if isinstance(old_body, str) and old_body.strip() and isinstance(new_body, str) and new_body.strip():
        if _normalized_exact_text(old_body) != _normalized_exact_text(new_body):
            conflicts.append((f"items[{item_id}].body", old_body, new_body))

    body_parts = _declaration_body_parts(existing)
    _append_unique_source_text(body_parts, _declaration_body_parts(incoming))
    if body_parts:
        merged.pop("body", None)
        merged["body_parts"] = body_parts

    for field in ("source_evidence_ids",):
        values = list(merged.get(field) or []) if isinstance(merged.get(field), list) else []
        incoming_values = incoming.get(field)
        if isinstance(incoming_values, list):
            for value in incoming_values:
                if isinstance(value, str) and value.strip() and value not in values:
                    values.append(value)
        if values:
            merged[field] = values

    old_placeholders = merged.get("signature_placeholders")
    new_placeholders = incoming.get("signature_placeholders")
    if isinstance(old_placeholders, list) and isinstance(new_placeholders, list):
        placeholders = copy.deepcopy(old_placeholders)
        for candidate in new_placeholders:
            if not isinstance(candidate, dict):
                placeholders.append(copy.deepcopy(candidate))
                continue
            role = candidate.get("role")
            same_role = next(
                (item for item in placeholders
                 if isinstance(item, dict) and role and item.get("role") == role),
                None,
            )
            if same_role is not None and same_role != candidate:
                conflicts.append((f"items[{item_id}].signature_placeholders[{role}]", same_role, candidate))
            elif candidate not in placeholders:
                placeholders.append(copy.deepcopy(candidate))
        merged["signature_placeholders"] = placeholders
    elif isinstance(new_placeholders, list):
        merged["signature_placeholders"] = copy.deepcopy(new_placeholders)

    known = {"id", "heading", "body", "body_parts", "source_evidence_ids", "signature_placeholders"}
    for key, value in incoming.items():
        if key in known:
            continue
        if key in merged and merged[key] != value:
            conflicts.append((f"items[{item_id}].{key}", merged[key], value))
        else:
            merged[key] = copy.deepcopy(value)
    existing.clear()
    existing.update(merged)
    return conflicts


def _merge_declaration_properties(
    dst: dict[str, Any], src: dict[str, Any],
) -> list[tuple[str, Any, Any]]:
    """Merge declaration properties by semantic item ID, not list position."""
    staged = copy.deepcopy(dst)
    conflicts: list[tuple[str, Any, Any]] = []
    for field in ("before_role",):
        old = staged.get(field)
        new = src.get(field)
        if new is None:
            continue
        if old and old != new:
            conflicts.append((field, old, new))
        elif not old:
            staged[field] = copy.deepcopy(new)

    incoming_items = src.get("items")
    if incoming_items is None:
        incoming_items = []
    if not isinstance(incoming_items, list):
        conflicts.append(("items", staged.get("items"), incoming_items))
    else:
        items = staged.get("items")
        if not isinstance(items, list):
            items = []
        by_id = {
            item.get("id"): item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        seen_incoming: set[str] = set()
        for item in incoming_items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item.get("id", "").strip():
                conflicts.append(("items", items, item))
                continue
            item_id = item["id"].strip()
            if item_id in seen_incoming:
                conflicts.append((f"items[{item_id}]", "duplicate", item))
                continue
            seen_incoming.add(item_id)
            if item_id in by_id:
                conflicts.extend(_merge_declaration_item(by_id[item_id], item, item_id))
            else:
                related = next(
                    (candidate for candidate in items
                     if isinstance(candidate, dict)
                     and _declaration_items_share_entity(candidate, item)),
                    None,
                )
                if related is not None:
                    conflicts.extend(_merge_declaration_item(
                        related, item, str(related.get("id")),
                    ))
                else:
                    copied = copy.deepcopy(item)
                    copied["id"] = item_id
                    items.append(copied)
                    by_id[item_id] = copied
        staged["items"] = items
    dst.clear()
    dst.update(staged)
    return conflicts


def _merge_role_group(roles: dict[str, Any], target_roles: tuple[str, ...],
                      props: dict[str, Any]) -> list[tuple[str, str, Any, Any]]:
    """Merge one semantic requirement into all normalized roles atomically."""
    staged = copy.deepcopy(roles)
    conflicts: list[tuple[str, str, Any, Any]] = []
    for target_role in target_roles:
        for prop, old, new in _deep_merge(staged.setdefault(target_role, {}), props):
            conflicts.append((target_role, prop, old, new))
    if not conflicts:
        roles.clear()
        roles.update(staged)
    return conflicts


def build_rule_result(source: Path, clauses: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    roles: dict[str, Any] = {}; page: dict[str, Any] = {}; requirements = []; questions = []; conflicts = []
    content_instances: list[dict[str, Any]] = []
    clause_map = {clause.get("id"): clause for clause in clauses if isinstance(clause, dict) and clause.get("id")}
    for clause in clauses:
        page_props = parse_page_properties(clause["text"])
        role_text = _non_page_text(clause["text"]) if page_props else clause["text"]
        props = parse_properties(role_text)
        if page_props:
            req = {"id": f"R{len(requirements)+1:05d}", "role": "page", "properties": page_props,
                   "evidence_ids": clause["evidence_ids"], "clause_ids": [str(clause["id"])],
                   "resolved_by": "rule", "confidence": .98,
                   "source_text": clause["text"]}
            for prop, old, new in _deep_merge(page, page_props):
                conflicts.append({"type": "requirement_conflict", "role": "page", "property": prop,
                                  "existing_value": old, "new_value": new, "requirement_id": req["id"],
                                  "evidence_ids": clause["evidence_ids"]})
            requirements.append(req)
        if not props:
            continue
        role, candidates = identify_role(role_text)
        if role is None and not candidates:
            role = infer_role_from_context(clause, role_text)
            if role is not None:
                candidates = [role]
        if role is None:
            questions.append({
                "id": f"Q{len(questions)+1:04d}", "clause_id": clause["id"],
                "question": "该格式要求对应哪个语义角色？", "source_text": clause["text"],
                "candidate_roles": candidates or ["unknown"], "evidence_ids": clause["evidence_ids"],
                "context_before": clause["context_before"], "context_after": clause["context_after"],
            })
            continue
        if role == "figure_table_title":
            target_roles = ("figure_caption", "table_caption")
        elif role == "all_text":
            target_roles = ALL_TEXT_ROLES
        else:
            target_roles = (role,)
        field_instance_ids: list[str] = []
        if role in CONTENT_INSTANCE_ROLES:
            instance_id, instance_error = _register_content_instance(
                content_instances,
                {"field_key": None},
                role,
                props,
                {str(clause["id"])},
                {str(item) for item in clause.get("evidence_ids", [])},
                clause_map,
                "deterministic requirement extraction",
            )
            if instance_error:
                conflicts.append({"type": "requirement_conflict", "role": role,
                                  "property": "field_instance", "existing_value": instance_error})
                continue
            if instance_id:
                field_instance_ids = [instance_id]
        normalized_props = _style_properties_for_role(role, props)
        merge_conflicts = _merge_role_group(roles, target_roles, normalized_props)
        if role in CONTENT_INSTANCE_ROLES:
            # A text-bearing requirement may carry an instance-specific style
            # override.  The shared role remains a useful default, but a
            # different instance style is not a singleton-role conflict.
            merge_conflicts = []
        if merge_conflicts:
            for target_role, prop, old, new in merge_conflicts:
                conflicts.append({"type": "requirement_conflict", "role": target_role, "property": prop,
                                  "existing_value": old, "new_value": new,
                                  "evidence_ids": clause["evidence_ids"]})
            continue
        for target_role in target_roles:
            req = {"id": f"R{len(requirements)+1:05d}", "role": target_role,
                   "properties": copy.deepcopy(normalized_props), "evidence_ids": clause["evidence_ids"],
                   "clause_ids": [str(clause["id"])], "resolved_by": "rule",
                   "confidence": .98, "source_text": clause["text"]}
            if field_instance_ids:
                req["field_instance_ids"] = list(field_instance_ids)
            requirements.append(req)
    status = "needs_clarification" if questions or conflicts else "rule_resolved"
    spec = {"schema_version": "1.0", "source_document": str(source),
            "roles": _sanitize_role_specs_for_backend(roles),
            "requirements": requirements, "content_instances": content_instances,
            "status": status}
    if page:
        spec["page"] = page
    return spec, questions, conflicts


def build_llm_request(questions: list[dict[str, Any]], clauses: list[dict[str, Any]],
                      evidence_doc: dict[str, Any] | None = None,
                      rule_spec: dict[str, Any] | None = None,
                      mode: str = "questions") -> dict[str, Any]:
    if mode == "full":
        # Clause records retain rich context for deterministic auditing, but
        # embedding that context into every clause duplicates the same DOCX
        # runs/location payload many times.  The LLM receives a compact clause
        # index plus one authoritative evidence map keyed by evidence_id.
        clause_packets = [
            {
                key: copy.deepcopy(clause[key])
                for key in ("id", "text", "evidence_ids", "source_kind", "location", "part_index")
                if key in clause
            }
            for clause in clauses
        ]
        evidence_context = {
            str(item.get("id")): item
            for item in (evidence_doc or {}).get("evidence", [])
            if isinstance(item, dict) and item.get("id")
        }
        format_schema = json.loads(
            (Path(__file__).resolve().parents[1] / "schema" / "format-spec.schema.json").read_text(encoding="utf-8")
        )
        role_schema_names = {
            role: ("roleSpec" if role not in TOP_LEVEL_REQUIREMENT_ROLES else {
                "page": "pageSpec", "table": "tableSpec", "objects": "objectPaginationSpec",
                "content_constraints": "contentConstraintSpec",
                "conditional_constraints": "conditionalConstraintSpec",
                "document_structure": "documentStructureSpec", "appendices": "appendixSpec",
                "equations": "equationLayoutSpec", "cover": "coverSpec",
                "declarations": "declarationsSpec",
            }[role])
            for role in sorted(ALLOWED_REQUIREMENT_ROLES)
        }
        request_defs = {
            key: copy.deepcopy(value)
            for key, value in format_schema.get("$defs", {}).items()
            if key not in {"contentInstance", "coverFieldInstance"}
        }
        if isinstance(request_defs.get("requirement"), dict):
            request_defs["requirement"].get("properties", {}).pop("field_instance_ids", None)
        request_rule_spec = copy.deepcopy(rule_spec or {})
        if isinstance(request_rule_spec, dict):
            request_rule_spec.pop("content_instances", None)
            request_rule_spec.pop("cover_field_instances", None)
        return {
            "contract_version": "2.1",
            "task": "extract_and_review_complete_thesis_format_spec",
            "instructions": [
                "Treat every supplied clause as in scope for completeness review.",
                "Return formatting requirements supported by cited clause_ids and evidence_ids.",
                "Every requirement object MUST include a non-empty reason explaining why its role and properties are supported by the cited clause/evidence.",
                "When a clause exactly supports an existing deterministic requirement, set existing_requirement_id and preserve that requirement's role, properties, and evidence_ids exactly.",
                "Do not invent values. Use unresolved_clause_ids when evidence is insufficient.",
                "Classify every clause exactly once in clause_reviews and provide a non-empty reason.",
                "Prefer executable, verify_existing, not_applicable, external_compliance, informational, requires_metadata, requires_source_content, unsupported_backend, or unverifiable.",
                "Normative-scope gate: a clause is not a requirement merely because it appears in the input. Use covered/executable/verify_existing only when the cited evidence contains explicit normative language or clearly identifies a fixed template structure/statement. Record the basis in normative_basis when applicable.",
                "A numbered bibliography entry, body prose, filled author/title/date value, sample data, appendix table value, education-history entry, or publication-list entry is source/sample content unless the cited evidence explicitly says that it must be copied or prescribes its format. Do not turn exact presence in a sample thesis into an executable requirement.",
                "Legacy covered/ignored/unresolved/unsupported remain accepted for compatibility, but unsupported means an applicable DOCX backend gap and blocks full compliance.",
                "Use not_applicable only when an explicit thesis condition makes a requirement inapplicable; never use it merely because the backend lacks support.",
                "Use external_compliance only for real-world submission duties that cannot be represented by a DOCX artifact, such as physical cover stock, printing, binding, actual signatures, or administrative approval.",
                "A blank author/supervisor/date signature placeholder is DOCX structure, never proof of an actual signature. For mixed clauses, emit a declarations requirement only for fixed text/order/placeholders and preserve actual signing as an external clause review rather than claiming it generated_and_verified.",
                "For a declarations requirement, copy each fixed-text heading and every fixed-text body paragraph exactly from the cited evidence into properties.items[].heading and properties.items[].body_parts. Use a semantic item id local to this run, such as originality or authorization, and include only source_evidence_ids and blank signature_placeholders. Do not invent resource_id, version, or sha256: the host materializes those fields from this run and the exact source text.",
                "Never identify a declaration by institution or school name in the execution contract. The current input evidence is the only source of fixed declaration text; if it is not available, classify the clause as unresolved or requires_source_content.",
                "A cover requirement declares document structure independently of instance metadata. Bind fields deterministically to thesis_profile.cover_metadata; when the complete confirmed metadata record is absent, preserve required cover fields with the neutral placeholder configured by cover.missing_value_placeholder (default ——). Never infer identity, degree, supervisor, security approval, physical cover color, or spine compliance.",
                "The rule_spec is advisory evidence, not authoritative; report disagreements in conflicts.",
                "For page numbering, identify the body start with first_heading_1, heading_text, or section_index.",
                "LLM output is declarative only and must never contain OOXML edits or executable code.",
                "Use applicability for explicit conditions/exceptions; never hide a condition in free text when it changes whether the requirement applies.",
                "Use input_prerequisites for required metadata, source content, template resources, or runtime services. Do not fabricate missing inputs.",
                "Use verification to declare the minimum evidence mode: static_docx, word_render, pdf_render, manual, or external.",
                "Resolve each clause through its evidence_ids and the matching evidence_context entry; the evidence map is authoritative for source text, runs, styles, location, and neighboring context.",
                "requirement_indexes are zero-based indexes into this response's requirements array. They MUST be [] for informational, requires_metadata, requires_source_content, external_compliance, not_applicable, unsupported_backend, unsupported, unverifiable, unresolved, or ignored reviews; only covered, executable, and verify_existing may reference requirements.",
                "Use verify_existing only when an existing_requirement_id is being reused and the emitted role, properties, evidence_ids, and source text are an exact evidence-backed match. If the evidence occurrence differs, emit a new requirement or use a non-executable classification; do not force an existing_requirement_id.",
                "Every emitted requirement must be referenced by at least one covered, executable, or verify_existing clause_review; do not emit unused requirement objects. Every such clause review reference must point to a semantically matching emitted requirement.",
                "Use only the allowed requirement roles and the corresponding properties schema in requirement_contract. Never invent role names such as cover_metadata, declaration_originality, authorization_statement, or other role names absent from that contract; use cover, declarations, document_structure, or a registered text role instead.",
                "For an existing requirement, the request-only field _eligible_clause_ids lists the exact clause occurrences whose evidence may be reused. Do not use that existing_requirement_id for any other clause_id; never copy an existing requirement from a different evidence occurrence.",
                "When reusing an existing requirement, do not combine unrelated clauses or repeated occurrences with different evidence. Every clause_id listed in that requirement must be exactly represented by its source text and cited evidence.",
                "Do not author, copy, abbreviate, or recompute provenance/hash fields; an automatic native bridge binds the accepted response to the current invocation. Offline/manual merger inputs must carry the exact chunk provenance before merge.",
            ],
            "clauses": clause_packets,
            "evidence_context": evidence_context,
            "requirement_contract": {
                "allowed_roles": sorted(ALLOWED_REQUIREMENT_ROLES),
                "role_properties_schema": {
                    role: {"$ref": f"#/$defs/{schema_name}"}
                    for role, schema_name in role_schema_names.items()
                },
                # Content-instance normalization is a host-side compatibility
                # layer.  Keep these output-only additions out of the request
                # contract so already-reviewed 2.1 responses retain their
                # original provenance hash after a backend schema extension.
                "$defs": request_defs,
            },
            "document_structure": (evidence_doc or {}).get("structure_evidence", {}),
            "page_evidence": (evidence_doc or {}).get("page_evidence", {}),
            "rule_spec": request_rule_spec,
            "response_schema": {
                "type": "object",
        "required": ["contract_version", "requirements", "clause_reviews", "unsupported_items", "reported_conflicts"],
                "properties": {
                    "contract_version": {"const": "2.1"},
                    "provenance": {"type": "object", "required": [
                        "version", "origin", "source_sha256", "evidence_sha256",
                        "clause_sha256", "request_sha256",
                    ]},
                    "requirements": {"type": "array", "items": {
                        "type": "object", "required": ["role", "properties", "clause_ids", "evidence_ids", "confidence", "reason"],
                        "properties": {
                            "existing_requirement_id": {"type": "string", "minLength": 1},
                            "role": {"type": "string"},
                            "properties": {"type": "object", "minProperties": 1},
                            "clause_ids": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number"}, "reason": {"type": "string", "minLength": 1},
                            "applicability": {"$ref": "#/$defs/applicabilitySpec"},
                            "input_prerequisites": {"type": "array", "items": {"$ref": "#/$defs/inputPrerequisiteSpec"}},
                            "verification": {"$ref": "#/$defs/verificationSpec"}
                        }}},
                    "clause_reviews": {"type": "array", "items": {
                        "type": "object", "required": ["clause_id", "classification", "requirement_indexes", "reason"],
                        "properties": {
                            "clause_id": {"type": "string"},
                            "classification": {"enum": sorted(ALLOWED_REVIEW_CLASSIFICATIONS)},
                            "requirement_indexes": {"type": "array", "items": {"type": "integer", "minimum": 0}, "uniqueItems": True},
                            "reason": {"type": "string", "minLength": 1},
                            "normative_basis": {"enum": [
                                "explicit_normative_text", "template_structure", "fixed_statement",
                                "sample_content", "source_content", "external_duty", "insufficient"
                            ]}
                        }, "additionalProperties": False}},
                    "unsupported_items": {"type": "array", "items": {"type": "string"}},
                    "reported_conflicts": {"type": "array", "items": {"type": "object"}}
                },
                "$defs": {
                    "applicabilitySpec": {
                        "type": "object", "required": ["status"],
                        "properties": {
                            "status": {"enum": ["always", "conditional", "excluded"]},
                            "conditions": {"type": "array", "items": {
                                "type": "object", "required": ["fact", "operator", "value"],
                                "properties": {
                                    "fact": {"type": "string", "pattern": "^(thesis_profile|source_inventory|template_profile)\\."},
                                    "operator": {"enum": ["equals", "not_equals", "in", "present", "absent"]},
                                    "value": {}
                                }, "additionalProperties": False}},
                            "exceptions": {"type": "array", "items": {"type": "string", "minLength": 1}}
                        }, "additionalProperties": False},
                    "inputPrerequisiteSpec": {
                        "type": "object", "required": ["kind", "key", "required", "reason"],
                        "properties": {
                            "kind": {"enum": ["metadata", "source_content", "template_resource", "runtime"]},
                            "key": {"type": "string", "pattern": "^(thesis_profile|source_inventory|template_profile|runtime)\\."},
                            "required": {"type": "boolean"},
                            "reason": {"type": "string", "minLength": 1}
                        }, "additionalProperties": False},
                    "verificationSpec": {
                        "type": "object", "required": ["mode", "checks"],
                        "properties": {
                            "mode": {"enum": ["static_docx", "word_render", "pdf_render", "manual", "external"]},
                            "checks": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
                        }, "additionalProperties": False}
                }, "additionalProperties": False
            }
        }
    clause_map = {c["id"]: c for c in clauses}
    return {
        "contract_version": "1.0", "task": "resolve_thesis_formatting_semantics",
        "instructions": [
            "Resolve only the supplied questions; do not invent formatting properties.",
            "Choose role only from candidate_roles and cite existing evidence_ids.",
            "Use unresolved=true when evidence is insufficient.",
        ],
        "questions": [{**q, "clause": clause_map.get(q["clause_id"], {})} for q in questions],
        "response_schema": {
            "type": "object",
            "required": ["resolutions"],
            "properties": {
                "resolutions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["question_id", "unresolved", "evidence_ids"],
                        "properties": {
                            "question_id": {"type": "string"},
                            "unresolved": {"type": "boolean"},
                            "role": {"type": "string"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "reason": {"type": "string"}
                        }
                    }
                }
            }
        }
    }


def merge_llm_primary(source: Path, rule_spec: dict[str, Any], clauses: list[dict[str, Any]],
                      response: dict[str, Any], evidence_ids: set[str], *,
                      expected_provenance: dict[str, Any] | None = None,
                      require_provenance: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and merge a complete LLM interpretation with deterministic evidence.

    The LLM owns semantic interpretation for an unseen template.  Deterministic
    parsing remains an independent cross-check and the LLM cannot cite unknown
    clauses/evidence or bypass the executable Schema.
    """
    clause_map = {c["id"]: c for c in clauses}
    all_clause_ids = set(clause_map)
    audit: list[dict[str, Any]] = []
    reported_conflicts = response.get("reported_conflicts") if isinstance(response, dict) else []
    unsupported_items = response.get("unsupported_items") if isinstance(response, dict) else []
    conflicts: list[dict[str, Any]] = list(reported_conflicts or []) if isinstance(reported_conflicts, list) else []
    reviews = copy.deepcopy(response.get("clause_reviews") or []) if isinstance(response, dict) else []
    requirements = response.get("requirements") or [] if isinstance(response, dict) else []
    manual_empty_indexes: set[int] = set()
    existing_requirement_map = {
        item.get("id"): item for item in rule_spec.get("requirements", [])
        if isinstance(item, dict) and item.get("id")
    }
    review_map: dict[str, dict[str, Any]] = {}
    if not isinstance(response, dict) or response.get("contract_version") != "2.1":
        conflicts.append({"type": "llm_contract", "reason": "contract_version_must_be_2.1"})
    if require_provenance:
        provenance_errors = validate_response_provenance(
            response, expected_provenance or {}, require_fresh_origin=True,
        )
        conflicts.extend({"type": "llm_provenance", "reason": error}
                         for error in provenance_errors)
    if not isinstance(reported_conflicts, list):
        conflicts.append({"type": "llm_contract", "reason": "reported_conflicts_must_be_array"})
    if not isinstance(unsupported_items, list) or any(not isinstance(item, str) for item in unsupported_items):
        conflicts.append({"type": "llm_contract", "reason": "unsupported_items_must_be_string_array"})
        unsupported_items = []
    sample_content_clause_ids: set[str] = set()
    sample_content_requirement_indexes: set[int] = set()
    if not isinstance(reviews, list) or not isinstance(requirements, list):
        conflicts.append({"type": "llm_contract", "reason": "requirements_and_clause_reviews_must_be_arrays"})
        reviews, requirements = [], []
    else:
        # Presence in a filled template is not sufficient evidence of a
        # normative obligation.  Apply a deterministic, high-confidence
        # scope guard before accepting the host response so a model cannot
        # promote sample bibliography/appendix content into executable DOCX
        # requirements.  The raw response remains untouched on disk; this
        # normalized decision is recorded in llm-merge-audit.json.
        sample_content_changes: list[dict[str, Any]] = []
        for review in reviews:
            if not isinstance(review, dict):
                continue
            clause_id = review.get("clause_id")
            clause = clause_map.get(clause_id)
            classification = review.get("classification")
            guard = (
                sample_content_guard(clause, clauses)
                if isinstance(clause, dict) and classification_requires_requirement(str(classification))
                else None
            )
            if not guard:
                continue
            original_indexes = list(review.get("requirement_indexes") or [])
            review["classification"] = "informational"
            review["requirement_indexes"] = []
            review["normative_basis"] = "sample_content"
            review["reason"] = (
                f"{str(review.get('reason') or '').strip()} {guard['reason']}"
            ).strip()
            sample_content_clause_ids.add(str(clause_id))
            sample_content_requirement_indexes.update(
                index for index in original_indexes
                if isinstance(index, int) and not isinstance(index, bool) and index >= 0
            )
            sample_content_changes.append({
                "clause_id": str(clause_id),
                "guard_kind": guard["kind"],
                "before_classification": classification,
                "before_requirement_indexes": original_indexes,
                "after_classification": "informational",
                "after_requirement_indexes": [],
                "reason": guard["reason"],
            })
        if sample_content_changes:
            audit.append({
                "type": "normative_scope_guard",
                "guard": "non_normative_sample_content",
                "changes": sample_content_changes,
            })
        # Word emits the modern drawing and its VML fallback as separate
        # textbox evidence.  When the bounded source context identifies those
        # isolated 3 cm labels as the spine demonstration, refine only their
        # disposition to physical/external compliance.  No DOCX property is
        # invented and the original host response remains preserved verbatim
        # in llm-response.raw.json.
        spine_changes = []
        for review in reviews:
            clause = clause_map.get(review.get("clause_id"))
            if (clause is not None
                    and review.get("classification") == "unresolved"
                    and spine_clearance_external(clause, clauses)):
                review["classification"] = "external_compliance"
                review["requirement_indexes"] = []
                review["reason"] = SPINE_CLEARANCE_REASON
                spine_changes.append({
                    "clause_id": clause["id"],
                    "before": "unresolved",
                    "after": "external_compliance",
                    "reason": SPINE_CLEARANCE_REASON,
                })
        if spine_changes:
            audit.append({
                "type": "evidence_context_reclassification",
                "guard": "physical_spine_clearance_annotation",
                "changes": spine_changes,
            })
        # A manual-only observation is not an executable formatting rule.  In
        # particular, a response for a label-only clause such as C00023 used
        # to emit ``properties: {}``, which then violated the requirement
        # contract.  Preserve that outcome as an explicit unverifiable/manual
        # clause state and do not retain the empty requirement.
        manual_empty_indexes = {
            index for index, item in enumerate(requirements)
            if isinstance(item, dict)
            and item.get("properties") == {}
            and isinstance(item.get("verification"), dict)
            and item["verification"].get("mode") == "manual"
        }
        if manual_empty_indexes:
            for review in reviews:
                if not isinstance(review, dict) or not isinstance(review.get("requirement_indexes"), list):
                    continue
                original_indexes = list(review["requirement_indexes"])
                review["requirement_indexes"] = [
                    index for index in original_indexes if index not in manual_empty_indexes
                ]
                if (len(review["requirement_indexes"]) != len(original_indexes)
                        and not review["requirement_indexes"]
                        and classification_requires_requirement(str(review.get("classification")))):
                    review["classification"] = "unverifiable"
                    review["reason"] = (
                        str(review.get("reason") or "").strip()
                        + " Manual verification was retained without an executable empty-properties requirement."
                    ).strip()
            audit.append({
                "type": "manual_empty_requirement_normalization",
                "response_indexes": sorted(manual_empty_indexes),
                "action": "removed_from_execution_and_preserved_as_unverifiable",
            })
        # A declaration entity without a fixed heading/body is not a
        # materializable declaration.  Signature-looking labels in an
        # unrelated section (for example a biography or acknowledgments
        # sample) must not be turned into a new resource or attached to the
        # nearest declaration by first/last-wins merging.
        placeholder_only_declaration_indexes = {
            index for index, item in enumerate(requirements)
            if isinstance(item, dict)
            and item.get("role") == "declarations"
            and isinstance(item.get("properties"), dict)
            and isinstance(item["properties"].get("items"), list)
            and item["properties"].get("items")
            and not any(
                isinstance(declaration, dict)
                and (
                    isinstance(declaration.get("heading"), str)
                    and declaration.get("heading", "").strip()
                    or _declaration_body_parts(declaration)
                )
                for declaration in item["properties"]["items"]
            )
        }
        if placeholder_only_declaration_indexes:
            changes: list[dict[str, Any]] = []
            for review in reviews:
                if not isinstance(review, dict) or not isinstance(review.get("requirement_indexes"), list):
                    continue
                original_indexes = list(review["requirement_indexes"])
                review["requirement_indexes"] = [
                    index for index in original_indexes
                    if index not in placeholder_only_declaration_indexes
                ]
                if (len(review["requirement_indexes"]) != len(original_indexes)
                        and not review["requirement_indexes"]
                        and classification_requires_requirement(str(review.get("classification")))):
                    before = review.get("classification")
                    review["classification"] = "informational"
                    review["normative_basis"] = "sample_content"
                    review["reason"] = (
                        str(review.get("reason") or "").strip()
                        + " The isolated placeholder has no fixed declaration heading or body and is not materialized as a declaration resource."
                    ).strip()
                    changes.append({
                        "clause_id": review.get("clause_id"),
                        "before_classification": before,
                        "after_classification": "informational",
                        "before_requirement_indexes": original_indexes,
                        "after_requirement_indexes": [],
                    })
            audit.append({
                "type": "declaration_placeholder_only_normalization",
                "response_indexes": sorted(placeholder_only_declaration_indexes),
                "changes": changes,
                "action": "removed_from_execution_and_preserved_as_informational_source_content",
            })
        non_executable_indexes = manual_empty_indexes | placeholder_only_declaration_indexes
    for review in reviews:
        if not isinstance(review, dict):
            conflicts.append({"type": "llm_contract", "reason": "invalid_clause_review", "response": review}); continue
        cid = review.get("clause_id"); classification = review.get("classification")
        indexes = review.get("requirement_indexes"); reason = review.get("reason")
        reasons = []
        if cid not in all_clause_ids: reasons.append("unknown_clause_id")
        if cid in review_map: reasons.append("duplicate_clause_review")
        if classification not in ALLOWED_REVIEW_CLASSIFICATIONS: reasons.append("invalid_classification")
        if not isinstance(indexes, list) or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(requirements) for i in (indexes or [])):
            reasons.append("invalid_requirement_indexes")
        elif classification_requires_requirement(classification) and not indexes: reasons.append("executable_requires_requirement_index")
        elif not classification_requires_requirement(classification) and indexes: reasons.append("nonexecutable_must_not_reference_requirement")
        if not isinstance(reason, str) or not reason.strip(): reasons.append("missing_reason")
        existing_indexes = indexes if isinstance(indexes, list) else []
        backed_by_existing = bool(existing_indexes) and all(
            isinstance(requirements[i], dict)
            and requirements[i].get("existing_requirement_id") in existing_requirement_map
            for i in existing_indexes
            if isinstance(i, int) and 0 <= i < len(requirements)
        ) and len(existing_indexes) == len([
            i for i in existing_indexes if isinstance(i, int) and 0 <= i < len(requirements)
        ])
        backed_by_fixed_declaration = bool(existing_indexes) and all(
            isinstance(requirements[i], dict)
            and _declaration_has_fixed_text(requirements[i])
            for i in existing_indexes
            if isinstance(i, int) and 0 <= i < len(requirements)
        ) and len(existing_indexes) == len([
            i for i in existing_indexes if isinstance(i, int) and 0 <= i < len(requirements)
        ])
        if cid in clause_map and _requires_external_artifact_verification(clause_map[cid]["text"]):
            if (classification_requires_requirement(classification)
                    and not (backed_by_existing or backed_by_fixed_declaration)) or classification in {"not_applicable", "verify_existing"}:
                reasons.append("external_artifact_cannot_be_docx_executable_or_verified")
        if reasons:
            conflicts.append({"type": "llm_contract", "reason": reasons, "response": review}); continue
        review_map[cid] = review
    missing = all_clause_ids - set(review_map)
    if missing: conflicts.append({"type": "completeness", "reason": "unreviewed_clauses", "clause_ids": sorted(missing)})
    covered = {cid for cid, r in review_map.items() if classification_requires_requirement(r["classification"])}
    ignored = {cid for cid, r in review_map.items() if r["classification"] in {"ignored", "informational"}}
    # These classifications are fully reviewed semantic outcomes, not open
    # interpretation questions.  They may block full compliance later, but a
    # supported-subset execution must be able to proceed while retaining them
    # in the clause-compliance report.  Only an explicit ``unresolved`` review
    # means that human clarification is still needed at analysis time.
    unresolved = {cid for cid, r in review_map.items() if r["classification"] == "unresolved"}
    review_unsupported = {cid for cid, r in review_map.items() if r["classification"] in {"unsupported", "unsupported_backend"}}

    preserved = {
        key: copy.deepcopy(value) for key, value in rule_spec.items()
        if key not in {"schema_version", "source_document", "analysis_mode", "requirements", "status",
                       "compliance_mode", "completeness", "clause_compliance", "compliance_summary",
                       "content_instances", "cover_field_instances"}
    }
    normalized_roles = _sanitize_role_specs_for_backend(rule_spec.get("roles", {}))
    if isinstance(normalized_roles, dict):
        for content_role in CONTENT_INSTANCE_ROLES:
            if isinstance(normalized_roles.get(content_role), dict):
                normalized_roles[content_role].pop("text", None)
    baseline_roles = copy.deepcopy(normalized_roles)
    spec: dict[str, Any] = {
        "schema_version": "1.0", "source_document": str(source), "analysis_mode": "llm_primary",
        **preserved, "roles": normalized_roles,
        "content_instances": copy.deepcopy(
            rule_spec.get("content_instances", rule_spec.get("cover_field_instances", []))
        ) if isinstance(rule_spec.get("content_instances", rule_spec.get("cover_field_instances", [])), list) else [],
        "requirements": [], "status": "semantic_resolved", "compliance_mode": "full",
        "completeness": {"reviewed_by": "llm", "covered_clause_ids": sorted(covered),
                         "ignored_clause_ids": sorted(ignored), "unresolved_clause_ids": sorted(unresolved),
                         "missing_clause_ids": sorted(missing),
                         "unsupported_items": list(unsupported_items or []) + sorted(review_unsupported)},
    }
    if expected_provenance is not None:
        spec["semantic_review_provenance"] = copy.deepcopy(expected_provenance)
        spec["semantic_review_provenance_valid"] = not any(
            item.get("type") == "llm_provenance" for item in conflicts
        )
    schema = json.loads((Path(__file__).resolve().parents[1] / "schema" / "format-spec.schema.json").read_text(encoding="utf-8"))
    accepted_indexes: set[int] = set()
    llm_shared_role_props: dict[str, dict[str, Any]] = {}
    # Keep conflicts between separate LLM requirements strict, but do not let
    # a deterministic baseline veto an otherwise schema-valid LLM semantic
    # result.  The baseline remains an auditable cross-check; the complete
    # evidence-bound LLM review owns the semantic value for this run.
    llm_primary_role_props: dict[str, dict[str, Any]] = {}
    llm_primary_top_level_props: dict[str, dict[str, Any]] = {}
    top_level_key_by_role = {
        "page": "page", "table": "tables", "objects": "objects",
        "content_constraints": "content_constraints",
        "conditional_constraints": "conditional_constraints",
        "document_structure": "document_structure", "appendices": "appendices",
        "equations": "equations", "cover": "cover", "declarations": "declarations",
    }

    def collect_shared_role_props(role: str, props: dict[str, Any], field_instance_ids: list[str]) -> None:
        """Collect non-literal style properties from the complete review."""
        if role not in CONTENT_INSTANCE_ROLES or field_instance_ids or "text" in props:
            return
        normalized = _style_properties_for_role(role, props)
        if not normalized:
            return
        target = llm_shared_role_props.setdefault(role, {})
        for key, value in normalized.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                target[key].update(copy.deepcopy(value))
            else:
                target[key] = copy.deepcopy(value)
    # Only IDs that are actually reused by this response need to be reserved.
    # Reserving every deterministic requirement ID would make the first newly
    # emitted requirement depend on unrelated rules in the baseline spec (for
    # example, it would start at R00002 merely because R00001 exists in the
    # deterministic cross-check).
    reserved_requirement_ids = {
        str(item.get("existing_requirement_id"))
        for item in requirements
        if isinstance(item, dict)
        and item.get("existing_requirement_id") in existing_requirement_map
    }
    emitted_requirement_ids: set[str] = set()
    next_requirement_number = 1

    def allocate_requirement_id() -> str:
        nonlocal next_requirement_number
        while True:
            candidate = f"R{next_requirement_number:05d}"
            next_requirement_number += 1
            if candidate not in reserved_requirement_ids and candidate not in emitted_requirement_ids:
                emitted_requirement_ids.add(candidate)
                return candidate

    for item_index, item in enumerate(requirements):
        if (
            item_index in sample_content_requirement_indexes
            and isinstance(item, dict)
            and set(item.get("clause_ids") or []) <= sample_content_clause_ids
        ):
            audit.append({
                "accepted": False,
                "response_index": item_index,
                "reason": "sample_content_not_executable",
                "guard": "non_normative_sample_content",
            })
            continue
        if item_index in non_executable_indexes:
            audit.append({"accepted": False, "response_index": item_index,
                          "reason": (
                              "manual_empty_requirement_is_non_executable"
                              if item_index in manual_empty_indexes
                              else "placeholder_only_declaration_is_non_executable"
                          )})
            continue
        if not isinstance(item, dict):
            conflicts.append({"type": "llm_contract", "reason": "requirement_must_be_object", "response": item}); continue
        existing_requirement_id = item.get("existing_requirement_id")
        existing_requirement = existing_requirement_map.get(existing_requirement_id)
        role = item.get("role"); props = item.get("properties")
        clause_ids = set(item.get("clause_ids") or []); cited_evidence = set(item.get("evidence_ids") or [])
        confidence = item.get("confidence")
        reasons = []
        if not role or not isinstance(props, dict) or not props: reasons.append("missing_role_or_properties")
        if role not in ALLOWED_REQUIREMENT_ROLES and existing_requirement is None: reasons.append("unknown_requirement_role")
        if not clause_ids or not clause_ids <= all_clause_ids: reasons.append("invalid_clause_ids")
        allowed_evidence = {eid for cid in clause_ids if cid in clause_map for eid in clause_map[cid]["evidence_ids"]}
        if not cited_evidence or not cited_evidence <= allowed_evidence or not cited_evidence <= evidence_ids:
            reasons.append("invalid_evidence_ids")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1: reasons.append("invalid_confidence")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip(): reasons.append("missing_requirement_reason")
        # A fixed-text DOCX requirement may legitimately cite a companion
        # external-duty clause (for example, the signature/date line of a
        # declaration) while the clause review keeps the actual act of
        # signing external_compliance.  Such a clause is evidence for the
        # placeholder structure, not an executable obligation.  Informational
        # or sample-content clauses remain disallowed here.
        external_clause_ids = {
            cid for cid in clause_ids
            if cid in review_map
            and review_map[cid].get("classification") == "external_compliance"
        }
        if not clause_ids <= (covered | external_clause_ids):
            reasons.append("requirement_clause_not_covered")
        schema_name = {"page": "pageSpec", "table": "tableSpec", "objects": "objectPaginationSpec",
                       "content_constraints": "contentConstraintSpec",
                       "conditional_constraints": "conditionalConstraintSpec",
                       "document_structure": "documentStructureSpec", "appendices": "appendixSpec",
                       "equations": "equationLayoutSpec", "cover": "coverSpec",
                       "declarations": "declarationsSpec"}.get(role, "roleSpec")
        target_schema = schema["$defs"][schema_name]
        if isinstance(props, dict): reasons.extend(validate_instance(props, target_schema, schema, "$.properties"))
        if role in CONTENT_INSTANCE_ROLES and "field_key" in item:
            if not isinstance(item.get("field_key"), str) or not item.get("field_key", "").strip():
                reasons.append("field_key_must_be_nonempty_string")
        for field, definition in (("applicability", "applicabilitySpec"),
                                  ("verification", "verificationSpec")):
            if field in item:
                reasons.extend(validate_instance(item[field], schema["$defs"][definition], schema,
                                                 f"$.{field}"))
        if "input_prerequisites" in item:
            prereqs = item.get("input_prerequisites")
            if not isinstance(prereqs, list):
                reasons.append("input_prerequisites_must_be_array")
            else:
                for prereq_index, prereq in enumerate(prereqs):
                    reasons.extend(validate_instance(prereq, schema["$defs"]["inputPrerequisiteSpec"], schema,
                                                     f"$.input_prerequisites[{prereq_index}]"))
        expected_indexes = {i for cid in clause_ids if cid in review_map for i in review_map[cid]["requirement_indexes"]}
        if item_index not in expected_indexes: reasons.append("requirement_not_referenced_by_clause_review")
        if existing_requirement_id is not None:
            if existing_requirement is None:
                reasons.append("unknown_existing_requirement_id")
            else:
                if role != existing_requirement.get("role") or props != existing_requirement.get("properties"):
                    reasons.append("existing_requirement_payload_mismatch")
                if cited_evidence != set(existing_requirement.get("evidence_ids") or []):
                    reasons.append("existing_requirement_evidence_mismatch")
                # A deterministic requirement may legitimately combine
                # several clauses from the same source statement.  Validate
                # the exact canonical composition instead of imposing a
                # one-clause restriction; multiplicity is preserved by the
                # sorted clause-id list and no fuzzy matching is used.
                expected_source = " | ".join(clause_map[cid].get("text", "") for cid in sorted(clause_ids))
                if _normalized_exact_text(existing_requirement.get("source_text")) != _normalized_exact_text(expected_source):
                    reasons.append("existing_requirement_source_text_mismatch")
        if reasons:
            conflicts.append({"type": "llm_contract", "reason": reasons, "response": item})
            audit.append({"accepted": False, "reason": reasons, "response": item}); continue
        field_instance_ids: list[str] = []
        if role in CONTENT_INSTANCE_ROLES:
            instance_id, instance_error = _register_content_instance(
                spec["content_instances"], item, role, props, clause_ids,
                cited_evidence, clause_map, reason,
            )
            if instance_error:
                conflicts.append({"type": "llm_internal_conflict", "role": role,
                                  "property": "field_instance", "clause_ids": sorted(clause_ids),
                                  **instance_error})
                audit.append({"accepted": False, "reason": "llm_internal_conflict", "response": item}); continue
            if instance_id:
                field_instance_ids = [instance_id]
        normalized_props = _style_properties_for_role(role, props)
        collect_shared_role_props(role, normalized_props, field_instance_ids)
        if existing_requirement is not None:
            req = copy.deepcopy(existing_requirement)
            req["clause_ids"] = sorted(clause_ids)
            req["properties"] = _requirement_properties_for_role(
                role, req.get("properties", props)
            )
            req["reason"] = reason.strip()
            if field_instance_ids:
                req["field_instance_ids"] = field_instance_ids
            collect_shared_role_props(role, req["properties"], field_instance_ids)
            spec["requirements"].append(req)
            emitted_requirement_ids.add(str(req["id"]))
            accepted_indexes.add(item_index)
            audit.append({"accepted": True, "response_index": item_index,
                          "requirement_ids": [req["id"]], "role": role,
                          "normalized_roles": [role], "reused_existing_requirement": True})
            continue
        target_roles = ("figure_caption", "table_caption") if role == "figure_table_title" else (role,)
        item_requirements = []
        top_key = top_level_key_by_role.get(target_roles[0])
        if top_key is not None:
            if top_key == "declarations":
                # Declaration items are a keyed collection.  A heading-only
                # fragment and a body-only fragment for the same semantic
                # item are complementary; treating ``items`` as an ordinary
                # list makes the first fragment win and leaves a broken
                # resource registry (the UJS failure mode).
                staged_declarations = copy.deepcopy(
                    llm_primary_top_level_props.get(top_key, {})
                )
                declaration_conflicts = _merge_declaration_properties(
                    staged_declarations, props
                )
                projection_conflicts = [
                    (target_roles[0], key, old, new)
                    for key, old, new in declaration_conflicts
                ]
                for target_role, key, old, new in projection_conflicts:
                    conflicts.append({
                        "type": "llm_role_projection_conflict", "role": target_role,
                        "property": key, "existing_value": old, "new_value": new,
                        "clause_ids": sorted(clause_ids),
                    })
                # Different source occurrences may provide alternate labels
                # for the same author/date placeholder.  Keep the first
                # materializable label and retain the variant as an audit
                # finding; a true anchor/body/entity conflict remains
                # blocking for this response item.
                blocking_declaration_conflicts = [
                    conflict for conflict in projection_conflicts
                    if ".signature_placeholders[" not in str(conflict[1])
                ]
                if not blocking_declaration_conflicts:
                    llm_primary_top_level_props[top_key] = staged_declarations
                    spec[top_key] = copy.deepcopy(staged_declarations)
                merge_conflicts = blocking_declaration_conflicts
            else:
                # Project compatible fields into the shared role object, but keep
                # each requirement as the authoritative clause-scoped record.  A
                # later requirement may legitimately constrain the same role with
                # a different local list/value (for example, a structure subset
                # or a degree-conditional keyword count); that projection conflict
                # must not discard the valid requirement itself.
                staged_llm = copy.deepcopy(llm_primary_top_level_props.get(top_key, {}))
                scoped_props = copy.deepcopy(props)
                if top_key == "document_structure" and isinstance(scoped_props.get("ordered_roles"), list):
                    incoming_order = scoped_props.pop("ordered_roles")
                    groups = staged_llm.setdefault("ordered_role_groups", [])
                    # A deterministic requirement may already have established
                    # one valid order before the first fresh LLM projection is
                    # seen.  Keep that order as a separate scope rather than
                    # overwriting it or inventing one global order.
                    baseline_order = spec.get(top_key, {}).get("ordered_roles")
                    if isinstance(baseline_order, list) and baseline_order and baseline_order not in groups:
                        groups.append(copy.deepcopy(baseline_order))
                    existing_order = staged_llm.get("ordered_roles")
                    if isinstance(existing_order, list) and existing_order and existing_order not in groups:
                        groups.append(copy.deepcopy(existing_order))
                    if incoming_order and incoming_order not in groups:
                        groups.append(copy.deepcopy(incoming_order))
                    if not isinstance(existing_order, list) or not existing_order:
                        staged_llm["ordered_roles"] = copy.deepcopy(incoming_order)
                    primary_order = staged_llm.get("ordered_roles")
                    if isinstance(primary_order, list):
                        staged_llm["ordered_role_groups"] = [
                            group for group in groups if group != primary_order
                        ]
                projection_conflicts = [
                    (target_roles[0], key, old, new)
                    for key, old, new in _merge_scoped_projection(staged_llm, scoped_props)
                ]
                for target_role, key, old, new in projection_conflicts:
                    conflicts.append({
                        "type": "llm_role_projection_conflict", "role": target_role,
                        "property": key, "existing_value": old, "new_value": new,
                        "clause_ids": sorted(clause_ids),
                    })
                # Projection conflicts are audit findings, not a reason to
                # discard the whole clause-scoped requirement.  The previous
                # all-or-nothing branch silently dropped later cover/object/
                # structure fragments, which made a valid response appear
                # incomplete after merging.
                llm_primary_top_level_props[top_key] = staged_llm
                # LLM-primary semantics override only the properties it
                # explicitly resolved, while scoped collections are unioned
                # into deterministic content already present in ``spec``.
                baseline_probe = copy.deepcopy(preserved.get(top_key, {}))
                baseline_conflicts = _deep_merge(baseline_probe, props)
                for key, old, new in baseline_conflicts:
                    conflicts.append({
                        "type": "rule_llm_conflict", "role": target_roles[0],
                        "property": key, "rule_value": old, "llm_value": new,
                        "clause_ids": sorted(clause_ids),
                    })
                merged_top_level = copy.deepcopy(spec.get(top_key, {}))
                scoped_baseline_conflicts = _merge_scoped_projection(
                    merged_top_level, scoped_props, prefer_incoming=True,
                )
                for key, old, new in scoped_baseline_conflicts:
                    conflicts.append({
                        "type": "rule_llm_conflict", "role": target_roles[0],
                        "property": key, "rule_value": old, "llm_value": new,
                        "clause_ids": sorted(clause_ids),
                    })
                if top_key == "document_structure":
                    if isinstance(staged_llm.get("ordered_roles"), list):
                        merged_top_level["ordered_roles"] = copy.deepcopy(
                            staged_llm["ordered_roles"]
                        )
                    if isinstance(staged_llm.get("ordered_role_groups"), list):
                        merged_top_level["ordered_role_groups"] = copy.deepcopy(
                            staged_llm["ordered_role_groups"]
                        )
                spec[top_key] = merged_top_level
                merge_conflicts = []
        else:
            if role in CONTENT_INSTANCE_ROLES:
                merge_conflicts = _merge_role_group(spec["roles"], target_roles, normalized_props)
                # Literal-content requirements may legitimately use a style
                # override for this particular instance.  The instance keeps
                # the full normalized style properties; the role stores only
                # the compatible shared/default subset.
                merge_conflicts = []
            else:
                # Apply the same LLM-primary rule to shared semantic roles.
                # Conflicts among LLM items remain strict; conflicts with the
                # deterministic baseline are recorded as audit findings.
                staged_llm_roles = copy.deepcopy(llm_primary_role_props)
                projection_conflicts: list[tuple[str, str, Any, Any]] = []
                for target_role in target_roles:
                    projection_conflicts.extend(
                        (target_role, key, old, new)
                        for key, old, new in _deep_merge(
                            staged_llm_roles.setdefault(target_role, {}), normalized_props
                        )
                    )
                for target_role, key, old, new in projection_conflicts:
                    conflicts.append({
                        "type": "llm_role_projection_conflict", "role": target_role,
                        "property": key, "existing_value": old, "new_value": new,
                        "clause_ids": sorted(clause_ids),
                    })
                if not projection_conflicts:
                    llm_primary_role_props = staged_llm_roles
                    for target_role in target_roles:
                        baseline_probe = copy.deepcopy(baseline_roles.get(target_role, {}))
                        baseline_conflicts = _deep_merge(baseline_probe, normalized_props)
                        for key, old, new in baseline_conflicts:
                            conflicts.append({
                                "type": "rule_llm_conflict", "role": target_role,
                                "property": key, "rule_value": old, "llm_value": new,
                                "clause_ids": sorted(clause_ids),
                            })
                        target_spec = copy.deepcopy(spec["roles"].get(target_role, {}))
                        _deep_overlay(target_spec, normalized_props)
                        spec["roles"][target_role] = target_spec
                merge_conflicts = []
        if merge_conflicts:
            conflicts.extend({"type": "llm_internal_conflict", "role": target_role, "property": key,
                              "existing_value": old, "new_value": new, "clause_ids": sorted(clause_ids)}
                             for target_role, key, old, new in merge_conflicts)
            audit.append({"accepted": False, "reason": "llm_internal_conflict", "response": item}); continue
        for target_role in target_roles:
            req = {"id": allocate_requirement_id(), "role": target_role,
                   "properties": _requirement_properties_for_role(role, props),
                   "evidence_ids": sorted(cited_evidence),
                   "clause_ids": sorted(clause_ids), "resolved_by": "llm", "confidence": float(confidence),
                   "source_text": " | ".join(clause_map[cid]["text"] for cid in sorted(clause_ids)),
                   "reason": reason.strip()}
            if field_instance_ids:
                req["field_instance_ids"] = list(field_instance_ids)
            for field in ("applicability", "input_prerequisites", "verification"):
                if field in item:
                    req[field] = copy.deepcopy(item[field])
            spec["requirements"].append(req); item_requirements.append(req)
        accepted_indexes.add(item_index)
        audit.append({"accepted": True, "response_index": item_index,
                      "requirement_ids": [r["id"] for r in item_requirements],
                      "role": role, "normalized_roles": list(target_roles)})

    # In a complete evidence-bound review, explicit shared body-text style
    # requirements take precedence over a contaminated deterministic baseline
    # (for example, a mixed title/abstract sentence misread as body text).
    # Literal content roles remain represented by their instance records.
    if "body_text" in llm_shared_role_props:
        spec["roles"]["body_text"] = copy.deepcopy(llm_shared_role_props["body_text"])

    for cid in covered:
        indexes = set(review_map[cid]["requirement_indexes"])
        if not indexes <= accepted_indexes or not any(
            isinstance(requirements[i], dict)
            and cid in set(requirements[i].get("clause_ids") or [])
            for i in indexes if isinstance(i, int) and 0 <= i < len(requirements)
        ):
            conflicts.append({"type": "llm_contract", "reason": "covered_clause_not_backed_by_accepted_requirement", "clause_id": cid})

    requirement_ids_by_index = {
        int(item["response_index"]): list(item.get("requirement_ids", []))
        for item in audit
        if item.get("accepted") and isinstance(item.get("response_index"), int)
    }
    spec["clause_compliance"] = build_clause_records(
        clauses, review_map, requirement_ids_by_index,
        accepted_requirement_indexes=accepted_indexes,
    )
    spec["compliance_summary"] = compliance_report(spec["clause_compliance"], "full", "analysis")

    # Rules are a cross-check only.  They may expose a missed property, but do
    # not silently overwrite the LLM-primary interpretation.
    for role, rule_props in [("page", rule_spec.get("page", {})), *list(rule_spec.get("roles", {}).items())]:
        if role == "body_text" and role in llm_shared_role_props:
            # The complete review has already replaced the shared body style;
            # comparing it with the known contaminated deterministic baseline
            # would report a stale conflict after the correct override.
            continue
        if role in CONTENT_INSTANCE_ROLES and isinstance(rule_props, dict):
            rule_props = _style_properties_for_role(role, rule_props)
        llm_props = spec.get("page", {}) if role == "page" else spec["roles"].get(role, {})
        probe = copy.deepcopy(llm_props)
        for key, old, new in _deep_merge(probe, rule_props):
            conflicts.append({"type": "rule_llm_conflict", "role": role, "property": key,
                              "llm_value": old, "rule_value": new})
        if rule_props and not llm_props:
            conflicts.append({"type": "rule_llm_gap", "role": role, "rule_properties": rule_props})
    # Requirements retain per-clause numbering details, but the role registry
    # consumed by the application must contain only executable shared style
    # defaults.  A later LLM requirement may have reintroduced a declarative
    # depth while merging, so sanitize once more at the boundary.
    spec["roles"] = _sanitize_role_specs_for_backend(spec.get("roles", {}))
    # Reported semantic disagreements and deterministic rule cross-checks are
    # audit findings, not automatically execution blockers: in llm_primary mode
    # the complete evidence-citing LLM review owns interpretation.  Only contract
    # violations and merge conflicts mean the structured response is unsafe.
    blocking_types = {"llm_contract", "llm_internal_conflict", "completeness", "llm_provenance"}
    blocking_conflicts = [item for item in conflicts if item.get("type") in blocking_types]
    if blocking_conflicts:
        spec["blocking_errors"] = copy.deepcopy(blocking_conflicts)
    if unresolved or missing or blocking_conflicts:
        spec["status"] = "needs_clarification"
    return spec, conflicts, audit


def _narrow_rule_spec_for_chunk(rule_spec: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep only deterministic requirements that can be safely reused here.

    The complete deterministic rule spec may contain hundreds of role variants
    for unrelated evidence occurrences.  Supplying all of them to every LLM
    chunk encourages spurious ``existing_requirement_id`` references.  An
    existing requirement is eligible for reuse only when its canonical source
    text exactly matches a clause in this chunk and its cited evidence overlaps
    that same chunk.  The original full rule spec remains available to the
    post-merge deterministic cross-check.
    """
    narrowed = copy.deepcopy(rule_spec or {})
    clause_texts = {
        _normalized_exact_text(clause.get("text", ""))
        for clause in clauses
        if isinstance(clause, dict) and clause.get("text")
    }
    clause_evidence_ids = {
        str(evidence_id)
        for clause in clauses
        if isinstance(clause, dict)
        for evidence_id in (clause.get("evidence_ids") or [])
    }
    eligible: list[dict[str, Any]] = []
    for requirement in rule_spec.get("requirements", []) if isinstance(rule_spec, dict) else []:
        if not isinstance(requirement, dict):
            continue
        source_text = _normalized_exact_text(requirement.get("source_text", ""))
        requirement_evidence_ids = {str(item) for item in (requirement.get("evidence_ids") or [])}
        matching_clause_ids = [
            str(clause.get("id"))
            for clause in clauses
            if isinstance(clause, dict)
            and _normalized_exact_text(clause.get("text", "")) == source_text
            and requirement_evidence_ids & {str(item) for item in (clause.get("evidence_ids") or [])}
        ]
        if source_text and source_text in clause_texts and matching_clause_ids:
            candidate = copy.deepcopy(requirement)
            # Request-only routing metadata; it is not accepted as part of
            # the emitted requirement contract and is removed by the model.
            candidate["_eligible_clause_ids"] = matching_clause_ids
            eligible.append(candidate)
    narrowed["requirements"] = eligible
    return narrowed


def _validate_host_review_chunk_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("host review chunk size must be a positive integer")
    return value


def _build_host_review_chunks(
    full_request: dict[str, Any],
    clauses: list[dict[str, Any]],
    evidence_doc: dict[str, Any],
    source_sha256: str,
    chunk_size: int,
) -> list[dict[str, Any]]:
    """Build bounded, evidence-bound requests for the current host Agent.

    This function is deliberately offline.  It only prepares packets; the
    Agent that invoked the skill is responsible for producing each JSON
    response with its current runtime model.
    """
    chunk_size = _validate_host_review_chunk_size(chunk_size)
    chunks = [clauses[i:i + chunk_size] for i in range(0, len(clauses), chunk_size)] or [[]]
    request_chunks: list[dict[str, Any]] = []
    all_evidence = {
        str(item.get("id")): item
        for item in evidence_doc.get("evidence", [])
        if isinstance(item, dict) and item.get("id")
    }
    for index, chunk in enumerate(chunks):
        chunk_ids = {cid for clause in chunk for cid in clause.get("evidence_ids", [])}
        chunk_evidence = copy.deepcopy(evidence_doc)
        chunk_evidence["evidence"] = [item for eid, item in all_evidence.items() if eid in chunk_ids]
        chunk_rule_spec = _narrow_rule_spec_for_chunk(full_request.get("rule_spec", {}), chunk)
        chunk_request = build_llm_request([], chunk, chunk_evidence, chunk_rule_spec, "full")
        chunk_request["batch"] = {
            "index": index + 1,
            "count": len(chunks),
            "clause_ids": [clause.get("id") for clause in chunk],
            "response_filename": f"llm-response-chunk-{index + 1:04d}.json",
        }
        chunk_request = attach_request_provenance(
            chunk_request,
            source_sha256=source_sha256,
            evidence_doc=chunk_evidence,
            clauses=chunk,
            run_id=full_request.get("provenance", {}).get("run_id"),
            origin=HOST_AGENT_ORIGIN,
        )
        # Chunk hashes must describe the exact packet visible to the host,
        # rather than rich extraction fields intentionally omitted from the
        # compact request.  The full request keeps its own rich evidence
        # identity; the chunk carries a separately verifiable projection.
        chunk_request["provenance"]["evidence_sha256"] = sha256_json(
            evidence_payload({
                "evidence": list(chunk_request.get("evidence_context", {}).values()),
                "page_evidence": chunk_request.get("page_evidence", {}),
                "structure_evidence": chunk_request.get("document_structure", {}),
                "structure_page_evidence": {},
            })
        )
        chunk_request["provenance"]["clause_sha256"] = sha256_json(
            chunk_request.get("clauses", [])
        )
        chunk_request["provenance"]["request_sha256"] = request_body_sha256(
            chunk_request
        )
        request_chunks.append(chunk_request)
    return request_chunks


def prepare_host_agent_review_packets(
    full_request: dict[str, Any],
    clauses: list[dict[str, Any]],
    evidence_doc: dict[str, Any],
    source_sha256: str,
    out: Path,
    *,
    chunk_size: int = 20,
) -> dict[str, Any]:
    """Persist the offline protocol consumed by the current host Agent."""
    write_json(out / "llm-request.json", full_request)
    request_chunks = _build_host_review_chunks(
        full_request, clauses, evidence_doc, source_sha256, chunk_size,
    )
    write_json(out / "llm-request-chunks.json", request_chunks)
    response_files = [item["batch"]["response_filename"] for item in request_chunks]
    manifest = {
        "schema_version": "1.0",
        "protocol": "host_agent_semantic_review",
        "contract_version": "2.1",
        "origin": full_request.get("provenance", {}).get("origin"),
        "request_body_sha256": request_body_sha256(full_request),
        "request_envelope_sha256": request_envelope_sha256(full_request),
        "request_path": "llm-request.json",
        "request_chunks_path": "llm-request-chunks.json",
        "response_files": response_files,
        "chunk_count": len(request_chunks),
        "chunk_size": chunk_size,
        "clause_count": len(clauses),
        "instructions": [
            "The host Agent must generate each response with its current runtime model.",
            "The project performs no provider/API call and must not receive an API key.",
            "Automatic native bridge runs bind provenance from the current invocation; the model must not author or copy long hashes.",
            "Offline/manual response files must include the exact chunk provenance before deterministic merge.",
            "Every supplied clause must appear exactly once in that chunk's clause_reviews.",
            "Run merge_host_agent_review.py only after every response file exists.",
        ],
    }
    write_json(out / "host-agent-review-manifest.json", manifest)
    return manifest


def _manifest_path(review_dir: Path, value: str) -> Path:
    root = review_dir.resolve()
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(
            f"manifest path escapes its run directory: {value!r} -> {resolved}"
        )
    return resolved


def _merge_output_path(review_dir: Path, value: Path) -> Path:
    """Resolve an output below the enclosing run directory, never elsewhere."""
    run_root = review_dir.resolve().parent
    resolved = value.expanduser().resolve()
    if resolved == run_root or run_root not in resolved.parents:
        raise ValueError(
            f"merge output escapes its run directory: {value} -> {resolved}"
        )
    return resolved


def _write_json_artifacts_atomic(artifacts: list[tuple[Path, Any]]) -> None:
    """Stage all JSON artifacts, then publish them without overwriting old files."""
    if not artifacts:
        return
    paths = [path for path, _ in artifacts]
    if len(set(paths)) != len(paths):
        raise ValueError("merge artifacts must use distinct output paths")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ValueError(f"refusing to overwrite existing merge artifact: {path}")
    temporary_paths: list[tuple[Path, Path]] = []
    published_paths: list[Path] = []
    try:
        for path, value in artifacts:
            temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_paths.append((temporary, path))
        for temporary, path in temporary_paths:
            temporary.replace(path)
            published_paths.append(path)
    except Exception:
        # No target existed before this transaction (checked above).  Remove
        # only artifacts published by this invocation so a partial receipt can
        # never masquerade as a complete merge after a filesystem failure.
        for path in published_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for temporary, _ in temporary_paths:
            if temporary.exists():
                temporary.unlink()


def merge_host_agent_review_packets(
    review_dir: Path,
    *,
    response_out: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and deterministically merge host-Agent chunk responses."""
    manifest_path = review_dir / "host-agent-review-manifest.json"
    manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol") != "host_agent_semantic_review":
        raise ValueError("review manifest is not a host-agent semantic-review manifest")
    full_request = strict_json_loads(
        _manifest_path(review_dir, str(manifest.get("request_path", "llm-request.json"))).read_text(encoding="utf-8")
    )
    if not isinstance(full_request, dict) or not isinstance(full_request.get("provenance"), dict):
        raise ValueError("host-agent full request is missing an object provenance")
    request_chunks = strict_json_loads(
        _manifest_path(review_dir, str(manifest.get("request_chunks_path", "llm-request-chunks.json"))).read_text(encoding="utf-8")
    )
    response_files = manifest.get("response_files")
    if not isinstance(request_chunks, list) or not request_chunks:
        raise ValueError("host-agent request chunks are missing or empty")
    declared_chunk_count = manifest.get("chunk_count")
    if (isinstance(declared_chunk_count, bool)
            or not isinstance(declared_chunk_count, int)
            or declared_chunk_count != len(request_chunks)):
        raise ValueError("host-agent manifest chunk_count does not match request chunks")
    if not isinstance(response_files, list) or len(response_files) != len(request_chunks):
        raise ValueError("host-agent response file manifest does not match request chunks")
    if any(not isinstance(item, str) or not item.strip() for item in response_files):
        raise ValueError("host-agent response file manifest contains a non-string path")
    if len(set(response_files)) != len(response_files):
        raise ValueError("host-agent response file manifest contains duplicate paths")

    response_out_path = (
        _merge_output_path(review_dir, response_out)
        if response_out is not None else None
    )
    merge_receipt_path = review_dir.resolve() / "merge-receipt.json"
    if response_out_path and response_out_path == merge_receipt_path:
        raise ValueError("merged response and merge receipt must use distinct paths")
    if response_out_path and response_out_path.exists():
        raise ValueError(f"refusing to overwrite existing merge artifact: {response_out_path}")
    if merge_receipt_path.exists():
        raise ValueError(f"refusing to overwrite existing merge artifact: {merge_receipt_path}")

    full_clauses = full_request.get("clauses")
    if not isinstance(full_clauses, list) or not full_clauses:
        raise ValueError("host-agent full request is missing clauses")
    full_clause_ids = [
        str(item.get("id")) for item in full_clauses
        if isinstance(item, dict) and item.get("id")
    ]
    if len(full_clause_ids) != len(full_clauses) or len(set(full_clause_ids)) != len(full_clause_ids):
        raise ValueError("host-agent full request contains duplicate or invalid clause ids")
    full_clause_id_set = set(full_clause_ids)
    full_provenance = full_request["provenance"]
    expected_manifest_body_sha = manifest.get("request_body_sha256")
    expected_manifest_envelope_sha = manifest.get("request_envelope_sha256")
    if expected_manifest_body_sha and expected_manifest_body_sha != request_body_sha256(full_request):
        raise ValueError("host-agent manifest request body hash does not match full request")
    if expected_manifest_envelope_sha and expected_manifest_envelope_sha != request_envelope_sha256(full_request):
        raise ValueError("host-agent manifest request envelope hash does not match full request")
    seen_chunk_indexes: set[int] = set()
    seen_chunk_clause_ids: set[str] = set()

    aggregate_requirements: list[dict[str, Any]] = []
    aggregate_reviews: list[dict[str, Any]] = []
    aggregate_unsupported: list[str] = []
    aggregate_conflicts: list[dict[str, Any]] = []
    response_clause_counts: list[int] = []
    for index, (chunk_request, response_name) in enumerate(zip(request_chunks, response_files), start=1):
        if not isinstance(chunk_request, dict):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} is not an object")
        response_path = _manifest_path(review_dir, str(response_name))
        batch = chunk_request.get("batch")
        if not isinstance(batch, dict):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} is missing batch metadata")
        batch_index = batch.get("index")
        batch_count = batch.get("count")
        if (isinstance(batch_index, bool) or not isinstance(batch_index, int)
                or batch_index != index or batch_index in seen_chunk_indexes):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} has invalid batch index")
        if (isinstance(batch_count, bool) or not isinstance(batch_count, int)
                or batch_count != len(request_chunks)):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} has invalid batch count")
        expected_response_name = batch.get("response_filename")
        if not isinstance(expected_response_name, str) or expected_response_name != response_name:
            raise ValueError(f"host-agent response filename does not match chunk {index} batch metadata")
        chunk_clause_ids = batch.get("clause_ids")
        if not isinstance(chunk_clause_ids, list) or any(
            not isinstance(item, str) or not item for item in chunk_clause_ids
        ) or len(set(chunk_clause_ids)) != len(chunk_clause_ids):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} has invalid clause ids")
        actual_chunk_clause_ids = [
            str(item.get("id")) for item in chunk_request.get("clauses", [])
            if isinstance(item, dict) and item.get("id")
        ]
        if actual_chunk_clause_ids != chunk_clause_ids:
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} batch clauses do not match packet clauses")
        if not set(chunk_clause_ids) <= full_clause_id_set or seen_chunk_clause_ids & set(chunk_clause_ids):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} has duplicate or unknown clauses")
        chunk_provenance = chunk_request.get("provenance")
        if not isinstance(chunk_provenance, dict):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} is missing provenance")
        for key in ("version", "origin", "source_sha256"):
            if chunk_provenance.get(key) != full_provenance.get(key):
                raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} provenance {key} does not match full request")
        if "run_id" in full_provenance and chunk_provenance.get("run_id") != full_provenance.get("run_id"):
            raise ValueError(f"host-agent request chunk {index}/{len(request_chunks)} provenance run_id does not match full request")
        visible_evidence_payload = evidence_payload({
            "evidence": list(chunk_request.get("evidence_context", {}).values()),
            "page_evidence": chunk_request.get("page_evidence", {}),
            "structure_evidence": chunk_request.get("document_structure", {}),
            "structure_page_evidence": {},
        })
        expected_chunk_hashes = {
            "evidence_sha256": sha256_json(visible_evidence_payload),
            "clause_sha256": sha256_json(chunk_request.get("clauses", [])),
            "request_sha256": request_body_sha256(chunk_request),
        }
        for key, expected_value in expected_chunk_hashes.items():
            if chunk_provenance.get(key) != expected_value:
                raise ValueError(
                    f"host-agent request chunk {index}/{len(request_chunks)} "
                    f"provenance {key} does not match its packet"
                )
        seen_chunk_indexes.add(batch_index)
        seen_chunk_clause_ids.update(chunk_clause_ids)
        if not response_path.is_file():
            raise ValueError(f"missing host-agent response {index}/{len(request_chunks)}: {response_path}")
        response = strict_json_loads(response_path.read_text(encoding="utf-8"))
        if not isinstance(response, dict):
            raise ValueError(f"host-agent response {index}/{len(request_chunks)} is not an object")
        if response.get("contract_version") != "2.1":
            raise ValueError(f"host-agent response {index}/{len(request_chunks)} is not contract 2.1")
        contract_errors = validate_host_review_response(response, chunk_request)
        if contract_errors:
            raise ValueError(
                f"host-agent response {index}/{len(request_chunks)} contract failed: "
                + "; ".join(contract_errors[:12])
            )
        provenance_errors = validate_response_provenance(
            response, chunk_request.get("provenance", {}), require_fresh_origin=True,
        )
        if provenance_errors:
            raise ValueError(
                f"host-agent response {index}/{len(request_chunks)} provenance failed: "
                + ", ".join(provenance_errors)
            )
        expected_clause_ids = [str(item) for item in chunk_request.get("batch", {}).get("clause_ids", [])]
        reviews = response.get("clause_reviews")
        if not isinstance(reviews, list):
            raise ValueError(f"host-agent response {index}/{len(request_chunks)} clause_reviews must be an array")
        actual_clause_ids = [str(item.get("clause_id")) for item in reviews if isinstance(item, dict)]
        if (len(actual_clause_ids) != len(expected_clause_ids)
                or len(set(actual_clause_ids)) != len(actual_clause_ids)
                or set(actual_clause_ids) != set(expected_clause_ids)):
            raise ValueError(
                f"host-agent response {index}/{len(request_chunks)} must review exactly its supplied clauses"
            )
        requirements = response.get("requirements")
        if not isinstance(requirements, list):
            raise ValueError(f"host-agent response {index}/{len(request_chunks)} requirements must be an array")
        offset = len(aggregate_requirements)
        aggregate_requirements.extend(copy.deepcopy(requirements))
        for review in reviews:
            shifted = copy.deepcopy(review)
            shifted["requirement_indexes"] = [
                item + offset for item in (review.get("requirement_indexes") or [])
            ]
            aggregate_reviews.append(shifted)
        for item in response.get("unsupported_items", []):
            if item not in aggregate_unsupported:
                aggregate_unsupported.append(item)
        aggregate_conflicts.extend(copy.deepcopy(response.get("reported_conflicts", [])))
        response_clause_counts.append(len(reviews))

    if seen_chunk_clause_ids != full_clause_id_set:
        missing = sorted(full_clause_id_set - seen_chunk_clause_ids)
        extra = sorted(seen_chunk_clause_ids - full_clause_id_set)
        raise ValueError(
            "host-agent request chunks do not partition the full request clauses: "
            f"missing={missing}, extra={extra}"
        )

    aggregate = {
        "contract_version": "2.1",
        "provenance": copy.deepcopy(full_request["provenance"]),
        "requirements": aggregate_requirements,
        "clause_reviews": aggregate_reviews,
        "unsupported_items": aggregate_unsupported,
        "reported_conflicts": aggregate_conflicts,
    }
    aggregate_errors = validate_host_review_response(aggregate, full_request)
    if aggregate_errors:
        raise ValueError(
            "merged host-agent response contract failed: "
            + "; ".join(aggregate_errors[:12])
        )
    metadata = {
        "protocol": "host_agent_semantic_review",
        "chunked": len(request_chunks) > 1,
        "chunk_count": len(request_chunks),
        "chunk_size": manifest.get("chunk_size"),
        "response_clause_reviews": response_clause_counts,
        "merged_response_path": str(response_out_path) if response_out_path else None,
        "merge_receipt_path": str(merge_receipt_path.resolve()),
        "aggregate_sha256": sha256_json(aggregate),
        # Keep the legacy name in the receipt for contract-2.1 consumers, but
        # make the two hash domains explicit for new gates.
        "request_sha256": full_provenance.get("request_sha256"),
        "request_body_sha256": request_body_sha256(full_request),
        "request_envelope_sha256": request_envelope_sha256(full_request),
        "request_file_sha256": sha256_file(
            _manifest_path(review_dir, str(manifest.get("request_path", "llm-request.json")))
        ),
    }
    receipt = {
        "schema_version": "1.0",
        "status": "merged",
        "protocol": "host_agent_semantic_review",
        "run_id": full_request.get("provenance", {}).get("run_id"),
        "request_sha256": full_request.get("provenance", {}).get("request_sha256"),
        **metadata,
    }
    artifacts = [(merge_receipt_path, receipt)]
    if response_out_path:
        artifacts.insert(0, (response_out_path, aggregate))
    _write_json_artifacts_atomic(artifacts)
    return aggregate, metadata


def response_contract_kind(response: Any) -> str:
    """Identify a semantic-response contract without consulting school mode."""
    if not isinstance(response, dict):
        return "unknown"
    if (response.get("contract_version") == "2.1"
            and isinstance(response.get("requirements"), list)
            and isinstance(response.get("clause_reviews"), list)):
        return "complete_clause_review"
    if isinstance(response.get("resolutions"), list):
        return "legacy_question_resolutions"
    return "unknown"


def merge_llm(spec: dict[str, Any], questions: list[dict[str, Any]], clauses: list[dict[str, Any]], response: dict[str, Any],
              has_rule_conflicts: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    qmap = {q["id"]: q for q in questions}; cmap = {c["id"]: c for c in clauses}
    remaining = []; audit = []; seen = set()
    for item in response.get("resolutions", []):
        qid = item.get("question_id"); q = qmap.get(qid)
        if not q or qid in seen: audit.append({"accepted": False, "reason": "unknown_or_duplicate_question", "response": item}); continue
        seen.add(qid)
        evidence = item.get("evidence_ids") or []
        allowed_evidence = set(q["evidence_ids"])
        role = item.get("role")
        if item.get("unresolved") or not evidence or not set(evidence) <= allowed_evidence:
            remaining.append(q); audit.append({"accepted": False, "reason": "unresolved_or_contract_violation", "response": item}); continue
        clause = cmap[q["clause_id"]]; props = parse_properties(clause["text"])
        # LLM may select a role, but it cannot change deterministic properties
        # or overwrite a property already resolved by deterministic rules.
        overlap_test = copy.deepcopy(spec["roles"].get(role, {})); merge_conflicts = _deep_merge(overlap_test, props)
        if merge_conflicts:
            remaining.append(q); audit.append({"accepted": False, "reason": "would_override_rule_result", "response": item}); continue
        _deep_merge(spec["roles"].setdefault(role, {}), props)
        spec["requirements"].append({"id": f"R{len(spec['requirements'])+1:05d}", "role": role, "properties": props,
            "evidence_ids": evidence, "clause_ids": [str(clause["id"])],
            "resolved_by": "llm", "confidence": .8, "source_text": clause["text"]})
        audit.append({"accepted": True, "question_id": qid, "role": role, "evidence_ids": evidence})
    remaining.extend(q for q in questions if q["id"] not in seen)
    spec["status"] = "needs_clarification" if remaining or has_rule_conflicts else "semantic_resolved"
    return remaining, audit


def validate_spec(spec: dict[str, Any], evidence_ids: set[str]) -> list[str]:
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "format-spec.schema.json"
    errors = load_and_validate(spec, schema_path)
    for key in ("schema_version", "source_document", "roles", "requirements", "status"):
        if key not in spec: errors.append(f"missing required key: {key}")
    if spec.get("schema_version") != "1.0": errors.append("schema_version must be 1.0")
    if spec.get("status") not in {"rule_resolved", "semantic_resolved", "needs_clarification", "unsupported"}: errors.append("invalid status")
    for req in spec.get("requirements", []):
        if not isinstance(req.get("properties"), dict) or not req.get("properties"):
            errors.append(f"{req.get('id')}: executable requirement properties must be non-empty")
        if req.get("resolved_by") not in {"rule", "llm", "user", "template"}: errors.append(f"{req.get('id')}: invalid resolved_by")
        if not set(req.get("evidence_ids", [])) <= evidence_ids: errors.append(f"{req.get('id')}: unknown evidence id")
        if not 0 <= req.get("confidence", -1) <= 1: errors.append(f"{req.get('id')}: invalid confidence")
    # Keep semantic clarification separate from hard response-contract
    # violations.  An external/manual clause may legitimately leave the spec
    # in needs_clarification without making this extraction artifact
    # structurally invalid, while malformed contract namespaces and invalid
    # evidence/index bindings must fail the current response transaction.
    hard_contract_reasons = {
        "unknown_requirement_role",
        "unknown_existing_requirement_id",
        "existing_requirement_payload_mismatch",
        "invalid_clause_ids",
        "invalid_evidence_ids",
    }
    for finding in spec.get("blocking_errors", []) if isinstance(spec.get("blocking_errors"), list) else []:
        if not isinstance(finding, dict) or finding.get("type") != "llm_contract":
            continue
        reason = finding.get("reason")
        reason_text = " ".join(str(item) for item in reason) if isinstance(reason, list) else str(reason or "")
        for marker in sorted(hard_contract_reasons):
            if marker in reason_text:
                errors.append(f"blocking semantic contract violation: {marker}")
                break
    registry = spec.get("resource_registry") if isinstance(spec, dict) else None
    registry_items = registry.get("items", {}) if isinstance(registry, dict) else {}
    if isinstance(registry_items, dict):
        for resource_id, resource in registry_items.items():
            if not isinstance(resource, dict):
                continue
            source_ids = resource.get("source_evidence_ids", [])
            if isinstance(source_ids, list) and not set(source_ids) <= evidence_ids:
                errors.append(f"$.resource_registry.items.{resource_id}.source_evidence_ids: unknown evidence id")
    return errors


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


GENERATED_REQUIREMENT_ARTIFACTS = {
    "document-evidence.json", "requirement-clauses.json", "format-spec.json",
    "questions.json", "conflicts.json", "schema-validation.json", "llm-request.json",
    "llm-request-chunks.json", "llm-response.raw.json", "llm-response-chunks.json",
    "host-agent-review-manifest.json", "llm-batch-manifest.json", "llm-merge-audit.json",
    "extraction-manifest.json",
    "evidence-context.json",
    "requirements-input-manifest.json",
    "template-evidence.json",
    "template-reconciliation.json",
    "TEMPLATE-RECONCILIATION.md",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_fresh_extraction(source: Path, out: Path, run_id: str | None = None) -> dict[str, Any]:
    """Invalidate prior products: this output directory is not a cache."""
    out.mkdir(parents=True, exist_ok=True)
    protected: list[str] = []
    for name in ("host-agent-run.json", "merge-receipt.json"):
        if (out / name).exists():
            protected.append(name)
    if (out / "host-agent-prompts").exists():
        protected.append("host-agent-prompts/")
    for pattern in ("llm-response-chunk-*.json", "llm-response-chunk-*.raw.json"):
        protected.extend(path.name for path in sorted(out.glob(pattern)) if path.is_file())
    if protected:
        raise ValueError(
            "refusing to rebuild a requirements directory containing immutable "
            "Host Agent review artifacts; choose a new stage directory: "
            + ", ".join(sorted(set(protected)))
        )
    removed: list[str] = []
    for name in sorted(GENERATED_REQUIREMENT_ARTIFACTS):
        path = out / name
        if path.exists():
            path.unlink()
            removed.append(name)
    return {
        "schema_version": "1.0",
        "run_id": run_id or str(uuid4()),
        "policy": "fresh_required_docx_extraction",
        "cache_reused": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_document": str(source.resolve()),
        "source_bytes": source.stat().st_size,
        "source_sha256": _sha256(source),
        "invalidated_prior_artifacts": removed,
    }


def normalize_role_line_spacing_units(spec: dict[str, Any]) -> None:
    """Complete line-based paragraph spacing after cross-clause role merges.

    A paragraph-spacing clause and a font-size clause may be merged into the
    same role at different times.  ``parse_properties`` cannot infer the line
    height from a font size that has not been seen yet, so perform that safe,
    deterministic completion once the whole role graph has been assembled.
    """
    for role_spec in spec.get("roles", {}).values():
        if not isinstance(role_spec, dict):
            continue
        paragraph = role_spec.get("paragraph")
        if not isinstance(paragraph, dict):
            continue
        if not ({"space_before_lines", "space_after_lines"} & set(paragraph)):
            continue
        if "spacing_line_height_pt" in paragraph:
            continue
        font = role_spec.get("font")
        size = font.get("size_pt") if isinstance(font, dict) else None
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
            paragraph["spacing_line_height_pt"] = float(size)


def analyse(args: argparse.Namespace) -> int:
    source = args.input.resolve(); out = args.out.resolve()
    extraction_manifest = prepare_fresh_extraction(source, out, args.run_id)
    normalization_manifest_path = out / "requirements-input-manifest.json"
    try:
        normalized = normalize_requirements_input(
            source,
            out / "requirements-input",
            normalization_manifest_path,
        )
    except RequirementsInputError as exc:
        normalization_manifest: dict[str, Any] | None = None
        if normalization_manifest_path.is_file():
            try:
                normalization_manifest = json.loads(
                    normalization_manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                normalization_manifest = None
        extraction_manifest.update({
            "status": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "failure_stage": "requirements_input_normalization",
            "error": str(exc),
            "requirements_input_manifest": str(normalization_manifest_path.resolve()),
            "requirements_input_normalization": normalization_manifest,
        })
        write_json(out / "extraction-manifest.json", extraction_manifest)
        print(f"requirements input normalization failed: {exc}", file=sys.stderr)
        return 2

    normalized_source = normalized.normalized_path
    original_record = normalized.manifest["original"]
    if (original_record.get("bytes") != extraction_manifest["source_bytes"] or
            original_record.get("sha256") != extraction_manifest["source_sha256"]):
        message = (
            "requirements input identity changed before normalization completed; "
            "retry with a stable .doc/.docx file"
        )
        extraction_manifest.update({
            "status": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "failure_stage": "requirements_input_provenance",
            "error": message,
            "requirements_input_manifest": str(normalized.manifest_path),
            "requirements_input_normalization": normalized.manifest,
        })
        write_json(out / "extraction-manifest.json", extraction_manifest)
        print(message, file=sys.stderr)
        return 2
    extraction_manifest.update({
        "requirements_input_manifest": str(normalized.manifest_path),
        "requirements_input_normalization": normalized.manifest,
        "normalized_source_document": str(normalized_source),
        "normalized_source_bytes": normalized.manifest["normalized"]["bytes"],
        "normalized_source_sha256": normalized.manifest["normalized"]["sha256"],
    })
    evidence = extract_document_evidence(normalized_source)
    # Keep the evidence/provenance identity stable across the prepare and final
    # stages.  The isolated DOCX path is an implementation artifact and changes
    # on every fresh invocation; the original user file remains the source.
    evidence["source_document"] = str(source)
    evidence["source_input_kind"] = normalized.manifest["original"]["kind"]
    evidence["normalized_input_kind"] = "docx"
    current_original_bytes = source.stat().st_size
    current_normalized_bytes = normalized_source.stat().st_size
    if (current_original_bytes != original_record["bytes"] or
            _sha256(source) != original_record["sha256"] or
            current_normalized_bytes != normalized.manifest["normalized"]["bytes"] or
            _sha256(normalized_source) != normalized.manifest["normalized"]["sha256"]):
        message = (
            "requirements input or normalized DOCX changed during evidence extraction; "
            "the run was rejected and must be retried"
        )
        extraction_manifest.update({
            "status": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "failure_stage": "requirements_input_provenance",
            "error": message,
        })
        write_json(out / "extraction-manifest.json", extraction_manifest)
        print(message, file=sys.stderr)
        return 2
    clauses = split_clauses(evidence)
    structure_source_record: dict[str, Any] | None = None
    if args.structure_docx:
        structure_source = args.structure_docx.resolve()
        structure_evidence = extract_document_evidence(structure_source)
        evidence["structure_source_document"] = str(structure_source)
        evidence["structure_evidence"] = structure_evidence.get("structure_evidence", {})
        evidence["structure_page_evidence"] = structure_evidence.get("page_evidence", {})
        structure_source_record = {
            "kind": "target_thesis_structure_docx", "path": str(structure_source),
            "bytes": structure_source.stat().st_size, "sha256": _sha256(structure_source),
        }
    official_template_evidence: dict[str, Any] | None = None
    if args.official_template_evidence_docx:
        official_path = args.official_template_evidence_docx.resolve()
        try:
            official_template_evidence = extract_template_evidence(official_path)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            failed_evidence = {
                "schema_version": "1.0", "status": "failed",
                "source": {"kind": "official_template_docx", "path": str(official_path)},
                "error": str(exc), "items": [],
            }
            write_json(out / "template-evidence.json", failed_evidence)
            extraction_manifest.update({
                "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(),
                "failure_stage": "official_template_evidence_extraction", "error": str(exc),
            })
            write_json(out / "extraction-manifest.json", extraction_manifest)
            print(f"official template evidence extraction failed: {exc}", file=sys.stderr)
            return 2
    rule_spec, rule_questions, rule_conflicts = build_rule_result(source, clauses)
    spec, questions, conflicts = rule_spec, rule_questions, rule_conflicts
    spec["analysis_mode"] = args.analysis_mode
    audit = []
    llm_request = None
    llm_batch = None
    supplied_response = None
    expected_provenance = None
    if args.llm_response:
        supplied_response = strict_json_loads(args.llm_response.read_text(encoding="utf-8"))
    supplied_contract_kind = response_contract_kind(supplied_response) if args.llm_response else "none"
    if args.llm_response and supplied_contract_kind == "unknown":
        raise ValueError(
            "supplied --llm-response was not consumed: expected a complete contract-2.1 "
            "clause review or a legacy resolutions response"
        )
    if args.analysis_mode == "llm_primary" and supplied_contract_kind == "legacy_question_resolutions":
        raise ValueError("llm_primary requires a complete contract-2.1 clause review")
    complete_review_supplied = supplied_contract_kind == "complete_clause_review"
    # Contract 2.1 is a complete, clause-by-clause semantic review.  Its
    # applicability must not depend on how the deterministic baseline was
    # produced: rule_only and known_template are baseline strategies, not
    # reasons to discard a complete review.  This also keeps full-compliance
    # behaviour identical for unseen and previously known university formats.
    if args.analysis_mode == "llm_primary" or complete_review_supplied:
        # Keep the complete deterministic baseline in ``rule_spec`` for the
        # post-merge cross-check, but expose only exact, evidence-eligible
        # existing requirements to the LLM request.
        llm_rule_spec = _narrow_rule_spec_for_chunk(rule_spec, clauses)
        llm_request = build_llm_request(rule_questions, clauses, evidence, llm_rule_spec, "full")
        llm_request["execution_policy"] = "fresh_run_no_cache"
        llm_request = attach_request_provenance(
            llm_request,
            source_sha256=extraction_manifest["source_sha256"],
            evidence_doc=evidence,
            clauses=clauses,
            run_id=extraction_manifest["run_id"],
            origin=HOST_AGENT_ORIGIN,
        )
        expected_provenance = llm_request["provenance"]
        # Persist the exact request before the host Agent is asked to interpret
        # it. The project owns no provider connection or API key.
        write_json(out / "llm-request.json", llm_request)
        response = supplied_response
        if response is None and args.llm_response:
            # A supplied legacy response is meaningful only on the legacy
            # question-resolution path below; llm_primary requires 2.1.
            response = supplied_response
        elif args.prepare_host_review:
            llm_batch = prepare_host_agent_review_packets(
                llm_request, clauses, evidence, extraction_manifest["source_sha256"], out,
                chunk_size=args.host_review_chunk_size,
            )
        if response is None:
            questions = [{"id": "Q0001", "question": "新模板需要 LLM 完整语义解析与完整性审查",
                          "candidate_roles": ["llm_primary_review"], "evidence_ids": [],
                          "source_text": "No LLM response was supplied."}]
            spec["status"] = "needs_clarification"
            spec["completeness"] = {"reviewed_by": "rule", "covered_clause_ids": [],
                                    "ignored_clause_ids": [], "unresolved_clause_ids": [c["id"] for c in clauses],
                                    "missing_clause_ids": [],
                                    "unsupported_items": []}
            spec["semantic_review_provenance"] = copy.deepcopy(expected_provenance)
            spec["semantic_review_provenance_valid"] = False
        else:
            write_json(out / "llm-response.raw.json", response)
            spec, conflicts, audit = merge_llm_primary(source, rule_spec, clauses, response,
                                                       {e["id"] for e in evidence["evidence"]},
                                                       expected_provenance=expected_provenance,
                                                       require_provenance=args.strict_provenance)
            questions = [{"id": f"Q{i+1:04d}", "question": "该条款证据不足，需要人工确认",
                          "clause_id": cid, "source_text": next((c["text"] for c in clauses if c["id"] == cid), ""),
                          "candidate_roles": ["unknown"],
                          "evidence_ids": next((c["evidence_ids"] for c in clauses if c["id"] == cid), [])}
                         for i, cid in enumerate(spec["completeness"]["unresolved_clause_ids"])]
    else:
        llm_request = build_llm_request(questions, clauses) if questions else None
        if questions and supplied_response is not None:
            response = supplied_response; write_json(out / "llm-response.raw.json", response)
            questions, audit = merge_llm(spec, questions, clauses, response, bool(conflicts))
    # Every analysis receives a new run binding.  This is deliberately done
    # after semantic merging so even a repeated school/template is rebuilt
    # from the current evidence and cannot inherit a previous resource id.
    spec["run_id"] = extraction_manifest["run_id"]
    reconciliation_sources = {
        "requirements_source": {
            "kind": normalized.manifest["original"]["kind"],
            "path": normalized.manifest["original"]["path"],
            "bytes": normalized.manifest["original"]["bytes"],
            "sha256": normalized.manifest["original"]["sha256"],
        },
        "requirements_normalized_source": copy.deepcopy(normalized.manifest["normalized"]),
        "official_template_source": (
            copy.deepcopy(official_template_evidence.get("source"))
            if official_template_evidence else None
        ),
        "structure_source": structure_source_record,
    }
    if official_template_evidence is not None and not args.prepare_host_review:
        def question_role_candidates(clause: dict[str, Any]) -> list[str]:
            role, candidates = identify_role(str(clause.get("text") or ""))
            if role is None and not candidates:
                inferred = infer_role_from_context(clause, str(clause.get("text") or ""))
                return [inferred] if inferred else []
            if role == "figure_table_title":
                return ["figure_caption", "table_caption"]
            return [role] if role else list(candidates)

        spec, questions, template_conflicts, reconciliation_report = reconcile_template(
            spec, clauses, questions, official_template_evidence,
            sources=reconciliation_sources,
            property_parser=lambda text: parse_properties(_non_page_text(text)),
            page_property_parser=parse_page_properties,
            role_candidates=question_role_candidates,
            fill_silent_values=True,
        )
        conflicts.extend(template_conflicts)
        if isinstance(spec.get("clause_compliance"), list):
            spec["compliance_summary"] = compliance_report(
                spec["clause_compliance"], "full", "analysis"
            )
    else:
        reconciliation_report = not_supplied_report(reconciliation_sources)
        if official_template_evidence is not None:
            reconciliation_report["status"] = "pending_semantic_review"
            reconciliation_report["summary"]["remaining_questions"] = len(questions)
    try:
        materialize_declaration_resources(
            spec, extraction_manifest["run_id"], evidence=evidence,
        )
    except ValueError as exc:
        conflict = {"type": "resource_registry", "reason": str(exc)}
        conflicts.append(conflict)
        spec.setdefault("blocking_errors", []).append(conflict)
        spec["status"] = "needs_clarification"
    normalize_role_line_spacing_units(spec)
    accepted_evidence_ids = {e["id"] for e in evidence["evidence"]}
    if official_template_evidence is not None:
        accepted_evidence_ids.update(
            str(item["id"]) for item in official_template_evidence.get("items", [])
            if isinstance(item, dict) and item.get("id")
        )
    errors = validate_spec(spec, accepted_evidence_ids)
    write_json(out / "document-evidence.json", evidence)
    write_json(out / "evidence-context.json", evidence_payload(evidence))
    write_json(out / "requirement-clauses.json", clauses)
    write_json(out / "format-spec.json", spec)
    write_json(out / "questions.json", questions)
    write_json(out / "conflicts.json", conflicts)
    write_json(out / "template-evidence.json", official_template_evidence or {
        "schema_version": "1.0", "status": "not_supplied", "source": None,
        "policy": {"sample_absence_is_prohibition": False}, "items": [],
        "summary": {"item_count": 0, "counts": {}, "page_field_locations": 0},
    })
    write_json(out / "template-reconciliation.json", reconciliation_report)
    (out / "TEMPLATE-RECONCILIATION.md").write_text(
        reconciliation_markdown(reconciliation_report), encoding="utf-8"
    )
    write_json(out / "schema-validation.json", {"valid": not errors, "errors": errors})
    if llm_request: write_json(out / "llm-request.json", llm_request)
    if audit: write_json(out / "llm-merge-audit.json", audit)
    extraction_manifest.update({
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "evidence_count": len(evidence.get("evidence", [])),
        "clause_count": len(clauses),
        "format_spec_sha256": _sha256(out / "format-spec.json"),
        "requirement_clauses_sha256": _sha256(out / "requirement-clauses.json"),
        "evidence_sha256": _sha256(out / "evidence-context.json"),
        # The old field is retained as a compatibility alias for the semantic
        # request body hash.  The explicit domains prevent a full-envelope
        # hash from being compared with the provenance body hash by accident.
        "llm_request_sha256": (
            request_body_sha256(llm_request)
            if llm_request else None
        ),
        "llm_request_body_sha256": (
            request_body_sha256(llm_request)
            if llm_request else None
        ),
        "llm_request_envelope_sha256": (
            request_envelope_sha256(llm_request)
            if llm_request else None
        ),
        "llm_request_file_sha256": (
            _sha256(out / "llm-request.json")
            if llm_request and (out / "llm-request.json").is_file() else None
        ),
        "semantic_review_provenance": expected_provenance,
        "llm_batch": llm_batch,
        "sources": reconciliation_sources,
        "template_evidence": {
            "path": str((out / "template-evidence.json").resolve()),
            "status": (official_template_evidence or {}).get("status", "not_supplied"),
            "sha256": _sha256(out / "template-evidence.json"),
        },
        "template_reconciliation": {
            "path": str((out / "template-reconciliation.json").resolve()),
            "status": reconciliation_report.get("status"),
            "summary": reconciliation_report.get("summary"),
            "sha256": _sha256(out / "template-reconciliation.json"),
        },
    })
    write_json(out / "extraction-manifest.json", extraction_manifest)
    print(json.dumps({"status": spec["status"], "requirements": len(spec["requirements"]),
                      "questions": len(questions), "conflicts": len(conflicts), "valid": not errors,
                      "output_dir": str(out)}, ensure_ascii=False))
    return 0 if args.prepare_host_review or not errors else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract structured thesis formatting requirements from .doc/.docx"
    )
    p.add_argument(
        "input", type=Path,
        help="school requirements Word file (.doc is normalized automatically; .docx passes through)",
    )
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--structure-docx", type=Path,
                   help="target/source thesis DOCX whose structure informs semantic review; never official-template evidence")
    p.add_argument("--official-template-evidence-docx", type=Path,
                   help="internal official DOCX evidence source, distinct from --structure-docx")
    p.add_argument("--llm-response", type=Path,
                   help="offline complete contract-2.1 response produced by the host Agent")
    p.add_argument("--prepare-host-review", action="store_true",
                   help="write evidence-bound packets for the current host Agent; never calls a provider")
    p.add_argument("--host-review-chunk-size", type=int, default=20,
                   help="number of clauses per host-Agent packet (default: 20)")
    p.add_argument("--strict-provenance", action="store_true",
                   help="require a fresh response bound to this extraction and exact LLM request")
    p.add_argument("--run-id",
                   help="explicit current semantic-review run id when resuming a fresh host response")
    p.add_argument("--analysis-mode", choices=["llm_primary", "rule_only", "known_template"], default="llm_primary",
                   help="fresh user inputs default to a complete LLM review; rule_only/known_template require explicit compatibility opt-in")
    args = p.parse_args(argv)
    if args.llm_response and args.prepare_host_review:
        p.error("choose exactly one semantic-review stage: --prepare-host-review or --llm-response")
    if args.structure_docx and not args.structure_docx.is_file():
        p.error(f"structure DOCX does not exist: {args.structure_docx}")
    if args.official_template_evidence_docx:
        if not args.official_template_evidence_docx.is_file():
            p.error(f"official template evidence DOCX does not exist: {args.official_template_evidence_docx}")
        if args.official_template_evidence_docx.suffix.lower() != ".docx":
            p.error("official template evidence must be a .docx file")
    if args.prepare_host_review:
        try:
            _validate_host_review_chunk_size(args.host_review_chunk_size)
        except ValueError as exc:
            p.error(str(exc))
        if args.analysis_mode != "llm_primary":
            p.error("--prepare-host-review requires --analysis-mode llm_primary")
    return args


if __name__ == "__main__":
    raise SystemExit(analyse(parse_args(sys.argv[1:])))
