# Source-backed title labels and date-context reassessment

The fresh `cf3ecfe` BSU failed after a retry changed a valid printed title-label
policy and selected one occurrence of an ambiguous date quotation. The first
attempt had a field-name-only schema restriction: `title_zh` and `title_en`
could never use `label_display_policy=always`. That restriction disagreed with
the source form's explicit title label and with the general prompt policy.

## Contract changes

- Both title fields retain their existing value-only default (`with_value` or
  absent). No document acquires a title label automatically.
- An explicit `always` policy can preserve a printed title label. The host
  validator requires exactly one current linked source occurrence matching
  that label, with valid source spans and a linked evidence ID. Invented labels,
  ambiguous occurrences, changed source hashes and foreign edges fail closed.
- The renderer honors an explicit source-backed policy for titles too; it does
  not fabricate a metadata value, approval, or an official table layout. The
  source label survives DOCX serialization. Default bare-title behavior stays.
- Removing the invalid field-name restriction leaves the captured candidate's
  remaining diagnostic as a quote-only error. The existing bounded primary
  retry can then explicitly select its current clause's full date context.
  Code still cannot choose between repeated normalized date substrings. A
  full-context selection is semantic reassessment, not mechanical equivalence,
  and must undergo fresh independent source-first review.
- The original model retry, which also changed the label policy, remains
  rejected. This repair does not authorize unrelated edits, increase retries,
  change primary classifications, delete obligations, or waive release gates.

## Evidence boundaries

`tests/fixtures/title-label-date-context-incident.json` contains the actual
rejected repair baseline and selected source context with original input/file
hashes. Tests rebuild the response schema from current code, rather than
reusing the obsolete captured schema. The tested full-context response is an
explicit offline proposal, not a claimed captured or fresh model result.

Regression checks cover the captured failure, source and edge tampering,
unrelated label changes during quote retry, legacy title output and serialized
source labels. Existing quote-reassessment bridge tests still require a fresh
independent review and stop on its disagreement. Offline success is not a
fresh BSU or Word visual acceptance; `submission_ready` remains false.
