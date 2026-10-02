"""Validator-bound compound primary proposals, never semantic equivalence.

An invalid parent can request a narrowly addressed new proposal. The validator
does not choose applicability, invent human obligations or select quotations.
Complete local revalidation and fresh source-first review remain mandatory.
"""
from __future__ import annotations

import copy
import re

from host_review_contract import contract_error_records
from semantic_contract import sha256_json
from source_atom_metadata import bind_atom_quote
from source_literal_binding import compose_source_fragments

POLICY = "validator_bound_compound_atom_proposal_v1"


def project(previous, proposed, records, chunk, *, validate, changed_paths, verified_parent_binding=None):
    audit = {"policy": POLICY, "status": "not_applicable",
             "independent_review_required": True, "submission_ready": False,
             "mechanical_equivalence_claimed": False}
    if not all(isinstance(v, dict) for v in (previous, proposed, chunk)):
        return None, audit
    if (previous.get("contract_version") != "3.0" or proposed.get("contract_version") != "3.0"
            or previous.get("reported_conflicts") or previous.get("conflicts")
            or proposed.get("reported_conflicts") or proposed.get("conflicts")
            or not isinstance(records, list) or not records
            or any(not isinstance(r, dict) for r in records)):
        return None, audit
    provenance = chunk.get("provenance")
    receipt_bound = (isinstance(verified_parent_binding, dict)
                    and verified_parent_binding.get("policy") == "verified_current_retry_repair_base_v1"
                    and verified_parent_binding.get("parent_response_sha256") == sha256_json(previous)
                    and verified_parent_binding.get("source_chunk_sha256") == sha256_json(chunk))
    if (not isinstance(provenance, dict) or not provenance.get("run_id")
            or (previous.get("provenance") != provenance
                and not (previous.get("provenance") is None and receipt_bound))
            or proposed.get("provenance") not in (None, provenance)):
        return None, audit
    try:
        actual = contract_error_records(validate(previous, chunk), response=previous, chunk=chunk)
        if (not actual or sorted(map(sha256_json, actual)) != sorted(map(sha256_json, records))
                or len(set(map(sha256_json, records))) != len(records)):
            return None, audit
        clauses = chunk["clauses"]
        by_clause = {c["id"]: c for c in clauses}
        if len(by_clause) != len(clauses):
            return None, audit
        old_reviews, new_reviews = previous["clause_reviews"], proposed["clause_reviews"]
        old_ids = [r["clause_id"] for r in old_reviews]
        if (old_ids != [r["clause_id"] for r in new_reviews]
                or len(set(old_ids)) != len(old_ids) or set(old_ids) != set(by_clause)):
            return None, audit
        candidate, proofs, permitted = copy.deepcopy(previous), [], set()
        for record in actual:
            path = record.get("json_pointer", "")
            inventory = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.obligations", path)
            field = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.obligations\[(\d+)\]\.(applicability|source_quote)", path)
            if record.get("code") != "contract_validation_error" or not (inventory or field):
                return None, audit
            i = int((inventory or field).group(1))
            old_review, new_review = old_reviews[i], new_reviews[i]
            cid = old_review["clause_id"]
            if record.get("clause_id") != cid or record.get("response_sha256") != sha256_json(previous):
                return None, audit
            binding = compose_source_fragments([cid], by_clause, chunk["evidence_context"])
            if inventory:
                if (record.get("raw_error") != path + ": mixed_execution_requires_covered_and_unverifiable_actions"
                        or old_review.get("classification") != "executable_with_external_check"):
                    return None, audit
                old, new = old_review.get("obligations"), new_review.get("obligations")
                if (not isinstance(old, list) or not old or not isinstance(new, list)
                        or len(new) <= len(old) or new[:len(old)] != old):
                    return None, audit
                statuses = {a.get("status") for a in old}
                if statuses not in ({"covered"}, {"unverifiable"}):
                    return None, audit
                missing = "unverifiable" if statuses == {"covered"} else "covered"
                ids = [a["id"] for a in new]
                if any(not isinstance(oid, str) or not oid for oid in ids) or len(set(ids)) != len(ids):
                    return None, audit
                quote_bindings = []
                for atom in new[len(old):]:
                    if (atom.get("status") != missing
                            or atom.get("route") != ("human" if missing == "unverifiable" else "automatic")):
                        return None, audit
                    quote_bindings.append(bind_atom_quote(atom["source_quote"], cid, by_clause, chunk["evidence_context"]))
                candidate["clause_reviews"][i]["obligations"] = copy.deepcopy(new)
                proof = {"added_obligation_ids": ids[len(old):], "added_quote_bindings": quote_bindings,
                         "existing_obligations_unchanged": True, "missing_status": missing}
            else:
                j, name = int(field.group(2)), field.group(3)
                old, new = old_review["obligations"][j], new_review["obligations"][j]
                if old.get("id") != new.get("id") or not isinstance(old.get("id"), str):
                    return None, audit
                value = new.get(name)
                if name == "applicability":
                    if (record.get("raw_error") != path + ": undecided_scope_cannot_be_covered"
                            or old.get("status") != "covered" or old.get(name) not in {"unknown", "conflicted"}
                            or value != "applicable"):
                        return None, audit
                    # This is the primary's new claim, not a code default.
                    proof = {"obligation_id": old["id"], "proposed_applicability": value}
                else:
                    if record.get("raw_error") != path + ": must_equal_current_source_subspan":
                        return None, audit
                    quote_binding = bind_atom_quote(value, cid, by_clause, chunk["evidence_context"])
                    proof = {"obligation_id": old["id"], "selected_quote_binding": quote_binding}
                candidate["clause_reviews"][i]["obligations"][j][name] = copy.deepcopy(value)
            if path in permitted:
                return None, audit
            permitted.add(path)
            proofs.append({**proof, "rule_id": POLICY, "json_pointer": path, "clause_id": cid,
                "source_binding": binding, "source_chunk_sha256": sha256_json(chunk),
                "run_id": provenance["run_id"], "validator_record_sha256": sha256_json(record),
                "source_binding_complete": True, "primary_semantic_reassessment": True,
                "verified_parent_binding": copy.deepcopy(verified_parent_binding) if receipt_bound else None,
                "independent_review_required": True, "submission_ready": False})
        changes = changed_paths(previous, candidate)
        if set(changes) != permitted or validate(candidate, chunk):
            return None, audit
        for proof in proofs:
            proof.update(parent_response_sha256=sha256_json(previous), candidate_response_sha256=sha256_json(candidate))
        model_changes = changed_paths(previous, proposed)
        audit.update(status="projected", parent_response_sha256=sha256_json(previous),
            model_retry_response_sha256=sha256_json(proposed), projected_response_sha256=sha256_json(candidate),
            source_chunk_sha256=sha256_json(chunk), error_bundle_sha256=sha256_json(actual),
            applied_paths=changes, discarded_unrequested_paths=sorted(set(model_changes) - permitted),
            authorized_proposals=proofs)
        return candidate, audit
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None, audit


def ledger(previous, current, records, paths, chunk, *, validate, changed_paths, verified_parent_binding=None):
    candidate, audit = project(previous, current, records, chunk, validate=validate, changed_paths=changed_paths,
                               verified_parent_binding=verified_parent_binding)
    if candidate is None or candidate != current or set(paths) != set(audit["applied_paths"]):
        return None
    return audit["authorized_proposals"]
