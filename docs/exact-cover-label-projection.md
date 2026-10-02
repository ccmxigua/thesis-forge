# Exact cover label projection

`exact_cover_label_terminal_colon_v1` restores a missing ASCII or full-width
terminal colon only when one currently cited source occurrence supplies that
exact character. It applies to an already-declared `title_zh` / `title_en` field
with an explicit `always` label-display policy. It does not infer a role from
a heading, alter whitespace, or merge repeated physical source occurrences.

The bridge requires a previously validated current chunk projection, complete
invocation fingerprints, and the complete error bundle recomputed from the
frozen candidate. The source-fragment binder verifies text, offsets, evidence
membership and source SHA-256. Duplicate or ambiguous bindings are refused.
Only the label is changed; field identity, order, value binding, requirements,
classifications and obligations are preserved. Audit records retain the old
and new label, source fragments and hashes, and response before/after hashes.

The result is a partial candidate, not acceptance. The production repair loop
reruns the full shared validator before applying another rule or authorizing
a bounded model retry. Independent review remains required, and this receipt
cannot establish document compliance or submission readiness.

Offline replay of the 2026-10-02 `91bf579` failed BSU chunk 1 demonstrates the
combination: first restore the cited title label's colon, then migrate both
security fields using the existing administrative-region rule. The independent
second title occurrence still lacks a derived requirement and remains blocked.
The empty second response remains rejected, not replaced with its parent.

Regression coverage in `tests/test_cover_label_terminal_colon.py` includes
renamed clauses/evidence and labels, both delimiter widths, wrong hashes,
foreign evidence, ambiguous occurrences, incomplete identity, stale feedback,
nonexact text, immutable unrelated fields, idempotence and empty retries.
