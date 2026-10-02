# Redundant blank signature print proposals

`source_bound_signature_block_projection_v2` handles the same redundant blank
signature block whether the native proposal uses exact `body_parts` or only
`signature_placeholders`, and whether its verification mode is `external` or
`static_docx`. These are print representations, not completed signatures.

The captured incident had a complete current-source declaration already
printing the exact adjacent author/date line via `source_signature_lines`, plus
a separate static-DOCX placeholder requirement linked to an external-only
clause. The old projection required external verification and copied body
paragraphs, so it could not remove the redundant placeholder-only object.
Execution-edge selection was **not** the defect: physical source selection
already permits non-executable source fragments outside the execution edges.

Removal requires the complete authenticated current validator feedback, one
uniquely selected retained declaration, exact source/group/adjacency/hash
binding, identical insertion anchor, and proof that every removed placeholder
is already printed by the retained source line. No existing identity, field
key, applicability, input prerequisite, registered checker or extra body
operation can be discarded. Stale feedback, filled/nonadjacent lines,
ambiguity, conflicts and any unrelated validator error remain failures.

The audit preserves the whole removed proposal, its print representation and
verification mode, the retained owner, source group/lines/hashes and unchanged
pending reviews. Complete validation and fresh independent source-first review
remain required. Classification, typed human actions and their pending status
do not change; a score or printed line does not establish attestation.

The same captured response contained a separate heading-only declaration
object, accepted locally but rejected at merge for missing fixed body text.
Contract 3.0 now rejects a standalone, source-selected heading-only entity
with `declaration_source_text_not_materialized` before independent review,
matching the existing merger boundary. Existing source-selector diagnostics,
scalar-body exactness paths and legacy 2.1 continuation fragments are not
replaced by this diagnostic; this is not a redesign of every declaration form.

`source_bound_declaration_heading_coalescence_v1` can remove that heading-only
object only when the exact current heading belongs to one complete retained
declaration already printing its full heading/body. It unions the two
previously supplied executable clause/evidence relations in source order;
it does not invent a new source edge or change a classification. The retained
print payload and every review stay unchanged. Conditions, existing identities,
input prerequisites, registered checkers, extra placeholders, source ambiguity
or foreign text reject this path. Both projections must leave a fully valid
candidate and require fresh independent review; audits preserve both original
objects and the exact relation union.

The captured two-attempt offline regression is not evidence of a completed
fresh BSU, merged format specification or Word visual acceptance. Its isolated
serialization test covers only the retained complete declaration resource.
The added integration regression also exercises the complete captured
candidate through the real local merger and resource allocator without the
previous declaration/heading conflicts. It still is not a real model or Word
acceptance test.
