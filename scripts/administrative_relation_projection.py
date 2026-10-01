"""Conservative, coverage-preserving cleanup of copied administrative payloads.

This is not an external-action classifier or a general requirement merger.
Unique DOCX operations and all primary obligation inventories stay untouched.
The caller must authenticate validator feedback and run independent review.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from native_semantic_review import NativeSemanticReviewError, _exact_clause_source_text
from semantic_contract import sha256_json
from source_obligation_compiler import (
    PUBLICATION_DEFAULT_OBLIGATION_ID,
    compile_known_source_obligation_ids,
    has_mixed_external_document_action_signal,
)


POLICY = "source_bound_administrative_copy_projection_v1"
EXECUTABLE = {"covered", "executable", "verify_existing"}


def project_copied_administrative_qualifiers(
    response: dict[str, Any], chunk: dict[str, Any], *,
    validate: Callable[[dict[str, Any], dict[str, Any]], list[str]],
    model_retry_response: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Remove a misbound qualifier copy, never its requirement or source duty.

    The qualifier must already survive on exactly one fully source-bound
    administrative table containing the same current table-cell edges. Both
    requirements keep their identity, prerequisites, checks and applicability.
    No requirement merge or relation addition is authorized by this rule.
    Different exceptions disable mechanical cleanup. An authenticated primary
    retry may instead propose removal at these exact validator-named fields;
    the caller must preserve the parent and re-run independent semantic review.
    """
    if (response.get("contract_version") != "3.0" or response.get("conflicts")
            or response.get("reported_conflicts")):
        return None, []
    requirements, clauses, reviews, evidence = (response.get("requirements"),
        chunk.get("clauses"), response.get("clause_reviews"), chunk.get("evidence_context"))
    provenance = chunk.get("provenance")
    if (not all(isinstance(x, list) and x for x in (requirements, clauses, reviews))
            or not isinstance(evidence, dict) or not isinstance(provenance, dict)
            or not isinstance(provenance.get("run_id"), str) or not provenance["run_id"]
            or any(not isinstance(provenance.get(k), str)
                   or re.fullmatch(r"[0-9a-f]{64}", provenance[k]) is None
                   for k in ("source_sha256", "clause_sha256", "evidence_sha256", "request_sha256"))):
        return None, []
    if "provenance" in response and response["provenance"] != provenance:
        return None, []
    by_id = {c.get("id"): c for c in clauses if isinstance(c, dict)}
    review_by_id = {r.get("clause_id"): r for r in reviews if isinstance(r, dict)}
    if (len(by_id) != len(clauses) or len(review_by_id) != len(reviews)
            or set(by_id) != set(review_by_id)
            or any(not isinstance(cid, str) or not cid for cid in by_id)):
        return None, []
    try:
        for clause in by_id.values():
            _exact_clause_source_text(clause, evidence)
    except (NativeSemanticReviewError, ValueError, KeyError, TypeError):
        return None, []
    for clause in clauses:
        span = clause.get("source_span", {})
        record = evidence.get(span.get("evidence_id"), {})
        if (not isinstance(span.get("location"), dict)
                or span["location"] != record.get("location")
                or clause.get("location", span["location"]) != span["location"]):
            return None, []
    errors = validate(response, chunk)
    prefix = r"\$\.requirements\[(\d+)\]\.properties\.non_public_administration"
    pattern = re.compile(prefix + r"\.(publication_default_policy|security_marking_options\[(\d+)\]\.shorter_duration_allowed): (must_be_bound_to_exact_linked_source_clause|must_be_bound_to_linked_source_clause)$")
    targets: dict[int, list[tuple[str, int | None]]] = {}
    for error in errors:
        match = pattern.fullmatch(error)
        if match is None:
            return None, []
        index = int(match[1])
        if index >= len(requirements):
            return None, []
        targets.setdefault(index, []).append((match[2], int(match[3]) if match[3] else None))
    if not targets:
        return None, []

    def links(req):
        ids, eids = req.get("clause_ids"), req.get("evidence_ids")
        if (not isinstance(ids, list) or not ids or any(cid not in by_id for cid in ids)
                or len(set(ids)) != len(ids) or not isinstance(eids, list)
                or len(set(eids)) != len(eids)
                or set(eids) != {eid for cid in ids for eid in by_id[cid].get("evidence_ids", [])}):
            return None
        return ids

    def admin(req):
        props = req.get("properties")
        value = props.get("non_public_administration") if isinstance(props, dict) else None
        return value if req.get("role") == "cover" and isinstance(value, dict) else None

    def typed_scope(value):
        keys = ("status", "conditions") if model_retry_response is not None else ("status", "conditions", "exceptions")
        return ({key: value.get(key) for key in keys}
                if isinstance(value, dict) else value)

    candidate = copy.deepcopy(response)
    repairs = []
    for index, fields in targets.items():
        duplicate = requirements[index]
        copied = admin(duplicate) if isinstance(duplicate, dict) else None
        ids = links(duplicate) if copied else None
        if not ids or not copied.get("fields"):
            return None, []
        if model_retry_response is not None:
            model_requirements = model_retry_response.get("requirements")
            if (not isinstance(model_requirements, list) or len(model_requirements) != len(requirements)
                    or any(not isinstance(a, dict) or not isinstance(b, dict)
                           or any(a.get(k) != b.get(k) for k in ("role", "field_key", "existing_requirement_id"))
                           for a, b in zip(requirements, model_requirements))):
                return None, []
            proposed = admin(model_requirements[index])
            if not proposed:
                return None, []
            for field, option_index in fields:
                if option_index is None:
                    if proposed.get(field) is not None:
                        return None, []
                else:
                    proposed_options = proposed.get("security_marking_options")
                    if (not isinstance(proposed_options, list) or not isinstance(copied.get("security_marking_options"), list)
                            or len(proposed_options) != len(copied["security_marking_options"])
                            or not isinstance(proposed_options[option_index], dict)
                            or proposed_options[option_index].get("shorter_duration_allowed") is not None
                            or {k: v for k, v in proposed_options[option_index].items() if k != "shorter_duration_allowed"}
                               != {k: v for k, v in copied["security_marking_options"][option_index].items() if k != "shorter_duration_allowed"}):
                        return None, []
        for cid in ids:
            review = review_by_id[cid]
            atoms = review.get("obligations")
            if (review.get("classification") not in EXECUTABLE
                    or not isinstance(atoms, list) or not atoms
                    or any(not isinstance(a, dict) or a.get("status") != "covered" for a in atoms)):
                return None, []
        locations = [by_id[cid]["source_span"]["location"] for cid in ids]
        tables = {(loc.get("part"), loc.get("table_child_index")) for loc in locations}
        if (len(tables) != 1 or any(not isinstance(loc.get("part"), str)
                or type(loc.get("table_child_index")) is not int for loc in locations)):
            return None, []
        options = copied.get("security_marking_options")
        anchors = []
        for other_index, req in enumerate(requirements):
            if other_index == index or other_index in targets or not isinstance(req, dict):
                continue
            retained, retained_ids = admin(req), links(req)
            if (not retained or not retained_ids or not set(ids) <= set(retained_ids)
                    or not _contained(copied["fields"], retained.get("fields"), ".fields")
                    or typed_scope(copied.get("applicability")) != typed_scope(retained.get("applicability"))
                    or typed_scope(duplicate.get("applicability")) != typed_scope(req.get("applicability"))):
                continue
            retained_tables = {(by_id[cid]["source_span"]["location"].get("part"),
                                by_id[cid]["source_span"]["location"].get("table_child_index"))
                               for cid in retained_ids
                               if "table_child_index" in by_id[cid]["source_span"]["location"]}
            if retained_tables != tables:
                continue
            bound = True
            for field, option_index in fields:
                if option_index is None:
                    bound = bound and copied.get(field) == retained.get(field) == "unapproved_is_public"
                else:
                    retained_options = retained.get("security_marking_options")
                    bound = bound and isinstance(options, list) and isinstance(retained_options, list)
                    if bound:
                        bound = (option_index < len(options) and isinstance(options[option_index], dict)
                                 and options[option_index].get("shorter_duration_allowed") is True
                                 and sum(o == options[option_index] for o in retained_options) == 1)
            if bound:
                anchors.append(other_index)
        if len(anchors) != 1:
            return None, []
        projected = candidate["requirements"][index]["properties"]["non_public_administration"]
        removed = []
        for field, option_index in fields:
            if option_index is None:
                removed.append({"path": field, "value": projected.pop(field)})
            else:
                removed.append({"path": field, "value": projected["security_marking_options"][option_index].pop("shorter_duration_allowed")})
        repairs.append({"rule_id": "source_bound_copied_administrative_qualifier_v1",
            "code": "cover_binding_violation", "requirement_index": index,
            "retained_requirement_index": anchors[0], "removed_copies": removed,
            "source_spans": {cid: copy.deepcopy(by_id[cid]["source_span"])
                             for cid in requirements[anchors[0]]["clause_ids"]},
            "source_response_sha256": sha256_json(response),
            "source_chunk_sha256": sha256_json(chunk), "provenance": copy.deepcopy(provenance),
            "primary_proposal_sha256": sha256_json(model_retry_response) if model_retry_response is not None else None,
            "exception_equivalence_asserted": model_retry_response is None,
            "requirement_relations_unchanged": True, "clause_reviews_unchanged": True,
            "independent_review_required": True, "submission_ready": False})
    if validate(candidate, chunk):
        return None, []
    for repair in repairs:
        repair["repaired_response_sha256"] = sha256_json(candidate)
    return candidate, repairs


def _contained(value: Any, retained: Any, path: str = "") -> bool:
    """Require exact values, with ordered field-subsets in the same region.

    Field order is relative to the retained full table, not the copied subset.
    No other numeric value, array order, empty scalar or unknown key is ignored.
    """
    if value is None:
        return True
    if isinstance(value, dict):
        return isinstance(retained, dict) and all(
            key in retained and _contained(item, retained[key], path + "." + key)
            for key, item in value.items()
        )
    if isinstance(value, list):
        if not isinstance(retained, list):
            return False
        if not value:
            return True
        if not path.endswith(".fields"):
            return value == retained
        indexes = []
        for field in value:
            if not isinstance(field, dict) or type(field.get("order")) is not int:
                return False
            semantic = {key: item for key, item in field.items() if key != "order"}
            matches = [i for i, other in enumerate(retained) if isinstance(other, dict)
                       and semantic == {k: v for k, v in other.items() if k != "order"}]
            if len(matches) != 1:
                return False
            indexes.append(matches[0])
        return indexes == sorted(set(indexes)) and [f["order"] for f in value] == sorted(
            set(f["order"] for f in value)
        )
    return type(value) is type(retained) and value == retained


def project_administrative_copies(
    response: dict[str, Any], chunk: dict[str, Any], *,
    validate: Callable[[dict[str, Any], dict[str, Any]], list[str]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Keep a unique source-backed table; remove only its redundant copies.

    A missing default-public edge can be completed only from the unique exact
    operative source sentence physically between this table's heading and its
    first row. No classification, obligation, property or verification is fixed.
    A complete local contract pass is required even for this partial repair.
    """
    if (response.get("contract_version") != "3.0" or response.get("conflicts")
            or response.get("reported_conflicts")):
        return None, []
    clauses, reviews, requirements, evidence = (
        chunk.get("clauses"), response.get("clause_reviews"),
        response.get("requirements"), chunk.get("evidence_context"),
    )
    if not all(isinstance(x, list) and x for x in (clauses, reviews, requirements)) or not isinstance(evidence, dict):
        return None, []
    by_id = {x.get("id"): x for x in clauses if isinstance(x, dict)}
    review_by_id = {x.get("clause_id"): x for x in reviews if isinstance(x, dict)}
    if (len(by_id) != len(clauses) or len(review_by_id) != len(reviews)
            or set(by_id) != set(review_by_id)
            or any(not isinstance(key, str) or not key for key in by_id)):
        return None, []
    try:
        texts = {key: _exact_clause_source_text(clause, evidence) for key, clause in by_id.items()}
    except (NativeSemanticReviewError, ValueError, KeyError, TypeError):
        return None, []
    for clause in by_id.values():
        span = clause.get("source_span", {})
        record = evidence.get(span.get("evidence_id"), {})
        if (span.get("location") != record.get("location")
                or clause.get("location", span.get("location")) != span.get("location")):
            return None, []

    def linked(req: dict[str, Any]) -> list[str] | None:
        ids, eids = req.get("clause_ids"), req.get("evidence_ids")
        if (not isinstance(ids, list) or not ids or any(not isinstance(cid, str) for cid in ids)
                or len(set(ids)) != len(ids) or any(cid not in by_id for cid in ids)
                or not isinstance(eids, list) or any(not isinstance(eid, str) for eid in eids)
                or len(set(eids)) != len(eids)
                or set(eids) != {eid for cid in ids for eid in by_id[cid].get("evidence_ids", [])}):
            return None
        return ids

    def executable(cid: str) -> bool:
        review = review_by_id[cid]
        obligations = review.get("obligations")
        return (review.get("classification") in EXECUTABLE
                and isinstance(obligations, list) and bool(obligations)
                and all(isinstance(item, dict) and item.get("status") == "covered" for item in obligations))

    def identity_free(req: dict[str, Any]) -> bool:
        return (set(req) <= {"role", "properties", "clause_ids", "evidence_ids", "reason", "confidence",
                            "verification", "existing_requirement_id", "field_key", "source_fragment_clause_ids",
                            "input_prerequisites", "applicability"}
                and req.get("existing_requirement_id") is None and req.get("field_key") is None
                and req.get("source_fragment_clause_ids") is None
                and req.get("input_prerequisites") in (None, [])
                and req.get("applicability") is None)

    def local_verification(verification: Any) -> bool:
        # The existing source compiler escalates the administrative checker
        # to external. That mode alone does not reclassify an executable table
        # as a pending real-world action. Keep the checker and all its checks.
        return (isinstance(verification, dict) and (
            verification.get("mode") == "static_docx"
            or (verification.get("mode") == "external"
                and verification.get("checker_ids") == ["cover_non_public_administration"])))

    anchors = []
    for index, req in enumerate(requirements):
        if not isinstance(req, dict) or req.get("role") != "cover" or not identity_free(req):
            continue
        ids = linked(req)
        props = req.get("properties")
        admin = props.get("non_public_administration") if isinstance(props, dict) else None
        verification = req.get("verification")
        if (not ids or not all(executable(cid) for cid in ids)
                or not isinstance(admin, dict) or not isinstance(admin.get("fields"), list)
                or not admin["fields"] or not local_verification(verification)):
            continue
        heading_ids = [cid for cid in ids if texts[cid] == admin.get("source_region")]
        table_locations = [by_id[cid]["source_span"].get("location", {}) for cid in ids
                           if "table_child_index" in by_id[cid]["source_span"].get("location", {})]
        if len(heading_ids) != 1 or not table_locations:
            continue
        heading = by_id[heading_ids[0]]["source_span"].get("location", {})
        table_ids = {loc.get("table_child_index") for loc in table_locations}
        if (len(table_ids) != 1 or type(heading.get("child_index")) is not int
                or not isinstance(heading.get("part"), str)
                or any(loc.get("part") != heading.get("part") for loc in table_locations)
                or any(type(loc.get("table_child_index")) is not int for loc in table_locations)
                or heading["child_index"] >= min(table_ids)):
            continue
        fields = [*(props.get("fields") or []), *admin["fields"]]
        if any(not isinstance(field, dict) or not isinstance(field.get("label"), str)
               or not any(texts[cid] == field["label"] for cid in ids) for field in fields):
            continue
        anchors.append((index, ids, heading, next(iter(table_ids))))
    # Two tables/possible scopes are a semantic choice, never first-match wins.
    if len(anchors) != 1:
        return None, []
    anchor_index, anchor_ids, heading, table_index = anchors[0]
    anchor = requirements[anchor_index]
    admin = anchor["properties"]["non_public_administration"]
    if admin.get("publication_default_policy") != "unapproved_is_public" or admin.get("public_policy") != "blank":
        return None, []
    # Duplicate region headings anywhere in the packet invalidate location.
    if sum(text == admin.get("source_region") for text in texts.values()) != 1:
        return None, []
    policy_ids = []
    for cid, clause in by_id.items():
        loc = clause["source_span"].get("location", {})
        if (executable(cid) and PUBLICATION_DEFAULT_OBLIGATION_ID in compile_known_source_obligation_ids(texts[cid])
                and loc.get("part") == heading.get("part") and type(loc.get("child_index")) is int
                and heading["child_index"] < loc["child_index"] < table_index):
            policy_ids.append(cid)
    if len(policy_ids) != 1:
        return None, []
    policy_location = by_id[policy_ids[0]]["source_span"]["location"]
    policy_evidence_ids = set(by_id[policy_ids[0]]["evidence_ids"])
    if (policy_location["child_index"] != heading["child_index"] + 1
            or table_index != policy_location["child_index"] + 1):
        return None, []
    # Physical proximity alone does not authorize crossing another heading,
    # exception or unknown instruction between the heading and table.
    for cid, clause in by_id.items():
        loc = clause["source_span"].get("location", {})
        if (loc.get("part") == heading.get("part") and type(loc.get("child_index")) is int
                and heading["child_index"] < loc["child_index"] < table_index
                and cid not in policy_ids
                and (review_by_id[cid].get("classification") != "external_compliance"
                     or set(clause.get("evidence_ids", [])) != policy_evidence_ids)):
            return None, []
    candidate = copy.deepcopy(response)
    added_ids = [cid for cid in policy_ids if cid not in anchor_ids]
    retained = candidate["requirements"][anchor_index]
    retained["clause_ids"].extend(added_ids)
    for cid in added_ids:
        for eid in by_id[cid]["evidence_ids"]:
            if eid not in retained["evidence_ids"]:
                retained["evidence_ids"].append(eid)
    removed = []
    for index, req in enumerate(requirements):
        if index == anchor_index or not isinstance(req, dict) or req.get("role") != "cover" or not identity_free(req):
            continue
        ids = linked(req)
        verification = req.get("verification")
        if not ids or not isinstance(verification, dict):
            continue
        external = (verification.get("mode") == "external" and all(
            review_by_id[cid].get("classification") == "external_compliance"
            and isinstance(review_by_id[cid].get("obligations"), list)
            and bool(review_by_id[cid]["obligations"])
            and all(isinstance(item, dict) and item.get("status") == "unverifiable"
                    for item in review_by_id[cid]["obligations"])
            and not compile_known_source_obligation_ids(texts[cid])
            and not has_mixed_external_document_action_signal(texts[cid])
            for cid in ids
        ))
        duplicate = (local_verification(verification)
                     and set(ids) <= set(retained["clause_ids"])
                     and all(executable(cid) for cid in ids)
                     and req.get("properties") == anchor.get("properties"))
        if not (external or duplicate) or not _contained(req.get("properties"), anchor.get("properties")):
            continue
        if external:
            # The same physical region must enclose every pending action.
            if any(by_id[cid]["source_span"].get("location", {}).get("part") != heading.get("part")
                   or type(by_id[cid]["source_span"].get("location", {}).get("child_index")) is not int
                   or not (
                       (by_id[cid]["source_span"]["location"]["child_index"] == policy_location["child_index"]
                        and set(by_id[cid]["evidence_ids"]) == policy_evidence_ids)
                       or by_id[cid]["source_span"]["location"]["child_index"] == table_index + 1)
                   for cid in ids):
                continue
        removed.append(index)
    if not added_ids and not removed:
        return None, []
    candidate["requirements"] = [req for index, req in enumerate(candidate["requirements"]) if index not in removed]
    # Do not conceal a unique erroneous payload or any independent blocker.
    if validate(candidate, chunk):
        return None, []
    return candidate, [{
        "rule_id": POLICY, "code": "non_requirement_classification_relation",
        "json_pointer": "$.requirements", "retained_original_index": anchor_index,
        "retained_candidate_index": anchor_index - sum(i < anchor_index for i in removed),
        "added_clause_ids": added_ids, "removed_indexes": removed,
        "removed_requirements": [copy.deepcopy(requirements[i]) for i in removed],
        "source_response_sha256": sha256_json(response),
        "repaired_response_sha256": sha256_json(candidate),
        "source_chunk_sha256": sha256_json(chunk),
        "provenance": copy.deepcopy(chunk.get("provenance")),
        "source_spans": {cid: copy.deepcopy(by_id[cid]["source_span"]) for cid in by_id},
        "properties_unchanged": retained["properties"] == anchor["properties"],
        "clause_reviews_unchanged": candidate["clause_reviews"] == response["clause_reviews"],
        "external_actions_remain_pending": True, "independent_review_required": True,
    }]
