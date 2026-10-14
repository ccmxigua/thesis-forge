"""Fail-closed binding for explicit source ranges used by format requirements.

The module does not interpret prose such as “the above rule”.  A reviewer must
select exact clause IDs; range forms are accepted only when their endpoints and
all clauses between them are present in the same extracted paragraph or table
row.  This keeps semantic ambiguity and parser limitations visible.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


_ANAPHORA = re.compile(
    r"(?:上述|前述|上文|前文|前项|前一项|本条|该条|前款|上述规定|前述要求|"
    r"对应填写(?:的)?内容|对应内容)"
)
_ORDINAL = re.compile(r"第[一二三四五六七八九十百零〇两\d]+(?:条|款|项)")
_FORWARD_REFERENCE = re.compile(r"(?:如下|下表|下图|下文|后文|见下|参见后)")
_SOURCE_RANGE = re.compile(r"(?P<left>[^，。；;：:（）()]+?)至(?P<right>[^，。；;：:（）()]+)")


def _clean_source_range_endpoint(value: str) -> str:
    value = re.split(
        r"(?:中文字体|西文字体|字体|字号|对齐|无缩进|单倍行距|标题及内容|标题|字段|内容|采用|使用|为|[，,。；;：:\s])",
        value,
        maxsplit=1,
    )[0].strip()
    return re.sub(r"(?:标题|字段|内容)$", "", value).strip()


def _source_range_records(text: str) -> list[dict[str, str]]:
    """Extract literal range endpoints and any explicit scope qualifier.

    A clause may state conditional alternatives in parentheses, such as one
    range for a master title page and another for a doctoral title page.  Keep
    every range and its qualifier so those alternatives remain visible instead
    of silently using only the first one.
    """
    results: list[dict[str, str]] = []
    for match in _SOURCE_RANGE.finditer(text):
        raw_left = match.group("left").strip()
        condition = None
        qualifier = re.match(
            r"(?P<condition>[^，。；;（）()]*?(?:扉页|封面))的(?P<endpoint>[^，。；;（）()]*)$",
            raw_left,
        )
        if qualifier:
            condition = qualifier.group("condition").strip()
            raw_left = qualifier.group("endpoint").strip()
        left = _clean_source_range_endpoint(raw_left)
        right = _clean_source_range_endpoint(match.group("right"))
        if left and right:
            record = {"left": left, "right": right}
            if condition:
                record["condition"] = condition
            if record not in results:
                results.append(record)
    return results


def _source_range_terms_all(text: str) -> list[tuple[str, str]]:
    return [(item["left"], item["right"]) for item in _source_range_records(text)]


def _source_range_terms(text: str) -> tuple[str, str] | None:
    """Compatibility helper for the single-range field-list compiler."""
    ranges = _source_range_terms_all(text)
    return ranges[0] if ranges else None


def _section_compatible(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Exclude provably cross-section candidates while retaining ambiguity.

    A current explicit section anchor requires the same explicit candidate
    anchor. With no current anchor, candidates stay diagnostic only; the code
    cannot infer that they are cross-section selections.
    """
    current_section = _section_marker(current)
    candidate_section = _section_marker(candidate)
    if current_section is not None:
        return current_section == candidate_section
    return True


def _adjacent_preceding_source_clauses(
    clause: dict[str, Any], clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the closest contiguous clause(s) in the same source paragraph.

    This narrows a diagnostic for “corresponding content” to the actual nearby
    antecedent.  It is evidence for a human, never an automatic scope choice.
    """
    current_span = clause.get("source_span")
    current_text = clause.get("source_evidence_text")
    if not isinstance(current_span, dict) or not isinstance(current_text, str):
        return []
    start = current_span.get("start_offset")
    if type(start) is not int:
        return []
    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in clauses:
        if item is clause or not _section_compatible(clause, item):
            continue
        span = item.get("source_span")
        if not isinstance(span, dict):
            continue
        if any(span.get(key) != current_span.get(key) for key in (
            "evidence_id", "source_sha256", "location",
        )):
            continue
        end = span.get("end_offset")
        if type(end) is not int or end > start:
            continue
        gap = current_text[end:start]
        if len(gap) > 2 or any(char not in "。；;，,：: \t\r\n" for char in gap):
            continue
        candidates.append((len(gap), item))
    if not candidates:
        return []
    closest_gap = min(gap for gap, _ in candidates)
    return [item for gap, item in candidates if gap == closest_gap]


def resolve_template_range_target(
    clause: dict[str, Any], *,
    official_template_evidence: dict[str, Any] | None,
    target_clauses: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Bind one explicit source range to unique, hash-matched template labels.

    Template labels are accepted only as endpoints when their evidence is
    explicitly attributed to one official-template source hash.  The target
    is reported absent only when a supplied target structure contains neither
    endpoint.  Partial or present targets remain unverified.
    """
    ranges = _source_range_terms_all(str(clause.get("text") or ""))
    if len(ranges) != 1:
        return {"status": "semantic_ambiguity" if ranges else "parser_capability_gap",
                "reason": "template_range_requires_one_explicit_endpoint_pair",
                "target_clause_ids": []}
    if not isinstance(official_template_evidence, dict):
        return {"status": "parser_capability_gap",
                "reason": "official_template_endpoint_evidence_not_supplied",
                "target_clause_ids": []}
    source = official_template_evidence.get("source")
    source_sha = source.get("sha256") if isinstance(source, dict) else None
    if (not isinstance(source, dict) or source.get("kind") != "official_template_docx"
            or not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha)):
        return {"status": "parser_capability_gap",
                "reason": "official_template_source_identity_unverified",
                "target_clause_ids": []}

    left, right = ranges[0]

    def endpoint_matches(endpoint: str) -> list[dict[str, Any]]:
        normalized_endpoint = _normalized_field_literal(endpoint)
        matches: list[dict[str, Any]] = []
        items = official_template_evidence.get("items")
        if not isinstance(items, list):
            return matches
        for item in items:
            if not isinstance(item, dict) or item.get("category") != "fixed_text":
                continue
            provenance = item.get("provenance")
            if (not isinstance(provenance, dict)
                    or provenance.get("source_kind") != "official_template_docx"
                    or provenance.get("source_sha256") != source_sha):
                continue
            literal = _normalized_field_literal(item.get("normalized_text") or item.get("value"))
            if not literal.startswith(normalized_endpoint):
                continue
            suffix = literal[len(normalized_endpoint):]
            if suffix and suffix[0] not in {":", "："}:
                continue
            location = item.get("source_location")
            if (not isinstance(location, dict)
                    or location.get("part") != "word/document.xml"
                    or type(location.get("paragraph_index")) is not int):
                continue
            matches.append(item)
        return matches

    left_matches = endpoint_matches(left)
    right_matches = endpoint_matches(right)
    if len(left_matches) != 1 or len(right_matches) != 1:
        return {
            "status": "semantic_ambiguity" if len(left_matches) > 1 or len(right_matches) > 1
            else "parser_capability_gap",
            "reason": "official_template_range_endpoints_not_unique",
            "field_labels": [left, right],
            "template_evidence_ids": [
                str(item.get("id")) for item in [*left_matches, *right_matches] if item.get("id")
            ],
            "template_source_sha256": source_sha,
            "target_clause_ids": [],
        }
    left_location = left_matches[0]["source_location"]
    right_location = right_matches[0]["source_location"]
    if left_location["paragraph_index"] >= right_location["paragraph_index"]:
        return {"status": "semantic_ambiguity",
                "reason": "official_template_range_endpoint_order_conflict",
                "field_labels": [left, right],
                "template_evidence_ids": [left_matches[0]["id"], right_matches[0]["id"]],
                "template_source_sha256": source_sha,
                "target_clause_ids": []}

    target_ids: list[str] = []
    observed: list[str] = []
    if target_clauses is not None:
        for item in target_clauses:
            if not isinstance(item, dict):
                continue
            text = _normalized_field_literal(item.get("text"))
            for endpoint in (left, right):
                normalized_endpoint = _normalized_field_literal(endpoint)
                if text.startswith(normalized_endpoint + ":") or text.startswith(normalized_endpoint + "："):
                    target_ids.append(str(item.get("id")))
                    observed.append(endpoint)
    absent = target_clauses is not None and not observed
    return {
        "status": "target_absent" if absent else "resolved",
        "reason": ("official_template_range_bound_but_no_endpoint_in_target"
                   if absent else "official_template_range_bound_target_not_absent_or_not_supplied"),
        "rule_interpretation_status": "resolved",
        "target_status": "target_absent" if absent else (
            "target_unobserved" if target_clauses is None else "target_present_unverified"
        ),
        "verification_status": "unverified",
        "field_labels": [left, right],
        "template_evidence_ids": [left_matches[0]["id"], right_matches[0]["id"]],
        "template_source_sha256": source_sha,
        "template_paragraph_indexes": [left_location["paragraph_index"], right_location["paragraph_index"]],
        "candidate_target_clause_ids": target_ids,
        "target_clause_ids": [],
    }


def _normalized_field_literal(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    return text.strip(" ：:。；;，,、")


def _section_marker(clause: dict[str, Any]) -> str | None:
    explicit = clause.get("source_section_marker")
    if explicit in {"cover", "title_page"}:
        return str(explicit)
    context = clause.get("context_before")
    if not isinstance(context, list):
        context = []
    for item in reversed(context):
        text = str(item or "")
        if "扉页" in text:
            return "title_page"
        if "封面" in text:
            return "cover"
    text = str(clause.get("text") or "")
    if "扉页" in text:
        return "title_page"
    if "封面" in text:
        return "cover"
    return None


def _ordered_field_list(clause: dict[str, Any]) -> list[str] | None:
    text = str(clause.get("text") or "")
    match = re.search(
        r"(?:自上而下|从上到下|自上至下|依次)(?:地)?(?:\s*为|\s*包括|\s*包含)?\s*[：:]?\s*(.+)",
        text,
    )
    if not match:
        return None
    body = re.split(
        r"[，,]\s*(?:最末|最后)(?:一行|一项|一条|一个字段)|[。；;]",
        match.group(1),
        maxsplit=1,
    )[0]
    values = [_normalized_field_literal(item) for item in re.split(r"[、，,；;]", body)]
    values = [item for item in values if item]
    return values if len(values) >= 2 and len(values) == len(set(values)) else None


def resolve_ordered_field_targets(
    clause: dict[str, Any], clauses: list[dict[str, Any]], role: str,
) -> dict[str, Any]:
    """Resolve a prose field range only when its ordered source list and target table agree.

    This compiler joins three observable facts: the requirement's endpoints or
    “corresponding contents” reference, one ordered field list in the same
    named document section, and one table column containing exactly that
    ordered label sequence.  Value roles select the unique peer cell on each
    selected label row.  It never chooses a table by ordinal position.
    """
    if role not in {"cover_field_label", "cover_field_value"}:
        return {"status": "unsupported_role", "target_clause_ids": []}
    section = _section_marker(clause)
    if section != "cover":
        return {
            "status": "parser_capability_gap",
            "reason": "ordered_field_range_requires_an_explicit_cover_section_anchor",
            "target_clause_ids": [],
        }
    range_terms = _source_range_terms(str(clause.get("text") or ""))
    list_candidates: list[tuple[dict[str, Any], list[str]]] = []
    for candidate in clauses:
        if candidate is clause or _section_marker(candidate) != section:
            continue
        labels = _ordered_field_list(candidate)
        if not labels:
            continue
        if range_terms:
            left, right = range_terms
            if labels.count(left) != 1 or labels.count(right) != 1:
                continue
            left_index, right_index = labels.index(left), labels.index(right)
            if left_index > right_index:
                continue
            labels = labels[left_index:right_index + 1]
        list_candidates.append((candidate, labels))
    if len(list_candidates) != 1:
        return {
            "status": "semantic_ambiguity" if len(list_candidates) > 1 else "parser_capability_gap",
            "reason": "ordered_field_list_not_unique" if len(list_candidates) > 1
            else "ordered_field_list_not_observable_in_current_section",
            "candidate_clause_ids": [str(item[0].get("id")) for item in list_candidates],
            "target_clause_ids": [],
        }
    list_clause, field_labels = list_candidates[0]

    table_cells: dict[tuple[str, str, int, int], dict[str, list[dict[str, Any]]]] = {}
    table_section_markers: dict[tuple[str, str, int], set[str]] = {}
    for item in clauses:
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        table_index, row, column = (
            location.get("table_child_index"), location.get("row"), location.get("column"),
        )
        if all(type(value) is int for value in (table_index, row, column)):
            source_document = str(item.get("source_document") or "")
            source_sha256 = str(item.get("source_document_sha256") or "")
            key = (source_document, source_sha256, table_index, column)
            table_cells.setdefault(key, {}).setdefault(str(row), []).append(item)
            marker = _section_marker(item)
            if marker:
                table_section_markers.setdefault(
                    (source_document, source_sha256, table_index), set(),
                ).add(marker)

    table_candidates: list[dict[str, Any]] = []
    for (source_document, source_sha256, table_index, label_column), rows in table_cells.items():
        # A table with the same labels on a title page must not be captured by
        # a cover-only rule. Require a single explicit section marker on the
        # table as a whole; missing or conflicting anchors remain unresolved.
        table_key = (source_document, source_sha256, table_index)
        if table_section_markers.get(table_key) != {section}:
            continue
        selected_labels: list[dict[str, Any]] = []
        selected_rows: list[int] = []
        valid = True
        for label in field_labels:
            matches = [
                item for row_items in rows.values() for item in row_items
                if _normalized_field_literal(item.get("text")) == label
            ]
            if len(matches) != 1:
                valid = False
                break
            location = matches[0].get("location") or {}
            selected_labels.append(matches[0])
            selected_rows.append(location.get("row"))
        if not valid or any(type(row) is not int for row in selected_rows):
            continue
        if selected_rows != sorted(selected_rows) or len(set(selected_rows)) != len(selected_rows):
            continue
        row_low, row_high = selected_rows[0], selected_rows[-1]
        observed_labels = []
        for row_number in sorted(int(value) for value in rows):
            if row_low <= row_number <= row_high:
                observed_labels.extend(
                    _normalized_field_literal(item.get("text"))
                    for item in rows[str(row_number)]
                    if _normalized_field_literal(item.get("text"))
                )
        if observed_labels != field_labels:
            continue
        target_items = selected_labels
        paired_labels: list[str] = []
        if role == "cover_field_value":
            target_items = []
            value_columns = sorted({
                column for (candidate_source, candidate_sha, candidate_table, column) in table_cells
                if (candidate_source, candidate_sha, candidate_table) == table_key
                and column != label_column
            })
            if not value_columns:
                continue
            for label_item, row_number in zip(selected_labels, selected_rows):
                peer_cells = [
                    item for value_column in value_columns
                    for item in table_cells[(source_document, source_sha256, table_index, value_column)].get(str(row_number), [])
                    if _normalized_field_literal(item.get("text"))
                ]
                if len(peer_cells) != 1:
                    valid = False
                    break
                target_items.append(peer_cells[0])
                paired_labels.append(str(label_item.get("id")))
            if not valid:
                continue
        table_candidates.append({
            "table_child_index": table_index,
            "label_column": label_column,
            "source_document": source_document or None,
            "source_document_sha256": source_sha256 or None,
            "field_labels": field_labels,
            "label_clause_ids": [str(item.get("id")) for item in selected_labels],
            "target_clause_ids": [str(item.get("id")) for item in target_items],
            "paired_label_clause_ids": paired_labels,
        })

    if len(table_candidates) != 1:
        status = "semantic_ambiguity" if len(table_candidates) > 1 else "target_absent"
        return {
            "status": status,
            "reason": "multiple_tables_match_ordered_field_range" if len(table_candidates) > 1
            else "rule_and_field_list_resolved_but_target_table_absent",
            "field_labels": field_labels,
            "field_list_clause_ids": [str(list_clause.get("id"))],
            "candidate_table_child_indexes": [item["table_child_index"] for item in table_candidates],
            "target_clause_ids": [],
        }
    resolved = table_candidates[0]
    return {
        "status": "resolved",
        "reason": "unique_ordered_field_list_and_matching_table_column",
        "field_list_clause_ids": [str(list_clause.get("id"))],
        "field_labels": field_labels,
        "target_kind": role,
        **resolved,
    }


def diagnose_source_scope_reference(
    clause: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Describe an unresolved prose reference without selecting its target."""
    text = str(clause.get("text") or "")
    source_text = clause.get("source_evidence_text")
    source_text = source_text if isinstance(source_text, str) else text
    ordinal_terms = list(dict.fromkeys(_ORDINAL.findall(text)))
    anaphoric = bool(_ANAPHORA.search(text))
    forward = bool(_FORWARD_REFERENCE.search(text))
    source_range_records = _source_range_records(text)
    source_ranges = [(item["left"], item["right"]) for item in source_range_records]
    if not source_ranges and source_text != text:
        candidate_range_records = _source_range_records(source_text)
        candidate_ranges = [(item["left"], item["right"]) for item in candidate_range_records]
        source_ranges = [
            pair for pair in candidate_ranges
            if any(term and term in text for term in pair)
        ]
        source_range_records = [
            item for item in candidate_range_records
            if (item["left"], item["right"]) in source_ranges
        ]
    source_range = source_ranges[0] if len(source_ranges) == 1 else None
    if not (ordinal_terms or anaphoric or forward or source_ranges):
        return None

    current_index = next((i for i, item in enumerate(clauses) if item is clause), None)
    candidates: list[dict[str, Any]] = []
    if ordinal_terms:
        for item in clauses:
            if item is clause or not _section_compatible(clause, item):
                continue
            item_text = str(item.get("text") or "")
            matched = [term for term in ordinal_terms if term in item_text]
            if matched:
                candidates.append({
                    "clause_id": item.get("id"),
                    "text": item_text,
                    "evidence_ids": list(item.get("evidence_ids") or []),
                    "location": dict(item.get("location") or {}),
                    "matched_terms": matched,
                })
    elif len(source_ranges) > 1:
        # Multiple explicit ranges in one rule usually encode conditional
        # variants.  Keep the literal alternatives rather than choosing one.
        pass
    elif source_range:
        endpoint_terms = source_range
        # Endpoint labels sometimes have a semantic suffix in the requirement
        # prose (e.g. “指导教师标题”).  Match the stripped literal endpoint
        # against current extracted clauses for diagnosis only.
        for item in clauses:
            if item is clause or not _section_compatible(clause, item):
                continue
            item_text = str(item.get("text") or "")
            matched = [term for term in endpoint_terms if term and term in item_text]
            if matched:
                candidates.append({
                    "clause_id": item.get("id"),
                    "text": item_text,
                    "evidence_ids": list(item.get("evidence_ids") or []),
                    "location": dict(item.get("location") or {}),
                    "matched_terms": matched,
                })
    elif anaphoric and current_index is not None:
        adjacent = _adjacent_preceding_source_clauses(clause, clauses)
        preceding = adjacent or [
            item for item in clauses[max(0, current_index - 6):current_index]
            if _section_compatible(clause, item)
        ]
        # The closest source-span adjacency gives a precise candidate, but is
        # still only diagnostic evidence and never automatically binds scope.
        for item in preceding:
            antecedent_ranges = _source_range_records(str(item.get("text") or ""))
            candidates.append({
                "clause_id": item.get("id"),
                "text": str(item.get("text") or ""),
                "evidence_ids": list(item.get("evidence_ids") or []),
                "location": dict(item.get("location") or {}),
                **({"antecedent_source_ranges": [
                        dict(record) for record in antecedent_ranges
                ]} if antecedent_ranges else {}),
            })
    elif forward and current_index is not None:
        for item in clauses[current_index + 1:current_index + 7]:
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            if item.get("source_kind") == "table_cell" or location.get("table_child_index") is not None:
                candidates.append({
                    "clause_id": item.get("id"),
                    "text": str(item.get("text") or ""),
                    "evidence_ids": list(item.get("evidence_ids") or []),
                    "location": dict(location),
                })

    if len(source_ranges) > 1:
        classification = "semantic_ambiguity"
        reason = "source_clause_contains_conditional_range_alternatives"
    elif anaphoric and any(
        len(item.get("antecedent_source_ranges", [])) > 1 for item in candidates
    ):
        classification = "semantic_ambiguity"
        reason = "anaphoric_antecedent_contains_conditional_range_alternatives"
    elif source_range and candidates:
        matched_counts: dict[str, int] = {}
        for candidate in candidates:
            for term in candidate.get("matched_terms", []):
                matched_counts[str(term)] = matched_counts.get(str(term), 0) + 1
        if any(count > 1 for count in matched_counts.values()):
            classification = "semantic_ambiguity"
            reason = "multiple_source_anchors_match_range_endpoint"
        else:
            classification = "human_confirmation_required"
            reason = "source_range_requires_explicit_target_clause_set"
    elif len(candidates) > 1:
        classification = "semantic_ambiguity"
        reason = "multiple_source_anchors_match_reference"
    elif forward and not candidates:
        classification = "parser_capability_gap"
        reason = "forward_target_not_structurally_observable"
    elif anaphoric:
        classification = "human_confirmation_required"
        reason = "anaphoric_reference_requires_explicit_anchor"
    elif ordinal_terms and len(candidates) == 1:
        classification = "human_confirmation_required"
        reason = "ordinal_reference_requires_explicit_anchor"
    elif source_ranges:
        classification = "parser_capability_gap"
        reason = "source_range_endpoints_not_observable_in_current_extraction"
    else:
        classification = "parser_capability_gap"
        reason = "source_reference_not_resolved_by_supported_scope_grammar"
    return {
        "status": "pending_input",
        "classification": classification,
        "reason": reason,
        "source_clause_id": clause.get("id"),
        "source_text": text,
        "reference_terms": ordinal_terms + (["anaphora"] if anaphoric else [])
        + (["forward_reference"] if forward else [])
        + (["source_range"] if source_ranges else []),
        "source_ranges": [
            dict(item) for item in source_range_records
        ],
        "candidate_clause_ids": [str(item["clause_id"]) for item in candidates
                                 if isinstance(item.get("clause_id"), str)],
        "candidates": candidates,
        "resolution_policy": "human_selects_exact_clause_ids_before_execution",
    }


def _clause_span(clause: dict[str, Any]) -> tuple[str, int, int, str, dict[str, Any]] | None:
    span = clause.get("source_span")
    if not isinstance(span, dict):
        return None
    evidence_id = span.get("evidence_id")
    start, end = span.get("start_offset"), span.get("end_offset")
    source_hash, source_text = span.get("source_sha256"), span.get("text")
    location = span.get("location") or clause.get("location")
    if (not isinstance(evidence_id, str) or type(start) is not int or type(end) is not int
            or start < 0 or end <= start or not isinstance(source_hash, str)
            or not isinstance(source_text, str) or not isinstance(location, dict)):
        return None
    full_text = clause.get("source_evidence_text")
    if not isinstance(full_text, str) or full_text[start:end] != source_text:
        return None
    digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    if digest != source_hash:
        return None
    return evidence_id, start, end, source_hash, dict(location)


def validate_source_scope_binding(
    scope: dict[str, Any], clauses: list[dict[str, Any]], *,
    cited_clause_ids: set[str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Validate an explicit source selector and return its code-owned projection."""
    if not isinstance(scope, dict):
        return ["source_scope_must_be_object"], None
    if "anchor_clause_ids" not in scope:
        return ["source_scope_anchor_clause_ids_required"], None
    kind = scope.get("kind")
    targets = scope.get("target_clause_ids")
    anchors = scope.get("anchor_clause_ids", [])
    if kind not in {"explicit_clause_set", "same_paragraph_range", "same_table_row_range"}:
        return ["source_scope_kind_unsupported"], None
    if (not isinstance(targets, list) or not targets
            or any(not isinstance(value, str) or not value for value in targets)
            or len(targets) != len(set(targets))):
        return ["source_scope_target_clause_ids_invalid"], None
    if (not isinstance(anchors, list)
            or any(not isinstance(value, str) or not value for value in anchors)
            or len(anchors) != len(set(anchors))):
        return ["source_scope_anchor_clause_ids_invalid"], None
    if kind == "explicit_clause_set" and anchors:
        return ["explicit_clause_set_must_not_claim_range_anchors"], None
    if kind != "explicit_clause_set" and len(anchors) != 2:
        return ["source_scope_range_requires_two_anchors"], None
    by_id = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    referenced = set(targets) | set(anchors)
    errors: list[str] = []
    if not referenced <= set(by_id):
        errors.append("source_scope_references_unknown_clause")
    if cited_clause_ids is not None and not referenced <= cited_clause_ids:
        errors.append("source_scope_references_uncited_clause")
    if errors:
        return errors, None
    spans: dict[str, tuple[str, int, int, str, dict[str, Any]]] = {}
    for clause_id in referenced:
        span = _clause_span(by_id[clause_id])
        if span is None:
            errors.append(f"source_scope_clause_span_unverifiable:{clause_id}")
        else:
            spans[clause_id] = span
    if errors:
        return errors, None

    resolved_targets = list(targets)
    if kind == "same_paragraph_range":
        start_anchor, end_anchor = anchors
        start_span, end_span = spans[start_anchor], spans[end_anchor]
        if (start_span[0] != end_span[0] or start_span[4] != end_span[4]
                or start_span[1] > end_span[1]):
            return ["source_scope_paragraph_range_anchors_do_not_share_ordered_paragraph"], None
        expected = [
            str(item["id"]) for item in clauses
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and (candidate := _clause_span(item)) is not None
            and candidate[0] == start_span[0]
            and candidate[4] == start_span[4]
            and start_span[1] <= candidate[1] and candidate[2] <= end_span[2]
        ]
        if resolved_targets != expected or not expected:
            return ["source_scope_paragraph_range_target_set_mismatch"], None
    elif kind == "same_table_row_range":
        start_anchor, end_anchor = anchors
        start_span, end_span = spans[start_anchor], spans[end_anchor]
        start_loc, end_loc = start_span[4], end_span[4]
        row_keys = ("table_child_index", "row")
        if (not all(key in start_loc and key in end_loc for key in row_keys)
                or any(start_loc[key] != end_loc[key] for key in row_keys)
                or not all(type(start_loc.get(key)) is int for key in ("column", "paragraph"))
                or not all(type(end_loc.get(key)) is int for key in ("column", "paragraph"))):
            return ["source_scope_table_row_range_anchor_location_unverifiable"], None
        start_position = (start_loc["column"], start_loc["paragraph"], start_span[1])
        end_position = (end_loc["column"], end_loc["paragraph"], end_span[2])
        if start_position > end_position:
            return ["source_scope_table_row_range_anchors_not_ordered"], None
        selected = []
        for item in clauses:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            candidate = _clause_span(item)
            if candidate is None or candidate[0] != start_span[0]:
                # Evidence IDs differ per cell, so row membership is determined
                # by the extracted table/row locator below.
                candidate = _clause_span(item)
            if candidate is None:
                continue
            loc = candidate[4]
            if any(loc.get(key) != start_loc.get(key) for key in row_keys):
                continue
            if not all(type(loc.get(key)) is int for key in ("column", "paragraph")):
                return ["source_scope_table_row_target_location_unverifiable"], None
            position = (loc["column"], loc["paragraph"], candidate[1])
            candidate_end_position = (loc["column"], loc["paragraph"], candidate[2])
            if start_position <= position and candidate_end_position <= end_position:
                selected.append(str(item["id"]))
        if resolved_targets != selected or not selected:
            return ["source_scope_table_row_range_target_set_mismatch"], None

    source_spans = [
        {"clause_id": clause_id, "evidence_id": spans[clause_id][0],
         "start_offset": spans[clause_id][1], "end_offset": spans[clause_id][2],
         "source_sha256": spans[clause_id][3], "location": spans[clause_id][4]}
        for clause_id in resolved_targets
    ]
    return [], {
        "kind": kind,
        "anchor_clause_ids": list(anchors),
        "target_clause_ids": resolved_targets,
        "target_spans": source_spans,
    }
