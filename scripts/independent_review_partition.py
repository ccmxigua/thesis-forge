"""Lossless output partitioning, never partitioning the primary source graph.

Native child outputs are an explicit code projection, not one native response.
Every consumer replays the children before the existing full-request semantic
compilation and ledger validation. Partial children never authorize acceptance.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from semantic_contract import sha256_json, strict_json_loads

LEGACY_POLICY = "whole_candidate_checks_4_v1"
ARRAY_TWO_POLICY = "whole_candidate_checks_2_v1"
KEYED_TWO_POLICY = "whole_candidate_keyed_checks_2_v1"
LEGACY_SINGLE_CHECK_POLICY = "whole_candidate_keyed_check_1_v1"
PREVIOUS_SINGLE_CHECK_POLICY = "whole_candidate_keyed_check_1_v2"
POLICY = "whole_candidate_keyed_check_1_v3"
SINGLE_CHECK_POLICIES = frozenset({
    LEGACY_SINGLE_CHECK_POLICY, PREVIOUS_SINGLE_CHECK_POLICY, POLICY,
})
COMPACT_ORIENTATION_POLICIES = frozenset({PREVIOUS_SINGLE_CHECK_POLICY, POLICY})
KEYED_POLICIES = frozenset({KEYED_TWO_POLICY, *SINGLE_CHECK_POLICIES})
PROTOCOL = "native_independent_partition_projection_v1"
PACKING_THRESHOLD = 2
CHECKS_PER_INVOCATION = 1


def partition_limits(policy: str | None) -> tuple[int, int] | None:
    """Keep historical packet/proof interpretation fixed by its request marker."""
    if policy is None:
        return None
    if policy == LEGACY_POLICY:
        return 8, 4
    if policy in {ARRAY_TWO_POLICY, KEYED_TWO_POLICY}:
        return 3, 2
    if policy in SINGLE_CHECK_POLICIES:
        return PACKING_THRESHOLD, CHECKS_PER_INVOCATION
    raise ValueError("unknown native review partition policy")


def partition_schema(schema: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    """Keep whole per-check constraints; prune only unreachable definitions."""
    result = copy.deepcopy(schema)
    branches = result["properties"]["results"]["items"]["anyOf"]
    by_id = {branch["properties"]["check_id"]["enum"][0]: branch for branch in branches}
    if len(by_id) != len(branches) or len(set(ids)) != len(ids) or any(cid not in by_id for cid in ids):
        raise ValueError("partition schema has duplicate or unknown checks")
    result["properties"]["results"]["items"]["anyOf"] = [by_id[cid] for cid in ids]
    result["properties"]["results"]["minItems"] = len(ids)
    result["properties"]["results"]["maxItems"] = len(ids)
    definitions = result.pop("$defs", {})
    reachable: set[str] = set()

    def scan(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                if name not in definitions:
                    raise ValueError("partition schema has an unresolved definition")
                if name not in reachable:
                    reachable.add(name)
                    scan(definitions[name])
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)

    scan(result)
    if reachable:
        result["$defs"] = {name: definitions[name] for name in sorted(reachable)}
    return result


def partition_generation_schema(schema: dict[str, Any], ids: list[str], policy: str) -> dict[str, Any]:
    """Bind one complete result to each check using portable required keys.

    Array cardinality/uniqueness is not available on the native wire. The new
    shape removes the repeated choice of a result branch, not any semantic
    field, source atom, or local validator. Historical policies stay exact.
    """
    partition_limits(policy)
    result = partition_schema(schema, ids)
    if policy in KEYED_POLICIES:
        branches = result["properties"]["results"]["items"]["anyOf"]
        result["properties"]["results"] = {
            "type": "object", "additionalProperties": False,
            "required": list(ids), "properties": dict(zip(ids, branches)),
        }
    return result


def partition_packets(request: dict[str, Any], packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic focus subsets of one immutable full source catalog.

    The focused checks and orientation context together retain the complete
    source group, including cross-clause links, physical geometry, conditions
    and exact text. Q/RR selectors retain their whole-request identities. Only
    the required output set is smaller.
    """
    policy = request.get("native_review_partition_policy")
    limits = partition_limits(policy)
    if limits is None:
        raise ValueError("partition policy is missing or unknown")
    threshold, batch_size = limits
    checks = packet.get("checks")
    if not isinstance(checks, list) or len(checks) < threshold:
        raise ValueError("partition request is not an oversized source group")
    ids = [check.get("check_id") for check in checks if isinstance(check, dict)]
    if (len(ids) != len(checks) or any(not isinstance(cid, str) or not cid for cid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("partition request check identity is invalid")
    count = (len(checks) + batch_size - 1) // batch_size
    output = []
    for index, start in enumerate(range(0, len(checks), batch_size), 1):
        child = copy.deepcopy(packet)
        child["checks"] = copy.deepcopy(checks[start:start + batch_size])
        focused_ids = set(ids[start:start + batch_size])
        orientation = (
            [check for check in checks if check["check_id"] not in focused_ids]
            if policy in COMPACT_ORIENTATION_POLICIES else checks
        )
        child["orientation_only_checks"] = copy.deepcopy(orientation)
        child["native_review_partition"] = {
            "protocol": PROTOCOL, "policy": policy,
            "whole_request_sha256": sha256_json(request),
            "whole_source_packet_sha256": sha256_json(packet),
            "batch_index": index, "batch_count": count,
            "check_ids": ids[start:start + batch_size],
        }
        output.append(child)
    return output


def join_partition_responses(packets: list[dict[str, Any]], responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Require exact disjoint coverage; never fill omissions or accept a tail."""
    if len(packets) != len(responses) or not packets:
        raise ValueError("native partition response count mismatch")
    joined, seen = [], set()
    for packet, response in zip(packets, responses):
        expected = packet["native_review_partition"]["check_ids"]
        if not isinstance(response, dict) or set(response) != {"results"}:
            raise ValueError("native partition response is not a closed results object")
        records = response["results"]
        policy = packet["native_review_partition"]["policy"]
        partition_limits(policy)
        if policy in KEYED_POLICIES:
            if not isinstance(records, dict) or set(records) != set(expected):
                raise ValueError("native keyed partition has missing or extra check keys")
            if any(not isinstance(records[cid], dict) or records[cid].get("check_id") != cid
                   for cid in expected):
                raise ValueError("native keyed partition check key and payload identity differ")
            records = [records[cid] for cid in expected]
        if not isinstance(records, list):
            raise ValueError("native partition results have the wrong versioned shape")
        ids = [item.get("check_id") for item in records if isinstance(item, dict)]
        if (len(ids) != len(records) or len(ids) != len(expected)
                or len(set(ids)) != len(ids) or set(ids) != set(expected)
                or seen.intersection(ids)):
            raise ValueError("native partition has missing, duplicate, extra or wrong-batch checks")
        by_id = dict(zip(ids, records))
        joined.extend(copy.deepcopy(by_id[cid]) for cid in expected)
        seen.update(ids)
    return {"results": joined}


def validate_partition_receipt(request: dict[str, Any], root: Path, raw: dict[str, Any],
                               audit: dict[str, Any]) -> None:
    """Replay bound inputs, native terminal outputs and exact aggregation.

    File hashes alone are insufficient: packets/schemas/prompts are rebuilt
    from the complete request and validated retry scope, then native JSONL and
    last-message must independently reproduce each stored raw child output.
    """
    from host_adapters import codex
    from native_wire_schema import review_wire_schema
    from native_semantic_review import OBLIGATION_COVERAGE_SCHEMA, _prompt
    from semantic_source_references import build_source_reference_packet, source_reference_schema, source_inventory_generation_schema
    from independent_retry_scope import prepare_retry_scope, constrain_retry_schema

    policy = request.get("native_review_partition_policy")
    expected = partition_limits(policy) is not None and audit.get("adapter_id") == "codex"
    pointer = audit.get("native_partition_projection")
    proof_path = root / "native-partition-projection.json"
    if not expected:
        if pointer is not None or proof_path.exists():
            raise ValueError("unexpected native partition projection")
        return
    if not isinstance(pointer, dict) or set(pointer) != {"protocol", "path", "sha256"}:
        raise ValueError("native partition projection receipt is missing")
    if pointer["protocol"] != PROTOCOL or Path(pointer["path"]).resolve() != proof_path.resolve():
        raise ValueError("native partition projection path/protocol mismatch")
    if not proof_path.is_file() or hashlib.sha256(proof_path.read_bytes()).hexdigest() != pointer["sha256"]:
        raise ValueError("native partition projection bytes mismatch")
    proof = strict_json_loads(proof_path.read_text(encoding="utf-8"))
    packet = build_source_reference_packet(request)
    packets = partition_packets(request, packet)
    locks, _ = prepare_retry_scope(request, root, OBLIGATION_COVERAGE_SCHEMA, provider_nullable_optionals=True)
    schema = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True, constrain_requirement_links=True)
    if locks:
        schema = constrain_retry_schema(schema, locks)
    schema = source_inventory_generation_schema(schema, retained_results=locks)
    if (not isinstance(proof, dict) or set(proof) != {"protocol", "policy", "whole_request_sha256", "children", "aggregate_sha256"}
            or proof.get("protocol") != PROTOCOL or proof.get("policy") != policy
            or proof.get("whole_request_sha256") != sha256_json(request)
            or not isinstance(proof.get("children"), list) or len(proof["children"]) != len(packets)):
        raise ValueError("native partition projection identity/coverage mismatch")
    responses = []
    for index, (child, record) in enumerate(zip(packets, proof["children"]), 1):
        if not isinstance(record, dict) or set(record) != {"batch_index", "check_ids", "artifacts"} or record["batch_index"] != index or isinstance(record["batch_index"], bool) or record["check_ids"] != child["native_review_partition"]["check_ids"]:
            raise ValueError("native partition child identity mismatch")
        directory = root / f"native-batch-{index:04d}"
        names = {"source-reference-packet.json", "prompt.txt", "provider-response-schema.json", "stdout.jsonl", "stderr.txt", "last-message.txt", "raw-response.json"}
        artifacts = record["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != names:
            raise ValueError("native partition artifact inventory mismatch")
        for name, digest in artifacts.items():
            path = directory / name
            if root.resolve() not in path.resolve().parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("native partition artifact is missing, escaped or mutated")
        load = lambda name: strict_json_loads((directory / name).read_text(encoding="utf-8"))
        focus_locks = {cid: value for cid, value in locks.items() if cid in record["check_ids"]}
        if (load("source-reference-packet.json") != child
                or load("provider-response-schema.json") != review_wire_schema(partition_generation_schema(schema, record["check_ids"], policy), request)
                or (directory / "prompt.txt").read_text(encoding="utf-8") != _prompt(request, retained_results=focus_locks, source_packet=child)):
            raise ValueError("native partition input is not reconstructed from the immutable whole request")
        stdout = (directory / "stdout.jsonl").read_text(encoding="utf-8")
        parsed, _ = codex.parse_result(stdout, last_message=(directory / "last-message.txt").read_text(encoding="utf-8"))
        if parsed != load("raw-response.json"):
            raise ValueError("native partition raw output does not reproduce from native terminal events")
        responses.append(parsed)
    aggregate = join_partition_responses(packets, responses)
    if raw != aggregate or proof["aggregate_sha256"] != sha256_json(aggregate):
        raise ValueError("native partition aggregate does not reproduce exactly")
