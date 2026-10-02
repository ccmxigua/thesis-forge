# Separate empty proposals without resolving source ambiguity

## Observed failure

The fresh `5641ebc` BSU run stopped at chunk 6 after one primary attempt.
The exact source occurrence was a numbered literature-review heading. Its
primary review remained `unresolved`, but a newly proposed `body_text`
requirement had only null/empty properties. The unchanged validator correctly
rejected both the empty payload and its non-executable clause relation. With no
proved conservation projection, the bridge recorded `repair_plan_unavailable`;
the two-attempt limit does not authorize a semantic rewrite by itself.

Neither a guessed heading level nor a blanket deletion rule is appropriate.

## Bounded representation projection

`_project_unresolved_empty_requirements` accepts only the complete, freshly
recomputed validator bundle: one empty-payload diagnostic and one non-executable
relation diagnostic for each proposed object. It requires the caller's validated
current chunk hash, full invocation fingerprints, exact source spans and
self-identifying evidence, a single current clause/review, and an unchanged
`unresolved` review with no authored obligation inventory to discard.

Existing requirement selectors, field bindings, nonempty properties, unknown
schema payloads, fragment selectors, conditions, prerequisites, verification
checks, reported conflicts and registered executable source facts refuse this
projection. Mixed or incomplete error bundles also refuse it; other established
repair paths and failure-closed behavior remain responsible for them.

The only mutation is separating the invalid object from the executable
requirements list. No requirement/property is invented; every other requirement
and review remains unchanged. The full separated proposal, its reason and role,
source occurrence, validator bundle and before/after hashes are retained in the
mechanical repair audit and representation-bound transaction. This audit is not
retry deletion authority and does not say the source has no obligations.

## Independent and release gates

The complete local validator runs again; the production bridge then performs a
fresh source-first independent obligation review. An empty independent inventory
for an unresolved clause still fails. Source-bound ambiguity may be retained,
but recognized unrepresented duties and invalid coverage still block according
to the existing contract. An accepted review packet is not a compliant DOCX.

The clause remains unresolved in compliance records and blocks full compliance
and submission. Review-draft processing must preserve pending human decisions;
the projection records `submission_ready=false`. Scoring and red markers do not
override source identity, executable coverage or publication gates.

## Regression scope

`tests/test_unresolved_empty_proposal.py` uses generated clause/evidence IDs and
different heading text, multiple shells, exact audit conservation, nonempty
neighbor preservation, stale source/span/provenance/validation markers, wrong
evidence, duplicate/incomplete diagnostics, substantive payloads and controls,
registered source facts and fresh independent acceptance/rejection. Native CLI
and reviewer calls in these tests are offline doubles; captured BSU replay and
fresh BSU validation are recorded separately under ignored `build/`.
