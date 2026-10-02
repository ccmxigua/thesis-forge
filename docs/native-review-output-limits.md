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

## Bound independent output partitions

For a newly built obligation-coverage request with more than eight checks,
code records policy `whole_candidate_checks_4_v1`. The Codex native reviewer
generates at most four check results per invocation. This is an engineering
packing policy, not a measured token limit or guarantee of successful output.
The primary source group, candidate and entire request remain immutable.
Other host adapters retain their existing complete-review route; no host or
model substitution is performed.

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
