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
}

_VERIFICATION_MODE_RANK = {
    "static_docx": 0,
    "pdf_render": 1,
    "word_render": 2,
    "manual": 3,
    "external": 4,
}


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
    return sorted(set(result))


def compile_known_source_obligations(source_text: Any) -> list[dict[str, Any]]:
    """Return source-derived facts with their deterministic payload bindings."""
    return [
        {"id": obligation_id, **KNOWN_SOURCE_OBLIGATION_BINDINGS[obligation_id]}
        for obligation_id in compile_known_source_obligation_ids(source_text)
        if obligation_id in KNOWN_SOURCE_OBLIGATION_BINDINGS
    ]


def materialize_known_source_verification(
    response: Any, clauses: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Project required checker bindings from exact compiled source facts.

    The host may propose verification metadata, but it cannot omit a checker
    that the deterministic source-obligation registry requires.  Only a
    role/property value that exactly matches a compiled source fact receives
    the binding; wrong or incomplete payloads remain unchanged and fail the
    normal validator.  The input is never mutated.
    """
    if not isinstance(response, dict) or not isinstance(clauses, list):
        return copy.deepcopy(response), []
    projected = copy.deepcopy(response)
    requirements = projected.get("requirements")
    if not isinstance(requirements, list):
        return projected, []

    def read_property(value: Any, path: str) -> Any:
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

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

    audit: list[dict[str, Any]] = []
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
