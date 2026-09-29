"""Deterministic clause-to-requirement ledger for accepted host reviews.

This module records relationships that are present in the accepted response;
it never infers semantic identity, continuity, or missing obligations.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from compliance import classification_requires_requirement, normalized_state
from format_spec_validation import load_and_validate
from host_review_contract import derived_requirement_indexes
from semantic_contract import sha256_json
from source_obligation_compiler import compile_known_source_obligations


def _stable_requirement_id(requirement: dict[str, Any]) -> str:
    """Derive an ID from the accepted contract, never from array position.

    The model may propose a legacy ``existing_requirement_id``, but that value
    is retained only as source metadata.  The ledger's relation graph uses a
    deterministic ID owned by this code so reordering a response cannot move
    an edge to a different requirement.
    """
    canonical = {
        "role": requirement.get("role"),
        "field_key": requirement.get("field_key"),
        "properties": requirement.get("properties") or {},
        "clause_ids": sorted(str(value) for value in (requirement.get("clause_ids") or [])),
        "evidence_ids": sorted(str(value) for value in (requirement.get("evidence_ids") or [])),
        "applicability": requirement.get("applicability"),
        "input_prerequisites": requirement.get("input_prerequisites"),
        "verification": requirement.get("verification"),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"R{digest[:16]}"


def _canonical_requirement(requirement: dict[str, Any]) -> str:
    """Return the exact code-owned identity used for duplicate detection."""
    return json.dumps(requirement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _shadow_node_id(kind: str, response_sha256: str, identity: Any) -> str:
    digest = sha256_json({
        "protocol": "obligation_shadow_graph_v1",
        "response_sha256": response_sha256,
        "kind": kind,
        "identity": identity,
    })
    return f"SG-{kind}-{digest[:24]}"


_REVIEW_PRIORITY_BY_STATE = {
    "unresolved": 100,
    "missing": 100,
    "failed": 100,
    "unsupported_backend": 90,
    "unverifiable": 90,
    "external_compliance": 85,
    "requires_metadata": 80,
    "requires_source_content": 80,
    "input_provided_unverified": 80,
    "pending_execution": 60,
    "generated_and_verified": 30,
    "verified_existing": 30,
    "not_applicable": 20,
    "informational": 10,
}


def _review_priority(classification: Any) -> tuple[int, str, str]:
    """Rank *review work*, never the normative importance of a source rule.

    Unreviewed/unknown states go first. The score cannot grant compliance,
    suppress a requirement, or resolve applicability.
    """
    state = normalized_state(classification) if isinstance(classification, str) else "unreviewed"
    score = _REVIEW_PRIORITY_BY_STATE.get(state, 100)
    band = "urgent" if score >= 90 else "high" if score >= 70 else "routine" if score >= 40 else "low"
    return score, band, state


def _obligation_shadow_graph(
    response: dict[str, Any], clauses: list[dict[str, Any]],
    clause_records: list[dict[str, Any]], requirements: list[Any],
    requirement_ids: dict[int, str],
    duplicate_groups: dict[str, list[int]],
) -> dict[str, Any]:
    """Build a run-bound analysis graph without changing requirements or gates.

    Source facts, model-declared obligations, and executable requirements are
    separate node types.  Cross-links are only emitted from explicit IDs or
    code-owned compiler bindings; the graph never infers that two nodes are
    semantically equivalent or that a declared checker actually ran.
    """
    response_sha256 = sha256_json(response)
    provenance = response.get("provenance") if isinstance(response.get("provenance"), dict) else {}
    binding = {
        key: copy.deepcopy(provenance.get(key))
        for key in ("run_id", "source_sha256", "clause_sha256", "evidence_sha256", "request_sha256")
    }
    binding.update({
        "case_id": copy.deepcopy(response.get("case_id", provenance.get("case_id"))),
        "school_id": copy.deepcopy(response.get("school_id", provenance.get("school_id"))),
        "response_sha256": response_sha256,
    })
    clause_record_by_id = {
        str(item.get("clause_id")): item for item in clause_records
        if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
    }
    clause_node_ids: dict[str, str] = {}
    requirement_node_ids: dict[int, str] = {}
    nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []
    review_count = 0
    model_obligation_count = 0
    compiled_fact_count = 0
    review_priority_entries: list[dict[str, Any]] = []

    for clause in clauses:
        if not isinstance(clause, dict) or not isinstance(clause.get("id"), str):
            continue
        clause_id = clause["id"]
        record = clause_record_by_id.get(clause_id, {})
        node_id = _shadow_node_id("clause", response_sha256, {
            "clause_id": clause_id,
            "source_text_sha256": record.get("source_text_sha256"),
            "evidence_ids": clause.get("evidence_ids") or [],
        })
        clause_node_ids[clause_id] = node_id
        reviewed = isinstance(record.get("classification"), str)
        review_count += int(reviewed)
        priority_score, priority_band, priority_state = _review_priority(record.get("classification"))
        review_priority_entries.append({
            "clause_id": clause_id,
            "node_id": node_id,
            "source_text_sha256": record.get("source_text_sha256"),
            "review_classification": record.get("classification") if reviewed else None,
            "normalized_state": priority_state,
            "score": priority_score,
            "band": priority_band,
        })
        nodes.append({
            "node_id": node_id,
            "node_type": "source_clause",
            "source_ref": {
                "clause_id": clause_id,
                "source_text_sha256": record.get("source_text_sha256"),
                "evidence_ids": copy.deepcopy(clause.get("evidence_ids") or []),
            },
            "dimensions": {
                "source_kind": clause.get("source_kind") or "not_declared",
                "force": "not_assessed",
                "applicability": "not_declared",
                "execution_method": "not_declared",
                # Review classification is not evidence that the operation
                # ran. Keep execution separate from the review disposition.
                "execution_status": "not_executed_here",
                "verification_status": "not_assessed",
                "importance": "not_assessed",
            },
            "record": {
                "review_classification": record.get("classification"),
                "source_obligation_inventory_complete": False,
                "obligation_decomposition": record.get("obligation_decomposition", "not_supplied"),
            },
        })

    for clause_record in clause_records:
        clause_id = clause_record.get("clause_id")
        clause_node_id = clause_node_ids.get(str(clause_id))
        if not clause_node_id:
            continue
        for index, obligation in enumerate(clause_record.get("obligations") or []):
            model_obligation_count += 1
            node_id = _shadow_node_id("model-obligation", response_sha256, {
                "clause_id": clause_id, "index": index, "record": obligation,
            })
            nodes.append({
                "node_id": node_id,
                "node_type": "model_declared_obligation",
                "source_ref": {"clause_id": clause_id},
                "dimensions": {
                    "source_kind": "model_decomposition",
                    "force": "not_assessed",
                    "applicability": "not_declared",
                    "execution_method": "not_declared",
                    "execution_status": "not_executed_here",
                    "verification_status": "not_assessed",
                    "importance": "not_assessed",
                },
                "record": copy.deepcopy(obligation) if isinstance(obligation, dict) else {"value": obligation},
            })
            graph_edges.append({
                "from_node_id": node_id,
                "to_node_id": clause_node_id,
                "relation": "model_declares_obligation_for_clause",
                "authority": "accepted_clause_reviews_obligations",
                "status": "source_attribution_only",
            })
        for fact_index, fact in enumerate(clause_record.get("source_obligation_inventory") or []):
            compiled_fact_count += 1
            node_id = _shadow_node_id("compiled-fact", response_sha256, {
                "clause_id": clause_id, "index": fact_index,
                "fact_id": fact.get("id") if isinstance(fact, dict) else fact,
            })
            nodes.append({
                "node_id": node_id,
                "node_type": "compiled_source_fact",
                "source_ref": {"clause_id": clause_id},
                "dimensions": {
                    "source_kind": "code_compiler",
                    "force": "not_assessed",
                    "applicability": "not_declared",
                    "execution_method": "not_declared",
                    "execution_status": "candidate_only",
                    "verification_status": "declared_only",
                    "importance": "not_assessed",
                },
                "record": copy.deepcopy(fact) if isinstance(fact, dict) else {"value": fact},
            })
            graph_edges.append({
                "from_node_id": node_id,
                "to_node_id": clause_node_id,
                "relation": "compiler_fact_compiled_from_clause",
                "authority": "source_obligation_compiler",
                "status": "code_recognized_fact_not_complete_inventory",
            })

    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        requirement_id = requirement_ids.get(index)
        node_id = _shadow_node_id("requirement", response_sha256, {
            "response_index": index, "requirement_id": requirement_id,
        })
        requirement_node_ids[index] = node_id
        applicability = requirement.get("applicability")
        verification = requirement.get("verification")
        declared_mode = verification.get("mode") if isinstance(verification, dict) else None
        nodes.append({
            "node_id": node_id,
            "node_type": "requirement",
            "source_ref": {
                "requirement_id": requirement_id,
                "response_index": index,
                "clause_ids": copy.deepcopy(requirement.get("clause_ids") or []),
                "evidence_ids": copy.deepcopy(requirement.get("evidence_ids") or []),
            },
            "dimensions": {
                "source_kind": "accepted_host_requirement",
                "force": "not_assessed",
                "applicability": copy.deepcopy(applicability) if isinstance(applicability, dict) else "not_declared",
                "execution_method": declared_mode or "not_declared",
                "execution_status": "not_executed_here",
                "verification_status": "declared_only" if isinstance(verification, dict) else "not_declared",
                "importance": "not_assessed",
            },
            "record": {
                "role": requirement.get("role"),
                "field_key": requirement.get("field_key"),
                "canonical_requirement_sha256": hashlib.sha256(
                    _canonical_requirement(requirement).encode("utf-8")
                ).hexdigest(),
            },
        })
        for clause_id in requirement.get("clause_ids") or []:
            clause_node_id = clause_node_ids.get(str(clause_id))
            if clause_node_id:
                graph_edges.append({
                    "from_node_id": node_id,
                    "to_node_id": clause_node_id,
                    "relation": "requirement_explicitly_cites_clause",
                    "authority": "requirements[].clause_ids",
                    "status": "explicit_reference_not_semantic_equivalence",
                })

    # Compiler candidate bindings are intentionally labelled as candidates;
    # they are not promoted into model-obligation coverage or execution proof.
    fact_nodes_by_clause_and_id = {
        (str(node.get("source_ref", {}).get("clause_id")), str(node.get("record", {}).get("id"))): node["node_id"]
        for node in nodes if node.get("node_type") == "compiled_source_fact"
    }
    for clause_record in clause_records:
        clause_id = str(clause_record.get("clause_id"))
        for fact in clause_record.get("source_obligation_inventory") or []:
            if not isinstance(fact, dict):
                continue
            fact_node_id = fact_nodes_by_clause_and_id.get((clause_id, str(fact.get("id"))))
            for candidate in fact.get("candidate_requirement_bindings") or []:
                requirement_index = candidate.get("requirement_index") if isinstance(candidate, dict) else None
                requirement_node_id = requirement_node_ids.get(requirement_index) if isinstance(requirement_index, int) and not isinstance(requirement_index, bool) else None
                if fact_node_id and requirement_node_id:
                    graph_edges.append({
                        "from_node_id": fact_node_id,
                        "to_node_id": requirement_node_id,
                        "relation": "compiler_candidate_requirement_binding",
                        "authority": "compiler_role_and_property_path_match",
                        "status": "candidate_requires_property_and_receipt_validation",
                    })

    count_by_type: dict[str, int] = {}
    for node in nodes:
        node_type = str(node.get("node_type"))
        count_by_type[node_type] = count_by_type.get(node_type, 0) + 1
    return {
        "protocol": "obligation_shadow_graph_v1",
        "schema_version": "1.1",
        "status": "analysis_only",
        "submission_ready": False,
        "inventory_completeness": "incomplete_by_design",
        "binding": binding,
        "review_priority": {
            "policy": "operational_review_triage_v1",
            "scope": "source_clauses_only",
            "effect": "sort_only_no_compliance_or_release_effect",
            "entries": sorted(
                review_priority_entries,
                key=lambda item: (-item["score"], item["clause_id"]),
            ),
        },
        "metrics": {
            "source_clause_count": len(clause_node_ids),
            "reviewed_clause_count": review_count,
            "unreviewed_clause_count": max(0, len(clause_node_ids) - review_count),
            "requirement_count": sum(isinstance(item, dict) for item in requirements),
            "explicit_requirement_clause_edge_count": sum(
                item.get("relation") == "requirement_explicitly_cites_clause" for item in graph_edges
            ),
            "model_declared_obligation_count": model_obligation_count,
            "compiled_source_fact_count": compiled_fact_count,
            "node_count_by_type": count_by_type,
            "source_obligation_inventory_complete": False,
            "model_obligation_inventory_complete": False,
            "importance_assessment_complete": False,
            "review_priority_scored_clause_count": len(review_priority_entries),
            "final_applicable_requirement_count": None,
            "manual_marker_count": None,
        },
        "nodes": nodes,
        "edges": graph_edges,
        "equivalence_candidates": [
            {
                "requirement_id": requirement_id,
                "response_indexes": list(indexes),
                "comparison": "exact_canonical_requirement_only",
                "disposition": "recorded_not_merged_by_shadow_graph",
            }
            for requirement_id, indexes in sorted(duplicate_groups.items()) if len(indexes) > 1
        ],
        "equivalence_policy": "no_semantic_equivalence_inference_or_pruning",
        "manual_review_crosswalk": {
            "status": "pending_pipeline_manual_review_projection",
            "entries": [],
        },
    }


def deduplicate_exact_requirements(
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove only byte-for-byte semantic duplicates in contract 3.0.

    Cross-chunk semantic deduplication is unsafe because two similar clauses
    can intentionally produce different obligations.  The only automatic
    merge allowed here is an exact normalized requirement object, including
    its clause/evidence edges.  Anything less remains distinct and is recorded
    for review instead of being guessed together.
    """
    if response.get("contract_version") != "3.0":
        return copy.deepcopy(response), {
            "mode": "disabled_for_legacy_contract",
            "removed_indexes": [],
            "duplicate_groups": [],
        }
    output = copy.deepcopy(response)
    requirements = output.get("requirements")
    if not isinstance(requirements, list):
        return output, {"mode": "exact_only", "removed_indexes": [], "duplicate_groups": []}
    seen: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    removed: list[int] = []
    groups: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            kept.append(requirement)
            continue
        key = _canonical_requirement(requirement)
        if key not in seen:
            seen[key] = len(kept)
            kept.append(requirement)
            continue
        removed.append(index)
        group = next((item for item in groups if item["kept_index"] == seen[key]), None)
        if group is None:
            group = {"kept_index": seen[key], "removed_indexes": []}
            groups.append(group)
        group["removed_indexes"].append(index)
    output["requirements"] = kept
    return output, {
        "mode": "exact_only",
        "removed_indexes": removed,
        "duplicate_groups": groups,
    }


def build_semantic_review_ledger(
    response: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, Any]:
    requirements = response.get("requirements") if isinstance(response, dict) else []
    reviews = response.get("clause_reviews") if isinstance(response, dict) else []
    requirements = requirements if isinstance(requirements, list) else []
    reviews = reviews if isinstance(reviews, list) else []
    clause_by_id = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    review_by_id = {
        str(item.get("clause_id")): item for item in reviews
        if isinstance(item, dict) and item.get("clause_id")
    }
    reverse = derived_requirement_indexes(response, clauses)
    requirement_ids = {
        index: _stable_requirement_id(item)
        for index, item in enumerate(requirements)
        if isinstance(item, dict)
    }
    edges: list[dict[str, Any]] = []
    for clause_id in sorted(reverse):
        review = review_by_id.get(clause_id)
        if not isinstance(review, dict) or not classification_requires_requirement(
            str(review.get("classification"))
        ):
            continue
        for requirement_index in reverse[clause_id]:
            requirement = requirements[requirement_index]
            if not isinstance(requirement, dict):
                continue
            edges.append({
                "clause_id": clause_id,
                "requirement_index": requirement_index,
                "requirement_id": requirement_ids.get(requirement_index),
                "edge_id": hashlib.sha256(
                    f"{clause_id}\0{requirement_ids.get(requirement_index)}".encode("utf-8")
                ).hexdigest()[:16],
                "classification": review.get("classification"),
                "evidence_ids": copy.deepcopy(requirement.get("evidence_ids") or []),
            })

    clause_records: list[dict[str, Any]] = []
    duplicate_groups: dict[str, list[int]] = {}
    source_aliases: list[dict[str, Any]] = []
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            continue
        requirement_id = requirement_ids.get(index)
        if requirement_id:
            duplicate_groups.setdefault(requirement_id, []).append(index)
        source_id = item.get("id") or item.get("existing_requirement_id")
        if isinstance(source_id, str) and source_id.strip() and requirement_id:
            source_aliases.append({
                "source_requirement_id": source_id,
                "requirement_id": requirement_id,
                "response_index": index,
                "resolution": "code_owned_exact_source_alias",
            })
    for clause_id in sorted(clause_by_id):
        clause = clause_by_id[clause_id]
        review = review_by_id.get(clause_id)
        clause_requirement_indexes = list(reverse.get(clause_id, []))
        source_obligation_inventory: list[dict[str, Any]] = []
        source_text = str(clause.get("text") or clause.get("source_text_full") or "")
        source_text_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        for fact in compile_known_source_obligations(source_text):
            candidates: list[dict[str, Any]] = []
            for requirement_index in clause_requirement_indexes:
                requirement = requirements[requirement_index]
                if (
                    not isinstance(requirement, dict)
                    or requirement.get("role") not in fact["roles"]
                ):
                    continue
                value: Any = requirement.get("properties")
                for path_part in str(fact["property_path"]).split(".")[1:]:
                    value = value.get(path_part) if isinstance(value, dict) else None
                candidates.append({
                    "requirement_index": requirement_index,
                    "requirement_id": requirement_ids.get(requirement_index),
                    "observed_value": copy.deepcopy(value),
                    "declared_checker_ids": copy.deepcopy(
                        requirement.get("verification", {}).get("checker_ids", [])
                        if isinstance(requirement.get("verification"), dict) else []
                    ),
                })
            expected = fact["expected_value"]
            matched_candidates = [
                item for item in candidates
                if item.get("observed_value") == expected
                and type(item.get("observed_value")) is type(expected)
            ]
            required_checker_ids = list(fact.get("required_checker_ids") or [])
            declared_checker_ids = sorted({
                checker_id
                for item in matched_candidates
                for checker_id in item.get("declared_checker_ids", [])
                if isinstance(checker_id, str)
            })
            missing_checker_ids = sorted(set(required_checker_ids) - set(declared_checker_ids))
            source_obligation_inventory.append({
                "id": fact["id"],
                "source_clause_sha256": source_text_sha256,
                "allowed_roles": copy.deepcopy(fact["roles"]),
                "property_path": fact["property_path"],
                "expected_value": copy.deepcopy(fact["expected_value"]),
                "candidate_requirement_bindings": candidates,
                "required_checker_ids": required_checker_ids,
                "declared_checker_ids": declared_checker_ids,
                "missing_checker_ids": missing_checker_ids,
                "checker_binding_status": "bound" if not missing_checker_ids else "missing_required_checker",
                "execution_receipt_status": "pending_generation",
                "render_status": (
                    "pending_word_render"
                    if "docx.word_render" in required_checker_ids else "not_required"
                ),
                "compiled_by": "source_obligation_compiler",
                "coverage_gate": "host_review_contract_role_property_check",
                "model_echo_required": False,
            })
        record: dict[str, Any] = {
            "clause_id": clause_id,
            "source_text_sha256": source_text_sha256,
            "classification": review.get("classification") if isinstance(review, dict) else None,
            "requirement_indexes": clause_requirement_indexes if isinstance(review, dict) else [],
            "evidence_ids": copy.deepcopy(clause.get("evidence_ids") or []),
            "source_obligation_inventory": source_obligation_inventory,
            "source_obligation_inventory_complete": False,
        }
        if isinstance(review, dict) and isinstance(review.get("obligations"), list):
            record["obligations"] = copy.deepcopy(review["obligations"])
            record["obligation_decomposition"] = "model_semantic_items_plus_code_compiled_source_facts"
        else:
            record["obligations"] = []
            record["obligation_decomposition"] = "not_supplied"
        clause_records.append(record)

    provenance = response.get("provenance") if isinstance(response.get("provenance"), dict) else None
    ledger_response_sha256 = sha256_json(response)
    shadow_graph = _obligation_shadow_graph(
        response, clauses, clause_records, requirements, requirement_ids,
        duplicate_groups,
    )
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "obligation-shadow-graph.schema.json"
    graph_errors = load_and_validate(shadow_graph, schema_path)
    if graph_errors:
        raise ValueError(
            "generated obligation shadow graph failed schema validation: "
            + "; ".join(graph_errors[:8])
        )
    node_ids = [item["node_id"] for item in shadow_graph["nodes"]]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("generated obligation shadow graph contains duplicate node IDs")
    node_id_set = set(node_ids)
    if any(
        edge["from_node_id"] not in node_id_set or edge["to_node_id"] not in node_id_set
        for edge in shadow_graph["edges"]
    ):
        raise ValueError("generated obligation shadow graph contains a dangling edge")
    return {
        "schema_version": "1.3",
        "contract_version": response.get("contract_version"),
        "source_obligation_inventory_scope": "partial_machine_recognized_supplement",
        "source": "accepted_host_review_response",
        "relationship_policy": {
            "authoritative_edge": "requirements[].clause_ids",
            "reverse_relation": "derived_by_code",
            "semantic_inference": "disabled",
            "cross_chunk_deduplication": "exact_only_code_owned",
            "semantic_aliasing": "disabled",
            "duplicate_node_policy": "same_exact_node_id_and_record_group",
        },
        "provenance": copy.deepcopy(provenance),
        "clauses": clause_records,
        "requirements": [
            {
                "index": index,
                "requirement_id": requirement_ids.get(index),
                "source_requirement_id": item.get("id") or item.get("existing_requirement_id"),
                "role": item.get("role"),
                "clause_ids": copy.deepcopy(item.get("clause_ids") or []),
                "evidence_ids": copy.deepcopy(item.get("evidence_ids") or []),
                "canonical_requirement_sha256": hashlib.sha256(
                    _canonical_requirement(item).encode("utf-8")
                ).hexdigest(),
            }
            for index, item in enumerate(requirements)
            if isinstance(item, dict)
        ],
        "edges": edges,
        "source_aliases": source_aliases,
        "duplicate_requirement_groups": [
            {"requirement_id": requirement_id, "response_indexes": indexes}
            for requirement_id, indexes in sorted(duplicate_groups.items())
            if len(indexes) > 1
        ],
        "obligation_ledger": [
            {
                "clause_id": item["clause_id"],
                "obligations": copy.deepcopy(item.get("obligations") or []),
                "source_obligation_inventory": copy.deepcopy(
                    item.get("source_obligation_inventory") or []
                ),
                "status": item.get("obligation_decomposition"),
            }
            for item in clause_records
        ],
        "response_sha256": ledger_response_sha256,
        "obligation_shadow_graph": shadow_graph,
    }
