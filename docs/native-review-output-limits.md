# Native review output limits

`max_output_tokens` in a native Codex `turn.failed.error.message` is a runtime
output truncation, not low confidence, semantic drift, or a project timeout.
Persist stdout/stderr and bind the terminal failure to the current candidate
and run, including hashes of persisted native failure artifacts. Only a single
failed terminal in an intact JSONL stream can classify capacity or truncation;
malformed streams or mixed terminal outcomes never authorize capacity retry.
Do not accept a partial last message and do not repeat the same
oversized request in the semantic/provider retry loop. Capacity failures keep
their existing separate bounded retry policy.

All entrypoints now share a default **8-clause packing target**, still
overrideable with `--host-review-chunk-size`. This is conservative workload
mitigation, not a measured model token limit or a guarantee against truncation.
The previous failure had 18 independent checks and a roughly 1 MB native
schema; other 18-check reviews succeeded. Count alone cannot prove capacity.

Packing happens before primary generation. All source clauses remain in order
and exactly once; each new block receives a fresh request/provenance, a full
primary response, independent review, and the existing complete merge checks.
Adjacent context is orientation-only. Physical source occurrences and fixed
declarations stay atomic, even above the target. No source, obligation,
requirement, schema guard, or independent check is discarded to fit a limit.

There is no fabricated native aggregate event or reuse of old accepted
responses. On another output-limit failure, record the explicit
nonretryable code `independent_obligation_review_output_limit` and stop. A
fresh smaller target may help multi-occurrence blocks; it does not split an
oversized single atomic group. Final DOCX/Word acceptance
and `submission_ready` remain independent gates. Native service route and
actual output token limit remain unobserved.

## Exact shared wire definitions

The fresh `25f2efa0-5b8b-4b54-9a9e-dc9e3ed37c08` run still truncated a
four-check partition (C00009–C00012). Its provider schema was 162,359 bytes;
successful four-check schemas in the same run were 83,727–102,263 bytes.
This shows duplicated representation cost, not proof of the service failure's
cause. No failed independent final message or output-token count was available.

New independent requests bind `native_wire_schema_policy=exact_subtree_refs_v1`.
After the existing native projection, identical complete schema subtrees are
shared through root `$defs` references. Descriptions, enums, mandatory fields,
source selectors and all existing native constraints remain intact. Code
expands only the newly introduced definitions and requires exact equality to
the original projected schema before dispatch. This does not change the local
canonical schema, response format, source group, output partitions or budgets.
The captured failed schema becomes 89,089 bytes, with exact reverse expansion;
this is a measured reduction, not a demonstrated remedy for native truncation.

Requests without the version marker retain their original wire projection.
Both receipt consumers reconstruct the declared supported version, and native
partition replay requires the exact resulting provider schema. Unknown
versions, changed shared definitions and resealed schema mutations fail closed.
Historical requests and native responses are never rewritten into new receipts.
Local mocked replay and projection tests are not a fresh model-backed BSU run.

## Bound independent output partitions

The historical `whole_candidate_keyed_checks_2_v1` obligation-coverage policy
starts at three checks. Under that version the Codex native reviewer
generates at most two check results per invocation. This is an engineering
packing policy, not a measured token limit or guarantee of successful output.
The primary source group, candidate and entire request remain immutable.
Other host adapters retain their existing complete-review route; no host or
model substitution is performed.

The fresh `346d5e60-8ee0-4d94-b409-de642c777de3` BSU run failed at exactly
eight checks (C00017–C00024), below the previous strict `> 8` trigger. The
inclusive boundary now covers that observed workload without changing the
top-level eight-clause target, source group, model, concurrency or attempt
limits. That historical policy's two four-check calls share the same 900-second invocation budget;
they are output partitions, not additional corrective attempts. This is an
offline-verified mitigation, not a guarantee against future native truncation.
The same run also recorded an unlocalized PermissionError in a parallel
review. Partitioning neither diagnoses nor authorizes bypassing that denial.

The fresh `d8db61be-d42b-4932-bf39-47b18dbe9945` run still failed on
C00009–C00012 after exact shared definitions reduced the four-check schema to
89,089 bytes. No complete response or numeric output budget was observable.
The original two-check policy reduces the required result set and its schema per
native call while retaining the whole source catalog and exact final union.
The primary eight-clause packing, four concurrent host agents, two-attempt
limit, 900-second whole-invocation deadline and 1,800-second stage deadline
are unchanged. More children do not get a new timeout or extra retry budget.
This is a workload mitigation, not proof of the native service's root cause.

Versioned replay remains exact: `whole_candidate_checks_2_v1` retains its
two-result array shape and three-check threshold. `whole_candidate_checks_4_v1` retains its
original inclusive eight-check threshold and four-result batches. Requests
without a marker stay unpartitioned. Both consumers reconstruct the declared
version, and unknown versions are rejected before dispatch. Old requests,
child events and proofs are never rewritten to claim a new two-check run.

Each child receives its complete focused checks, the whole source group as
orientation-only context, and unchanged cross-clause requirement support,
table geometry, source spans and source/requirement references. Its schema
retains every focused check's constraints and prunes only unreachable `$defs`.
Q/RR references still bind to the whole request, not a partial candidate.
No source text or obligation is cut off. Calls share the original timeout
budget and existing cancellation controller; no partial aggregate is accepted.

`native-batch-NNNN/` captures each exact packet, prompt, native schema,
stdout/stderr, last message and raw response. `native-partition-projection.json`
is an explicit **code-generated projection proof**, not a native assistant
response or a release receipt. The top-level stdout is a trace index, not a
fabricated `turn.completed`. The aggregate raw response is accepted only after
the children reproduce from native JSONL/last-message and their exact output
sets form a disjoint, complete union. Then the unchanged full-request wire,
semantic, retry-sibling and AO-ledger validators run. Both bridge and pipeline
receipt consumers reconstruct all child inputs and repeat this replay; merely
resealing an edited prompt, schema, source packet or output hash is insufficient.

The observed fifteen-check failure on `5b324cd` persists at primary packing
targets 8/4/2. Its native terminal reports `max_output_tokens`; actual output
token counts and upstream route are not exposed. Partitioning addresses the
output unit without assuming model judgments are correct. A child output limit,
provider error, semantic conflict or failed final file acceptance still blocks
the run. Passing offline/mock-provider tests is not real BSU/Word acceptance.

## Exact result slots on the native wire

Fresh run `045bdb8d-b457-4e60-a46c-4ea0583f6786` still truncated the
C00033/C00034 two-check declaration partition. It exposed no partial answer,
token usage, numeric output budget, or proven semantic field at fault. Its
53,525-byte provider schema is not by itself evidence of the cause.

Offline inspection reproduced a narrower generation-protocol gap: portable
native projection drops array `minItems`, `maxItems` and `uniqueItems`. The old
result array can therefore generate no results or repeat a check indefinitely,
although local acceptance correctly rejects incomplete/duplicate sets. This
does not establish that duplication occurred in the failed native response.

The new `whole_candidate_keyed_checks_2_v1` keeps the same partition membership,
three-check threshold, two-check output unit and shared deadline. Each native
child now returns a closed `results` object keyed by its exact selected IDs.
Every key is required and has its original complete per-check result schema,
including the matching `check_id`, verdict, evidence and every obligation
field. No inventory is shortened and no semantic answer is supplied by code.
The native schema structurally prevents missing/foreign result slots; strict
JSON parsing rejects duplicate keys. The join verifies key/payload identity,
then projects the complete values into canonical source order without changing
their contents. Only this code aggregate uses the historical result array.

Raw child JSON, native events and last messages remain in their original keyed
shape. Receipt consumers rebuild the versioned child schema and prompt, replay
the original keyed responses, and reproduce the exact canonical aggregate.
Old array policies retain byte-identical schema/prompt reconstruction. Unknown
policies, list output under the keyed policy, mismatched IDs, resealed payloads,
and partial children fail closed. This fixes generation cardinality for new
partitioned reviews; it is not a proven cure for native output truncation or
evidence of full BSU, DOCX or Word acceptance.

## One complete check per native output

Fresh run `ca344905-1dd5-483b-91de-5ff1e2c9fca1` completed chunks 1–4 but
again failed on the C00033/C00034 declaration partition. Its closed keyed
schema was 52,494 bytes and prompt 104,860 bytes. The native terminal again
reported `max_output_tokens` without an answer, token count or numeric limit.
Static inspection found no dangling or cyclic schema reference; it did not
establish the service's root cause. The keyed representation alone did not
remove the observed failure.

New requests with at least two checks bind
`whole_candidate_keyed_check_1_v1`: each child returns one entire check result.
The whole candidate, source graph, cross-clause support, source selectors and
every atom within that check remain present. In particular the printing and
human truth-attestation duties of C00034 are not split or omitted. The complete
keyed result is joined only after every child finishes and reproduces exactly.

This reduces the mandatory output unit, not the semantics or acceptance bar.
The captured six-check group now requires six sequential child calls instead
of three, sharing the same 900-second review deadline. Primary packing 8,
host concurrency 4, attempt limit 2 and stage timeout 1,800 remain unchanged.
Single-check requests remain unpartitioned. A child limit or timeout still
fails closed; no partial aggregate is accepted or response reused.

The old keyed two-check version retains its three-check threshold and exact
schema/prompt/response reconstruction; old array versions remain unchanged.
Tests pin the captured old wire and prompt hashes, preserve all current
per-check schema fields and source context, exercise both receipt consumers,
and verify the shared deadline. These offline checks establish representation
and replay behavior only, not a successful native run or Word acceptance.

## De-duplicate focused orientation context

The captured C00034 single-check request also serialized the complete focused
check twice: once in `checks`, where its result was required, and again in
`orientation_only_checks`, alongside the other five source checks. The new
`whole_candidate_keyed_check_1_v2` policy keeps the complete selected check in
`checks` and puts only the other source checks in `orientation_only_checks`.
Together they preserve the whole source group once per child; no duty, check,
source span, linked requirement or schema constraint is removed. The prior
`whole_candidate_keyed_check_1_v1` policy remains reconstructible for existing
receipts.

This is a measurable reduction of repeated request text, not a proven cause or
guaranteed cure for `max_output_tokens`. The runtime has not exposed the
numeric output budget or a supported Codex CLI setting to raise it. A new
native run is still required to learn whether this input reduction changes the
observed failure; all child responses and the exact-union receipt checks remain
mandatory.

For the latest real run `f9a581b9-0a44-4f64-9721-971c748a9224`, the current
code reconstructs the failed v1 child prompt byte-for-byte (95,673 bytes).
The v2 projection produces an 83,029-byte prompt, removing 12,644 bytes
(13.2%). The serialized provider response schema remains 37,882 bytes under
both versions, so this repair reduces repeated input context; it does not
reduce the output schema or establish that the native output-token failure is
fixed. These measurements are local reconstructions, not a new model call.

## Compact prompt JSON encoding

The `whole_candidate_keyed_check_1_v3` policy keeps v2's complete one-check
focus and whole-source orientation context. It serializes the same packet as
compact JSON in the Codex prompt; it does not remove fields, source text,
source spans, obligations, or schema constraints. The v1 and v2 prompt
encodings remain available for exact historical receipt replay. Because each
new policy marker binds a new request identity, generated opaque source-span
IDs are freshly bound while their source field, offsets, text, and source hash
remain unchanged.

For the latest failed C00034 v2 packet, the indented prompt payload was
60,103 bytes and the same parsed JSON object in compact form is 41,995 bytes,
a reduction of 18,108 bytes. This measures input serialization only. It does
not establish a corresponding token reduction or show that the
`max_output_tokens` failure is resolved; that requires a separately authorized
fresh native run, and output-limit failures still fail closed.
