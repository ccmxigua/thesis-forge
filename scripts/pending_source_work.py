"""Closed source grammars for pending human work, never execution authority."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from source_obligation_compiler import _inside_quote, is_explicit_authoring_content_quote

PENDING_WORK_PROTOCOL = "source-bound-pending-work/v1"
ANTI_EXCERPT = "authoring_anti_excerpt_verification"
REPETITION_SCOPE = "authoring_repetition_scope_verification"
SECTION_CHOICE = "conditional_section_omission_verification"
PENDING_WORK_CODES = (ANTI_EXCERPT, REPETITION_SCOPE, SECTION_CHOICE)


def compile_pending_source_work(source: Any) -> list[dict[str, Any]]:
    """Keep exact conditions/permissions; do not decide facts or delete sections.

    Unknown, quoted/example, negated and additional conditional instructions
    remain outside these grammars. The semantic reviewer still decomposes all
    other obligations. Each fact is only a minimum human-work inventory.
    """
    if not isinstance(source, str) or not source.strip():
        return []
    facts = []
    if is_explicit_authoring_content_quote(source):
        for part in re.finditer(r"[^，,。；;！？!?\n]+", source):
            if _inside_quote(source, part.start()):
                continue
            text = part.group()
            code = None
            if re.fullmatch(r"\s*(?:不能|不得|不可)(?:是)?文献(?:资料)?的简单摘录\s*", text):
                code = ANTI_EXCERPT
            elif re.fullmatch(r"\s*(?:计算在重复率内|计入重复率(?:计算)?)\s*", text):
                code = REPETITION_SCOPE
            if code:
                facts.append({"code": code, "source_quote": text,
                              "start": part.start(), "end": part.end()})
    # A full source sentence is required: a permission is not a deletion order,
    # and the absence of materials is not established by this sentence.
    permission = re.fullmatch(
        r"\s*(?P<condition>(?:若|如果)(?:论文|本论文)?研究(?:确实)?(?:无|没有)"
        r"(?:国内|国外|相关)(?:资料|文献))[，,]?\s*"
        r"(?P<permission>(?:本部分|本节|本章|该部分|该节|该章)(?:也)?(?:可|可以)删除)"
        r"[。.]?\s*", source,
    )
    if permission:
        facts.append({"code": SECTION_CHOICE, "source_quote": source,
                      "start": 0, "end": len(source),
                      "condition": permission.group("condition"),
                      "permission": permission.group("permission"),
                      "condition_verified": False, "decision": "pending_human_decision"})
    for fact in facts:
        fact.update(protocol=PENDING_WORK_PROTOCOL,
                    source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    execution_authorized=False)
    return facts


def pending_work_inventory_is_bound(source: str, obligations: Any, *, authoring: bool) -> bool:
    """Require each recognized atom once as an exact-source human check.

    This does not rewrite model output or establish semantic completeness.
    Other local/external/unknown work cannot be smuggled through this channel.
    """
    facts = compile_pending_source_work(source)
    required = {f["code"]: f for f in facts}
    if not required or len(required) != len(facts) or not isinstance(obligations, list):
        return False
    if authoring:
        if SECTION_CHOICE in required or not is_explicit_authoring_content_quote(source):
            return False
    elif set(required) != {SECTION_CHOICE}:
        return False
    seen = set()
    authored = 0
    for item in obligations:
        if not isinstance(item, dict) or item.get("requirement_refs"):
            return False
        quote = item.get("source_quote")
        if not isinstance(quote, str) or not quote or source.count(quote) != 1:
            return False
        code = item.get("pending_work_code")
        if item.get("disposition") == "authoring_content_pending":
            if not authoring or code is not None or not is_explicit_authoring_content_quote(quote, source_text=source):
                return False
            authored += 1
            continue
        fact = required.get(code)
        if (item.get("disposition") != "source_content_verification_pending"
                or fact is None or code in seen
                or not isinstance(item.get("obligation_summary"), str)
                or not item["obligation_summary"].strip()):
            return False
        start = source.index(quote)
        if not (start <= fact["start"] and start + len(quote) >= fact["end"]):
            return False
        seen.add(code)
    return seen == set(required) and (authored > 0 if authoring else authored == 0)


def pending_work_action(code: str | None) -> str | None:
    return {
        ANTI_EXCERPT: "请人工核对该研究现状不是文献资料的简单摘录，记录分类、总结、归纳与引用的核验结果；未核验不得提交。",
        REPETITION_SCOPE: "请人工确认本部分已纳入重复率检查范围，保留查重范围和报告证据；系统不会代做查重或宣称通过。",
        SECTION_CHOICE: "请先人工核实原文条件是否成立，再决定保留或删除对应部分，并记录章节位置、依据和决定；系统不会自动删章。",
    }.get(code)
