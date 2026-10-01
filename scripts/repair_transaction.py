"""Representation-bound repair receipts; never standalone edit authority."""
from __future__ import annotations

import copy
from semantic_contract import sha256_json


def changed_paths(before, after, path="$"):
    if type(before) is not type(after):
        return [path]
    if isinstance(before, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            child = path + "." + key
            result.extend([child] if key not in before or key not in after
                          else changed_paths(before[key], after[key], child))
        return result
    if isinstance(before, list):
        if len(before) != len(after):
            return [path]
        return [p for i, (left, right) in enumerate(zip(before, after))
                for p in changed_paths(left, right, f"{path}[{i}]")]
    return [] if before == after else [path]


def repair_receipt(before: dict, after: dict, errors: list, proof: list, chunk: dict) -> dict:
    paths = changed_paths(before, after)
    if not paths or not proof:
        raise ValueError("repair transaction requires an actual change and conservation proof")
    receipt = {"protocol": "candidate_repair_transaction_v1", "status": "projected_pending_independent_review",
               "candidate_sha256": sha256_json(before), "error_bundle_sha256": sha256_json(errors),
               "result_sha256": sha256_json(after), "source_chunk_sha256": sha256_json(chunk),
               "binding": copy.deepcopy(chunk.get("provenance", {})),
               "allowed_changed_paths": paths, "preserved_clause_reviews_sha256": sha256_json(before.get("clause_reviews")),
               "proofs": copy.deepcopy(proof), "independent_review_required": True, "submission_ready": False}
    if before.get("clause_reviews") != after.get("clause_reviews"):
        receipt["classification_revision"] = "explicit_projection_proof_required"
    receipt["transaction_sha256"] = sha256_json(receipt)
    return receipt


def validate_repair_receipt(receipt: dict, before: dict, after: dict, errors: list, chunk: dict) -> bool:
    try:
        return receipt == repair_receipt(before, after, errors, receipt["proofs"], chunk)
    except (ValueError, KeyError, TypeError):
        return False
