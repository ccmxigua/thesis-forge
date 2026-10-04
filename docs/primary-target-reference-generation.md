# Explicit independent target agreement

The fresh BSU run `2910c153-7cd8-409e-a504-0aacb6e6a9b6` exhausted
chunk 2 on a typed target mismatch. The primary target was `一级学科：`;
the independent reviewer returned `一级学科` while explaining that the colon
did not change its meaning. This captured rejection remains a rejection.
Neither punctuation removal nor lexical similarity proves semantic agreement.

The source-reference wire contract now also permits an explicit reviewer
selection: `target: {"primary_target_ref": "<current primary obligation id>"}`.
That atom must select the same `primary_obligation_id`. Only that check's
current, nonempty, known primary target is selectable. The compiler resolves
the exact selected representation and records the primary hash and resolved
target hash alongside the source-selection receipt. It never rewrites a
literal target, infers equivalence or supplies another typed field.

The reviewer must assess the source first. A primary target is an untrusted
claim, not source evidence. Different, unsupported or unresolved targets and
newly discovered duties remain expressible as literal strings. All source,
identity, coverage, modality, applicability, condition and exact typed
alignment validators still run. A reference is not a compliance verdict or
submission permission. Historical literal responses compile unchanged.

Scoped retries retain the original wire choice of validated siblings,
including references. Even replacing a retained reference by the same
literal target is a forbidden payload change. Both receipt consumers replay
the immutable raw response and current request; old source selectors or
tampered compiled targets fail. The generation pipeline now recognizes the
existing typed-alignment corrective feedback with the same source and
candidate binding checks already enforced by the bridge, followed by the
persisted parent-rejection and scope replay.

Offline tests cover the captured rejection, explicit synthetic agreement,
renamed identities, foreign references, other typed disagreements, immutable
sibling choices and the real bridge's two-call correction/persistence path
with both consumers. Synthetic agreement is not a fresh provider result.
Full-suite results, fresh BSU generation and actual Word acceptance must be
reported separately.
