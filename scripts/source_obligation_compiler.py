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


SOURCE_VERIFICATION_CLASSIFICATION_POLICY_VERSION = "source-verification-classification-v1"
SOURCE_KEYWORD_CONSTRAINT_PROJECTION_POLICY_VERSION = "source-keyword-constraints-v1"


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
_KEYWORD_COUNT_CHARACTER_MEASURE = re.compile(
    r"(?:最多|至多|不超过|不得超过|上限|maximum(?:\s+of)?|at\s+most|"
    r"no\s+more\s+than|up\s+to)\s*\d+\s*(?:个\s*)?"
    r"(?:汉字|中文字符|字符|字|Chinese\s+characters?|CJK\s+characters?|"
    r"characters?|chars?)",
    re.IGNORECASE,
)
_KEYWORD_COUNT_MANDATORY_SIGNAL = re.compile(
    r"(?:关键词|关键字|\bkey\s*words?\b).{0,48}"
    r"(?:至少|最少|不少于|不得少于|最多|至多|不超过|不得超过|上限|下限|"
    r"at\s+least|no\s+fewer\s+than|not\s+less\s+than|at\s+most|"
    r"no\s+more\s+than|maximum|minimum|required|must)"
    r".{0,32}\d+|"
    r"(?:至少|最少|不少于|不得少于|最多|至多|不超过|不得超过|上限|下限|"
    r"at\s+least|no\s+fewer\s+than|not\s+less\s+than|at\s+most|"
    r"no\s+more\s+than|maximum|minimum|required|must)"
    r".{0,32}\d+.{0,48}(?:关键词|关键字|\bkey\s*words?\b)",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL_REAL_WORLD_ACTION_SIGNAL = re.compile(
    r"签字|签名|盖章|签章|印章|物理签署|实际签署|装订|打印|递交纸质|"
    r"\b(?:signature|sign(?:ed|ing)?|stamp(?:ed|ing)?|seal(?:ed|ing)?|binding|printing)\b",
    re.IGNORECASE,
)
_LOCAL_DOCUMENT_ACTION = (
    r"(?:(?:应当|应该|必须|需要|须|应|需)?(?:有|含有|具有|包含|包括|列有|附有|载有|"
    r"填写|填入|写明|注明|标注|载明|列出|列明|写入|显示|排版|设置|设有|安排|保持|"
    r"重复|保留|预留|留有|增加|添加|插入|加入))"
)
_LOCAL_DOCUMENT_TARGET = (
    r"(?:封面|学号|姓名|作者|题目|标题|日期|学院|专业|导师|摘要|关键词|页码|字体|字号|"
    r"行距|页边距|目录|参考文献|图表|公式|表头|续表|落款|签名栏|签字栏|签署栏|签章栏|"
    r"签字页|签名页|签署页|签章页|签字区|签名区|签署区|签章区|声明页|首页|页眉|页脚|"
    r"字段|栏位|版面|签字位置|签名位置|签章位置)"
)
_LOCAL_DOCUMENT_FIELD_ACTION_SIGNAL = re.compile(
    rf"{_LOCAL_DOCUMENT_ACTION}[^，,。；;\n]{{0,32}}{_LOCAL_DOCUMENT_TARGET}|"
    rf"{_LOCAL_DOCUMENT_TARGET}[^，,。；;\n]{{0,32}}{_LOCAL_DOCUMENT_ACTION}|"
    r"\b(?:write|state|enter|fill\s+in|list|include|display|format|align|set|repeat|retain|"
    r"preserve|keep|add|insert|place)"
    r"[^.;\n]{0,64}\b(?:cover|student\s*id|author|title|date|department|program|advisor|"
    r"abstract|keywords?|page\s+number|font|margins?|table|figure|equation|header\s+row|"
    r"signature\s+(?:block|line|page|field)|sign(?:ature)?\s+(?:block|line|page|field)|"
    r"imprint|signing\s+area)\b|"
    r"\b(?:cover|student\s*id|author|title|date|department|program|advisor|abstract|keywords?|"
    r"page\s+number|font|margins?|table|figure|equation|header\s+row|signature\s+"
    r"(?:block|line|page|field)|sign(?:ature)?\s+(?:block|line|page|field)|imprint|signing\s+area)\b"
    r"[^.;\n]{0,64}\b(?:write|state|enter|fill\s+in|list|include|display|format|align|set|repeat|"
    r"retain|preserve|keep|add|insert|place)\b",
    re.IGNORECASE,
)
SECURITY_MARKING_OPTIONS_OBLIGATION_ID = "cover.security_marking_options"
SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID = (
    "cover.security_marking_options.shorter_duration_allowed"
)
_SECURITY_MARKING_OPTION = re.compile(
    r"[□☐]\s*(?P<label>[^□☐\s,，;；()（）]{1,24})\s*[（(]\s*"
    r"(?:≤|不超过|至多|最多)\s*(?P<value>\d{1,4})\s*"
    r"(?P<unit>年|月|日)\s*[）)]"
)
_SECURITY_MARKING_SHORTER_ALLOWANCE = re.compile(
    r"(?:^|[：:，,;；。\s])"
    r"(?P<label>[^□☐\s,，;；:：。★☆*()（）]{1,24}?)\s*★\s*"
    r"(?P<value>\d{1,4})\s*(?P<unit>年|月|日)\s*[（(]\s*"
    r"可少于\s*(?P<shorter_value>\d{1,4})\s*"
    r"(?P<shorter_unit>年|月|日)\s*[）)]"
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
        "match_mode": "security_marking_options",
        "required_checker_ids": ["cover_non_public_administration"],
    },
    SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID: {
        "roles": ["cover"],
        "property_path": "properties.non_public_administration.security_marking_options",
        "match_mode": "security_marking_option",
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
_COUNT_UNIT_ZH = r"(?:条目|个词|个字|个|组|项|条|字|字符|词|篇|份|张|幅|套)"
_COUNT_UNIT_EN = r"(?:groups?|sets?|keywords?|items?|entries?|words?|characters?|chars?|pieces?|pages?)"
_EXPLICIT_KEYWORD_RANGE_ZH = re.compile(
    rf"(?:最少|至少|不少于)\s*(?P<minimum>\d+)\s*(?P<minimum_unit>{_COUNT_UNIT_ZH})?"
    rf"(?:[\s，,、；;]|并且|而且|同时|且|关键词|关键字){{0,24}}?"
    rf"(?:最多|至多|不超过)\s*(?P<maximum>\d+)\s*"
    rf"(?P<maximum_unit>{_COUNT_UNIT_ZH})?"
)
_EXPLICIT_KEYWORD_RANGE_EN = re.compile(
    rf"(?:at\s+least|no\s+fewer\s+than|not\s+less\s+than|minimum\s+of)\s*"
    rf"(?P<minimum>\d+)\s*(?P<minimum_unit>{_COUNT_UNIT_EN})?"
    rf"(?:[\s,;:]|and\b|with\b|a\b|the\b|key\s*words?\b){{0,48}}?"
    rf"(?:with\s+a\s+maximum\s+of|at\s+most|no\s+more\s+than|maximum\s+of)\s*"
    rf"(?P<maximum>\d+)\s*(?P<maximum_unit>{_COUNT_UNIT_EN})?",
    re.IGNORECASE,
)

_ABSTRACT_QUALITY_MAP = {
    "brief_statement_of_thesis_content": ("论文内容的简要陈述",),
    "independent_and_complete": ("独立性和完整性",),
    "reflects_central_idea": ("准确反映论文的中心思想",),
    "academic_language": ("规范的学术用语",),
    "logical_structure": ("逻辑性强", "结构严谨"),
    "highlight_innovation": (
        "体现出论文的新理论、新方法、新技术",
        "突出本论文的创造性成果",
        "突出论文的创造性成果",
    ),
    "new_theory_method_technology": ("新理论、新方法、新技术",),
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

_SOURCE_CORRECTION_TARGET_AMBIGUITY = re.compile(
    r"^\s*the following english is not correct[.!]?\s*$",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_SELECTION_ZH = re.compile(
    r"(?:关键词|关键字).{0,100}从.{0,12}(?:论文|学位论文|本文).{0,12}选取",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_ORIGIN_ZH = re.compile(
    r"(?:关键词|关键字).{0,80}(?:(?:须|应|需|必须|需要).{0,20}(?:源自|来自|选自|取自).{0,20}"
    r"(?:论文|学位论文|本文)|(?:源自|来自|选自|取自).{0,20}(?:论文|学位论文|本文))",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_TRACEABILITY_ZH = re.compile(
    r"(?:关键词|关键字).{0,160}(?:在论文中|论文中).{0,12}(?:有明确出处|可追溯|明确来源)",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_SELECTION_EN = re.compile(
    r"\bkeywords?\b.{0,120}\bselected\s+from\s+(?:the\s+)?(?:thesis|paper)\b",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_ORIGIN_EN = re.compile(
    r"\bkeywords?\b.{0,80}\b(?:must|should|required\s+to)\b.{0,20}"
    r"\b(?:originate|come|derive)\s+from\s+(?:the\s+)?(?:thesis|paper)\b",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_TRACEABILITY_EN = re.compile(
    r"\bkeywords?\b.{0,160}\b(?:traceable|clear\s+source)\b.{0,80}\b(?:thesis|paper|text)\b",
    re.IGNORECASE,
)
_SOURCE_VERIFICATION_EXAMPLE_CONTEXT = re.compile(
    r"(?:例如|比如|示例|反例|例[：:]|for\s+example|e\.g\.|as\s+an?\s+example|"
    r"the\s+following\s+example|counterexample)\s*[,，:：;；]?",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_NEGATION_ZH = re.compile(
    r"(?:关键词|关键字).{0,40}(?:无需|无须|不必|不要求|不需要).{0,24}"
    r"(?:源自|来自|选自|取自|从|出自).{0,24}(?:论文|学位论文|本文)|"
    r"(?:关键词|关键字).{0,40}(?:论文|学位论文|本文).{0,16}"
    r"(?:不是|并非|无需|无须|不必|不要求|不需要).{0,16}(?:来源|出处|选取依据)",
    re.IGNORECASE,
)
_KEYWORD_SOURCE_NEGATION_EN = re.compile(
    r"\bkeywords?\b.{0,48}\b(?:need\s+not|not\s+required\s+to|do\s+not\s+need\s+to)\b"
    r".{0,32}\b(?:originate|come|derive|be\s+selected)\b.{0,20}"
    r"\b(?:from\s+)?(?:the\s+)?(?:thesis|paper)\b",
    re.IGNORECASE,
)
_ABSTRACT_MANUAL_REVIEW_CONTEXT_UNSAFE = re.compile(
    r"\b(?:for\s+example|e\.g\.|example|counterexample|if\s+(?:applicable|the|this|these|those)|"
    r"where\s+applicable|only\s+if|unless)\b|"
    r"例如|比如|示例|反例|若适用|如果适用|仅当|除非",
    re.IGNORECASE,
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


def _compile_known_source_obligation_ids_in_segment(source_text: str) -> list[str]:
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
    if compile_security_marking_shorter_allowances(source_text) is not None:
        result.append(SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID)
    return sorted(set(result))


def _known_source_targets_mentioned(source_text: str) -> set[str]:
    """Identify only known obligation families named by an unsafe segment."""
    compact = re.sub(r"\s+", "", source_text)
    targets: set[str] = set()
    if "续" in compact and "表" in compact:
        targets.update({
            "table.continuation.caption_suffix",
            "table.continuation.repeat_header_row",
            "table.continuation.caption_optional",
            "table.continuation.caption_required",
        })
    if "表" in compact and re.search(r"表上方|置于表上|表上.*居中|居中.*表上", compact):
        targets.update({"table_caption.position_above", "table_caption.alignment_center"})
    if re.search(r"[□☐]", source_text) and re.search(r"限制|秘密|机密|密级", source_text):
        targets.add(SECURITY_MARKING_OPTIONS_OBLIGATION_ID)
    if "可少于" in compact and re.search(r"限制|秘密|机密|密级", compact):
        targets.add(SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID)
    return targets


def _safe_known_source_segments(source_text: str) -> list[str]:
    """Split sentence-level context so unrelated examples do not erase facts.

    Unsafe segments that mention a recognized target suppress promotion of
    that same target, including when a safe sentence elsewhere states it.
    """
    segments = [
        segment.strip()
        for segment in re.split(r"(?<=[。！？.!?\n])", source_text)
        if segment.strip()
    ]
    safe_segments: list[str] = []
    for segment in segments:
        if not _CONTEXT_UNSAFE.search(segment):
            safe_segments.append(segment)
    return safe_segments


def _unsafe_known_source_targets(source_text: str) -> set[str]:
    targets: set[str] = set()
    segments = re.split(r"(?<=[。！？.!?\n])", source_text)
    for segment in segments:
        if _CONTEXT_UNSAFE.search(segment):
            targets.update(_known_source_targets_mentioned(segment))
    return targets


def compile_known_source_obligation_ids(source_text: Any) -> list[str]:
    """Return stable IDs for narrowly recognized, machine-checkable source facts.

    Sentence-level unsafe context does not erase unrelated facts, but an
    unsafe sentence naming the same target suppresses that target globally.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return []
    result: set[str] = set()
    for segment in _safe_known_source_segments(source_text):
        result.update(_compile_known_source_obligation_ids_in_segment(segment))
    result.difference_update(_unsafe_known_source_targets(source_text))
    if {
        "table.continuation.caption_optional", "table.continuation.caption_required",
    }.issubset(result):
        result.difference_update({
            "table.continuation.caption_optional", "table.continuation.caption_required",
        })
    return sorted(result)


def has_mixed_external_document_action_signal(source_text: Any) -> bool:
    """Conservatively flag known DOCX actions co-located with real-world actions.

    This narrow lexical guard is not a semantic classifier. It prevents a
    recognized local field/layout action from being hidden under an external
    compliance label. A positive result requires splitting or another
    contract that can express both actions; absence of a cue does not prove
    arbitrary prose contains no mixed obligation.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return False
    has_local_document_action = bool(
        _LOCAL_DOCUMENT_FIELD_ACTION_SIGNAL.search(source_text)
        or compile_known_source_obligation_ids(source_text)
    )
    return bool(
        _EXTERNAL_REAL_WORLD_ACTION_SIGNAL.search(source_text)
        and has_local_document_action
    )


def compile_known_source_obligations(source_text: Any) -> list[dict[str, Any]]:
    """Return source-derived facts with their deterministic payload bindings."""
    facts: list[dict[str, Any]] = []
    safe_source = "\n".join(_safe_known_source_segments(source_text)) \
        if isinstance(source_text, str) else ""
    for obligation_id in compile_known_source_obligation_ids(source_text):
        binding = KNOWN_SOURCE_OBLIGATION_BINDINGS.get(obligation_id)
        if binding is None:
            continue
        fact = {"id": obligation_id, **copy.deepcopy(binding)}
        if obligation_id == SECURITY_MARKING_OPTIONS_OBLIGATION_ID:
            fact["expected_value"] = compile_security_marking_options(safe_source)
        elif obligation_id == SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID:
            fact["expected_value"] = compile_security_marking_shorter_allowances(safe_source)
        else:
            fact["expected_value"] = copy.deepcopy(binding.get("expected_value"))
        facts.append(fact)
    return facts


def compile_security_marking_shorter_allowances(
    source_text: Any,
) -> list[dict[str, Any]] | None:
    """Compile exact starred options whose parenthetical permits a shorter term.

    This deliberately recognizes only the explicit ``label★N年(可少于N年)``
    form. A missing/mismatched number or unit, an unparsed ``可少于`` phrase,
    duplicate labels, or example/conditional context fails closed.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return None
    if _CONTEXT_UNSAFE.search(source_text):
        return None
    matches = list(_SECURITY_MARKING_SHORTER_ALLOWANCE.finditer(source_text))
    if not matches or source_text.count("可少于") != len(matches):
        return None
    labels: list[str] = []
    compiled: list[dict[str, Any]] = []
    for match in matches:
        label = re.sub(r"\s+", " ", match.group("label")).strip()
        value = int(match.group("value"))
        shorter_value = int(match.group("shorter_value"))
        unit = match.group("unit")
        if (
            not label
            or value != shorter_value
            or unit != match.group("shorter_unit")
        ):
            return None
        labels.append(label)
        compiled.append({
            "label": label,
            "maximum_duration": {"value": value, "unit": unit},
            "shorter_duration_allowed": True,
        })
    if len(set(labels)) != len(labels):
        return None
    return compiled


def source_fact_value_matches(
    actual: Any, expected: Any, match_mode: str | None = None,
) -> bool:
    """Compare a candidate payload with a code-compiled source fact."""
    if match_mode is None:
        if isinstance(expected, bool):
            return isinstance(actual, bool) and actual is expected
        return actual == expected
    if match_mode not in {"security_marking_options", "security_marking_option"}:
        return False
    if not isinstance(actual, list) or not isinstance(expected, list) or not expected:
        return False
    actual_by_label: dict[str, list[dict[str, Any]]] = {}
    for item in actual:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            return False
        actual_by_label.setdefault(item["label"], []).append(item)
    if any(not isinstance(item, dict) or not isinstance(item.get("label"), str) for item in expected):
        return False
    expected_labels = [item["label"] for item in expected]
    if len(set(expected_labels)) != len(expected_labels):
        return False
    if match_mode == "security_marking_options":
        if len(actual) != len(expected) or set(actual_by_label) != set(expected_labels):
            return False
    for wanted in expected:
        candidates = actual_by_label.get(wanted["label"], [])
        if len(candidates) != 1:
            return False
        candidate = candidates[0]
        if not set(candidate).difference(wanted).issubset({"shorter_duration_allowed"}):
            return False
        for key, value in wanted.items():
            actual_value = candidate.get(key)
            if isinstance(value, bool):
                if not isinstance(actual_value, bool) or actual_value is not value:
                    return False
            elif actual_value != value:
                return False
    return True


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
        if re.search(r"\benglish\s+keywords?\b|\bkey\s*words?\b", source_text, re.I)
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


def source_text_candidates(clause: Any) -> list[str]:
    """Return deduplicated source-bound clause/evidence text from most exact to widest."""
    if not isinstance(clause, dict):
        return [clause] if isinstance(clause, str) and clause.strip() else []
    span = clause.get("source_span")
    if "source_span" in clause:
        if not isinstance(span, dict):
            raise ValueError("clause source_span must be an object")
        exact = span.get("text")
        if not isinstance(exact, str) or not exact.strip():
            raise ValueError("clause source_span.text must be non-empty source text")
        # source_text_full is deterministic extraction context and may carry
        # a subject split into an adjacent clause. Never mix the normalized
        # clause.text back into source-bound semantic checks: it can differ in
        # whitespace or punctuation and is not the citation source.
        values = (exact, clause.get("source_text_full"))
    else:
        values = (clause.get("text"), clause.get("source_text_full"))
    return list(dict.fromkeys(
        value for value in values
        if isinstance(value, str) and value.strip()
    ))


def exact_clause_source_text(clause: Any) -> str:
    """Return the authoritative exact clause text when a bound span exists."""
    candidates = source_text_candidates(clause)
    if not candidates:
        return ""
    if isinstance(clause, dict) and "source_span" in clause:
        return candidates[0]
    # Legacy extraction records have no span. They remain readable for
    # offline compatibility, but current host-review packets require spans.
    return candidates[0]


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
    match = matches[0]
    minimum, maximum = int(match.group("minimum")), int(match.group("maximum"))
    minimum_unit = _normalize_count_unit(match.groupdict().get("minimum_unit"))
    maximum_unit = _normalize_count_unit(match.groupdict().get("maximum_unit"))
    # A lower bound and an upper bound are comparable only when both state
    # the same measure. Missing one side is also ambiguous; never collapse
    # "groups" and "sets" into a single keyword count.
    if not minimum_unit or not maximum_unit or minimum_unit != maximum_unit:
        return None
    if minimum < 1 or maximum < minimum:
        return None
    return {"min_count": minimum, "max_count": maximum}


def _normalize_count_unit(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    unit = value.casefold()
    return unit[:-1] if unit.endswith("s") else unit


def _has_ambiguous_explicit_count_units(source_text: str) -> bool:
    matches = [
        match
        for pattern in (_EXPLICIT_KEYWORD_RANGE_ZH, _EXPLICIT_KEYWORD_RANGE_EN)
        for match in pattern.finditer(source_text)
    ]
    return any(
        not _normalize_count_unit(match.groupdict().get("minimum_unit"))
        or not _normalize_count_unit(match.groupdict().get("maximum_unit"))
        or _normalize_count_unit(match.groupdict().get("minimum_unit"))
        != _normalize_count_unit(match.groupdict().get("maximum_unit"))
        for match in matches
    )


def has_explicit_keyword_count_signal(source_text: Any) -> bool:
    """Detect a likely hard keyword-count rule even when its range is ambiguous.

    This is deliberately only a fail-closed signal.  It does not compile a
    numeric bound; callers must keep the item unresolved unless the narrow
    range compiler above succeeds.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return False
    if _CONTEXT_UNSAFE.search(source_text):
        return False
    # Character limits constrain keyword length, not the number of keyword
    # items. Remove those separately measurable bounds before detecting an
    # unresolved item-count rule; retain any distinct count signal elsewhere
    # in the same source passage (for example, "最多7个汉字；最少3组，最多8组").
    count_text = _KEYWORD_COUNT_CHARACTER_MEASURE.sub(" ", source_text)
    return _KEYWORD_COUNT_MANDATORY_SIGNAL.search(count_text) is not None


def _keyword_language_for_clause(clause: Any) -> str | None:
    """Resolve a keyword language only from one exact source/context target."""
    candidates = source_text_candidates(clause)
    if not candidates:
        return None

    def languages_in(text: str) -> set[str]:
        languages: set[str] = set()
        # "英文关键词/英文关键字" names the English-keyword field; do not
        # also classify the embedded Chinese word "关键词" as a Chinese target.
        if re.search(r"(?<!英文)(?:关键词|关键字)", text):
            languages.add("keywords_zh")
        if re.search(r"\bkey\s*words?\b|英文关键词", text, re.I):
            languages.add("keywords_en")
        return languages

    exact_languages = languages_in(candidates[0])
    if len(exact_languages) == 1:
        return next(iter(exact_languages))
    if exact_languages:
        return None
    context_languages = {
        language
        for value in candidates[1:]
        for language in languages_in(value)
    }
    return next(iter(context_languages)) if len(context_languages) == 1 else None


def compile_keyword_source_constraints(clause: Any) -> dict[str, Any] | None:
    """Compile only explicit, source-local keyword properties.

    ``source_text_full`` may identify the target language for a split clause,
    but numeric values and property cues are parsed only from the exact span.
    A model-authored top-level keyword role is not treated as proof that these
    nested content-constraint properties were represented.
    """
    if not isinstance(clause, dict):
        return None
    candidates = source_text_candidates(clause)
    if not candidates:
        return None
    exact_text = candidates[0]
    language_key = _keyword_language_for_clause(clause)
    if language_key is None or _CONTEXT_UNSAFE.search(exact_text):
        return None
    keyword_subject = re.compile(r"关键词|关键字|\bkey\s*words?\b", re.I)
    parse_text = exact_text
    if not keyword_subject.search(parse_text):
        parse_text = f"{'Key Words' if language_key == 'keywords_en' else '关键词'} {parse_text}"

    properties: dict[str, Any] = {}
    guidance = compile_soft_keyword_count_guidance(parse_text)
    if guidance is not None and guidance["language_key"] == language_key:
        properties["count_guidance"] = {
            "min_count": guidance["min_count"],
            "max_count": guidance["max_count"],
            "strength": "general_guidance",
        }

    hard_range = compile_explicit_keyword_count_range(parse_text)
    if hard_range is not None:
        properties.update(hard_range)

    character_limits = list(re.finditer(
        r"(?:最多|至多|不超过|不得超过|上限|maximum(?:\s+of)?|at\s+most|"
        r"no\s+more\s+than|up\s+to)\s*(?P<value>\d+)\s*(?:个\s*)?"
        r"(?P<metric>汉字|中文字符|Chinese\s+characters?|CJK\s+characters?)",
        parse_text,
        re.I,
    ))
    if len(character_limits) == 1:
        properties["max_item_chars"] = int(character_limits[0].group("value"))
        properties["item_length_metric"] = "cjk_characters"

    compact = re.sub(r"\s+", "", exact_text)
    if (
        language_key == "keywords_zh"
        and re.search(
            r"(?:关键词|关键字).{0,20}摘要(?:内容|正文).{0,8}(?:后|之后).{0,8}"
            r"(?:另起一行|另起一段|另行)",
            compact,
        )
    ):
        properties["require_after_role"] = "abstract_body_zh"
    elif (
        language_key == "keywords_en"
        and re.search(
            r"\bkey\s*words?\b.{0,80}(?:after\s+the\s+abstract|"
            r"in\s+the\s+abstract\s+content\s+after\s+another\s+line)",
            exact_text,
            re.I,
        )
    ):
        properties["require_after_role"] = "abstract_body_en"

    if (
        re.search(
            r"(?:之间\s*)?(?:用|以|采用)?\s*分号\s*(?:分开|分隔|隔开)|"
            r"separated\s+by\s+semi[- ]?colons?|semi[- ]?colon[- ]separated",
            exact_text,
            re.I,
        )
    ):
        properties["separator"] = "semicolon"

    if not properties:
        return None
    span = clause.get("source_span")
    return {
        "language_key": language_key,
        "properties": properties,
        "source_quote": exact_text,
        "source_sha256": span.get("source_sha256") if isinstance(span, dict) else None,
    }


def materialize_source_keyword_constraints(
    response: Any, clauses: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Add source-derived nested keyword constraints beside linked style roles.

    This does not reinterpret an unlinked source clause: projection requires a
    current, exact-evidence ``keywords_zh``/``keywords_en`` requirement and an
    executable review for each clause. It never changes review classifications
    or obligation statuses. Existing conflicting values are preserved so the
    ordinary validator can reject them rather than silently overwriting them.
    """
    if not isinstance(response, dict) or not isinstance(clauses, list):
        return copy.deepcopy(response), []
    projected = copy.deepcopy(response)
    requirements = projected.get("requirements")
    reviews = projected.get("clause_reviews")
    if not isinstance(requirements, list) or not isinstance(reviews, list):
        return projected, []

    review_map: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        if isinstance(review, dict) and isinstance(review.get("clause_id"), str):
            review_map.setdefault(review["clause_id"], []).append(review)

    groups: dict[str, list[dict[str, Any]]] = {}
    for clause in clauses:
        if not isinstance(clause, dict) or not isinstance(clause.get("id"), str):
            continue
        clause_id = clause["id"]
        matching_reviews = review_map.get(clause_id, [])
        if len(matching_reviews) != 1 or matching_reviews[0].get("classification") not in {
            "covered", "executable", "verify_existing",
        }:
            continue
        compiled = compile_keyword_source_constraints(clause)
        if compiled is None:
            continue
        raw_clause_evidence = clause.get("evidence_ids")
        clause_evidence_ids = list(dict.fromkeys(
            value for value in raw_clause_evidence
            if isinstance(value, str) and value
        )) if isinstance(raw_clause_evidence, list) else []
        if not clause_evidence_ids:
            continue
        parent_indexes = [
            index for index, requirement in enumerate(requirements)
            if isinstance(requirement, dict)
            and requirement.get("role") == compiled["language_key"]
            and clause_id in (requirement.get("clause_ids") or [])
            and set(clause_evidence_ids).issubset({
                value for value in (requirement.get("evidence_ids") or [])
                if isinstance(value, str)
            })
        ]
        if len(parent_indexes) != 1:
            continue
        parent = requirements[parent_indexes[0]]
        confidence = parent.get("confidence")
        if (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            continue
        groups.setdefault(compiled["language_key"], []).append({
            "clause_id": clause_id,
            "evidence_ids": clause_evidence_ids,
            "parent_index": parent_indexes[0],
            "confidence": float(confidence),
            "compiled": compiled,
        })

    audits: list[dict[str, Any]] = []
    for language_key, entries in groups.items():
        desired: dict[str, Any] = {}
        conflicted_fields: set[str] = set()
        for entry in entries:
            for field, value in entry["compiled"]["properties"].items():
                if field in conflicted_fields:
                    continue
                if field in desired and desired[field] != value:
                    desired.pop(field)
                    conflicted_fields.add(field)
                    continue
                desired[field] = copy.deepcopy(value)
        if not desired:
            continue

        clause_ids = [entry["clause_id"] for entry in entries]
        evidence_ids = list(dict.fromkeys(
            evidence_id for entry in entries for evidence_id in entry["evidence_ids"]
        ))
        clause_set = set(clause_ids)
        overlapping_constraints = [
            (index, requirement)
            for index, requirement in enumerate(requirements)
            if isinstance(requirement, dict)
            and requirement.get("role") == "content_constraints"
            and clause_set.intersection(requirement.get("clause_ids") or [])
        ]
        if len(overlapping_constraints) > 1:
            continue

        before = copy.deepcopy(projected)
        if overlapping_constraints:
            requirement_index, target = overlapping_constraints[0]
            target_clause_ids = target.get("clause_ids")
            target_evidence_ids = target.get("evidence_ids")
            if (
                not isinstance(target_clause_ids, list)
                or any(not isinstance(value, str) for value in target_clause_ids)
                or not set(target_clause_ids).issubset(clause_set)
                or not isinstance(target_evidence_ids, list)
                or any(not isinstance(value, str) for value in target_evidence_ids)
                or not set(target_evidence_ids).issubset(set(evidence_ids))
            ):
                continue
            properties = target.get("properties")
            if not isinstance(properties, dict):
                continue
            nested = properties.get(language_key)
            if nested is None:
                nested = {}
                properties[language_key] = nested
            if not isinstance(nested, dict):
                continue
            existing_conflicts: set[str] = set()
            for field, value in desired.items():
                current = nested.get(field)
                if current is None:
                    nested[field] = copy.deepcopy(value)
                elif current != value:
                    existing_conflicts.add(field)
            target["clause_ids"] = list(dict.fromkeys([*target_clause_ids, *clause_ids]))
            target["evidence_ids"] = list(dict.fromkeys([*target_evidence_ids, *evidence_ids]))
        else:
            new_requirement = {
                "role": "content_constraints",
                "properties": {language_key: copy.deepcopy(desired)},
                "clause_ids": clause_ids,
                "evidence_ids": evidence_ids,
                "confidence": min(entry["confidence"] for entry in entries),
                "reason": (
                    "Source-bound keyword constraints were projected deterministically; "
                    "qualified count ranges remain guidance and only explicit mandatory "
                    "ranges become hard bounds."
                ),
            }
            requirements.append(new_requirement)
            requirement_index = len(requirements) - 1

        if projected.get("contract_version") == "2.1":
            for review in reviews:
                if (
                    isinstance(review, dict)
                    and review.get("clause_id") in clause_set
                    and review.get("classification") in {"covered", "executable", "verify_existing"}
                    and isinstance(review.get("requirement_indexes"), list)
                    and requirement_index not in review["requirement_indexes"]
                ):
                    review["requirement_indexes"].append(requirement_index)

        before_bytes = json.dumps(
            before, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        after_bytes = json.dumps(
            projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if before_bytes != after_bytes:
            audits.append({
                "projection_policy_version": SOURCE_KEYWORD_CONSTRAINT_PROJECTION_POLICY_VERSION,
                "rule_id": "source_bound_keyword_constraint_projection_v1",
                "authorization": "exact_current_source_and_linked_keyword_role_v1",
                "semantic_inference": "none",
                "language_key": language_key,
                "action": "complete_existing_content_constraint" if overlapping_constraints else "add_content_constraint",
                "requirement_index": requirement_index,
                "parent_requirement_indexes": sorted({entry["parent_index"] for entry in entries}),
                "clause_ids": clause_ids,
                "evidence_ids": evidence_ids,
                "source_bindings": [
                    {
                        "clause_id": entry["clause_id"],
                        "evidence_ids": entry["evidence_ids"],
                        "source_quote": entry["compiled"]["source_quote"],
                        "source_sha256": entry["compiled"]["source_sha256"],
                        "projected_fields": sorted(entry["compiled"]["properties"]),
                    }
                    for entry in entries
                ],
                "properties": copy.deepcopy(desired),
                "unprojected_conflicting_fields": sorted(
                    conflicted_fields | (existing_conflicts if overlapping_constraints else set())
                ),
                "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
                "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
            })
    return projected, audits


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
    if not isinstance(source_text, str):
        return []
    match = _ABSTRACT_SOURCE_MANUAL_REVIEW.search(source_text)
    context = (
        source_text[max(0, match.start() - 80):min(len(source_text), match.end() + 80)]
        if match is not None else ""
    )
    codes: list[str] = []
    if (
        match is not None
        and not _ABSTRACT_MANUAL_REVIEW_CONTEXT_UNSAFE.search(context)
        and not _inside_quote(source_text, match.start())
    ):
        codes.append("abstract_target_metric_ambiguity")
    if not _CONTEXT_UNSAFE.search(source_text) and _has_ambiguous_explicit_count_units(source_text):
        codes.append("quantitative_scope_unit_ambiguity")
    if (
        _SOURCE_CORRECTION_TARGET_AMBIGUITY.fullmatch(source_text)
        and not _inside_quote(source_text, 0)
    ):
        codes.append("source_correction_target_ambiguity")
    return sorted(set(codes))


def compile_source_content_verification_codes(source_text: Any) -> list[str]:
    """Recognize source-origin checks that require a human, never an auto-pass."""
    if not isinstance(source_text, str) or not source_text.strip():
        return []
    if (
        _KEYWORD_SOURCE_NEGATION_ZH.search(source_text)
        or _KEYWORD_SOURCE_NEGATION_EN.search(source_text)
    ):
        return []
    chinese_selection = _KEYWORD_SOURCE_SELECTION_ZH.search(source_text)
    chinese_origin = _KEYWORD_SOURCE_ORIGIN_ZH.search(source_text)
    chinese_traceability = _KEYWORD_SOURCE_TRACEABILITY_ZH.search(source_text)
    english_selection = _KEYWORD_SOURCE_SELECTION_EN.search(source_text)
    english_origin = _KEYWORD_SOURCE_ORIGIN_EN.search(source_text)
    english_traceability = _KEYWORD_SOURCE_TRACEABILITY_EN.search(source_text)
    if not (
        chinese_origin
        or english_origin
        or (chinese_selection and chinese_traceability)
        or (english_selection and english_traceability)
    ):
        return []
    source_match = chinese_origin or english_origin or chinese_selection or english_selection
    prefix = source_text[:source_match.start()] if source_match is not None else ""
    sentence_start = max(
        (prefix.rfind(mark) for mark in (".", "!", "?", "。", "！", "？", "\n")),
        default=-1,
    )
    if _SOURCE_VERIFICATION_EXAMPLE_CONTEXT.search(prefix[sentence_start + 1:]):
        return []
    if source_match is not None and _inside_quote(source_text, source_match.start()):
        return []
    return ["keyword_source_traceability_verification"]


def is_explicit_authoring_content_quote(quote: Any) -> bool:
    """Recognize only a source instruction that explicitly asks for authored content.

    This conservative lexical gate is shared by the independent reviewer and
    source-verification projections. It is not a general semantic classifier:
    unsupported or ambiguous wording remains pending instead of being
    reinterpreted as an authoring task.
    """
    if not isinstance(quote, str) or not quote.strip():
        return False
    compact = re.sub(r"\s+", "", quote).casefold()
    chinese_scope_markers = (
        "不得", "不要", "不能", "不应", "不宜", "不可", "无需", "无须", "禁止", "避免", "切勿",
        "如果", "若", "假如", "倘若", "除非", "只有在", "仅当", "如有", "当……时",
    )
    if any(token in compact for token in chinese_scope_markers):
        return False
    english = quote.casefold()
    if re.search(
        r"\b(?:not|never|don't|doesn't|didn't|cannot|can't|shouldn't|mustn't|without|unless|if|when|only\s+if|provided\s+that)\b",
        english,
    ):
        return False
    chinese_sample = any(token in compact for token in (
        "示例", "样例", "范例", "虚构", "杜撰", "编的", "编写的",
    ))
    chinese_author = any(token in compact for token in ("作者", "自行", "自己", "本人"))
    chinese_action = any(token in compact for token in (
        "撰写", "编写", "补充", "填写", "提供", "替换",
    ))
    chinese_genuine_content = any(token in compact for token in (
        "真实内容", "实际内容", "真实研究", "实际研究", "本人内容",
    ))
    if chinese_author and chinese_action and (chinese_sample or chinese_genuine_content):
        return True

    english_sample = any(token in english for token in (
        "example", "sample", "fictitious", "fabricated", "placeholder",
    ))
    english_author = bool(re.search(r"\b(author|you|yourself)\b", english))
    english_action = bool(re.search(
        r"\b(write|draft|provide|replace|fill\s+in|supply)\b", english,
    ))
    english_genuine_content = any(token in english for token in (
        "genuine content", "actual research", "original content",
    ))
    return english_author and english_action and (english_sample or english_genuine_content)


def has_explicit_authoring_action_cue(source_text: Any) -> bool:
    """Conservatively detect any author/learner action mixed into a clause.

    This is intentionally broader than the placeholder-authoring gate above.
    A clause that combines traceability with a genuine author task must not be
    collapsed to a verification-only classification, even when the task does
    not mention a sample or placeholder.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return False
    compact = re.sub(r"\s+", "", source_text).casefold()
    chinese_author = any(token in compact for token in (
        "作者", "毕业生", "学生本人", "本人", "申请人",
    ))
    chinese_action = any(token in compact for token in (
        "撰写", "编写", "补充", "填写", "提供", "替换", "选择", "选取",
        "整理", "录入", "添加", "列出", "写出", "提交", "撰录", "编制", "创作",
    ))
    if chinese_author and chinese_action:
        return True
    english = source_text.casefold()
    english_author = bool(re.search(r"\b(?:author|student|applicant|you)\b", english))
    english_action = bool(re.search(
        r"\b(?:write|draft|provide|supply|create|prepare|compose|fill|add|select|choose|enter|submit)\b",
        english,
    ))
    return english_author and english_action


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
        source_candidates = source_text_candidates(clause)
        guidance_candidates = [
            result for candidate in source_candidates
            if (result := compile_soft_keyword_count_guidance(candidate)) is not None
        ]
        unique_guidance = {
            (item["language_key"], item["min_count"], item["max_count"], item["strength"])
            for item in guidance_candidates
        }
        if len(unique_guidance) > 1:
            continue
        guidance = guidance_candidates[0] if guidance_candidates else None
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
            linked_source_candidates = [
                source_text
                for other_id in linked_clause_ids
                for source_text in source_text_candidates(clauses_by_id.get(other_id, {}))
            ]
            explicit_ranges = [
                compile_explicit_keyword_count_range(source_text)
                for source_text in linked_source_candidates
            ]
            hard_signals = [
                has_explicit_keyword_count_signal(source_text)
                for source_text in linked_source_candidates
            ]
            independently_mandatory = any(value is not None for value in explicit_ranges)
            mandatory_signal_present = any(hard_signals)
            removed_hard_bounds: list[str] = []
            # An unparsed hard-looking source statement is not permission to
            # erase model-provided bounds.  Keep them for review, while the
            # contract reports the unresolved source rule and blocks release.
            if not independently_mandatory and not mandatory_signal_present:
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
                    "mandatory_count_signal_present": mandatory_signal_present,
                    "mandatory_count_signal_fully_compiled": independently_mandatory,
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
        review = reviews_by_id.get(clause_id)
        # A source compiler may complete an already executable/covered review,
        # but it cannot resolve a semantic question that the Host Agent left
        # unresolved. Even a recognizable source bundle is not authority to
        # change the review disposition or create executable requirements.
        if not isinstance(review, dict) or review.get("classification") == "unresolved":
            continue
        source_text = exact_clause_source_text(clause)
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
                other_text = exact_clause_source_text(other_clause)
                if isinstance(other_text, str):
                    source_context_items.append(other_text)
        compiled = compile_abstract_source_constraints(
            source_text, source_context="\n".join(source_context_items),
        )
        evidence_ids = sorted({
            str(value) for value in (clause.get("evidence_ids") or [])
            if isinstance(value, str) and value
        })
        if compiled is None or not evidence_ids:
            continue
        if review.get("classification") not in {"executable", "covered", "verify_existing"}:
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
                    exact_clause_source_text(clauses_by_id.get(other_id, {})),
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
        # This compiler may project exact-source properties into the
        # requirement graph, but it is not a semantic reviewer.  Preserve the
        # Host Agent's classification, reason, normative basis, and complete
        # obligation inventory exactly; the independent source audit remains
        # responsible for identifying omissions and coverage.
        review_before = copy.deepcopy(review)
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
                exact_clause_source_text(clause).encode("utf-8")
            ).hexdigest(),
            "requirement_index": requirement_index,
            "bundle": compiled["bundle"],
            "obligation_ids": compiled["obligation_ids"],
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "semantic_review_preserved": review == review_before,
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
        source_text = exact_clause_source_text(clause)
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
            and isinstance(requirement.get("clause_ids"), list)
            and clause_id in requirement["clause_ids"]
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
                value for value in (
                    clause.get("evidence_ids")
                    if isinstance(clause.get("evidence_ids"), list) else []
                )
                if isinstance(value, str) and value
            ],
            "source_obligation_ids": [SECURITY_MARKING_OPTIONS_OBLIGATION_ID],
            "property_path": "properties.non_public_administration.security_marking_options",
            "before_property_sha256": before_sha256,
            "after_property_sha256": after_sha256,
            "authorization": "exact_source_checkbox_duration_projection_v1",
            "rule_id": "compile_security_marking_options_v1",
        })

    for clause in clauses:
        if not isinstance(clause, dict) or not isinstance(clause.get("id"), str):
            continue
        clause_id = clause["id"]
        source_text = exact_clause_source_text(clause)
        allowances = compile_security_marking_shorter_allowances(source_text)
        review = review_by_id.get(clause_id, {})
        if (
            allowances is None
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
        options = (
            administration.get("security_marking_options")
            if isinstance(administration, dict) else None
        )
        if not isinstance(options, list):
            continue
        before = copy.deepcopy(options)
        safe_to_project = True
        for allowance in allowances:
            matches = [
                item for item in options
                if isinstance(item, dict) and item.get("label") == allowance["label"]
            ]
            if len(matches) != 1:
                safe_to_project = False
                break
            item = matches[0]
            if item.get("maximum_duration") != allowance["maximum_duration"]:
                safe_to_project = False
                break
            if "shorter_duration_allowed" in item and item["shorter_duration_allowed"] is not True:
                safe_to_project = False
                break
        if not safe_to_project:
            continue
        for allowance in allowances:
            next(item for item in options if item.get("label") == allowance["label"])[
                "shorter_duration_allowed"
            ] = True
        if options == before:
            continue
        before_sha256 = hashlib.sha256(json.dumps(
            before, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        after_sha256 = hashlib.sha256(json.dumps(
            options, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        property_projections.append({
            "requirement_index": requirement_index,
            "source_clause_ids": [clause_id],
            "source_evidence_ids": [
                value for value in clause.get("evidence_ids", [])
                if isinstance(value, str) and value
            ],
            "source_obligation_ids": [SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID],
            "property_path": "properties.non_public_administration.security_marking_options",
            "before_property_sha256": before_sha256,
            "after_property_sha256": after_sha256,
            "authorization": "exact_source_security_marking_qualifier_projection_v1",
            "rule_id": "compile_security_marking_shorter_allowances_v1",
        })

    bindings: dict[int, dict[str, Any]] = {}
    for clause in clauses:
        if not isinstance(clause, dict) or not isinstance(clause.get("id"), str):
            continue
        clause_id = clause["id"]
        source_text = exact_clause_source_text(clause)
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
                value_matches = source_fact_value_matches(
                    actual, expected, fact.get("match_mode"),
                )
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


def materialize_source_verification_classifications(
    response: Any, clauses: Any, *, provenance: Any = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Keep code-registered existing-content checks in a human-verification state.

    A primary model may describe existing-content provenance as content that
    must be supplied, even though the current source only requires the content
    to originate in the thesis and be traceable. For the narrow v3 shape below,
    code projects that misclassification to ``requires_source_verification``.
    It does not create a requirement or claim the check passed. Explicit
    authoring instructions, executable requirements, mixed/manual source facts,
    and non-singleton model obligation inventories are left untouched.
    """
    if (
        not isinstance(response, dict)
        or response.get("contract_version") != "3.0"
        or not isinstance(clauses, list)
    ):
        return copy.deepcopy(response), []
    reviews = response.get("clause_reviews")
    requirements = response.get("requirements")
    if not isinstance(reviews, list) or not isinstance(requirements, list):
        return copy.deepcopy(response), []
    clause_map = {
        item.get("id"): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    review_map = {
        item.get("clause_id"): item for item in reviews
        if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
    }
    if len(clause_map) != len(clauses) or len(review_map) != len(reviews):
        return copy.deepcopy(response), []

    projected = copy.deepcopy(response)
    projected_review_map = {
        item.get("clause_id"): item for item in projected["clause_reviews"]
        if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
    }
    audit: list[dict[str, Any]] = []
    for clause_id, clause in sorted(clause_map.items()):
        source_text = exact_clause_source_text(clause)
        verification_codes = compile_source_content_verification_codes(source_text)
        if (
            not verification_codes
            or is_explicit_authoring_content_quote(source_text)
            or has_explicit_authoring_action_cue(source_text)
            or compile_known_source_obligation_ids(source_text)
            or compile_unresolved_manual_review_codes(source_text)
        ):
            continue
        review = projected_review_map.get(clause_id)
        original_review = review_map.get(clause_id)
        obligations = original_review.get("obligations") if isinstance(original_review, dict) else None
        if (
            not isinstance(review, dict)
            or not isinstance(original_review, dict)
            or original_review.get("classification") != "requires_source_content"
            or not isinstance(obligations, list)
            or len(obligations) != 1
            or not isinstance(obligations[0], dict)
            or obligations[0].get("status") != "requires_source_content"
            or any(
                isinstance(requirement, dict)
                and clause_id in (requirement.get("clause_ids") or [])
                for requirement in requirements
            )
        ):
            continue

        before_response_sha256 = hashlib.sha256(json.dumps(
            response, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        before_review = copy.deepcopy(review)
        review["classification"] = "requires_source_verification"
        review["obligations"] = []
        after_response_sha256 = hashlib.sha256(json.dumps(
            projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        audit.append({
            "rule_id": "project_registered_existing_content_verification_v1",
            "policy_version": SOURCE_VERIFICATION_CLASSIFICATION_POLICY_VERSION,
            "authorization": "registered_source_verification_without_authoring_instruction_v1",
            "clause_id": clause_id,
            "source_evidence_ids": sorted({
                str(value) for value in (clause.get("evidence_ids") or [])
                if isinstance(value, str) and value
            }),
            "source_quote_sha256": hashlib.sha256(
                source_text.encode("utf-8")
            ).hexdigest(),
            "source_content_verification_codes": verification_codes,
            "provenance": copy.deepcopy(provenance) if isinstance(provenance, dict) else None,
            "before_classification": "requires_source_content",
            "after_classification": "requires_source_verification",
            "original_primary_obligations": copy.deepcopy(obligations),
            "original_review_sha256": hashlib.sha256(json.dumps(
                before_review, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "before_response_sha256": before_response_sha256,
            "after_response_sha256": after_response_sha256,
            "human_verification_required": True,
            "submission_ready": False,
        })
    return projected, audit
