# Source obligation scope assessments

The review-draft scorecard has two distinct inventories:

* **Diagnostic items** are serialized DOCX checks, findings, and manual-review
  markers. Their count is not the number of independent source obligations.
* **Scoped obligation assessments** are computed only from a current source
  atom, an explicit object/condition scope inventory, and current property
  receipts explicitly mapped to that atom.

The assessment is a review aid. It does not determine school importance, create
an importance-weighted total, authorize submission, or replace final human
approval. Importance remains not assessed; review priority is restricted to
sorting only, without a compliance or release effect.

## Scope inventory

The source-obligation-scope-inventory schema defines the optional
--obligation-scope-inventory input to scripts/apply_format_spec.py. It binds
the inventory to the run, case, source bytes, and format specification. Each
scope binds:

* an exact source clause and span hash, plus evidence IDs;
* the explicit object reference and condition;
* the complete set of source atom IDs for that scope;
* property receipt IDs that provide evidence for each atom; and
* an optional applicability quotation when the source explicitly excludes the
  scope.

Typed atom source context and semantic fields are recomputed from the accepted
semantic ledger. The scope compiler rejects altered source spans, object or
condition values, foreign atoms, stale run/source/spec bindings, duplicated
atoms, and missing receipt IDs. Repeated receipts for one atom add evidence,
not denominator items. A complete inventory requires a source-first human
attestation bound to the current source SHA-256. Legacy units remain unknown;
their presence prevents declaring the full inventory complete.

An omitted inventory is represented as scope_inventory_missing. The
scorecard shows no obligation coverage or satisfaction percentage and reports
legacy unknown unit counts separately. It does not derive obligation counts
from diagnostic item totals or property receipt multiplicity.

## Outcomes and ratios

For an explicitly complete scope with known applicability:

* **Assessment coverage** is the fraction of applicable atoms with a decisive
  current observation (satisfied or failed).
* **Satisfaction ratio** is emitted only when every applicable atom has a
  decisive observation and each atom's force is known.
* Satisfied plus unresolved is shown as partial verification with unknown
  work; it does not produce a satisfaction percentage.
* Satisfied plus failed, with no unresolved atoms, is shown as partially
  satisfied, with the observed child-atom ratio.
* A failure plus unresolved evidence keeps both counts and is reported as
  partially assessed with failures.
* An incomplete scope, unknown/conflicted applicability, unknown force, or
  stale serialized-document evidence cannot be counted as a pass. Human- or
  input-owned atoms remain pending; a property receipt cannot resolve them.

These are scoped child-atom ratios. This first version has no importance
weighting and does not add child weights to a parent-level score. Any future
cross-scope weighting requires a separately approved, versioned policy.

## Independent submission gate

scripts/obligation_submission_gate.py independently checks the raw scope
inventory, current authoritative source units, and serialized property
receipts. It does not read scorecard percentages or trust stored aggregate
labels. Missing or incomplete scope inventories, unresolved required or
prohibited obligations, stale receipts, and source-binding errors block
submission readiness. An observed required/prohibited failure is a hard
blocker; an unknown force or applicability remains unresolved. The existing
serialized-package, render, template, metadata, and manual-review gates still
apply independently.

The audit result is emitted as obligation-assessment-audit.json; the scorecard
machine projection is emitted as obligation-assessment.json and is also
embedded in draft-scorecard.json. The review-draft DOCX displays a
plain-language summary that is checked against the JSON card. Review drafts
continue to carry submission_ready=false.

## Historical scorecards

Historical receipts and scorecards are immutable evidence. A migration report
must retain the exact parent document and card hashes, identify per-item
identity/status changes, and report when an older audit is bound to a different
DOCX. Missing historical scope or atom data stays unknown; migration does not
retrofit source semantics or applicability.
