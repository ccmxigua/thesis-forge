"""Small, fail-closed compilers for exact source-level formatting facts.

These rules are intentionally narrower than natural-language understanding.
They return ``unknown`` whenever negation, quoting, exceptions, conditions, or
competing formulations make the target/scope unsafe to derive.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


_OPTIONAL_CAPTION = re.compile(
    r"(?P<phrase>(?:续表题|表标题|表题)(?:\s|[（(]){0,4}"
    r"(?<!不)(?<!非)(?<!未)(?:可以|可)省略(?:[）)])?)",
    re.IGNORECASE,
)
_REQUIRED_CAPTION = re.compile(
    r"(?P<phrase>(?:续表题|表标题|表题)(?:\s|[（(]){0,8}"
    r"(?:不得|不可|不可以|不允许|不能|不应)省略(?:[）)])?)",
    re.IGNORECASE,
)
_OPTIONAL_CAPTION_EN = re.compile(
    r"(?P<phrase>table[- ]caption(?:\s+on\s+continuation)?\s+"
    r"(?:may be omitted|can be omitted|is optional))",
    re.IGNORECASE,
)
_REQUIRED_CAPTION_EN = re.compile(
    r"(?P<phrase>table[- ]caption(?:\s+on\s+continuation)?\s+"
    r"(?:must not be omitted|is required|cannot be omitted))",
    re.IGNORECASE,
)
_CONTEXT_UNSAFE = re.compile(
    r"如果|若|除非|但|然而|除.*外|仅当|只有.{0,12}才|当且仅当|仅在|"
    r"例如|比如|如下(?:例|图|所示)|见(?:下|上)?例|示例|例[：:]|错误示例|反例|"
    r"\b(?:if|unless|except|however|but|example|counterexample|only\s+if)\b",
    re.IGNORECASE,
)
_COMPETING_CAPTION_POLICY = re.compile(
    r"(?:或|或者|以及|并且).{0,16}(?:必须|应当|需要|不得|不可|不能|不应).{0,8}(?:保留|省略)|"
    r"(?:must|should|shall|may|cannot|must not).{0,12}"
    r"(?:be retained|be omitted|remain|appear)",
    re.IGNORECASE,
)
_QUOTE_PAIRS = (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"))
SECURITY_MARKING_OPTIONS_OBLIGATION_ID = "cover.security_marking_options"
_SECURITY_MARKING_OPTION = re.compile(
    r"[□☐]\s*(?P<label>[^□☐\s,，;；()（）]{1,24})\s*[（(]\s*"
    r"(?:≤|不超过|至多|最多)\s*(?P<value>\d{1,4})\s*"
    r"(?P<unit>年|月|日)\s*[）)]"
)

# These are code-owned source facts, not IDs that the model must copy into its
# semantic decomposition.  The shared validator checks the corresponding
# requirement payload independently; the ledger records the compiled fact and
# its candidate property binding separately from model-authored obligations.
KNOWN_SOURCE_OBLIGATION_BINDINGS: dict[str, dict[str, Any]] = {
    "table.continuation.caption_suffix": {
        "roles": ["table"],
        "property_path": "properties.continuation.caption_suffix",
        "expected_value": "(续)",
        "required_checker_ids": ["docx.property_receipts", "docx.word_render"],
    },
    "table.continuation.repeat_header_row": {
        "roles": ["table"],
        "property_path": "properties.continuation.repeat_header_row",
        "expected_value": True,
        "required_checker_ids": ["docx.property_receipts", "docx.word_render"],
    },
    "table.continuation.caption_optional": {
        "roles": ["table"],
        "property_path": "properties.continuation.caption_required_on_continuation",
        "expected_value": False,
        "required_checker_ids": ["docx.property_receipts", "docx.word_render"],
    },
    "table.continuation.caption_required": {
        "roles": ["table"],
        "property_path": "properties.continuation.caption_required_on_continuation",
        "expected_value": True,
        "required_checker_ids": ["docx.property_receipts", "docx.word_render"],
    },
    "table_caption.position_above": {
        "roles": ["table_caption", "figure_table_title"],
        "property_path": "properties.position",
        "expected_value": "above",
        "required_checker_ids": ["docx.property_receipts"],
    },
    "table_caption.alignment_center": {
        "roles": ["table_caption", "figure_table_title"],
        "property_path": "properties.paragraph.alignment",
        "expected_value": "center",
        "required_checker_ids": ["docx.property_receipts"],
    },
    SECURITY_MARKING_OPTIONS_OBLIGATION_ID: {
        "roles": ["cover"],
        "property_path": "properties.non_public_administration.security_marking_options",
        "required_checker_ids": ["cover_non_public_administration"],
    },
}

KNOWN_CHECKER_POLICIES: dict[str, dict[str, str]] = {
    "docx.property_receipts": {
        "mode": "static_docx",
        "check": "核对生成 DOCX 的属性回执是否覆盖这项来源义务。",
    },
    "docx.word_render": {
        "mode": "word_render",
        "check": "通过 Microsoft Word 渲染结果核验这项来源义务。",
    },
    "cover_non_public_administration": {
        "mode": "external",
        "check": (
            "核对非公开行政区域的选项及期限与当前来源一致；此项不代表已生成或验证 DOCX 行政表。"
        ),
    },
}

_VERIFICATION_MODE_RANK = {
    "static_docx": 0,
    "pdf_render": 1,
    "word_render": 2,
    "manual": 3,
    "external": 4,
}

_KEYWORD_RANGE_ZH = re.compile(
    r"一般\s*(?P<minimum>\d+)\s*[～~至—-]\s*(?P<maximum>\d+)\s*(?:个|组)?"
)
_KEYWORD_RANGE_EN = re.compile(
    r"\b(?:generally|usually|typically)\s+(?P<minimum>\d+)\s*"
    r"(?:~|～|to|[-–—])\s*(?P<maximum>\d+)\b",
    re.IGNORECASE,
)
_EXPLICIT_KEYWORD_RANGE_ZH = re.compile(
    r"(?:最少|至少|不少于)\s*(?P<minimum>\d+)\s*(?:个|组)?.{0,16}?"
    r"(?:最多|至多|不超过)\s*(?P<maximum>\d+)\s*(?:个|组)?"
)
_EXPLICIT_KEYWORD_RANGE_EN = re.compile(
    r"(?:at\s+least|minimum\s+of)\s*(?P<minimum>\d+).{0,32}?"
    r"(?:at\s+most|no\s+more\s+than|maximum\s+of)\s*"
    r"(?P<maximum>\d+)",
    re.IGNORECASE,
)

_ABSTRACT_QUALITY_MAP = {
    "independent_and_complete": ("独立性和完整性",),
    "reflects_central_idea": ("准确反映论文的中心思想",),
    "academic_language": ("规范的学术用语",),
    "logical_structure": ("逻辑性强", "结构严谨"),
    "highlight_innovation": (
        "体现出论文的新理论、新方法、新技术",
        "突出本论文的创造性成果",
        "突出论文的创造性成果",
    ),
    "main_information_equivalent_to_thesis": (
        "与论文等同的主要信息",
        "与论文相同的主要信息",
    ),
}

_ABSTRACT_SECTION_MAP = {
    "purpose": ("目的意义", "研究目的", "目的"),
    "methods": ("研究方法", "方法"),
    "results": ("研究成果", "研究结果", "成果", "结果"),
    "conclusions": ("结论",),
    "innovation": ("创新性", "创造性成果"),
}

_ABSTRACT_SOURCE_MANUAL_REVIEW = re.compile(
    r"(?:the\s+chinese\s+abstract).{0,300}?(?:\d{2,4}\s*(?:to|[-–~～])\s*"
    r"\d{1,3}(?:,\d{3})?\s+words)|(?:\d{2,4}\s*(?:to|[-–~～])\s*"
    r"\d{1,3}(?:,\d{3})?\s+words)"
    r".{0,300}?(?:the\s+chinese\s+abstract)",
    re.IGNORECASE | re.DOTALL,
)


def _inside_quote(text: str, offset: int) -> bool:
    for opening, closing in _QUOTE_PAIRS:
        start = text.rfind(opening, 0, offset + 1)
        if start < 0:
            continue
        end = text.find(closing, start + len(opening))
        if end >= 0 and offset < end:
            return True
    return False


def compile_continuation_caption_requirement(source_text: Any) -> dict[str, Any]:
    """Compile an unambiguous caption optionality fact from cited source text.

    The result is evidence-bearing metadata, not a general language parser.
    Callers may mechanically project a value only for ``optional`` or
    ``required``; ``unknown`` must preserve the model/source state unchanged.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return {"state": "unknown", "rule_id": "continuation_caption_optionality_v1"}
    text = source_text.strip()
    optional = list(_OPTIONAL_CAPTION.finditer(text)) + list(_OPTIONAL_CAPTION_EN.finditer(text))
    required = list(_REQUIRED_CAPTION.finditer(text)) + list(_REQUIRED_CAPTION_EN.finditer(text))
    if (
        len(optional) + len(required) != 1
        or _COMPETING_CAPTION_POLICY.search(text)
    ):
        return {"state": "unknown", "rule_id": "continuation_caption_optionality_v1"}
    match = (optional or required)[0]
    if _inside_quote(text, match.start("phrase")) or _CONTEXT_UNSAFE.search(text):
        return {"state": "unknown", "rule_id": "continuation_caption_optionality_v1"}
    return {
        "state": "optional" if optional else "required",
        "rule_id": "continuation_caption_optionality_v1",
        "evidence_text": match.group("phrase"),
    }


def compile_known_source_obligation_ids(source_text: Any) -> list[str]:
    """Return stable IDs for narrowly recognized, machine-checkable source facts.

    This inventory is deliberately incomplete: it supplements rather than
    replaces semantic decomposition by the host agent. Unknown prose is never
    treated as proof that no other obligation exists.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return []
    # Do not promote propositions embedded in examples, quotations, or
    # conditional prose into code-owned facts.  This compiler is only a
    # conservative supplement; skipping a candidate is safer than treating
    # illustrative/conditional wording as a binding source requirement.
    if _CONTEXT_UNSAFE.search(source_text):
        return []
    text = re.sub(r"\s+", "", source_text)
    result: list[str] = []
    is_continuation_table = "续" in text and "表" in text
    if is_continuation_table and re.search(r"[（(]续[）)]|续表", text):
        result.append("table.continuation.caption_suffix")
    if is_continuation_table and "重复表头" in text:
        result.append("table.continuation.repeat_header_row")
    caption_rule = compile_continuation_caption_requirement(source_text)
    if is_continuation_table and caption_rule["state"] in {"optional", "required"}:
        result.append(f"table.continuation.caption_{caption_rule['state']}")
    if "表" in text and re.search(r"表上方|置于表上|表上.*居中|居中.*表上", text):
        result.append("table_caption.position_above")
    if "表" in text and "居中" in text:
        result.append("table_caption.alignment_center")
    if compile_security_marking_options(source_text) is not None:
        result.append(SECURITY_MARKING_OPTIONS_OBLIGATION_ID)
    return sorted(set(result))


def compile_known_source_obligations(source_text: Any) -> list[dict[str, Any]]:
    """Return source-derived facts with their deterministic payload bindings."""
    facts: list[dict[str, Any]] = []
    for obligation_id in compile_known_source_obligation_ids(source_text):
        binding = KNOWN_SOURCE_OBLIGATION_BINDINGS.get(obligation_id)
        if binding is None:
            continue
        fact = {"id": obligation_id, **copy.deepcopy(binding)}
        if obligation_id == SECURITY_MARKING_OPTIONS_OBLIGATION_ID:
            fact["expected_value"] = compile_security_marking_options(source_text)
        else:
            fact["expected_value"] = copy.deepcopy(binding.get("expected_value"))
        facts.append(fact)
    return facts


def compile_security_marking_options(source_text: Any) -> list[dict[str, Any]] | None:
    """Compile explicit checkbox choices and durations without guessing.

    Require a complete set of at least two choices, a distinct label for each,
    and an explicit numeric maximum plus unit for every checkbox. Conditional,
    example, and unmatched-checkbox text fails closed.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return None
    if _CONTEXT_UNSAFE.search(source_text):
        return None
    matches = list(_SECURITY_MARKING_OPTION.finditer(source_text))
    if len(matches) < 2 or len(re.findall(r"[□☐]", source_text)) != len(matches):
        return None
    labels = [re.sub(r"\s+", " ", match.group("label")).strip() for match in matches]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        return None
    return [
        {
            "label": label,
            "maximum_duration": {
                "value": int(match.group("value")),
                "unit": match.group("unit"),
            },
        }
        for label, match in zip(labels, matches)
    ]


def compile_soft_keyword_count_guidance(source_text: Any) -> dict[str, Any] | None:
    """Compile a clearly qualified keyword range without making it binding."""
    if not isinstance(source_text, str) or not source_text.strip() or _CONTEXT_UNSAFE.search(source_text):
        return None
    language_key = (
        "keywords_en"
        if re.search(r"\benglish\s+keywords?\b|\bkeywords?\b", source_text, re.I)
        else "keywords_zh" if "关键词" in source_text else None
    )
    if language_key is None:
        return None
    patterns = (
        _KEYWORD_RANGE_EN if language_key == "keywords_en" else _KEYWORD_RANGE_ZH,
    )
    matches = [match for pattern in patterns for match in pattern.finditer(source_text)]
    if len(matches) != 1:
        return None
    match = matches[0]
    minimum, maximum = int(match.group("minimum")), int(match.group("maximum"))
    if minimum < 1 or maximum < minimum:
        return None
    return {
        "language_key": language_key,
        "min_count": minimum,
        "max_count": maximum,
        "strength": "general_guidance",
        "source_quote": match.group(0),
        "rule_id": "soft_keyword_count_guidance_v1",
    }


def compile_explicit_keyword_count_range(source_text: Any) -> dict[str, int] | None:
    """Compile only explicitly mandatory keyword counts, excluding examples."""
    if not isinstance(source_text, str) or not source_text.strip() or _CONTEXT_UNSAFE.search(source_text):
        return None
    # A numeric range is not sufficient authorization for a keyword constraint:
    # the source clause itself must identify keywords as its subject.
    if not re.search(r"关键词|关键字|\bkey\s*words?\b", source_text, re.IGNORECASE):
        return None
    matches = [
        match
        for pattern in (_EXPLICIT_KEYWORD_RANGE_ZH, _EXPLICIT_KEYWORD_RANGE_EN)
        for match in pattern.finditer(source_text)
    ]
    if len(matches) != 1:
        return None
    minimum, maximum = int(matches[0].group("minimum")), int(matches[0].group("maximum"))
    if minimum < 1 or maximum < minimum:
        return None
    return {"min_count": minimum, "max_count": maximum}


def compile_abstract_source_constraints(
    source_text: Any, *, source_context: str = "",
) -> dict[str, Any] | None:
    """Compile a small, fully recognized Chinese-abstract rule bundle.

    This is intentionally a closed lexical compiler for source clauses whose
    complete obligation set has registered representations. It does not
    reinterpret the ambiguous English ``Chinese abstract``/words clause.
    Partial matches may be inspected by callers, but are never promoted to an
    executable review.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return None
    if _ABSTRACT_SOURCE_MANUAL_REVIEW.search(source_text):
        return None
    normalized = re.sub(r"\s+", "", source_text)
    has_abstract_subject = bool(re.search(r"中文摘要|Chinese\s+abstract", source_text, re.I))
    refers_to_preceding_abstract = (
        "其内容包括" in normalized
        and bool(re.search(r"中文摘要", source_context))
        and "论文内容的简要陈述" in re.sub(r"\s+", "", source_context)
    )
    if not has_abstract_subject and not refers_to_preceding_abstract:
        return None
    properties: dict[str, Any] = {}
    obligation_ids: list[str] = []

    third_person = re.search(r"一般.{0,12}第三人称|(?:usually|generally).{0,30}third.person", source_text, re.I)
    length_match = re.search(
        r"(?P<minimum>\d{2,4})\s*[～~至]\s*(?P<maximum>\d{2,4})\s*字"
        r"(?:[（(](?P<exception>[^）)]{1,80}(?:特殊需要|特殊情况)[^）)]{0,80})[）)])?",
        source_text,
    )
    if third_person:
        properties["third_person_guidance"] = "general_guidance"
        obligation_ids.append("abstract_zh.third_person_guidance")
    if length_match and (
        "一般" in source_text[max(0, length_match.start() - 12):length_match.start()]
        or length_match.group("exception")
    ):
        minimum, maximum = int(length_match.group("minimum")), int(length_match.group("maximum"))
        if minimum < 1 or maximum < minimum:
            return None
        properties["length_guidance"] = {
            "min_chars": minimum,
            "max_chars": maximum,
            "length_metric": "cjk_characters",
            "strength": "general_guidance",
            "exception_text": length_match.group("exception") or "",
        }
        obligation_ids.append("abstract_zh.length_guidance")
    if "不加评论和解释" in normalized:
        properties["prohibit_commentary"] = True
        obligation_ids.append("abstract_zh.prohibit_commentary")

    quality_targets = [
        key for key, phrases in _ABSTRACT_QUALITY_MAP.items()
        if any(phrase in normalized for phrase in phrases)
    ]
    if quality_targets:
        properties["quality_guidance"] = quality_targets
        obligation_ids.extend(f"abstract_zh.quality_guidance:{key}" for key in quality_targets)

    has_section_list = "内容包括" in normalized
    if has_section_list:
        sections = [
            section for section, phrases in _ABSTRACT_SECTION_MAP.items()
            if any(phrase in normalized for phrase in phrases)
        ]
        if not {"purpose", "methods", "results", "conclusions"}.issubset(sections):
            return None
        properties["required_sections"] = sections
        obligation_ids.extend(f"abstract_zh.required_sections:{section}" for section in sections)

    prohibited: list[str] = []
    if re.search(r"不可出现.{0,12}图", normalized):
        prohibited.append("figures")
    if re.search(r"不可出现.{0,20}表", normalized):
        prohibited.append("tables")
    if "化学方程式" in normalized and re.search(r"不可出现.{0,40}化学方程式", normalized):
        prohibited.append("chemical_equations")
    if "非公知公用的符号和术语" in normalized and re.search(
        r"不可出现.{0,60}非公知公用的符号和术语", normalized,
    ):
        prohibited.append("nonpublic_symbols_and_terminology")
    if prohibited:
        properties["prohibited_objects"] = prohibited
        obligation_ids.extend(f"abstract_zh.prohibited_objects:{item}" for item in prohibited)

    # Promote only the two complete, registered source bundles. This prevents
    # a partial phrase match from changing an unresolved clause into executable.
    c66_complete = (
        "中文摘要" in normalized
        and "论文内容的简要陈述" in normalized
        and third_person is not None
        and length_match is not None
        and length_match.group("exception") is not None
        and "不加评论和解释" in normalized
        and all(any(phrase in normalized for phrase in phrases)
                for key, phrases in _ABSTRACT_QUALITY_MAP.items()
                if key != "main_information_equivalent_to_thesis")
    )
    c67_complete = (
        has_section_list
        and refers_to_preceding_abstract
        and {"purpose", "methods", "results", "conclusions"}.issubset(
            set(properties.get("required_sections") or [])
        )
        and "应与论文等同的主要信息" in normalized
        and "要突出本论文的创造性成果" in normalized
        and {"figures", "tables", "chemical_equations", "nonpublic_symbols_and_terminology"}.issubset(
            set(prohibited)
        )
    )
    if not (c66_complete or c67_complete):
        return None
    return {
        "properties": properties,
        "obligation_ids": sorted(set(obligation_ids)),
        "bundle": "c66_chinese_abstract_guidance" if c66_complete else "c67_chinese_abstract_contents",
        "rule_id": "complete_abstract_source_constraints_v1",
    }


def compile_unresolved_manual_review_codes(source_text: Any) -> list[str]:
    """Return narrowly recognized source ambiguities safe for draft-only review."""
    if isinstance(source_text, str) and _ABSTRACT_SOURCE_MANUAL_REVIEW.search(source_text):
        return ["abstract_target_metric_ambiguity"]
    return []


def _explicit_abstract_hard_support(source_text: Any, property_name: str) -> bool:
    if not isinstance(source_text, str) or not source_text.strip():
        return False
    text = re.sub(r"\s+", "", source_text)
    if property_name == "require_third_person":
        return bool(re.search(r"(?:必须|应当|应该|应以|须以).{0,16}第三人称|"
                              r"(?:must|shall|should).{0,40}third.person", source_text, re.I))
    if property_name in {"min_chars", "max_chars"}:
        has_range = bool(re.search(r"\d{2,4}\s*[～~至]\s*\d{2,4}\s*字", source_text))
        has_mandatory = bool(re.search(r"必须|不得少于|至少|不少于|应为|不得超过|至多|最多", text))
        return has_range and has_mandatory
    return False


def materialize_soft_keyword_count_guidance(
    response: Any, clauses: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Preserve soft keyword ranges and retain only independently sourced hard limits."""
    if not isinstance(response, dict) or not isinstance(clauses, list):
        return copy.deepcopy(response), []
    projected = copy.deepcopy(response)
    requirements = projected.get("requirements")
    if not isinstance(requirements, list):
        return projected, []
    clauses_by_id = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    audit: list[dict[str, Any]] = []
    for clause_id, clause in sorted(clauses_by_id.items()):
        source_text = clause.get("text") or clause.get("source_text_full")
        guidance = compile_soft_keyword_count_guidance(source_text)
        if guidance is None:
            continue
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, dict) or clause_id not in (requirement.get("clause_ids") or []):
                continue
            if requirement.get("role") != "content_constraints":
                continue
            properties = requirement.get("properties")
            if not isinstance(properties, dict):
                continue
            keyword_rule = properties.get(guidance["language_key"])
            if not isinstance(keyword_rule, dict):
                continue
            before = copy.deepcopy(keyword_rule)
            verification_before = copy.deepcopy(requirement.get("verification"))
            desired = {
                "min_count": guidance["min_count"],
                "max_count": guidance["max_count"],
                "strength": "general_guidance",
            }
            current_guidance = keyword_rule.get("count_guidance")
            if current_guidance is not None and current_guidance != desired:
                continue
            keyword_rule["count_guidance"] = desired
            linked_clause_ids = [
                str(value) for value in requirement.get("clause_ids", [])
                if isinstance(value, str)
            ]
            explicit_ranges = [
                compile_explicit_keyword_count_range(
                    clauses_by_id.get(other_id, {}).get("text")
                    or clauses_by_id.get(other_id, {}).get("source_text_full")
                )
                for other_id in linked_clause_ids
                if other_id != clause_id
            ]
            independently_mandatory = any(value is not None for value in explicit_ranges)
            removed_hard_bounds: list[str] = []
            if not independently_mandatory:
                for bound in ("min_count", "max_count"):
                    if bound in keyword_rule:
                        keyword_rule.pop(bound)
                        removed_hard_bounds.append(bound)
                verification = requirement.get("verification")
                if isinstance(verification, dict) and isinstance(verification.get("checks"), list):
                    verification["checks"] = [
                        check for check in verification["checks"]
                        if not (
                            isinstance(check, str)
                            and re.search(r"keyword\s+count|关键词.{0,12}(?:数量|个数)", check, re.I)
                            and re.search(
                                rf"\b{guidance['min_count']}\b.{{0,16}}\b{guidance['max_count']}\b",
                                check,
                            )
                        )
                    ]
            if (
                keyword_rule != before
                or requirement.get("verification") != verification_before
            ):
                before_bytes = json.dumps({
                    "keyword_rule": before,
                    "verification": verification_before,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                after_bytes = json.dumps({
                    "keyword_rule": keyword_rule,
                    "verification": requirement.get("verification"),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                audit.append({
                    "requirement_index": index,
                    "language_key": guidance["language_key"],
                    "source_clause_ids": [clause_id],
                    "source_evidence_ids": sorted({
                        str(value) for value in (clause.get("evidence_ids") or []) if value
                    }),
                    "source_quote": guidance["source_quote"],
                    "independently_mandatory_range_present": independently_mandatory,
                    "removed_hard_bounds": removed_hard_bounds,
                    "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
                    "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
                    "authorization": "source_qualified_keyword_range_projection_v1",
                    "rule_id": guidance["rule_id"],
                })
    return projected, audit


def materialize_complete_abstract_source_constraints(
    response: Any, clauses: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Materialize only complete, exact-source abstract bundles into response requirements."""
    if not isinstance(response, dict) or not isinstance(clauses, list):
        return copy.deepcopy(response), []
    projected = copy.deepcopy(response)
    requirements = projected.get("requirements")
    reviews = projected.get("clause_reviews")
    if not isinstance(requirements, list) or not isinstance(reviews, list):
        return projected, []
    clauses_by_id = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    reviews_by_id = {
        str(item.get("clause_id")): item for item in reviews
        if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
    }
    audit: list[dict[str, Any]] = []
    for clause_id, clause in sorted(clauses_by_id.items()):
        source_text = clause.get("text") or clause.get("source_text_full")
        source_context_items = []
        clause_evidence_ids = set(clause.get("evidence_ids") or [])
        location = clause.get("location") if isinstance(clause.get("location"), dict) else {}
        location_part = location.get("part")
        location_child_index = location.get("child_index")
        location_order = location.get("order")
        has_stable_location = (
            isinstance(location_part, str) and bool(location_part.strip())
            and isinstance(location_child_index, int) and not isinstance(location_child_index, bool)
            and location_child_index >= 0
            and isinstance(location_order, int) and not isinstance(location_order, bool)
            and location_order >= 0
        )
        for other_id, other_clause in clauses_by_id.items():
            if (
                other_id == clause_id
                or not has_stable_location
                or not clause_evidence_ids.intersection(other_clause.get("evidence_ids") or [])
            ):
                continue
            other_location = other_clause.get("location") if isinstance(other_clause.get("location"), dict) else {}
            if (
                other_location.get("part") == location_part
                and other_location.get("child_index") == location_child_index
                and other_location.get("order") == location_order
            ):
                other_text = other_clause.get("text") or other_clause.get("source_text_full")
                if isinstance(other_text, str):
                    source_context_items.append(other_text)
        compiled = compile_abstract_source_constraints(
            source_text, source_context="\n".join(source_context_items),
        )
        review = reviews_by_id.get(clause_id)
        evidence_ids = sorted({
            str(value) for value in (clause.get("evidence_ids") or [])
            if isinstance(value, str) and value
        })
        if compiled is None or not evidence_ids or not isinstance(review, dict):
            continue
        if review.get("classification") not in {"unresolved", "executable", "covered", "verify_existing"}:
            continue
        linked = [
            (index, item) for index, item in enumerate(requirements)
            if isinstance(item, dict)
            and clause_id in (item.get("clause_ids") or [])
            and item.get("role") == "content_constraints"
        ]
        abstract_linked = [
            (index, item) for index, item in linked
            if isinstance(item.get("properties"), dict)
            and isinstance(item["properties"].get("abstract_zh"), dict)
        ]
        if len(abstract_linked) > 1:
            continue
        if not abstract_linked and linked:
            # Do not merge a complete abstract source bundle into a different
            # content rule merely because the roles happen to match.
            continue
        if abstract_linked:
            requirement_index, requirement = abstract_linked[0]
            requirement_before = copy.deepcopy(requirement)
            properties = requirement["properties"]["abstract_zh"]
        else:
            requirement_index = len(requirements)
            requirement = {
                "role": "content_constraints",
                "properties": {"abstract_zh": {}},
                "clause_ids": [clause_id],
                "evidence_ids": evidence_ids,
                "confidence": 1.0,
                "reason": "Code materialized the complete, registered abstract obligations from the exact cited source clause.",
            }
            requirements.append(requirement)
            requirement_before = None
            properties = requirement["properties"]["abstract_zh"]
        conflict = any(
            key in properties and properties[key] != value
            for key, value in compiled["properties"].items()
            if key not in {"quality_guidance", "prohibited_objects"}
        )
        if conflict:
            if requirement_before is None:
                requirements.pop()
            continue
        before_properties = copy.deepcopy(properties)
        for key, value in compiled["properties"].items():
            if key in {"quality_guidance", "prohibited_objects"}:
                merged = list(dict.fromkeys([*(properties.get(key) or []), *value]))
                properties[key] = merged
            else:
                properties[key] = copy.deepcopy(value)
        linked_clause_ids = [
            str(value) for value in requirement.get("clause_ids", [])
            if isinstance(value, str)
        ]
        has_hard_source = {
            name: any(
                _explicit_abstract_hard_support(
                    clauses_by_id.get(other_id, {}).get("text")
                    or clauses_by_id.get(other_id, {}).get("source_text_full"),
                    name,
                )
                for other_id in linked_clause_ids
            )
            for name in ("require_third_person", "min_chars", "max_chars")
        }
        if properties.get("third_person_guidance") and not has_hard_source["require_third_person"]:
            properties.pop("require_third_person", None)
        length_guidance = properties.get("length_guidance")
        if isinstance(length_guidance, dict):
            for field in ("min_chars", "max_chars"):
                if (
                    not has_hard_source[field]
                    and properties.get(field) == length_guidance.get(field)
                ):
                    properties.pop(field, None)
            verification = requirement.get("verification")
            if isinstance(verification, dict) and isinstance(verification.get("checks"), list):
                minimum = length_guidance.get("min_chars")
                maximum = length_guidance.get("max_chars")
                verification["checks"] = [
                    check for check in verification["checks"]
                    if not (
                        isinstance(check, str)
                        and re.search(r"abstract|摘要", check, re.I)
                        and re.search(r"length|count|字数|字符", check, re.I)
                        and re.search(rf"\b{minimum}\b.{{0,20}}\b{maximum}\b", check)
                    )
                ]
        clause_ids = requirement.get("clause_ids")
        if not isinstance(clause_ids, list):
            clause_ids = []
            requirement["clause_ids"] = clause_ids
        if clause_id not in clause_ids:
            clause_ids.append(clause_id)
        current_evidence = requirement.get("evidence_ids")
        if not isinstance(current_evidence, list):
            current_evidence = []
            requirement["evidence_ids"] = current_evidence
        for evidence_id in evidence_ids:
            if evidence_id not in current_evidence:
                current_evidence.append(evidence_id)
        review_before = copy.deepcopy(review)
        review["classification"] = "executable"
        review["reason"] = (
            "Every obligation in this recognized source bundle is represented by a deterministic, "
            "source-bound content constraint; soft qualifiers remain guidance, not hard limits."
        )
        review["normative_basis"] = "explicit_normative_text"
        review["obligations"] = [
            {"id": identifier, "status": "covered", "reason": "Represented by the registered source-bound content property."}
            for identifier in compiled["obligation_ids"]
        ]
        if projected.get("contract_version") == "2.1":
            review["requirement_indexes"] = [
                index for index, item in enumerate(requirements)
                if isinstance(item, dict) and clause_id in (item.get("clause_ids") or [])
            ]
        before_bytes = json.dumps(
            {"requirement": requirement_before, "review": review_before},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        after_bytes = json.dumps(
            {"requirement": requirement, "review": review},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        before_sha256 = hashlib.sha256(before_bytes).hexdigest()
        after_sha256 = hashlib.sha256(after_bytes).hexdigest()
        if before_sha256 == after_sha256:
            continue
        audit.append({
            "clause_id": clause_id,
            "source_evidence_ids": evidence_ids,
            "source_quote_sha256": hashlib.sha256(
                str(clause.get("text") or clause.get("source_text_full") or "").encode("utf-8")
            ).hexdigest(),
            "requirement_index": requirement_index,
            "bundle": compiled["bundle"],
            "obligation_ids": compiled["obligation_ids"],
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "authorization": "complete_registered_source_bundle_projection_v1",
            "rule_id": compiled["rule_id"],
        })
    return projected, audit


def materialize_known_source_verification(
    response: Any, clauses: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Project exact source facts and required checker bindings.

    The host may propose verification metadata, but it cannot omit a checker
    that the deterministic source-obligation registry requires.  Only a
    role/property value that exactly matches a compiled source fact receives
    the binding. A missing registered security-marking choice list is
    materialized only on one uniquely linked cover administration requirement;
    conflicting or ambiguous payloads are never overwritten and fail the
    normal validator. The input is never mutated.
    """
    if not isinstance(response, dict) or not isinstance(clauses, list):
        return copy.deepcopy(response), []
    projected = copy.deepcopy(response)
    requirements = projected.get("requirements")
    if not isinstance(requirements, list):
        return projected, []
    property_projections: list[dict[str, Any]] = []

    def read_property(value: Any, path: str) -> Any:
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    reviews = projected.get("clause_reviews")
    review_by_id = {
        str(item.get("clause_id")): item
        for item in reviews if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
    } if isinstance(reviews, list) else {}

    for clause in clauses:
        if not isinstance(clause, dict) or not isinstance(clause.get("id"), str):
            continue
        clause_id = clause["id"]
        source_text = clause.get("text") or clause.get("source_text_full")
        choices = compile_security_marking_options(source_text)
        review = review_by_id.get(clause_id, {})
        if (
            choices is None
            or review.get("classification") not in {"covered", "executable", "verify_existing"}
        ):
            continue
        candidates = [
            (index, requirement) for index, requirement in enumerate(requirements)
            if isinstance(requirement, dict)
            and requirement.get("role") == "cover"
            and clause_id in (requirement.get("clause_ids") or [])
        ]
        if len(candidates) != 1:
            continue
        requirement_index, requirement = candidates[0]
        properties = requirement.get("properties")
        administration = (
            properties.get("non_public_administration")
            if isinstance(properties, dict) else None
        )
        if not isinstance(administration, dict):
            continue
        current = administration.get("security_marking_options")
        if current is not None:
            # Exact source values are accepted; conflicting model values are
            # left intact so the source-fact validator rejects them.
            continue
        before_sha256 = hashlib.sha256(json.dumps(
            current, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        administration["security_marking_options"] = copy.deepcopy(choices)
        after_sha256 = hashlib.sha256(json.dumps(
            choices, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        property_projections.append({
            "requirement_index": requirement_index,
            "source_clause_ids": [clause_id],
            "source_evidence_ids": [
                value for value in clause.get("evidence_ids", [])
                if isinstance(value, str) and value
            ],
            "source_obligation_ids": [SECURITY_MARKING_OPTIONS_OBLIGATION_ID],
            "property_path": "properties.non_public_administration.security_marking_options",
            "before_property_sha256": before_sha256,
            "after_property_sha256": after_sha256,
            "authorization": "exact_source_checkbox_duration_projection_v1",
            "rule_id": "compile_security_marking_options_v1",
        })

    bindings: dict[int, dict[str, Any]] = {}
    for clause in clauses:
        if not isinstance(clause, dict) or not isinstance(clause.get("id"), str):
            continue
        clause_id = clause["id"]
        source_text = clause.get("text") or clause.get("source_text_full")
        for fact in compile_known_source_obligations(source_text):
            for index, requirement in enumerate(requirements):
                if not isinstance(requirement, dict):
                    continue
                clause_ids = requirement.get("clause_ids")
                if not isinstance(clause_ids, list) or clause_id not in clause_ids:
                    continue
                if requirement.get("role") not in fact["roles"]:
                    continue
                property_path = str(fact["property_path"]).removeprefix("properties.")
                actual = read_property(requirement.get("properties"), property_path)
                expected = fact["expected_value"]
                value_matches = (
                    isinstance(expected, bool)
                    and isinstance(actual, bool)
                    and actual is expected
                ) or (not isinstance(expected, bool) and actual == expected)
                if not value_matches:
                    continue
                entry = bindings.setdefault(index, {
                    "source_clause_ids": set(),
                    "source_evidence_ids": set(),
                    "source_obligation_ids": set(),
                    "required_checker_ids": set(),
                })
                entry["source_clause_ids"].add(clause_id)
                evidence_ids = clause.get("evidence_ids")
                if isinstance(evidence_ids, list):
                    entry["source_evidence_ids"].update(
                        value for value in evidence_ids
                        if isinstance(value, str) and value
                    )
                entry["source_obligation_ids"].add(str(fact["id"]))
                entry["required_checker_ids"].update(
                    checker_id for checker_id in fact["required_checker_ids"]
                    if checker_id in KNOWN_CHECKER_POLICIES
                )

    audit: list[dict[str, Any]] = list(property_projections)
    for index, binding in sorted(bindings.items()):
        requirement = requirements[index]
        checker_ids = sorted(binding["required_checker_ids"])
        policies = [KNOWN_CHECKER_POLICIES[item] for item in checker_ids]
        desired_mode = max(
            (item["mode"] for item in policies),
            key=lambda mode: _VERIFICATION_MODE_RANK[mode],
            default="static_docx",
        )
        original_verification = copy.deepcopy(requirement.get("verification"))
        verification = requirement.get("verification")
        if verification is None:
            verification = {"mode": desired_mode, "checks": []}
            requirement["verification"] = verification
        if not isinstance(verification, dict):
            continue
        current_ids = verification.get("checker_ids")
        if current_ids is None:
            current_ids = []
            verification["checker_ids"] = current_ids
        if not isinstance(current_ids, list):
            continue
        for checker_id in checker_ids:
            if checker_id not in current_ids:
                current_ids.append(checker_id)
        current_checks = verification.get("checks")
        if current_checks is None:
            current_checks = []
            verification["checks"] = current_checks
        if not isinstance(current_checks, list):
            continue
        for policy in policies:
            if policy["check"] not in current_checks:
                current_checks.append(policy["check"])
        current_mode = verification.get("mode")
        if current_mode is None:
            verification["mode"] = desired_mode
        elif current_mode in _VERIFICATION_MODE_RANK and (
            _VERIFICATION_MODE_RANK[current_mode] < _VERIFICATION_MODE_RANK[desired_mode]
        ):
            verification["mode"] = desired_mode
        if verification != original_verification:
            before_bytes = json.dumps(
                original_verification, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            after_bytes = json.dumps(
                verification, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            audit.append({
                "requirement_index": index,
                "source_clause_ids": sorted(binding["source_clause_ids"]),
                "source_evidence_ids": sorted(binding["source_evidence_ids"]),
                "source_obligation_ids": sorted(binding["source_obligation_ids"]),
                "required_checker_ids": checker_ids,
                "before_verification_sha256": hashlib.sha256(before_bytes).hexdigest(),
                "after_verification_sha256": hashlib.sha256(after_bytes).hexdigest(),
                "authorization": "exact_compiled_source_obligation_checker_binding",
                "rule_id": "materialize_known_source_verification_v1",
            })
    return projected, audit
