# Empty informational inventories

The local v3 contract allows informational and not-applicable reviews to omit
their optional `obligations` array. Strict native providers represent optional
fields with `null`, and may also emit the schema-valid empty array. These three
representations contain no atoms. Comparing omission against `[]` as a semantic
edit incorrectly rejects otherwise authorized, source-bound retry proposals.

`normalize_native_response` now gives these representations one normal form:
the explicit empty array, but only for reviews not referenced by requirements
or diagnostics. This is limited to the v3 top-level `clause_reviews` path and
those two classifications, under a selected current schema branch explicitly
declaring an optional array with no positive minimum. It does not normalize nonempty or
malformed inventories, required arrays, unknown fields, other array-valued
properties, or executable/pending/unknown classifications. Referenced reviews
retain the distinction between omitted and explicit empty inventories: context
edge projection requires the explicit empty assertion in addition to source
grammar, current source authority, and subsequent independent review. No such
proof may be synthesized by normalization.

The decoded raw artifact is preserved. Normalization is idempotent and precedes
both candidate preparation and retry comparison. Nothing authorizes changing a
classification, deleting a real atom, or changing a source edge. Source-bound
retry grants and fresh independent review are unchanged; a failed independent
review still prevents acceptance and merging. Historical receipt hashes are not
rewritten or reused as evidence of a new run.

Tests cover null/omitted/empty forms, schema restrictions, malformed and nonempty
values, neighboring real-duty deletion, changed classifications and source links,
and the production bridge's two-attempt publication-policy proposal path. That
path retains the raw empty array, accepts only its three authorized semantic
changes, invokes independent review, and refuses merging when that review fails.

Offline replay of the captured 2026-10-03 chunk 6 removes only the informational
review's empty-array difference. The three remaining changes have the existing
`v3_source_bound_publication_inventory_reassessment` authorization. This proves
the local retry boundary, not live independent-review or document acceptance.
