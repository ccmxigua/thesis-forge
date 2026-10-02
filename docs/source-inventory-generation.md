# Portable source-inventory generation

The ea54c09 fresh BSU stopped at chunk 4: both independent provider attempts
returned eight `uncertain` results with empty inventories. The existing
source-bound correction path ran but did not yield a valid result. The native
schema projector deliberately omits `minItems`; text guidance alone cannot
enforce a nonempty inventory in that schema.

New coverage generation uses `source_inventory_first_remaining_v1`:

```json
{"identified_obligations": {"first": null, "remaining": []}}
```

That empty shape is offered only for `consistent`. Every other generated
verdict requires an actual source atom as `first`; `remaining` preserves the
rest in order. `consistent` may also contain faithful source duties. Shared
atom definitions keep the source-bound unions compact. This is a portable
JSON object/enum/anyOf/ref constraint, not a provider-specific array-length
keyword, a school-specific rule or an automatic semantic decision.

The compiler copies and converts the envelope into the unchanged canonical
array. It never changes verdicts, atoms, source selections or their order.
Closed keys, null-head/nonempty-tail and malformed atom checks fail closed.
Raw output is immutable; compilation records both raw and decoded hashes and
converted check IDs. Historical arrays remain replayable. Normal textual
content reviews, primary responses and canonical schemas are unchanged.

Retry locks operate on decoded canonical wire arrays. Parent replay and both
completed-chain consumers rebuild from raw source evidence, so merely
resealing a success audit or scope proof cannot authorize a different result.
The existing two-attempt correction budget and semantic validators remain.
Retained inventories keep their local count bounds and their exact first
atom in generation. Native array keywords still cannot enforce every tail
count or order (an existing provider limitation); full ordered equality is
therefore always checked locally against the independently validated lock.
An adapter that ignores generation constraints can still return invalid raw
data; that data is preserved and rejected, not silently approved. Real
ambiguity may be a valid review result but remains non-executable/pending;
this representation does not resolve it or authorize submission.

Regression evidence includes both captured chunk-4 requests/raw/compiled
attempts at ea54c09, all diagnostic verdicts with renamed IDs, exact source
selection and ordered nullable normalization, malformed/foreign source
rejection, native persistence and both consumers, and historical array
replay. Captured artifacts are evidence, not a successful real model run.
Full-suite and fresh-BSU results must be recorded separately after execution.
