# Blank date context and execution edges

The context-edge repair can propose separation of an informational, zero-duty
date blank from a cover requirement when current source geometry proves a
unique immediately preceding field label in the same complete, unmerged row.
It does not infer field meaning from proximity, confirm date formatting, or
assert that an approval, signature, or other external action occurred.

Eligibility requires current authenticated source projection and invocation
fingerprints, the complete current validator error bundle, exact source spans,
valid row/text hashes, a unique retained full-span label clause, and one retained
field with a registered date metadata type and matching metadata binding.
Filled dates, merged/RTL/nested/ambiguous geometry, duplicate owners or fields,
missing inventories, payload source selectors, and unrelated contract errors
are not repaired this way. No school, clause, evidence, or run ID is hardcoded.

Only requirement clause/evidence execution edges change. The field payload,
primary reviews and external obligation inventories remain unchanged. The
receipt records the original requirement, detached exact source, owner field
and row geometry. All clauses, including detached context, remain in the
independent source-first review request. A full validator pass produces only a
candidate pending independent review, never a publication or compliance pass.

Regression coverage includes a captured primary response from commit 220c73f,
renamed current source IDs, multiple mechanical errors, idempotence, unchanged
payloads/inventories, retained independent checks, stale/partial authorization,
wrong metadata binding, ambiguous table geometry and filled dates. Run:

```sh
python3 -m unittest discover -s tests -p test_context_relation_projection.py -v
python3 -m unittest discover -s tests -p test_table_structure_review.py -v
python3 -m unittest discover -s tests -v
```

These offline checks do not replace a fresh native-provider BSU run or the
required DOCX, Word, PDF and draft/release acceptance checks.
