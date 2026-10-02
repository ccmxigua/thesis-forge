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

There is no fabricated aggregate independent-review receipt or reuse of old
accepted responses. On another output-limit failure, record the explicit
nonretryable code `independent_obligation_review_output_limit` and stop. A
fresh smaller target may help multi-occurrence blocks; an oversized single
atomic group requires a separately designed, fully bound partition protocol.
This change does not yet implement that protocol. Final DOCX/Word acceptance
and `submission_ready` remain independent gates. Native service route and
actual output token limit remain unobserved.
