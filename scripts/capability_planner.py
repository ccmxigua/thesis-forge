#!/usr/bin/env python3
"""Plan format-spec requirements against an explicit backend capability registry."""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path
from typing import Any

from format_spec_validation import load_and_validate
from pipeline_finding import evidence, finding
from role_registry import role_config, role_names

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "resources" / "backend-capabilities.default.json"
DISPOSITION_PRIORITY = {"unsupported": 3, "unknown": 2, "supported": 1}
EXECUTABLE_TEXT_ROLES = frozenset(role_names())
CLAUSE_INPUT_PREREQUISITES = frozenset({"requires_metadata", "requires_source_content"})
CLAUSE_RUNTIME_UNVERIFIABLE = frozenset({"unverifiable", "unresolved", "missing", "failed"})
CLAUSE_EXTERNAL_NOT_APPLICABLE = frozenset({"not_applicable", "external_compliance", "informational"})
ACTIONABLE_CATEGORIES = (
    "backend_capability_gap", "input_prerequisite", "runtime_manual_unverifiable")
CATEGORY_PRIORITY = {
    "backend_capability_gap": 3,
    "input_prerequisite": 2,
    "runtime_manual_unverifiable": 1,
    "input_prerequisite_satisfied": 0,
    "supported": 0,
    "external_not_applicable": 0,
}

# Labels that commonly occur as standalone cover/table fragments.  A clause
# with one of these labels is an input prerequisite only when the normalized
# metadata contract does not contain a value for the corresponding field.
# This is deliberately conservative: unknown labels remain blocked rather
# than being guessed from neighbouring cells.
METADATA_LABEL_FIELDS = {
    "论文题目": ("title_zh", "cn_title", "title"),
    "学位论文题目": ("title_zh", "cn_title", "title"),
    "论文题目（中文）": ("title_zh", "cn_title", "title"),
    "中文论文题目": ("title_zh", "cn_title", "title"),
    "英文题目": ("title_en", "en_title"),
    "副标题": ("subtitle", "cn_subtitle"),
    "作者": ("author", "author_name"),
    "作者姓名": ("author", "author_name"),
    "姓名": ("author", "author_name"),
    "研究生姓名": ("author", "author_name"),
    "论文作者": ("author", "author_name"),
    "学号": ("student_id",),
    "作者学号": ("student_id",),
    "学位申请人学号": ("student_id",),
    "指导教师": ("advisor", "supervisor_name"),
    "导师": ("advisor", "supervisor_name"),
    "导师姓名": ("advisor", "supervisor_name"),
    "合作导师": ("co_advisor",),
    "培养单位": ("school", "college", "college_name"),
    "所在学院": ("school", "college", "college_name"),
    "所在学院": ("school", "college", "college_name"),
    "所在学院": ("school", "college", "college_name"),
    "学院": ("school", "college", "college_name"),
    "专业": ("major", "discipline", "program_name"),
    "专业名称": ("major", "discipline", "program_name"),
    "专业类别": ("major", "discipline", "program_name"),
    "学科专业": ("major", "discipline", "program_name"),
    "一级学科": ("first_discipline",),
    "二级学科": ("second_discipline",),
    "研究方向": ("research_direction",),
    "研究方向（领域）": ("research_direction",),
    "分类号": ("classification_number", "class_no"),
    "中图分类号": ("classification_number", "class_no"),
    "UDC": ("udc",),
    "UDC分类号": ("udc",),
    "密级": ("confidentiality_level", "confidentiality", "security_level"),
    "提交日期": ("submit_date", "submit_date_cn", "completion_date"),
    "完成日期": ("submit_date", "submit_date_cn", "completion_date"),
    "答辩日期": ("defense_date",),
    "学位授予日期": ("degree_conferral_date", "degree_date"),
    "申请学位": ("degree_display", "degree_type"),
    "申请密级": ("confidentiality_level", "confidentiality", "security_level"),
    "保密期限": ("confidentiality_period",),
    "学位论文作者毕业后去向": ("author_post_graduation_destination",),
    "专业技术职称": ("defense_committee",),
    "学科门类": ("discipline_category",),
    "企业导师": ("co_supervisors.enterprise",),
    "培养单位代码*": ("unit_code",),
    "培养单位地址": ("unit_address",),
    "论文定稿时间填写": ("completion_date",),
    "填写论文定稿时间": ("completion_date",),
    "学院（部、研究院）": ("school", "college", "college_name"),
    "学科、专业": ("major", "discipline", "program_name"),
    "作者姓名*": ("author", "author_name"),
    "学号*": ("student_id",),
    "导师姓名*": ("advisor", "supervisor_name"),
    "密级*": ("confidentiality_level", "confidentiality", "security_level"),
    "中图分类号*": ("classification_number", "class_no"),
    "培养单位名称*": ("school", "college", "college_name"),
    "学科专业*": ("major", "discipline", "program_name"),
    "研究方向*": ("research_direction",),
}

# Composite cover fragments require every field group to be present.  Keep
# this allow-list exact: a substring match would incorrectly satisfy prose or
# an unrelated multi-label row.  Add composite layouts one at a time after
# verifying their template geometry and metadata contract.
METADATA_COMPOSITE_FIELDS = {
    "学号中文论文题目姓名": (
        ("student_id",),
        ("title_zh", "cn_title", "title"),
        ("author", "author_name"),
    ),
    "论文作者指导教师": (
        ("author", "author_name"),
        ("advisor", "supervisor_name"),
    ),
    "申请学位培养单位": (
        ("degree_display", "degree_type"),
        ("school", "college", "college_name"),
    ),
    "一级学科二级学科": (
        ("first_discipline",),
        ("second_discipline",),
    ),
}

# A small number of cover fields are extracted as a complete ``label: value``
# fragment rather than as an isolated label.  These are safe to satisfy from
# source metadata only when the label/value boundary is explicit.  Requiring
# a colon avoids treating labels such as ``培养单位地址`` as a training-unit
# value merely because they share a prefix.
METADATA_EMBEDDED_VALUE_FIELDS = {
    "所在学院": ("college_name", "college", "school"),
    "培养单位": ("institution_name", "school", "college", "college_name"),
}

# Semantic input bindings are deliberately narrower than arbitrary prose
# inference.  They establish only that a canonical source value exists for a
# recognizable obligation; output placement and value-specific constraints
# remain unverified until a clause-specific detector reports them.
SEMANTIC_CLAUSE_FIELDS = (
    (r"中文摘要|摘要.*(?:300|1000|目的意义|研究方法|研究成果|结论)", ("abstract_zh",)),
    (r"关键词.*(?:文献索引|全文主题|明确出处|3\s*[～~至-]\s*8|分号)", ("keywords_zh",)),
    (r"英文摘要|Abstract", ("abstract_en",)),
    (r"英文关键词|Key\s*words?", ("keywords_en",)),
)


def role_is_executable(role: str, capability_ids: list[str]) -> bool:
    """Return whether a registry match is backed by an application adapter.

    The backend capability registry describes property families, while the
    semantic role registry is the source of truth for text roles that the
    DOCX application stage can map, apply, and validate.  Requiring both keeps
    a broad capability role_pattern from claiming support for a role that has
    no executable style/structural adapter.
    """
    return ("text-role-formatting" not in capability_ids
            or (role in EXECUTABLE_TEXT_ROLES
                and role_config(role).get("backend_executable", True) is not False))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def leaf_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        return [path for key, child in value.items()
                for path in leaf_paths(child, f"{prefix}.{key}" if prefix else key)]
    if isinstance(value, list):
        return [prefix] if not value else [path for child in value for path in leaf_paths(child, prefix)]
    return [prefix]


def _inventory_has(inventory: dict[str, Any] | None, dotted: str) -> bool:
    current: Any = inventory
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return bool(current)


def _contract_input_has(key: str, source_inventory: dict[str, Any] | None,
                        template_profile: dict[str, Any] | None) -> bool:
    """Resolve a declarative prerequisite against the inputs supplied to the run."""
    if key.startswith("source_inventory."):
        return _inventory_has(source_inventory, key.removeprefix("source_inventory."))
    if key.startswith("template_profile."):
        return _inventory_has(template_profile, key.removeprefix("template_profile."))
    if key.startswith("thesis_profile."):
        return (_inventory_has(source_inventory, key)
                or _inventory_has(source_inventory, key.removeprefix("thesis_profile.")))
    if key.startswith("runtime."):
        return _inventory_has(source_inventory, key)
    return False


def _metadata_value(metadata: dict[str, Any] | None, key: str) -> Any:
    def present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return bool(str(value).strip())

    if not isinstance(metadata, dict):
        return None
    def resolve(container: dict[str, Any], dotted: str) -> Any:
        if dotted == "co_supervisors.enterprise":
            values = container.get("co_supervisors")
            if isinstance(values, list):
                matches = [item for item in values if isinstance(item, dict)
                           and item.get("kind") == "enterprise" and present(item.get("name"))]
                return matches or None
            return None
        current: Any = container
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    value = resolve(metadata, key)
    if present(value):
        return value
    # Accept the canonical nested profile used by the format-spec contract.
    for container_key in ("cover_metadata", "metadata"):
        container = metadata.get(container_key)
        if isinstance(container, dict):
            value = resolve(container, key)
            if present(value):
                return value
    return None


def _normalize_metadata_label(value: Any) -> str:
    """Normalize presentation-only differences in a standalone field label."""
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text)
    return text.strip("：:;；,，。")


def _compact_metadata_text(value: Any) -> str:
    """Normalize spacing while retaining field/value punctuation."""
    import unicodedata

    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).strip()


def _metadata_satisfies_clause(clause: dict[str, Any], metadata: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Return whether a metadata-only cover fragment has a supplied value.

    This does not infer values from prose.  It only turns a known standalone
    label into a satisfied prerequisite when an explicitly supplied
    canonical/legacy metadata field is non-empty.
    """
    if not isinstance(metadata, dict):
        return False, []
    text = _normalize_metadata_label(
        clause.get("text") or clause.get("source_text_full") or ""
    )
    for label, fields in sorted(METADATA_LABEL_FIELDS.items(), key=lambda item: len(item[0]), reverse=True):
        if text != _normalize_metadata_label(label):
            continue
        present = [field for field in fields if _metadata_value(metadata, field) is not None]
        return bool(present), present
    for label, field_groups in METADATA_COMPOSITE_FIELDS.items():
        if text != _normalize_metadata_label(label):
            continue
        present = []
        for fields in field_groups:
            field = next((candidate for candidate in fields
                          if _metadata_value(metadata, candidate) is not None), None)
            if field is None:
                return False, []
            present.append(field)
        return True, present

    # Embedded organization values are source-replaceable cover fields, not
    # template constants.  Only accept an explicit ``label: value`` fragment;
    # prefix matching would incorrectly consume ``培养单位地址``.
    compact = _compact_metadata_text(
        clause.get("text") or clause.get("source_text_full") or ""
    )
    for label, fields in METADATA_EMBEDDED_VALUE_FIELDS.items():
        match = re.fullmatch(rf"{re.escape(label)}[：:](.+)", compact)
        if not match:
            continue
        field = next((candidate for candidate in fields
                      if _metadata_value(metadata, candidate) is not None), None)
        return (field is not None), ([field] if field else [])

    # Confidentiality may be rendered either as one explicit value or as a
    # finite checkbox choice.  Never default to the template's first option:
    # the supplied metadata value must exactly equal the literal value or one
    # of the listed choices.
    confidentiality = next((field for field in (
        "confidentiality_level", "confidentiality", "security_level")
        if _metadata_value(metadata, field) is not None), None)
    match = re.fullmatch(r"密级[：:](.+)", compact)
    if match and confidentiality:
        supplied = _normalize_metadata_label(_metadata_value(metadata, confidentiality))
        rendered = match.group(1)
        if re.search(r"[□☐]", rendered):
            options = {
                _normalize_metadata_label(option)
                for option in re.findall(r"公开|内部\d+年|秘密|机密", rendered)
            }
            return supplied in options, ([confidentiality] if supplied in options else [])
        matches_literal = supplied == _normalize_metadata_label(rendered)
        return matches_literal, ([confidentiality] if matches_literal else [])
    return False, []


def _semantic_metadata_satisfies_clause(
        clause: dict[str, Any], metadata: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Bind narrowly recognized semantic obligations to canonical source values.

    This proves input availability only.  It does not prove placement, length,
    separators, semantic quality, or other output constraints named by the
    clause.
    """
    if not isinstance(metadata, dict):
        return False, []
    text = _compact_metadata_text(
        clause.get("text") or clause.get("source_text_full") or ""
    )
    for pattern, fields in SEMANTIC_CLAUSE_FIELDS:
        if not re.search(pattern, text, re.IGNORECASE):
            continue
        present = [field for field in fields if _metadata_value(metadata, field) is not None]
        return bool(present), present
    return False, []


def _template_fixed_satisfies_clause(
        clause: dict[str, Any], fixed_values: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Satisfy only an exact school/template fixed-value contract.

    These values belong to the selected official template, not to the source
    thesis metadata.  The contract is therefore passed separately and never
    inferred from school names, file paths, or arbitrary clause text.
    """
    if not isinstance(fixed_values, dict):
        return False, []
    school_code = fixed_values.get("school_code")
    if school_code is None or str(school_code).strip() == "":
        return False, []
    text = _compact_metadata_text(
        clause.get("text") or clause.get("source_text_full") or ""
    )
    match = re.fullmatch(
        r"(?:(中图分类号))?学校代码(?:[：:]?([0-9A-Za-z_-]+))?", text
    )
    if not match:
        return False, []
    embedded = match.group(2)
    expected = _compact_metadata_text(school_code)
    if embedded is not None and embedded != expected:
        return False, []
    return True, ["template_fixed.school_code"]


def classify_property(role: str, path: str, registry: dict[str, Any],
                      source_inventory: dict[str, Any] | None,
                      template_profile: dict[str, Any] | None) -> dict[str, Any]:
    matches = []
    for capability in registry["capabilities"]:
        if not re.fullmatch(capability["role_pattern"], role):
            continue
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in capability["property_patterns"]):
            continue
        disposition = capability["disposition"]
        missing_inputs = [name for name in capability.get("requires_source_inventory", [])
                          if not _inventory_has(source_inventory, name)]
        if capability.get("requires_template_profile") and template_profile is None:
            missing_inputs.append("template_profile")
        if missing_inputs and disposition == "supported":
            disposition = "unknown"
        matches.append({"capability_id": capability["id"], "disposition": disposition,
                        "missing_inputs": missing_inputs, "message": capability.get("message", "")})
    if not matches:
        return {"path": path, "disposition": "unknown", "capability_ids": [], "missing_inputs": []}
    disposition = max((item["disposition"] for item in matches), key=DISPOSITION_PRIORITY.get)
    capability_ids = sorted({item["capability_id"] for item in matches})
    unsupported_reason = None
    if disposition == "supported" and not role_is_executable(role, capability_ids):
        disposition = "unsupported"
        unsupported_reason = "semantic_role_not_executable"
    return {"path": path, "disposition": disposition,
            "capability_ids": capability_ids,
            "missing_inputs": sorted({name for item in matches for name in item["missing_inputs"]}),
            **({"reason": unsupported_reason} if unsupported_reason else {})}


def _requirement_finding(item: dict[str, Any], mode: str) -> dict[str, Any] | None:
    disposition = item["disposition"]
    if disposition == "supported":
        return None
    category = item["category"]
    blocking = mode == "full"
    # Keep the historical ``requirement_unknown``/``requirement_unsupported``
    # codes for genuine backend dispositions, but make missing inputs
    # explicit.  Previously every requirement finding was initialized as a
    # backend gap by the aggregate counter, which misreported R00677 even
    # though its content-instance property was supported.
    code_suffix = category if category == "input_prerequisite" else disposition
    return finding(
        f"capability.requirement_{code_suffix}", "capability_preflight",
        "error" if blocking else "warning", blocking,
        f"Requirement {item['requirement_id']} ({item['role']}) is not execution-ready: {category}.",
        [evidence("requirement_id", item["requirement_id"]), evidence("role", item["role"]),
         evidence("category", category),
         evidence("property_paths", [p["path"] for p in item["properties"] if p["disposition"] == disposition])],
    )


def _requirement_category(item: dict[str, Any]) -> str:
    """Classify a requirement independently from any clauses that cite it."""
    disposition = item["disposition"]
    if disposition == "supported":
        return "supported"
    if disposition == "unknown":
        if item.get("missing_declared_inputs"):
            return "input_prerequisite"
        decisive = [prop for prop in item["properties"] if prop["disposition"] == "unknown"]
        if decisive and all(prop.get("missing_inputs") for prop in decisive):
            return "input_prerequisite"
    return "backend_capability_gap"


def _category_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    return {name: sum(item["category"] == name for item in items)
            for name in (*ACTIONABLE_CATEGORIES, "external_not_applicable")}


def _unique_clause_counts(clauses: list[dict[str, Any]]) -> dict[str, int]:
    """Count each independently classified clause once.

    Requirement-derived projections remain in legacy findings for backwards
    compatibility, but are excluded here because the requirement is their sole
    capability decision.  A missing clause id cannot identify a shared clause,
    so each such record is retained as a distinct anonymous clause.
    """
    unique: dict[tuple[str, Any], dict[str, Any]] = {}
    for index, clause in enumerate(clauses):
        if clause.get("category_source") == "requirement":
            continue
        clause_id = clause.get("clause_id")
        key = ("id", clause_id) if clause_id is not None else ("record", index)
        previous = unique.get(key)
        if previous is None or CATEGORY_PRIORITY[clause["category"]] > CATEGORY_PRIORITY[previous["category"]]:
            unique[key] = clause
    counts = _category_counts(list(unique.values()))
    counts["gaps"] = sum(counts[name] for name in ACTIONABLE_CATEGORIES)
    counts["total"] = len(unique)
    return counts


def _clause_classification(record: dict[str, Any], rid_items: list[dict[str, Any]]) -> tuple[str, str]:
    """Return the legacy disposition and the explicit reporting category.

    Disposition remains intentionally coarse for existing consumers.  Category
    identifies why a clause is not ready, so missing inputs and manual/runtime
    verification are not misreported as backend capability gaps.
    """
    status = record.get("status")
    scope = record.get("scope")
    if scope in {"external_submission", "informational"} or status in CLAUSE_EXTERNAL_NOT_APPLICABLE:
        return "not_applicable", "external_not_applicable"
    if status in CLAUSE_INPUT_PREREQUISITES:
        return "unknown", "input_prerequisite"
    if status in CLAUSE_RUNTIME_UNVERIFIABLE:
        return ("unsupported" if status == "unverifiable" else "unknown"), "runtime_manual_unverifiable"
    if status == "unsupported_backend":
        return "unsupported", "backend_capability_gap"
    if rid_items:
        disposition = max((item["disposition"] for item in rid_items), key=DISPOSITION_PRIORITY.get)
        categories = [item.get("category", "backend_capability_gap") for item in rid_items]
        category = max(categories, key=lambda value: CATEGORY_PRIORITY.get(value, 0))
        if category == "supported":
            return "supported", "supported"
        if category == "input_prerequisite":
            return "unknown", category
        if category == "runtime_manual_unverifiable":
            return ("unsupported" if disposition == "unsupported" else "unknown"), category
        return disposition, "backend_capability_gap"
    if status in {"generated_and_verified", "verified_existing"}:
        return "supported", "supported"
    return "unknown", "runtime_manual_unverifiable"


def _clause_finding_code(clause: dict[str, Any]) -> str:
    status = clause["source_status"]
    category = clause["category"]
    if category == "input_prerequisite":
        return f"capability.clause_input_{status}"
    if category == "runtime_manual_unverifiable":
        suffix = status if status in CLAUSE_RUNTIME_UNVERIFIABLE else "unverifiable"
        return f"capability.clause_runtime_{suffix}"
    return f"capability.clause_backend_{clause['disposition']}"


def _clause_reason(record: dict[str, Any], category: str) -> str:
    if record.get("reason"):
        return str(record["reason"])
    status = record.get("status") or "unknown"
    defaults = {
        "input_prerequisite": f"input prerequisite is not satisfied ({status})",
        "runtime_manual_unverifiable": f"clause requires runtime or manual verification ({status})",
        "backend_capability_gap": f"backend capability disposition is not supported ({status})",
        "external_not_applicable": f"clause is external, informational, or not applicable ({status})",
        "supported": f"clause is supported ({status})",
    }
    return defaults[category]


def plan_capabilities(spec: dict[str, Any], registry: dict[str, Any], compliance_mode: str = "full",
                      source_inventory: dict[str, Any] | None = None,
                      template_profile: dict[str, Any] | None = None,
                      extracted_clauses: list[dict[str, Any]] | None = None,
                      metadata: dict[str, Any] | None = None,
                      template_fixed_values: dict[str, Any] | None = None) -> dict[str, Any]:
    requirements = []
    findings = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, requirement in enumerate(spec.get("requirements", []), 1):
        rid = requirement.get("id") or f"requirement-{index}"
        role = requirement.get("role") or ""
        properties = [classify_property(role, path, registry, source_inventory, template_profile)
                      for path in leaf_paths(requirement.get("properties", {}))]
        # A content-instance requirement intentionally has no singleton role
        # properties after normalization.  Its executable unit is the literal
        # instance plus the shared role style; do not classify an empty
        # property set as an unknown backend capability.
        if not properties and requirement.get("field_instance_ids"):
            properties = [{
                "path": "content_instance",
                "disposition": "supported",
                "capability_ids": ["text-role-formatting"],
                "missing_inputs": [],
            }]
        declared_prerequisites = requirement.get("input_prerequisites", [])
        missing_declared_inputs = [item.get("key") for item in declared_prerequisites
                                   if isinstance(item, dict) and item.get("required") is True
                                   and not _contract_input_has(str(item.get("key", "")), source_inventory,
                                                               template_profile)]
        dispositions = [item["disposition"] for item in properties] or ["unknown"]
        disposition = max(dispositions, key=DISPOSITION_PRIORITY.get)
        if disposition == "supported" and missing_declared_inputs:
            disposition = "unknown"
        item = {"requirement_id": rid, "role": role, "clause_ids": requirement.get("clause_ids", []),
                "disposition": disposition, "properties": properties, "findings": [],
                "missing_declared_inputs": missing_declared_inputs,
                "verification": requirement.get("verification"),
                "applicability": requirement.get("applicability")}
        item["category"] = _requirement_category(item)
        requirements.append(item); by_id[rid] = item
        issue = _requirement_finding(item, compliance_mode)
        if issue:
            item["findings"].append(issue)
            findings.append(issue)

    clauses = []
    # Keep the raw finding-category stream aligned with the requirement
    # findings already emitted above.  Initializing all of them as backend
    # gaps was the source of the R00677 misclassification.
    finding_categories: list[str] = [
        item["category"] for item in requirements if item["findings"]
    ]
    compliance_records = spec.get("clause_compliance", [])
    expected_clause_ids = {
        str(item.get("id")) for item in (extracted_clauses or [])
        if isinstance(item, dict) and item.get("id")
    }
    reviewed_clause_ids = {
        str(item.get("clause_id")) for item in compliance_records
        if isinstance(item, dict) and item.get("clause_id")
    }
    missing_clause_ids = sorted(expected_clause_ids - reviewed_clause_ids)
    if missing_clause_ids:
        blocking = compliance_mode == "full"
        issue = finding(
            "capability.missing_clause_compliance", "capability_preflight",
            "error" if blocking else "warning", blocking,
            f"{len(missing_clause_ids)} extracted clauses have no compliance record.",
            [evidence("expected_clause_count", len(expected_clause_ids)),
             evidence("reviewed_clause_count", len(reviewed_clause_ids & expected_clause_ids)),
             evidence("missing_clause_count", len(missing_clause_ids)),
             evidence("missing_clause_ids_sample", missing_clause_ids[:50])],
        )
        findings.append(issue)
        finding_categories.append("runtime_manual_unverifiable")
    for record in compliance_records:
        rid_items = [by_id[rid] for rid in record.get("requirement_ids", []) if rid in by_id]
        status = record.get("status")
        disposition, category = _clause_classification(record, rid_items)
        reason = _clause_reason(record, category)
        metadata_satisfied = False
        metadata_fields: list[str] = []
        template_fixed_satisfied = False
        template_fixed_fields: list[str] = []
        # A few template analyzers historically classified an isolated cover
        # label such as ``论文题目`` as requires_source_content rather than
        # requires_metadata.  It is still safe to satisfy that clause from
        # the explicit metadata contract, but only for an exact standalone
        # known label.  Compound labels and prose never enter this path.
        profile_fixed_values = ((template_profile or {}).get("fixed_values")
                                if isinstance(template_profile, dict) else None)
        effective_fixed_values = profile_fixed_values or template_fixed_values
        if category == "input_prerequisite" and status in {
                "requires_metadata", "requires_source_content"}:
            clause_source = next((item for item in (extracted_clauses or [])
                                  if str(item.get("id")) == str(record.get("clause_id"))), record)
            metadata_satisfied, metadata_fields = _metadata_satisfies_clause(clause_source, metadata)
            if not metadata_satisfied and status == "requires_source_content":
                metadata_satisfied, metadata_fields = _semantic_metadata_satisfies_clause(
                    clause_source, metadata)
            if metadata_satisfied:
                # Input existence is not output compliance.  Keep the legacy
                # Keep the legacy capability category for existing consumers;
                # the explicit input/output axes below carry the stricter
                # meaning that output placement remains unverified.
                disposition, category = "supported", "supported"
                reason = "模板字段已由输入元数据合同提供，但指定输出位置尚未逐条验证：" + ", ".join(metadata_fields)
        if category == "input_prerequisite" and status in {
                "requires_metadata", "requires_source_content"}:
            clause_source = next((item for item in (extracted_clauses or [])
                                  if str(item.get("id")) == str(record.get("clause_id"))), record)
            template_fixed_satisfied, template_fixed_fields = _template_fixed_satisfies_clause(
                clause_source, effective_fixed_values)
            if template_fixed_satisfied:
                disposition, category = "supported", "supported"
                source = "template profile" if profile_fixed_values else "template fixed-value contract"
                reason = f"所选{source}已提供，但指定输出位置尚未逐条验证：" + ", ".join(template_fixed_fields)
        explicit_clause_category = (
            record.get("scope") in {"external_submission", "informational"}
            or status in (CLAUSE_EXTERNAL_NOT_APPLICABLE | CLAUSE_INPUT_PREREQUISITES
                          | CLAUSE_RUNTIME_UNVERIFIABLE | {"unsupported_backend"})
            or not rid_items)
        if metadata_satisfied:
            explicit_clause_category = True
        if template_fixed_satisfied:
            explicit_clause_category = True
        clause = {"clause_id": record.get("clause_id"), "requirement_ids": record.get("requirement_ids", []),
                  "evidence_ids": record.get("evidence_ids", []), "scope": record.get("scope"),
                  "source_status": status, "reason": reason, "disposition": disposition,
                  "category": category,
                  "input_status": ("provided" if (metadata_satisfied or template_fixed_satisfied)
                                   else ("missing" if category == "input_prerequisite" else "not_applicable")),
                  "binding_status": ("bound" if (metadata_satisfied or template_fixed_satisfied)
                                     else ("unbound" if category == "input_prerequisite" else "not_applicable")),
                  "output_status": ("unverified" if (metadata_satisfied or template_fixed_satisfied)
                                    else ("pending_input" if category == "input_prerequisite" else "not_applicable")),
                  "category_source": "clause" if explicit_clause_category else "requirement",
                  "findings": [],
                  **({"metadata_fields": metadata_fields} if metadata_fields else {}),
                  **({"input_evidence": [{"kind": "semantic_metadata", "key": field}
                                         for field in metadata_fields]} if metadata_fields else {}),
                  **({"template_fixed_fields": template_fixed_fields}
                     if template_fixed_fields else {}),
                  **({"input_evidence": [{"kind": "template_fixed", "key": field}
                                         for field in template_fixed_fields]}
                     if template_fixed_fields else {})}
        clauses.append(clause)
        # A clause whose only decision comes from a cited requirement is a
        # projection, not a second independent blocker.  Keep the clause
        # classification for traceability, but let the requirement finding be
        # the single actionable diagnostic.  This removes the R00677/C00395
        # double count without hiding the clause relationship.
        if (category in {"backend_capability_gap", "input_prerequisite", "runtime_manual_unverifiable"}
                and clause["category_source"] != "requirement"):
            blocking = compliance_mode == "full"
            issue = finding(
                _clause_finding_code(clause), "capability_preflight",
                "error" if blocking else "warning", blocking,
                f"Clause {clause['clause_id']} is not execution-ready: {reason}.",
                [evidence("clause_id", clause["clause_id"]),
                 evidence("source_status", status), evidence("scope", clause["scope"]),
                 evidence("requirement_ids", clause["requirement_ids"]),
                 evidence("evidence_ids", clause["evidence_ids"]), evidence("reason", reason)],
            )
            clause["findings"].append(issue)
            findings.append(issue)
            finding_categories.append(category)

    blocking_findings = [item for item in findings if item["blocking"]]
    # ``finding_*`` counts retain the raw diagnostic stream for audit/debugging.
    # The public unprefixed counts are issue-level counts: requirement-derived
    # clause projections and duplicate clause records are counted once.
    finding_category_counts = {name: finding_categories.count(name) for name in ACTIONABLE_CATEGORIES}
    requirement_counts = _category_counts(requirements)
    requirement_counts["gaps"] = sum(requirement_counts[name] for name in ACTIONABLE_CATEGORIES)
    clause_counts = _unique_clause_counts(clauses)
    unique_category_counts = {
        name: requirement_counts[name] + clause_counts[name]
        for name in ACTIONABLE_CATEGORIES
    }
    unique_gaps = sum(unique_category_counts[name] for name in ACTIONABLE_CATEGORIES)
    # Missing compliance records are independent coverage blockers and are not
    # represented in ``requirements`` or ``clauses``.  Add them to the
    # issue-level aggregate without changing the clause counts.
    if missing_clause_ids:
        unique_category_counts["runtime_manual_unverifiable"] += 1
        unique_gaps += 1
    return {
        "schema_version": "1.0", "stage": "capability_preflight", "backend": registry["backend"],
        "compliance_mode": compliance_mode,
            "status": "blocked" if blocking_findings else ("gaps_present" if unique_gaps else "ready"),
        "execution_ready": not blocking_findings,
        "inputs": {"source_inventory_provided": source_inventory is not None,
                   "template_profile_provided": template_profile is not None},
        "summary": {"requirements": len(requirements), "clauses": len(clauses),
                    "extracted_clauses": len(expected_clause_ids),
                    "reviewed_extracted_clauses": len(reviewed_clause_ids & expected_clause_ids),
                    "missing_clause_records": len(missing_clause_ids),
                    "findings": len(findings), "blocking_findings": len(blocking_findings),
                    "gaps": unique_gaps, "unique_gaps": unique_gaps,
                    "finding_gaps": sum(finding_category_counts[name] for name in ACTIONABLE_CATEGORIES),
                    "backend_capability_gaps": unique_category_counts["backend_capability_gap"],
                    "input_prerequisites": unique_category_counts["input_prerequisite"],
                    "runtime_manual_unverifiable": unique_category_counts["runtime_manual_unverifiable"],
                    "external_not_applicable": clause_counts["external_not_applicable"],
                    "finding_backend_capability_gaps": finding_category_counts["backend_capability_gap"],
                    "finding_input_prerequisites": finding_category_counts["input_prerequisite"],
                    "finding_runtime_manual_unverifiable": finding_category_counts["runtime_manual_unverifiable"],
                    "requirement_gaps": requirement_counts["gaps"],
                    "requirement_backend_capability_gaps": requirement_counts["backend_capability_gap"],
                    "requirement_input_prerequisites": requirement_counts["input_prerequisite"],
                    "requirement_runtime_manual_unverifiable": requirement_counts["runtime_manual_unverifiable"],
                    "clause_gaps": clause_counts["gaps"],
                    "clause_backend_capability_gaps": clause_counts["backend_capability_gap"],
                    "clause_input_prerequisites": clause_counts["input_prerequisite"],
                    "clause_runtime_manual_unverifiable": clause_counts["runtime_manual_unverifiable"],
                    "clause_external_not_applicable": clause_counts["external_not_applicable"]},
        "requirements": requirements, "clauses": clauses, "findings": findings,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("format_spec", type=Path)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--source-inventory", type=Path)
    parser.add_argument("--template-profile", type=Path)
    parser.add_argument("--clauses", type=Path,
                        help="extracted requirement-clauses.json used to verify full review coverage")
    parser.add_argument("--metadata", type=Path,
                        help="normalized thesis metadata used to satisfy explicit metadata-only cover fields")
    parser.add_argument("--template-fixed-values", type=Path,
                        help="validated school/template fixed-value contract")
    parser.add_argument("--template-school",
                        help="exact school key selecting one entry from --template-fixed-values")
    parser.add_argument("--compliance-mode", choices=["full", "supported_subset"], default="full")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if bool(args.template_fixed_values) != bool(args.template_school):
        parser.error("--template-fixed-values and --template-school must be supplied together")
    if args.template_fixed_values:
        fixed_errors = load_and_validate(
            read_json(args.template_fixed_values), ROOT / "schema" / "template-fixed-values.schema.json")
        if fixed_errors:
            print("invalid template fixed-value contract:\n" + "\n".join(fixed_errors), file=sys.stderr)
            return 2
    registry = read_json(args.registry)
    errors = load_and_validate(registry, ROOT / "schema" / "backend-capability-registry.schema.json")
    if errors:
        print("invalid capability registry:\n" + "\n".join(errors), file=sys.stderr); return 2
    fixed_contract = read_json(args.template_fixed_values) if args.template_fixed_values else None
    selected_fixed_values = (
        fixed_contract.get("schools", {}).get(args.template_school, {})
        if fixed_contract and args.template_school else None
    )
    report = plan_capabilities(read_json(args.format_spec), registry, args.compliance_mode,
                               read_json(args.source_inventory) if args.source_inventory else None,
                               read_json(args.template_profile) if args.template_profile else None,
                               read_json(args.clauses) if args.clauses else None,
                               read_json(args.metadata) if args.metadata else None,
                               selected_fixed_values)
    write_json(args.out, report)
    print(json.dumps({"status": report["status"], "report": str(args.out)}, ensure_ascii=False))
    return 3 if not report["execution_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
