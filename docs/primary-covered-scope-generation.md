# Primary covered-scope generation constraint

Fresh BSU run `54f4e84e-3c7f-45b0-b41f-0f769ee23864` stopped on C00003,
the exact source text `10043`. Its first proposal described the filled value
as sample content but emitted an obligation with `status: covered` and
`applicability: unknown`. The unchanged local contract rejected this pair as
`undecided_scope_cannot_be_covered`. A retry changed applicability to
`not_applicable`; the existing semantic-change guard rejected that unreviewed
change. No chunk, merged review or review-draft DOCX completed in that run.

The initial prompt already required pending treatment for unknown scope, but
the primary generation schema did not express the local cross-field rule.
New contract-3.0 primary generation now uses complete object `anyOf` branches:
`covered` permits the existing `applicable` and `not_applicable` values; all
other statuses retain every original applicability value. This mirrors only
the current local predicate. It does not establish that a not-applicable duty
is covered, or that a specific classification is correct. Full source-bound
contract and independent semantic checks still decide acceptance.

The schema does not choose a scope, change a classification, fill an obligation
or remove a duty. All force values, including `unknown`, remain available;
optional conditions and every other atom field retain their original schemas.
An informational/sample-content proposal with no obligations remains
representable. These are model choices to be checked against the exact source,
not automatic repairs of the captured failed answer.

Only `primary_generation_schema` changes. Canonical/historical schemas,
ordinary retry schemas, local validation, scope-proposal limits, independent
review, provenance and release gates stay unchanged. Complete alternatives
survive native projection; conditional `if`/`then`, `not` or `allOf` assertions
would be omitted on this provider wire and therefore do not implement the fix.
Tests exercise every status/applicability/force combination with and without
a condition, the captured invalid proposal, the unchanged retry rejection,
empty/pending representations, and legacy/independent compatibility. Offline
passing results do not demonstrate fresh BSU or Word acceptance.
