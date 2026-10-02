---
name: thesis-latex2docx
description: Convert thesis LaTeX or DOCX files using a school's .doc/.docx formatting requirements, with evidence-bound semantic review performed by the current host Agent model and deterministic local DOCX validation. Use when a user asks to format, audit, or produce a thesis DOCX from an unseen university template.
---

# Thesis LaTeX/DOCX Formatting

This project is a host-runtime skill. The Agent invoking it supplies semantic
interpretation with the model it is currently using. The deterministic Python
commands do not require a Codex executable: any agent with local file and
Python access can use the packet workflow below. Automatic invocation exists
only for hosts with an implemented native adapter. Python does not gain access
to an arbitrary chat agent's model merely because that agent loaded this file.

## Ordinary conversation: start here

Use this skill directly in the invoking conversation. The current Agent does
the semantic reading with its current model; do not launch another agent CLI,
choose a model, set provider credentials, or edit global settings by default.
A host needs file read/write access and a Python execution tool with the project
dependencies. A chat without those tools cannot execute the local pipeline;
report the missing capability rather than claim universal execution.

1. From the skill directory, run `python3 scripts/thesis_format.py
   /absolute/requirements.docx /absolute/thesis.docx`. Omitted execution mode
   prepares packets without calling a provider. Omitted `--work-dir` creates a
   unique directory under the caller's `build/`; retain the printed `work_dir`.
   An optional output path at this stage is not a generated document.
2. Read that run's `review/requirements/host-agent-review-manifest.json` and
   **all** `llm-request-chunks.json` packets. In this conversation, produce each
   declarative response at its requested `batch.response_filename`. Follow
   each packet's schema, evidence and provenance. Do not reuse old responses.
3. Run `python3 scripts/merge_host_agent_review.py <work>/review/requirements
   --response-out <work>/review/host-agent-response.json`. This validates and
   merges locally; failed contracts must be corrected, not bypassed.
4. Run `python3 scripts/thesis_format.py /absolute/requirements.docx
   /absolute/thesis.docx /absolute/review.docx --work-dir <work>
   --llm-response <work>/review/host-agent-response.json`. A packet response with
   no native audit defaults to the existing non-release draft workflow. No
   extra host/model/offline option is needed. The original run, sources, merge
   receipt and code fingerprint must match. Existing native audits do not
   silently fall back to this path if invalid.

These commands are orchestration steps for the Agent, not options the user
must configure. Use absolute script paths when running outside the skill
directory. DOCX input needs no LaTeX converter; `.tex` needs Pandoc and legacy
`.doc` needs an available Word conversion tool. The skill never selects a model
for this conversation or verifies an unexposed model identity. Missing
independent review remains explicitly unverified, and `submission_ready=false`.
Inspect generated drafts and report unresolved duties; creation is not Word
visual acceptance. Formal submission still requires the release gates below.
Only explicit authorization for `--auto-host-agent` selects the separate native
subprocess workflow and its model policy. A requested pinned BSU benchmark is
that exception, not the ordinary skill default.

## Operating contract

- Preserve the current Agent model for every semantic review decision.
- Treat every user-supplied requirements/template `.doc` or `.docx` as a fresh full-review
  input: extract it again and send the resulting evidence-bound clauses to the
  current Host Agent for contract-3.0 semantic review. Contract-2.1 remains a
  read-only compatibility path for already-bound legacy responses; new native
  runs must use 3.0. Existing files, template
  profiles, or prior responses must never silently disable this path.
- Do not set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `THESIS_FORMAT_LLM_*`, or a
  provider/model override for this skill. The bridge may copy the current
  parent session's route; `--host-agent-model` is reserved for an explicit
  intentional override, not an independent provider client.
- Keep semantic output declarative JSON only. Never let a model emit OOXML
  edits, shell commands, or executable code.
- Treat the local scripts as the authority for extraction, provenance,
  contract validation, merging, formatting, and release gates.
- Treat the frozen request and its deterministic chunk projection as immutable
  source data. The bridge and merge stages rebuild and compare that projection
  before accepting any native response; a model-supplied hash or provenance
  replacement is never an authorization to continue.
- Bind every batch case, fresh run, code fingerprint, and explicitly confirmed
  thesis profile into the request/runtime receipt. A profile may resolve
  conditional metadata only when its schema, confirmation flag, and source-byte
  provenance are valid; missing metadata remains unresolved and is not guessed.
- A batch manifest may name a per-case `thesis_profile` for confirmed metadata;
  the batch runner passes it through as an explicit source-bound input rather
  than treating `template_profile` or a prior run as a metadata substitute.
- Fail closed when a clause is missing, ambiguous, unsupported, stale, or not
  backed by the supplied evidence. Never fill gaps with a plausible guess.

Metadata prerequisites may select the exact aggregate key `thesis_profile`
for the whole supplied profile, or a registered dotted field/sub-object.
Only `kind: metadata` may use this aggregate. It resolves the current explicit
metadata object (or the explicitly namespaced legacy profile), never unrelated
source data. Supplied stable field types and contradictory profile inputs are
checked; presence is not completeness, provenance validation, approval or
compliance. Specific missing fields require their own prerequisites and checks.
The whole profile and `thesis_profile.cover_metadata` are distinct scopes;
neither code nor schema feedback may silently substitute one for the other.
Raw responses, source bindings and ordinary retry/independent-review gates
remain unchanged.

## Existing requirement reference integrity

Typed obligation quotations bind to the exact current evidence occurrence,
not to a normalized clause string. An exact quote may include the surrounding
source sentence, but this context never broadens the selected clause's
execution scope. Source hashes, offsets and evidence links must still match;
an unrelated occurrence cannot supply the quote. For validator-named metadata
errors only, code may reconstruct a whitespace/edge-punctuation-equivalent
quote from its original source span, or derive a route from the unchanged
classification and obligation status. A proper clause subspan may restore
whitespace runs only when every nonblank token and delimiter is unchanged and
the current source has exactly one matching occurrence; repeated or stale
subspans remain rejected. Never broaden that quotation to the whole clause.
When an exact unique current-evidence context quote omits only the selected
span's leading enumeration marker, code may restore that original marker
without trimming or adding any prose. Dot-numbering must be followed by
whitespace; decimal quantities, negation, heading words and semantic prefixes
are not recoverable fringes. Recovering a numeric marker does not establish
whether the source is a heading, numbered duty or normative instruction; its
classification and typed semantic fields stay unproved by this operation.
The quote must already contain all remaining selected prose,
and all neighboring context is preserved, not promoted to execution scope.
Record the original/recovered quote, exact offsets, source binding and
enumeration-recovery policy. Independent review still scopes its reading to
the selected clause and must assess the unchanged typed semantic fields.
Preserve the immutable raw response and
record the source-bound repair transaction. No actor, action, target, force,
condition or classification is guessed or changed by this projection, and
complete contract validation plus fresh independent semantic review remain
mandatory before acceptance.

A repeated whitespace-normalized atom quote is not mechanically recoverable:
code must not pick a first/last occurrence. With complete current validator
feedback and a receipt-bound parent, a primary retry may explicitly select
only the same clause's exact complete source span as quotation context. This
bounded semantic reassessment may change only the named `source_quote` fields;
every atom field, condition, status, route, requirement edge and ordering stays
unchanged. Both old and new quotes, ambiguous match count, full source binding,
validator/parent/candidate hashes and the reassessment policy are recorded in
the retry authorization ledger. It is not code-proven equivalence or a broader
execution scope. An already valid quote, lexical change, foreign/stale source,
unrelated error or another semantic edit does not qualify. Complete contract
validation and a fresh independent source-first review of the new candidate
are mandatory; independent disagreement still fails closed.

After the unchanged-candidate independent review budget is exhausted, a pure
typed `target` and/or `condition` disagreement may authorize one separate primary
proposal under a shared one-shot scope-proposal budget. Ordinary parse/schema
repair attempts cannot consume this opportunity before a valid candidate first
reaches independent review. The bridge reconstructs the complete rejected
source-bound feedback before reserving it and records both budget categories,
the feedback/candidate/source hashes and the reservation in the attempt receipt.
At most one scope proposal may start per chunk; changing from target to condition
does not reset that budget. Another disagreement or
any failure of that proposal is terminal even if ordinary slots remain. This
does not expand ordinary retries or authorize other typed dimensions.
Code reconstructs the complete
current source review and rejects missing/duplicate/foreign selectors, changed
source/run identity, a disputed actor/action/quote/force/applicability, or any
other invalid check. Only the exact named atom target/condition fields may
change; the reviewer does not dictate a value. A whole paragraph and an
individual sentence are not automatically equivalent, and an empty or
`unknown` target cannot avoid the typed comparison.
Only for a named condition disagreement, an existing field bound to the same
exact printed source label may additionally declare
`label_display_policy: always`. A target-only dispute never authorizes cover edits.
The primary
keeps value policy, value bindings, source quotes, all other semantic fields
and the graph fixed.
Raw and projected stages are checked separately against the same authorization,
and the new candidate must pass full validation plus a fresh independent review.
Rejected artifacts never become a successful ledger or submission evidence.

Cover field `label_display_policy` defaults to `with_value` for compatibility;
`always` is an explicit source-supported rendering duty, not an inference from
missing metadata. An optional empty value can therefore leave its label visible
without fabricating a value, placeholder, approval or trusted-data receipt.
Administrative labels with this policy are printed blank while approval/value
verification remains unchanged. Serialized cover audits check the labels and
do not count them as trusted metadata. Real Word/visual acceptance is separate.

Printed 一级学科 and 二级学科 labels bind independently to
`thesis_profile.cover_metadata.first_discipline` and `.second_discipline`.
The normalizer copies only their explicit source metadata; neither is inferred
from a major, `degree_discipline`, `field_name`, or the other level. These are
optional profile fields until a current source requirement needs them. A
missing required value remains a placeholder/pending input, not a verified
discipline value. Do not attach an unresolved discipline duty to an executable
cover requirement; the mixed-relation contract remains fail-closed.

Native structured-output schemas are provider projections, not the local
contract. Unsupported composition constraints (`allOf`, `not`, `if`/`then`/
`else`, and non-portable `oneOf`) remain in the local schema and are described
but omitted from the native wire schema. Preflight rejects any such keyword
left on the wire, as well as a constraint-only schema without a concrete shape.
Every returned candidate must still pass the unchanged local contract.

Existing requirement IDs are selectors into the current input, never IDs for
the model to allocate. Each freshly prepared chunk constrains the selector to
its supplied candidates before computing its request hash. A new requirement
omits `existing_requirement_id` (`null` in native structured output); only the
deterministic merger assigns its final ID. The shared chunk/merge validator
checks exact role, clause set, evidence set and canonical source occurrence.
Only after that identity matches may the existing deterministic properties be
projected, with before/after hashes in the merge audit and raw output preserved.
Unknown IDs, wrong occurrences and malformed references fail closed at chunk
acceptance. Retry feedback must not authorize guessing a replacement, deleting
the ID to disguise a mismatch, or turning that integrity error into a red
manual-review placeholder. Semantic uncertainty remains eligible for explicit
review-draft markers under the separate policy below.

### Document-wide English font scope

An explicitly document-wide English-font rule must not be narrowed to
`body_text` alone. For the narrowly recognized document-wide wording, code may
complete the current source-bound catalog's text-role references only when all
catalog roles, exact properties, source spans and evidence match. It never
changes a manual classification, fabricates missing catalog values, or guesses
which clause/occurrence a model meant. Preserve the immutable native response
and separate `document_font_projections` hashes; the completed candidate still
requires the ordinary contract and a fresh independent obligation review.

The recognized “论文中出现英文时” condition is compiled from current source
text into `source_inventory.english_text` / `present` / `null` on every catalog
and completed requirement. Completion requires the candidate to retain that
exact condition; missing/changed conditions, additional conditions, exceptions
and input prerequisites are not silently repaired or discarded. Missing
inventory facts remain unknown at capability preflight. The font executor
operates only on directly observed Latin runs and records their count as local
execution evidence, not as an inferred global fact or submission approval.

The formatter applies the bound Latin font directly to editable WordprocessingML
English runs, including tables, headers, footers, hyperlinks, text boxes and
note parts. It leaves the East Asian font slots and text unchanged. The
serialized `document-font-audit.json` and final post-Word format comparison
check the actual run properties, not just the paragraph style. This wording
does not itself authorize changing standalone numbers, symbols, or mathematical
fonts. Compiler and merge keep this source's font out of general role styles;
formatter and comparison use the same filtered role view for legacy specs.
All source-bound role requirements remain intact for the dedicated executor.
A separate unconditional general-font requirement may retain that role's font;
the English-only source cannot supply unrelated font or paragraph properties.
Latin text in OMML/DrawingML that this executor cannot validate remains
an explicit technical verification blocker, not a successful font check or a
red author TODO. OOXML font values also do not prove that the required font is
installed or that Word rendered the expected glyphs: real renderer acceptance
is still required.

For contract 3.0, a requirement linked only to clauses classified
`external_compliance` is not a DOCX requirement. The bridge may remove that
invalid requirement object only when the exact current validator records,
source spans, clause evidence, and a non-empty `unverifiable` obligation
inventory prove that every linked clause is an external pending action; it
must preserve the clause reviews and obligations unchanged. Native structured
output may encode a new requirement's absent `existing_requirement_id` as
`null`, and an absent/null `verification` is treated as no verification claim
for this removal check. Any non-null existing ID or non-null verification mode
other than `external` remains ineligible. A removable edge must be either a
single `body_text` source echo whose text exactly equals one linked clause's
verified source span, or a role-schema-declared all-null/empty properties shell
with a matching `empty_requirement_properties` validator record and an
explicitly external verification mode. The latter has no DOCX operation; its
raw response remains in the original sidecar and the removed object in the
repair audit. A conditional all-null external shell is also removable only
when its external verification, exact current-source binding, and pending
obligations pass the same checks; its complete applicability remains in the
repair audit as **pending human scope**, not a verified condition. Non-null
role-specific properties, field keys, input prerequisites, altered text, and
mixed/local payloads are not removable by this source-echo/null-shell rule.
After projection the complete contract and independent source-first obligation
review must pass; the external actions stay pending and block submission.
Mixed executable/external edges, empty/orphan clause/evidence relations, stale
records, or failed source/evidence binding must fail closed; the bridge must
never guess or attach an orphan to a clause. A source clause is also ineligible
when code recognizes any local machine-checkable obligation or a conservative
local-action cue together with an external physical action (for example,
`封面应有学号` plus an advisor signature, or a repeated table header plus a
seal). These guards reject projection; they do not infer that an arbitrary
clause is purely external. The independent source-first review remains required
and must reject external-only status whenever its exact source packet contains
code-known local DOCX obligations.

There is a separate conservative administrative-copy repair, not a general
external-requirement deletion rule. One uniquely identified executable table
may retain all DOCX operations while a model has copied its properties onto
pure pending approvals/seals or onto a redundant default-public requirement.
The bridge authenticates the complete current validator bundle, exact evidence
spans and physical locations. The heading, operative default-public paragraph
and table must be adjacent; another possible table, heading, unknown scope or
conflicting rule disables this repair. Only the unique source-compiled
default-public clause may complete a missing table edge. Every removed payload
must be contained in the retained table with identical values and source order;
field order numbers may be relative to the copied subset, but their relative
sequence cannot change. An executable duplicate must have identical properties
and no unique source edge. Identities, prerequisites, unique properties, altered
conditions or mixed duties are not removable. Complete removed objects, current
source spans, run provenance and before/after hashes stay in the repair audit.
Classifications, obligation inventories and the retained properties never
change. The full contract and fresh independent source-first review are still
required; pending actions remain pending and never authorize submission.
Other layouts are not declared unsupported: they simply receive no mechanical
copy repair and still require their ordinary source-bound semantic review.

A distinct field-instance requirement must not be deleted merely because it
copied an administrative qualifier. For the exact default-public policy and
shorter-duration flags rejected by the current validator, code may remove only
the misbound copies when exactly one valid current-source table requirement
already retains the identical values. The field instance's clause/evidence
edges must be contained in that same physical table, and the typed scope must
match, including exception lists. Requirement count, identities, fields, source regions, prerequisites,
checks, applicability and clause-review atoms remain unchanged. Unknown,
conflicting, stale or ambiguously retained values fail closed; the complete
contract and a fresh source-first review still run. Both original copies and
the unchanged authoritative source requirements remain in the repair audit.
This is qualifier-copy cleanup, not a semantic merge or submission approval.
Different exception text never proves equivalent scope. With complete current
validator feedback, a receipt-bound primary retry may explicitly propose
removal of just the rejected qualifier copies. Code reads that proposal as a
limited patch onto the authenticated parent, rather than accepting the whole
model replacement. It retains both requirements' original exception lists and
all source edges; unrelated raw edits are preserved as discarded observations
in the retry receipt. Other values, options, identities or requirement counts
cannot authorize this patch. It is a semantic reassessment proposal, not code
resolving exceptions, and needs the ordinary source-bound retry authorization,
full contract validation and a new independent review before chunk acceptance.

When one response has both a non-removable external-only requirement and a
different unbound requirement with no clause/evidence links, the two errors do
not form a mechanical relation-addition retry. Stop before sending a parent
retry, retain both raw/error records, and require a fresh source-bound semantic
review. The external requirement may carry conditional meaning, while the
orphan may not be assigned a guessed relation; deleting both just to satisfy
the schema is not an authorized correction. Initial Host Agent generation
should emit only the executable cover structure and put real-world consent,
application, and approval in distinct pending clause-review obligations. An
empty/default cover shell is never an additional requirement.

For a source-first review that explicitly authorizes a single existing-content
verification reclassification, the authorization hashes identify the
source-materialized candidate, not the raw provider JSON. On retry, reproduce
both complete native candidates from the immutable raw responses and the current
chunk before comparing candidate hashes. Only the named classification paths
may change in the model proposal; all other raw fields stay frozen. If that
classification requires a different responsibility route, only the existing
metadata projector may derive it for validator-named route errors on the same
reviews, with a separate source-bound projection proof. It cannot change atom
content or attest that human verification occurred. Changed source links,
unrelated payload changes, stale hashes, or incomplete path/source authorization
still fail closed. The complete validator and a fresh independent source-first
review must pass before acceptance; submission readiness remains false.

Fixed-declaration candidates are source-text groupings, not executable-clause
lists. A requirement may link only clauses independently classified as
executable/covered/verify_existing/executable_with_external_check and evidence backing those clauses; the
nested `source_evidence_ids` selects the exact current source paragraphs to
print. Printing an approval, signature, or seal instruction does not attest
that the real-world action occurred. Administrative approval/marking regions
remain conditional cover structures, not fixed declarations.

The declaration's optional `source_fragment_clause_ids` is likewise a render
selector, not an executable obligation edge. It may include informational
headings/connective context without obligations and external pending clauses,
only as one unique complete current heading/body grouping selected by the same
nested source evidence. The heading need not be an executable edge. Every
external extra clause retains nonempty human, unverifiable obligations;
exact current source spans and locations, full paragraph coverage and
role-native text are still checked. No requirement edge or review changes,
and the projection audit lists render-only and executable clauses separately.
Other roles keep the ordinary selector-subset restriction. Signature lines
remain separately bound, not part of the heading/body selector.
A clause with
both a DOCX action and a distinct real-world approval, consent, signature, or
seal action may use `executable_with_external_check` only after a source-bound
review identifies at least one covered DOCX obligation and at least one
`unverifiable` external obligation. The requirement edge applies only to the
DOCX obligation. Independent review must map each obligation one-to-one,
leave the external action pending, and emit a current-run-bound manual marker.
This state is never submission-ready. A mixed edge without that complete
inventory is a semantic split failure: retain the raw response and source
references, stop the mechanical retry, and require a fresh source-bound
review. A missing edge caused by that mixed parent is diagnostic,
not permission to add a title-only declaration. Empty declarations that cannot
be materialized from current evidence block merge; they are never silently
reclassified as informational. Draft markers may show pending actions but
never make a document submission-ready.
Each `external_compliance` review lists distinct source-grounded actions as
`obligations[]` with `status=unverifiable`; the independent source-first review
must keep each action pending and unlinked to a DOCX requirement, mapping each
independently identified action to exactly one current primary obligation ID.
The mapping is structural evidence, not deterministic proof of semantic
correctness; disagreement remains a failed review. A blank
inventory or a claimed covered external action is a contract error.

## Explicit semantic issue acknowledgements

Independent source-obligation reviews do not invent duties for a mere label,
heading or description. A source-first conclusion that no duty exists retains
exact evidence references and rationale with an empty obligation inventory;
the primary informational classification alone is never proof of that conclusion.
`represented` always requires at least one current, same-check requirement
selector. The generated review schema excludes represented status where no
such selector exists and constrains valid selectors otherwise. Raw responses
remain immutable; parsing never deletes a supposed obligation to make it pass.
An unlinked represented claim is rejected with a typed contract error and may
receive at most one independent corrective read of the unchanged candidate.
Real omitted duties must stay unrepresented or independently justified pending
work. The retry does not authorize primary reclassification, invented links,
new properties, a passing score, or submission readiness. Exhaustion fails closed.
Native providers that omit unsupported `minItems` still require the local
non-empty-reference check; schema generation is not proof of semantic completeness.

`pending_work_code` identifies a registered exact-source human-work atom,
not every task that needs a person. Its per-check wire alternatives are derived
from the current source grammar, never copied from another clause's inventory.
Only a source-content-verification pending item with no requirement link and
a selected current span covering that code's exact source atom may select it.
Generic provenance/topic checks and other duties omit
the field (native: `null`) and keep their exact quotation and human disposition.
They are not deleted or converted to executable work. A wrong code is rejected
by the immutable source-reference contract and can receive only its existing
bounded independent corrective read; no automatic relabeling or release waiver.

Source-atom coverage uses a complete one-to-one assignment when exact quotations
overlap; a greedy first match must not consume the only atom for another duty.
One represented atom can never cover two distinct source facts. Typed-primary
mapping, quotation or condition disagreements remain rejected, but may receive
one fresh independent read of the identical candidate. Corrective feedback is
bound to current checks, primary identities and content hashes, provenance, run
and candidate hash. A reviewer may preserve an agreed interpretation's exact
representation only after reassessing the source; code never fills semantic
fields from the primary answer. Both failed and corrected artifacts are retained,
and persistent disagreement or changed inputs fail closed without a success ledger.

An independent response whose current, complete, unique check set fails the
source-reference wire schema may receive one fresh corrective read of the same
candidate. Feedback preserves the rejected result, per-check/atom diagnostics,
original request and schema hashes, current checks, provenance and candidate
identity. Replaying that feedback cannot authorize semantic edits or passing
projections. Unknown, duplicate or missing checks do not use this correction
route; unchanged local and semantic validators still reject invalid results.
Old source selectors belong only to the rejected invocation, never to its retry.

Fixed declaration text can be materialized for a source-bound mixed clause only
with distinct automatic/covered and human/unverifiable atoms and exact quotation
bindings, while retaining the original clause classifications and all atom fields.
The source-selected prose is not evidence that an author attestation occurred.

An exactly blank author/supervisor signature or date paragraph immediately
following one uniquely selected complete declaration may be preserved in that
declaration's `source_signature_lines`. Each line carries its exact current
evidence ID, byte hash and `placeholder_presence_only` scope. It is not added
to the declaration's executable clause edges and does not change external
signature/date obligations into covered duties. Signed/filled text, ambiguous
ownership, other physical regions or altered source bindings are ineligible.
The model omits `source_signature_lines` (must emit `null` on the native wire),
including its text, evidence IDs and hashes. Only code generates these fields
from the current source. The local declaration/resource schemas and source
binding checks remain unchanged; an already supplied wrong hash is rejected,
not replaced with current identity. Raw output and code projection stay distinct.
The run-scoped resource digest includes the exact lines; serialization audits
check their text and position after the body. Legacy neutral placeholders
continue to work, including additional labels not represented by source lines.
A validator-rejected separate signature-only declaration can be removed only
when the complete current feedback and exact adjacent source group prove that
one retained declaration already prints every line, with no unique instance,
condition or DOCX operation lost. Preserve the removed payload and pending
reviews in the repair audit; full validation and fresh independent review are
still mandatory. This is not a general external-requirement deletion rule.

A validator-targeted retry may remove adjacent blank signature paragraphs
from a declaration's heading/body `source_evidence_ids` only when the model
selects one exact current declaration group. Code never chooses the selector.
The entire current error bundle, parent hash, request/clause hashes, source
spans and invocation fingerprints are checked. Every other raw field stays
unchanged, including source literals and pending human obligations. The
compiler then prints the exact heading/body and separately bound blank
signature lines. Retry receipts distinguish model selector changes from
code-owned literal materialization; a supplied comparison is not proof.
Full validation and a fresh independent source-first review remain mandatory.
Foreign evidence, duplicate groups, filled/non-adjacent signature lines,
stale errors or another model edit cannot use this correction path.

When a user confirms that an unresolved clause is a real semantic ambiguity but
does not provide its authoritative interpretation, record that acknowledgement
in a run-bound `semantic-issue-confirmation` sidecar and pass it with
`--semantic-issue-confirmations`. The pipeline validates the clause, question,
evidence, case, run, and source hashes and writes a bound
`semantic-issue-ledger.json` and a separate
`semantic-issue-confirmation-receipt.json`. This is an audit disposition, not a Host Agent
classification: the original question and `unresolved` review remain intact,
and no requirement or formatting property is created.

The acknowledgement may let analysis and explicit `supported_subset` previews
continue, while the report remains marked
`analysis_ready_with_confirmed_semantic_issues`. Full compliance, submission
readiness, release, and real batch execution remain blocked with
`confirmed_semantic_issues` until an authoritative interpretation is supplied.
Other unresolved clauses are unaffected. Never add a global rule for a clause
ID from one run, and never use `--allow-unresolved` as a substitute for this
bound record.

## Requirements input normalization

The written requirements input may be an OOXML `.docx` or a legacy binary Word
`.doc`. A `.docx` is validated and used directly. A `.doc` is converted
automatically by a deterministic local converter (LibreOffice `soffice`, with
macOS `textutil` as a fallback) into a new isolated per-invocation DOCX. The
original is never overwritten, and no normalized artifact is reused silently.

The audit trail is written to the stage-specific requirements directory:
`<work-dir>/review/requirements/requirements-input-manifest.json` for the
prepare/review stage and `<work-dir>/execution/requirements/` for the final
deterministic stage. It records the
original path/kind/size/SHA-256, converter command/tool/version, normalized
path/size/SHA-256, validation result, and conversion status. Missing converters,
converter failures, and invalid DOCX output fail closed with no semantic work
or document formatting performed.

Normalization is byte/file handling only. Every freshly extracted clause from
the normalized DOCX still goes to the current Host Agent for the complete
contract-3.0 review. The original `.doc` identity and SHA-256 remain the source
provenance anchor; `rule_only` and `known_template` are not fallback paths.

## Host-native workflow (default)

The default workflow is orchestrated by the host Agent that is currently
executing this skill.  The Python pipeline performs deterministic preparation,
then either stops at evidence-bound packets or, when `--auto-host-agent` is
explicitly requested, invokes the native adapter for the declared host.  The
current host Agent must read every packet, write the declarative JSON responses,
and invoke the local merge and formatting stages; an adapter is only a native
CLI bridge for that same host and never a cross-host substitute.  A Python
subprocess does not call back into the current chat session implicitly.

Prepare a fresh run with:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/host-review-$(date -u +%Y%m%d-%H%M%S)
```

Then the current host Agent must read the manifest and all chunk requests,
write one response per requested filename, and run the offline merge followed
by the final deterministic pipeline.  It must not copy a response from an
earlier run or silently hand the semantic work to another host.  When the
current host does not expose an automatic adapter, this packet workflow is
the supported path; the missing capability is reported rather than replaced
by OpenClaw, Claude, Codex, or another installed program.

The packet workflow is portable across Codex, Claude, OpenClaw, and other
hosts that can read/write the packets and run Python. It is a **non-release
review-draft** path unless the host supplies the full current-run independent
review receipts and final Word acceptance. The host identity and the
model/provider identity are separate
dimensions.  If a host does not expose a model name, record it as
`unobservable`; do not infer it from an installed CLI or from a model label.

## Native host adapters (explicit opt-in)

`--auto-host-agent` is selected only after the execution context explicitly
declares `THESIS_FORGE_HOST_RUNTIME` (or the matching `--host-runtime`).  The
runtime declaration is the dispatch boundary: `openclaw` selects the explicit
OpenClaw adapter, and `codex` selects the native `codex exec` adapter.  An
unknown or currently unsupported host stops with an actionable error instead
of calling an installed program from another environment.

The Codex adapter uses an ephemeral, read-only `codex exec --json` invocation
per chunk, validates the JSONL terminal event and binds the terminal message to
the captured invocation, then applies the same contract, merge, and
deterministic DOCX gates as the packet workflow.  The automatic bridge stores
the raw response and binds the current request provenance itself; the model is
not asked to copy long hashes.  It does not read OpenClaw sessions or accept
OpenClaw route parameters.  The Codex CLI's provider/model identity is not
inferred from the binary name; when it is not exposed, the run audit records
route visibility as `unobservable`.

The explicit automatic subprocess's native Codex default is `gpt-6-luna`,
not the default model of the skill or the current conversation. Omitting `--codex-model`
uses this versioned project policy for both primary and independent review;
the post-format review follows the selected host model unless explicitly
overridden. Use `--codex-model` or `--semantic-review-model` for an explicit
override. This does not modify global Codex settings or other native hosts'
routes. An unavailable model fails closed; it is not replaced with an older
model. Requested model identity is recorded separately from actual route
visibility, which may remain `unobservable`.

For an explicitly requested native reasoning effort, pass
`--codex-reasoning-effort max` (or the exact model-advertised value). The wrapper
and batch runner forward it to primary extraction, every bounded independent
review retry, and the post-format semantic review. Direct post-format callers
use `--semantic-review-reasoning-effort` with runtime `codex`. Omission preserves
the native default; it never changes timeouts, retry budgets, provider, or
authentication. Audits record the requested effort separately from the observed
effort, which remains unavailable when the native stream does not expose it.
A locally accepted configuration string is not proof that the model supports
or actually executed that effort. No effort fallback is permitted.

For compound native-review failures, repair authorization is bound to the
complete validator bundle and its exact parent candidate. Empty obligation
inventories may be completed only at named current-source targets; typed atoms
and their derived schema errors require full response revalidation. Other
classification, requirement, condition or provenance changes are not granted
by a missing-inventory finding. Fresh independent source-first review remains
mandatory after this bounded correction.

Registered abstract quality enum flags can be compiled from an authenticated
exact Chinese-abstract source span into one already-linked target. This adds
guidance only, never creates a parent, resolves ambiguity, changes an obligation
inventory or hardens a soft rule. Enum flags use a canonical set order across
attempts; original response, current span/hash and projection receipts remain
separate. Unknown, negated, quoted, conditional or conflicting source targets
remain fail-closed. Compilation and retry authorization are not release proof.

For a Codex host, use the current Codex CLI and its configured native account:

```bash
THESIS_FORGE_HOST_RUNTIME=codex \
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/host-codex-$(date -u +%Y%m%d-%H%M%S) \
  --host-runtime codex \
  --auto-host-agent \
  --codex-bin "$(command -v codex)"
```

The same selection applies to the ten-school batch runner.  Do not pass
OpenClaw-only options such as a parent session key or provider/model route to a
Codex run; the command fails closed if they are supplied.

### OpenClaw adapter (explicit opt-in)

When this explicit adapter is authorized, a complete fresh path can be run
with:

```bash
THESIS_FORGE_HOST_RUNTIME=openclaw \
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/host-openclaw-$(date -u +%Y%m%d-%H%M%S) \
  --host-runtime openclaw \
  --auto-host-agent \
  --host-agent-parent-session-key '<exact-bound-parent-session>'
```

The parent session key must be supplied by trusted invocation context or an
explicit argument.  The bridge never selects a recent Telegram session, a
global default, or another "most active" session.  `--host-agent-model`
selects an intentional OpenClaw route only; it does not prove that the current
caller is the parent session.  A missing or conflicting host/session binding
fails closed before semantic requests start.  The adapter records the
runtime, invocation, parent session, expected/observed route, and verification
status in the run audit.  Local process exit does not by itself prove that a
remote Gateway operation was cancelled.

All hosts use the same response contract and offline merge.  OpenClaw is a
legal host and is not prohibited; it simply cannot silently substitute for a
different current host.

The deterministic `rule_only` and `known_template` modes are compatibility and
development modes only. They are never selected implicitly for a user input;
they require an explicit low-level mode choice, and the batch runner additionally
requires `--allow-supported-subset`.

## Review-draft policy for human decisions

When a user wants the pipeline to produce an editable artifact while leaving
uncertain semantic choices for manual review, use:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex review-draft.docx \
  --work-dir build/review-draft \
  --output-policy review_draft \
  --llm-response build/review-draft/review/host-agent-response.json \
  --offline-review-draft
```

The pipeline writes a run-bound `manual-review-items.json` sidecar and appends
the corresponding `MR-0001`-style items to the DOCX in red text with a pale
highlight. Red markers are reserved for choices or inputs a person must
resolve: unresolved semantic ambiguity, missing user/source input, genuinely
unverifiable runtime checks, missing official templates, unresolved style
mappings, or a native semantic reviewer that returns `uncertain`. A detected
semantic violation is recorded as a finding, not disguised as an uncertainty
marker. Deterministic formatting repairs (for example, a declared keyword
separator change that preserves each keyword) are applied by code and rechecked.
Deterministic format/property/coverage failures and render evidence required by
the current contract remain in their technical reports and keep `format_ready` or
`submission_ready` false; they are not red TODOs for the author. The serialized
marker layer rejects technical categories even if an old or hand-written ledger
contains them. A red marker never means that a requirement passed.

`review_draft` is explicitly non-submission: it sets `submission_ready=false`,
keeps `format_ready=false` when any technical finding remains, defers the strict
official-template comparison and Word/PDF release audit, and records
`draft_manual_review`/`review_draft_pending`. A diagnostic draft may be emitted
as an inspectable artifact (`diagnostic_draft_generated`). An accepted review
draft requires a current-input-bound scorecard, complete receipt identities,
valid DOCX package, completed native checks and visible review markers.
Formatting shortfalls and source-bound semantic disputes may remain explicitly
unmet/pending for human review; `valid`/`format_ready` are not rewritten to true.
Safety, identity, invocation and serialization failures stop the current case
and a fail-fast batch. Draft acceptance is not a formatting or release pass.
The ten-school batch runner defaults to this policy. Use
`--output-policy submission` only after the red items are resolved and the
submission-mode run has been started fresh; that mode retains the full
fail-closed capability, render, and final audit gates.

`--allow-offline-review` is the host-neutral packet path for non-release
review drafts and offline tests. It requires
`--output-policy review_draft` and cannot be combined with
`--require-submission-ready` or `--strict-release`; a supplied response without
current host receipts must never be presented as submission-ready.

### Visible review-draft placeholders

Every ledger item must have one visible `MR-xxxx` marker in the editable DOCX.
Markers use Chinese red text (`C00000`), pale-yellow shading (`FFF2CC`) and an
explicit East Asian font (`Noto Sans SC`); make that font available to the target
renderer and verify actual glyph rendering, not merely text extraction.
The renderer must not replace Chinese explanations with English-only labels.

The current sidecar contract is `manual-review-ledger.schema.json` 1.3 with
`manual_review_obligation_v1`. Each atomic manual obligation receives a
full-length `MO-<sha256>` identity derived from the current ledger binding and
the complete canonical semantic obligation payload. Append-only `producer_records`
are preserved as provenance but excluded from the MO digest so adding another
producer cannot invalidate existing DOCX or audit references. `MR-xxxx` is only
the document-facing label; it is not the obligation identity. Deduplicate only
exact normalized semantic duplicates after defaults are applied, preserving all
producer records; never merge different obligations by unioning selected fields
or keeping whichever record arrived first. Multiple independent obligations
for one clause must remain distinct. Validation recomputes and checks MO IDs at
sidecar ingress, filtering, serialized DOCX audit, and batch acceptance. The
DOCX audit verifies the MO→MR→serialized-paragraph crosswalk exactly once and
rejects missing, duplicate, unexpected, or misbound IDs. A complete current-run
binding is required to create or mutate a ledger. All legacy or unversioned
ledgers are rejected by mutation helpers and must be rebuilt from the current
run; do not promote them by relabeling their version. Batch acceptance compares
the marker receipt payload and stored serialization audit against the live
ledger and freshly audited DOCX, not merely item counts. None of these checks
asserts that the upstream semantic source-obligation inventory is complete:
when its producer says inventory is incomplete, preserve that fact and do not
infer missing obligations downstream.

Contract 3.0 also writes `obligation-shadow-graph_v1` inside the semantic
review ledger. It is an analysis-only, current-response-bound projection of
source clauses, model-declared obligation records, code-compiled source facts,
and requirements. It preserves explicit clause links and compiler candidates
without claiming semantic coverage, execution, or successful verification.
Importance and force remain `not_assessed` unless a separately validated
source supplies them; no requirement is removed or demoted from this graph.
The graph also publishes a deterministic 0–100 `review_priority` for each
source clause, keyed to its recorded review classification and current source
hash. This is a work-queue score only: unresolved/unreviewed items come first,
informational items last. It is **not** a school's normative importance score,
does not assert source-obligation completeness, and has no effect on
requirement coverage, capability, human-review, or submission gates.
Separately, explicit `review_draft` runs use
`evidence_based_draft_scorecard_v1` for **score-first draft triage**. Code grades
each recorded property/check: verified = 100 verified points; failed,
unverified and human-pending = 0 verified points. The equal-weight average is
only the percentage of *observed checks verified*, not normative importance,
model confidence, complete source coverage, or permission to submit. Every
item retains its expected/actual values, sources and original result in
`draft-scorecard.json`; the DOCX contains a readable numbered score section
and asks for human review of unmet/pending items. Counts and scores are
recomputed at batch acceptance against the current ledger and serialized DOCX.
Known capability shortfalls and actual formatting failures may remain in this
non-release draft only when accounted for by its bound scorecard. Source-bound
independent coverage disputes stay `incomplete` in the raw response and AO
ledger, with `completed_with_disputes` describing the review operation, not
successful coverage. Such disputes are human-review items, never invented
requirements or semantic reclassifications. Missing/duplicate references,
invalid schema, stale provenance, incomplete receipt inventories, invisible
markers, corrupt packages and failed native invocation still fail closed.
Submission/full gates remain unchanged; even 100/100 cannot promote a draft.
Always keep `submission_ready=false` and perform real Word visual acceptance
before describing the scored artifact as visually verified.
The graph is validated against `obligation-shadow-graph.schema.json` before it
is persisted. The manual-review ledger separately records an AO→MO→MR
crosswalk plus clause/requirement/question/evidence IDs copied from each same
ledger item, binds it to the current manual-review binding and semantic-ledger
digest, and keeps `submission_ready=false`. The DOCX marker must visibly retain
any AO identity it references; this is identifier traceability, not proof that
the human review has been completed.
Every AO link must include its full obligation-analysis identity; the AO digest,
run ID, case ID, clause, source reference, source hash, and selected range are
recomputed and matched against the current manual item before the crosswalk is
accepted. An unbound or stale AO is rejected rather than rendered as a current
review obligation.

Only code-owned role/property paths may anchor a marker beside source content.
Table-related notes go outside the table. Free-text questions, measurements
such as `3cm`, and missing official templates must never trigger a guessed cover
or abstract location. Unlocated items go at the front under
`待定位人工处理（审查草稿，不可提交）`, each with its MR ID, source excerpt, reason,
and editable handling prompt. Full original text stays in the bound ledger;
placement never changes unresolved status, requirements, or source paragraphs.
When a source obligation asks that existing thesis content, data, figures,
citations, or keyword choices be traceable to a source artifact that is not
included in the independent-review request, record a separate
`source_content_verification_pending` human check. “Not included in this review
request” does not mean the user never supplied that artifact. Do not reclassify
existing content as missing author-written content or ask the author to rewrite
it: the marker requests a human source-location check and remains a submission
blocker. For the explicit requirement that keywords originate from or be
selected from the thesis, emit a visible, source-bound red marker asking the
user to record where each keyword appears in the thesis. This is a code-owned
human-verification route, not an automated semantic match: it never passes the
requirement and blocks submission until the user supplies a verified result in
a fresh run. Deterministic code binds the exact quote, source span, evidence,
review request, candidate response, and current run. Exact correction notices
such as “The following English is not correct.” without an identified target or
approved replacement stay as scope-unresolved human markers; never reinterpret
them as author instructions or fabricate a correction.

If the primary response labels an exact existing-content verification
obligation `informational`, the independent review may request one bounded
correction. The bridge may change only that clause’s classification to
`requires_source_verification`, and only when the current source/evidence and
reviewed candidate are bound, the requirement graph and all other semantic
fields remain unchanged, and the corrected candidate passes the complete
contract and independent review again. This correction does not verify the
thesis content, satisfy the source obligation, or open a release gate. The
analysis ledger records a canonical `work_type` for each obligation; scope
dependency dimensions are valid only for `scope_clarification`, and unknown
work types fail closed rather than disappearing from the manual-review output.

`manual-review-marker-audit.json` reopens the serialized DOCX and checks unique
coverage, red text, shading, non-hidden text and CJK font binding. Batch
acceptance independently repeats the check; a JSON marker list is insufficient.
Word may normalize direct formatting into inherited styles; both are inspected.
The package audit deliberately leaves `visual_verification=required`. Exported
page count, extractable text and a successful renderer exit are not visual QA.
Inspect Word/PDF pages for readable Chinese, complete MR markers, tables,
overlap and pagination. Keep `submission_ready=false` throughout draft review.

## Two-stage workflow (packet-only/debug)

### 1. Prepare evidence packets

Run preparation first. It performs fresh deterministic extraction and stops
before generating a formatted DOCX:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex \
  --work-dir build/host-review \
  --prepare-agent-review
```

For a DOCX input, replace `input.tex` with the source DOCX. The preparation
manifest is:

```text
build/host-review/review/requirements/host-agent-review-manifest.json
```

Read the manifest and every request in
`build/host-review/review/requirements/llm-request-chunks.json`. Process **all**
chunks with the current host Agent model. For each chunk, write the exact JSON
contract to the filename in `batch.response_filename`, normally:

```text
build/host-review/review/requirements/llm-response-chunk-0001.json
```

Each manually supplied response must satisfy the request's `response_schema`
and must:

1. include that chunk's `provenance` object unchanged; automatic native
   bridge responses are bound by the bridge instead and retain the pre-binding
   raw response for audit;
2. include the packet's `contract_version` (`"3.0"` for a fresh run; `"2.1"`
   only for an explicitly bound legacy packet);
3. include every supplied clause exactly once in `clause_reviews`;
4. use a non-empty `reason` on every clause review and requirement;
5. cite only supplied clause/evidence IDs and allowed roles/properties;
6. for contract 3.0, put the authoritative relation only in
   `requirements[].clause_ids`; do not emit `clause_reviews[].requirement_indexes`.
   The bridge derives that reverse view deterministically. Contract 2.1 keeps
   zero-based `requirement_indexes` only for legacy compatibility, with an empty
   list for non-executable classifications;
7. report uncertainty as `unresolved`, `requires_metadata`,
   `requires_source_content`, `unsupported_backend`, `unverifiable`, or another
   contract classification rather than inventing a requirement.

If a source clause explicitly states both that an unapproved thesis is public
and that the administrative item is blank for a public thesis, retain **both**
effects on its source-linked cover requirement:
`non_public_administration.publication_default_policy="unapproved_is_public"`
and `public_policy="blank"`. The two effects need separate obligation entries.
This source rule does not establish whether a real approval was granted.
The condition “unapproved” does not itself instruct anyone to obtain approval.
For the complete, source-bound two-effect policy recognized by the compiler,
an added unverifiable approval atom is rejected locally. A bounded primary
retry may propose removing only the unsupported atoms, preserving both covered
policies, the requirement graph, and neighboring approval duties verbatim.
That proposal is not a code deletion or proof of equivalence: its complete
current-source feedback, full contract validation and fresh source-first
independent review are required before acceptance. Quoted, incomplete, extended
or unrecognized prose remains on the ordinary semantic-review path.

An administrative approval/marking sentence is not an administrative field
table. If the current source does not name exact fields such as an approval
number, approval date, security marking, or embargo range, never emit
`cover.non_public_administration.fields: []` as an executable requirement and
never invent those fields. Preserve any independently supported fixed
declaration text, remove the incomplete administrative requirement relation,
and keep the affected administrative obligation as a visible
`requires_source_content` manual-review item. Full/submission gates remain
fail-closed; only `review_draft` may continue with the red placeholder.

Do not combine chunks manually and do not copy a response from an earlier
run. The provenance hash binds the response to the exact fresh extraction.

The packet manifest also records request/body/envelope/file hashes, runtime
context, and the complete chunk list. Native runs record a lifecycle entry for
every chunk and attempt, including not-started, retrying, terminated, and
remote-unobservable states. A failure never produces a partial merged response,
and an existing response, audit, or attempt file is never reused. Contract 3.0
also writes a deterministic `semantic-review-ledger.json`; it records accepted
requirement edges, explicit model-supplied semantic obligations, and a separate
code-compiled inventory of narrowly recognized source facts. The model does
not need to echo compiler IDs: the shared validator checks each known fact
against its linked role-specific requirement properties, and the ledger records
those candidate bindings separately. This inventory is a mechanical floor,
not a claim that every natural-language obligation can be compiled; semantic
decomposition remains separately required. Before a native chunk is accepted,
the bridge makes a separate source-first coverage-review call through the same
current host route. It must cover the exact clause set, cite source text, bind
to the candidate response hash, and account for each identified obligation;
an incomplete or failed review prevents chunk acceptance and merge. This is a
fresh second review, not a different provider/model or a guarantee of
statistically independent judgment; unobservable host model identity remains
unverified. For Codex, a structured terminal `turn.failed` that explicitly
reports model-capacity exhaustion may trigger one bounded retry after a
five-second backoff. The retry uses the same requested model, run, candidate
response, and provenance, but a fresh attempt directory recorded in the audit.
Each response representation is stage-labeled and separately hashed (decoded
raw, normalized raw, unaccepted repair base when partial mechanical repairs
leave residual errors, projected candidate, validated candidate, accepted
response). A retry uses the latest receipt-verified semantic baseline whose
validator feedback it actually receives. When that is an unaccepted repair
base, the prompt, parent file digest, error ledger, and semantic-change
authorization all bind to that same repair base; the decoded raw response is
preserved and compared separately as an observation, never silently promoted
to the authorized parent. Otherwise decoded raw is the parent. Projected
candidates are compared only to projected candidates, and neither comparison
may conceal a model-side change. A retry-parent digest must identify the exact
sidecar file that the prompt reads; a mismatch fails closed. Stop a retry
without another model call only when the normalized raw response, blocker/repair
plan, candidate state, and complete source/clause/evidence/run/case/chunk/schema/code
invocation fingerprints are all unchanged. Missing or malformed fingerprints do
not count as proof of no progress. A successful semantic retry requires its own
decoded raw sidecar. If an undecodable attempt intervenes, semantic change
authorization comes from the most recent earlier decoded attempt that actually
failed review; the parse failure remains separate retry feedback and cannot
replace that semantic blocker.
In v3, code may collapse requirements that are exactly identical in every
field, including all source edges, before local validation. The original raw
response and an index/hash audit remain intact. The same deterministic
projection must yield the identical validated candidate whether applied to
the raw response or its already-collapsed form before it can narrow a retry
comparison; any differing payload or indexed clause review stays rejected.
For a mixed relation containing executable clauses and informational context,
code may detach only context edges whose unchanged primary review explicitly
has `normative_basis: insufficient` and an empty obligation inventory, with no
known source-compiled duty. The exact complete paragraph must additionally
match a closed nominal-heading grammar without normative/action/numeric-limit
cues; unknown prose remains on the ordinary semantic review path. This requires authenticated current-source chunk
projection, exact spans and locations, complete invocation fingerprints and
the complete current validator bundle. All retained execution inventories must
be covered; external/unresolved edges, existing requirements, literal selectors,
conflicts and payload references to detached source IDs are ineligible. Source
clauses and reviews are never removed or reclassified. The receipt preserves
the original requirement and full detached context/evidence, while the retained
payload, conditions, prerequisites and checks remain unchanged. The complete
validator and a fresh source-first review must still pass; a mistaken primary
informational label is not proof of absence of duties. This is candidate repair,
not coverage approval, score-based acceptance or permission to submit.
The source compiler may also create a keyword-content requirement without a
model-created parent only when the current evidence verifies the complete
source span and that whole sentence uniquely states the keyword placement,
semicolon separator, and qualified count guidance. “Generally” or “usually”
is recorded as nonmandatory guidance, never as a hard minimum or maximum.
Different or conflicting source wording is not completed by guesswork. For a
standalone heading, the compiler may bind a text requirement only when its
current evidence occurrence is the unique verified semantic-role anchor and
the role schema permits a text property. Matching heading words at another
source occurrence are not sufficient: the review remains unresolved for a
fresh source-grounded decision. Neither clause IDs nor school names authorize
these projections. The original model response, exact source and anchor
hashes, before/after response hashes, and projection policy are retained for
accepted and rejected attempts. Code compilation does not certify DOCX
compliance or make a draft submission-ready.
When source-first review identifies only a human verification duty for existing
content, a bounded primary retry may change `informational` or an unsubstantiated
`requires_source_content` classification to `requires_source_verification` only
when exact current-source quotes, evidence, run bindings, and an unchanged
requirement graph authorize that single-field correction. A `requires_source_content`
classification with any explicit authoring obligation is never eligible; the
verification remains pending and blocks submission. A model may split one
registered source-origin check into several `requires_source_content`
sub-obligations; their count alone does not create an authoring instruction.
Code may retain every original sub-obligation in the projection audit and
classify the current source as human verification only when all statuses and
identities are well formed, no executable requirement is linked, and the
source has no explicit authoring cue. Independent review still re-reads that
same source, and a mistaken multi-item authoring disposition receives only a
bounded re-review, never automatic approval.
For typed inventories this projection additionally requires the exact current
evidence binding, explicit normative basis, a complete pure-verification source
grammar, conservative verification actor/action/target grammars, and no
conflicting or executable edge. Unknown typed wording does not take this
code-projection path; it retains the ordinary semantic review path. It retains every typed atom
and all semantic dimensions; only the classification and responsibility
status/route become `requires_source_verification`, `unresolved` and `human`.
Display reasons are code-owned pending-verification explanations; contradictory
model reasons stay in the original raw/audit rather than becoming author TODOs.
Original and projected inventories remain separately hashed in the audit.
An incorrect typed action is not thereby proven correct: fresh independent
review must still map and assess every atom. A mislabelled authoring disposition
may receive the same bounded unchanged-candidate reread for this proven typed
pending shape; actual semantic disagreements and retry exhaustion still reject.
For source-owned author work items, an explicit positive section-writing or
research-summary instruction may coexist with a separate quality prohibition
such as not copying literature. Code recognizes the positive instruction in
unchanged source spans; it never drops the negative quality requirement or
authors the missing content. A model-selected quote must be unique, unquoted,
and authorized by its full current clause text, so selecting only the tail of
a conditional, example, layout demonstration, or conflicting instruction
cannot manufacture an unconditional author task. Unknown wording stays
unresolved. Accepted author work remains `source_content_pending`, with every
original obligation retained; it is not executable coverage or permission to
submit. A later fresh run must revalidate these decisions under the new code.
An `unresolved` primary review caused by backend inability to check a registered
keyword-origin duty may use the v3 deterministic classification projection only
when the entire exact current evidence span matches a closed pure-verification
grammar, the normative basis is explicit, all primary obligations are unresolved,
and no linked requirement or reported conflict exists. Additional formatting,
authoring, conditions, or unknown wording disable this projection. The full
original review and obligations, current evidence hash/span, run provenance,
and sequential before/after hashes are retained in success and failure audits.
Independent review must still identify the source-bound pending human duty;
validated receipts carry it into the manual ledger and visible red draft marker.
Neither this projection nor a confidence/priority score authorizes submission.
For a text property rejected only because its literal differs from the uniquely
bound current source span by Unicode whitespace, the native bridge may copy the
exact source text into that candidate field. It must validate current
invocation fingerprints, cited primary evidence, span offsets, and source hash;
preserve the raw model response; record before/after and source hashes; and rerun
the full validator. This projection cannot change punctuation or any
non-whitespace character. Missing, stale, foreign, or ambiguous source bindings
remain failures; the rule does not normalize source evidence or weaken the
exact-literal contract.
For a validator-targeted literal conflict, a retry may retain an already valid
`source_fragment_clause_ids` selector and change only the named `properties.text`
to the exact current-source composition, or to `null` for deterministic
materialization. A selector change is not required when it is already correct.
Authorization still checks the receipt-bound parent, current source hashes,
evidence, offsets, role schema and unchanged requirement graph. Selector-only
errors do not authorize unrelated text edits; guessed text, stale evidence,
changed classifications and other payload changes remain rejected.
Repeated fixed literals at distinct source locations are separate content
instances, not competing global role values. Emit one text requirement per
occurrence and use its own fragment selector; omit/null the literal when code
can materialize it. The native bridge may partition an aggregated relation
only when every cited clause independently carries the same complete fixed
wording (ignoring whitespace only), has an executable fixed/template review
with covered obligations, and its current primary evidence proves a distinct,
non-adjacent paragraph occurrence. Every instance keeps its own exact literal,
location, offsets and source hash. Styled/conditional payloads, reused existing
IDs, extra evidence, changed lexical text, reported conflicts, and ambiguous
bindings are not eligible. The bridge does not infer outer/inner-cover roles,
create new semantic duties, or weaken coverage or submission gates. Projection
receipts preserve the aggregate and every partitioned source occurrence.
For the narrowly authorized v3 missing-obligation-inventory correction, the
bridge constructs the candidate from the receipt-bound parent and copies only
the exact `clause_reviews[i].obligations` fields named by matching
response-hash- and object-bound validator records. Other fields changed in the
model's full retry response remain in the immutable raw artifact and are
recorded as discarded, not accepted. The projected candidate is then run
through the ordinary contract validator and a fresh source-first review; this
projection does not authorize unsupported obligations or weaken release gates.
If the prior attempt failed before any semantic JSON response could be decoded,
the bridge may proceed only with a structured no-response record and the exact
persisted raw-envelope artifact whose current SHA-256 matches the recorded
receipt. Before every subsequent host call, the bridge revalidates the artifact
receipt for every prior attempt. A successful retry compares against the most
recent prior verified semantic baseline (its unaccepted repair base when
residual feedback is bound there, otherwise its decoded raw response), even
if an intervening attempt could not be decoded; only a history with no decoded
semantic response may state that semantic
comparison was not possible. Any missing or changed artifact stops the chunk
before another host call and preserves the original semantic blocker separately
from the integrity failure.
There is one additional narrowly defined correction path: if the local
validator rejects `verdict=consistent` with an empty `identified_obligations`
list for a clause whose current candidate requires a requirement, the bridge
may make one fresh independent-review call for the unchanged candidate and
provenance. Its run-bound feedback identifies only the affected clause IDs and
requires the reviewer to re-read source spans and linked requirements; it
never inserts or rewrites an obligation. For each fixed candidate response,
the capacity retry and these corrections share a two-call maximum, so they
cannot compound into extra attempts. A second narrowly defined correction
applies only when an `external_compliance` candidate has no linked DOCX
requirement or declared primary obligations and the independent reviewer
returns `incomplete` with only exact-source-bound `unrepresented` actions. One
fresh independent-review call may reconsider those same source spans against
the unchanged candidate. It may return `external_compliance_pending` only if
the source itself clearly requires a real-world action outside the DOCX
pipeline; it must enumerate that action as `external_action_pending` with no
requirement reference and preserve the exact source-quote inventory that
triggered the correction. The bridge never creates an obligation or changes
the candidate, and the ordinary external-pending validator remains
authoritative. A pending total verdict paired with only unrepresented atoms
may use this same two-call budget when every observation already exactly
matches one complete, distinct, current unverifiable primary atom and there
are no linked or code-known local DOCX duties. Missing/duplicate/foreign atoms,
changed typed semantics, unknown force, or mixed local work cannot use it.
The rejected result and current primary inventory hashes stay in correction
feedback. The fresh reviewer must explicitly select pending dispositions and
one-to-one primary IDs; code does not fill those fields or assert an approval.
This eligibility match is not proof of a complete source inventory. The fresh
review must read the entire selected clause and identify additional local or
external duties even if both prior inventories missed them. Source-first
semantic review, not a matching hash or lack of compiler cues, assesses that
coverage. An omission or any changed inventory still rejects the correction.
For a clause already classified `unsupported_backend`, the independent review
may instead record `verdict=backend_unsupported` only when it enumerates every
identified source obligation with `disposition=backend_unsupported`, the
candidate has no linked requirement, and every `requirement_refs` list is
empty. This is analysis-only accounting: it prevents an unnecessary retry of
the entire primary response, but does not create an executable property or
pass state. `unsupported_backend` remains a full/submission blocker; capability
planning, document generation, and release gates are unchanged and remain
fail-closed. Missing obligations still use `incomplete`.
For a source classified `unresolved` with a current code-registered ambiguity,
the coverage reviewer may identify an obligation as `scope_unresolved` only
when its execution target or metric depends on that exact ambiguity. It must
preserve a concise obligation summary and dependency dimensions, leave
`requirement_refs` empty, and retain the primary `unresolved` classification.
This analysis-only disposition is bound to an exact source span in the
versioned `obligation-analysis-ledger.json`; it never counts as represented,
executable, satisfied, or submission-ready. Any clearly executable omission
that is independent of the registered ambiguity remains `unrepresented` and
blocks acceptance. A one-shot same-candidate reviewer correction can clarify
the accounting, but an unresolved-only omission with no safe primary-response
repair does not trigger another primary-model retry.
If the second review still reports an unrepresented obligation, cannot prove
an external action, or violates any other contract, the chunk fails closed.
A separately corrected primary response gets a new review budget only after
the existing bounded primary-response correction path produces and revalidates
a new candidate. Every retry must pass the original validator; a repeated
omission and any other semantic/schema, provenance, route, timeout, or
unclassified provider failure remain fail-closed. An explicit `incomplete`
verdict outside these narrowly defined correction signals continues to use the
existing bounded primary-response correction path, with the resulting
candidate revalidated and checked for semantic drift.

Both the source-first obligation review and post-format semantic review use
run-bound source-span references. Code creates exact span IDs from the current
request's `document_text`; the Host Agent selects IDs instead of retyping
quotations or machine obligation IDs. The bridge compiles those selections back
to the exact source bytes before applying the existing local quote, clause,
candidate, and verdict checks. Cited evidence shown as context does not enlarge
the selectable source catalog. Raw response, compiled response, source packet,
and selection audit are preserved separately. Sentence slicing is conservative:
decimal values and recognized single- and multi-part English abbreviations do
not become false sentence boundaries; every selected span retains exact source
offsets. Stale, unknown, duplicate, and cross-clause references are rejected.
The final pipeline reloads the persisted raw native response and deterministically
recompiles the source packet, selected references, compiled response, and analysis
ledger; changing a sidecar and merely resealing its local hashes is insufficient.
The registered abstract target/metric ambiguity is emitted only when it is
grounded in operative, unquoted source wording rather than a nearby example or
applicability condition. These source checks may suppress an ambiguity code;
they never resolve the semantic question or create a requirement.

For contract 3.0, `covered`, `executable`, and `verify_existing` reviews
must carry a non-empty `obligations` inventory in both the local contract and
provider schema. The model remains responsible for semantic decomposition;
code does not invent generic duties to satisfy the schema.

### 2. Merge and format locally

After every chunk response exists, merge them with deterministic local code:

```bash
python3 scripts/merge_host_agent_review.py \
  build/host-review/review/requirements \
  --response-out build/host-review/review/host-agent-response.json
```

The merge step validates each chunk's contract and provenance, checks complete
clause coverage, shifts local requirement indexes, and binds the merged
response to the full request. It performs no network call.

Any host with Python and the project dependencies can inspect that merge
before formatting, without installing a Codex/OpenClaw/Claude CLI:

```bash
python3 scripts/offline_review_receipt.py \
  --work-dir build/host-review \
  --response build/host-review/review/host-agent-response.json \
  --receipt build/host-review/review/requirements/merge-receipt.json \
  --extraction-manifest build/host-review/review/requirements/extraction-manifest.json
```

The command checks current-run identities, strict JSON, artifact bytes and
paths. Its result explicitly says `independent_review_verified=false`,
`provider_model_verified=false`, and `submission_ready=false`: these facts
cannot be proved by a locally generated merge receipt. Keep the source,
response and code fingerprint unchanged between preparation and continuation;
otherwise start a fresh work directory.

In an ordinary conversation (with or without an installed adapter), run the
**non-release** final stage:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/host-review \
  --llm-response build/host-review/review/host-agent-response.json
```

`--offline-review-draft` remains an explicit compatible spelling for this
non-release path. Submission mode never implicitly selects it.
This path verifies the current extraction, chunk contracts, aggregate, merge
receipt, semantic ledger, and commit hashes, then writes fresh deterministic
artifacts under
`build/host-review/execution/requirements/`, re-extracts the requirements,
and applies the format specification where the remaining gates permit it.
The wrapper explicitly requests the safe existing-work transition; the
pipeline still checks the preparation manifest, source hash and code
fingerprint before accepting it.
Its manifest says `offline_merged_without_independent_review`; it does **not**
claim independent source-first review or submission readiness. An unresolved
technical or semantic blocker may still prevent DOCX generation. A response
from another document, run, or chunk set must be rejected. Formal submission
still requires audited host execution, official-template and Word acceptance.

## Word/PDF release gate

The DOCX produced by the deterministic application stage is a pre-render
artifact, not yet a submission artifact. Microsoft Word may rewrite the DOCX
while updating fields and exporting PDF, so the final file must be written to a
separate path and audited again. The ten-school batch runner now performs this
post-render stage automatically for every case that produced a DOCX:

```text
generated.docx
  -> scripts/word_render_export.py
  -> final-word.docx + final.pdf + word-render-report.json
  -> scripts/submission_audit.py
  -> scripts/post_generation_format_audit.py
  -> post-render-acceptance.json
```

`scripts/post_render_acceptance.py` is the fail-closed coordinator for that
chain. It records separate `pre_render_docx_sha256` and
`post_render_docx_sha256` values. Property receipts remain bound to
`generated.docx`; never replace their hash with the post-Word hash without
re-extracting and re-verifying the properties. A case is submission-ready only
when the post-render acceptance, final submission audit, and final format
comparison all pass. A generated DOCX alone, or a self-authored render flag,
is not sufficient evidence.

Word field repair is source-bound only. TOC hyperlinks and post-update
`PAGEREF` fields may be repaired only from an explicit target map whose input
DOCX hash matches the current run; entry order, bookmark names, or similar
text are not sufficient evidence. The Word exporter stages work inside Word's
container, runs children in a process group, and commits final DOCX, PDF, and
reports only after independent path/hash validation.

The pipeline writes a durable `running` manifest before external conversion or
extraction. Unexpected exceptions and interrupts close it as terminal
`failed`/`interrupted` records. `--allow-existing-work` is limited to a
completed supported-subset rebuild or the explicit host-review-to-execution
transition; full compliance cannot reuse a directory without a fresh bound
contract response and immutable host receipts.

## Handling failures

- A review cannot declare `consistent` while retaining a pending human
  source-content verification atom. A source/quote/ref-valid contradiction
  permits at most one fresh independent reread of the unchanged candidate;
  pending factual verification may coexist with separately represented DOCX
  wording, but never becomes satisfied merely because that wording exists.
  Repeated contradictions remain rejected with their attempts and hashes.

- A standalone section description is not evidence that manuscript content is
  missing. The versioned `section_description.py` grammar may retract a pure
  `requires_source_content` inference only for a complete, hash-bound current
  paragraph with no linked/catalog requirement, condition, mixed duty, or
  reported conflict. Original typed claims remain in the projection audit;
  the corrected candidate still requires independent review. This does not
  create content, remove a document requirement, assess manuscript presence,
  or grant submission readiness. Unrecognized descriptions remain on the
  existing semantic path; no clause/evidence/school IDs authorize this rule.

- Independent coverage generation couples external pending verdicts to pending
  atoms with current primary IDs; mixed pending verdicts separate represented
  DOCX atoms from external actions using their current primary status. Unknown
  additional source duties remain reportable as diagnostic `incomplete` /
  `unrepresented` atoms without an invented ID. Generation constraints are not
  completeness proof: native decoding cannot enforce every array constraint,
  so canonical coverage, one-to-one identity, source and mixed-duty checks still
  apply. Parsing preserves rejected raw states for bounded correction; it must
  not relabel an unrepresented duty as pending merely to pass schema validation.
- Missing chunk response: stop and report the exact filename.
- Input prerequisites use the code-owned `requirement_contract.input_catalog`
  for their exact kind. Generation schema couples kind and key by registered
  enums, retained in native projection. Current nested source/template paths
  and existing registered declarations can extend the catalog; absent roots
  remain selectable without implying supplied content, completeness or approval.
  Do not alias an unknown key, erase a prerequisite or replace its scope on retry.
  Unregistered-path feedback is a typed namespace error, not authorization to
  guess a new semantic input. Source, identity and final acceptance gates remain.
- A registered pure keyword-origin instruction may retain a typed selection
  relation (selecting terms from the thesis), while routing responsibility to
  pending human verification. Require current exact evidence, whole-source
  purity and a closed origin relation; never rewrite actor/action/target or
  infer that verification passed. Policy v5 records the original and projected
  inventory. Generation keeps this pure route separate from authoring content,
  requires current primary IDs for a pending verdict, and retains unmatched
  diagnostics. Duplicate IDs, actual authoring instructions, mixed duties,
  unknown actions and stale source bindings still fail closed.
- Provenance mismatch: rerun preparation and review the new packets; do not
  edit hashes by hand.
- Contract or clause-coverage failure: correct the host-Agent response using
  the supplied evidence, then rerun the merge.
- `needs_clarification`, unsupported, unverifiable, or external-compliance
  findings: preserve them in the audit and do not claim full submission-ready
  compliance.

The final artifacts and JSON manifests under the selected work directory are
the audit trail. Do not delete or overwrite an earlier run while diagnosing a
failure.

For a retry whose complete authenticated parent feedback consists only of
`missing_derived_requirement`, the bridge may extract the model's newly proposed
requirements for those exact executable targets as a bounded patch. It retains
every old requirement and all other parent fields verbatim, including quotations
and pending human duties. Unrequested model edits stay in the immutable raw
response and discarded-path audit, not in the accepted candidate. Current source
spans, unique identities, exact parent-bound validator records, complete target
coverage, role schemas and the full contract must validate. Mixed or unrelated
parent errors cannot use this path. The additions still require a fresh
independent source-first review; neither projection nor a score proves compliance
or permits submission.

Scope-reassessment grants are per clause, obligation and field, not per top-level
feedback code. An otherwise unchanged retry may contain surplus scope-field
edits; a source-bound patch retains the authenticated parent and copies only the
named target/condition/applicability proposals. Surplus scope edits are audited,
not accepted. Other payload/quotation/identity edits still fail closed. The
existing shared one-proposal budget, full validators and fresh independent
source-first review remain mandatory; no reviewer value is auto-copied.

An independently rejected action/target decomposition may use a separate
source-bound primary action proposal. It shares the same single semantic
proposal budget and authorizes only each named atom's explicitly listed fields.
Actor, exact quotation, force, pending status, route, identity and requirements
stay frozen. Different wording is not automatically equivalent: the full
validators and a fresh independent review must accept the new primary proposal.

## Unresolved empty proposals

An unresolved source clause is not an executable formatting requirement. The
bridge may separate a newly proposed, entirely empty object only under
`unresolved_empty_proposal_separation_v1`: validated current invocation and exact
source/evidence binding, complete paired validator diagnostics, no existing
identity, substantive property, condition, prerequisite, check or authored
obligation, and no registered executable source fact. Preserve the complete
rejected proposal in audit and leave source text/review/uncertainty unchanged.
Require full revalidation and fresh independent source-obligation review. This
does not resolve the clause, authorize model deletion, guess heading levels or
release a submission-ready document. See `docs/unresolved-empty-proposals.md`.

## Source responsibility and declaration representation

For a uniquely bound pure keyword-origin clause, a complete exact predicate
quotation may inherit its keyword subject from that same clause. A closed
selection-plus-verification action and explicit thesis-origin target remain
pending human work, never automatic compliance. Preserve typed fields and
require fresh independent review. Half quotations and mixed duties fail closed.

Fixed declarations use one body representation. A scalar duplicating the first
paragraph may be removed only after proving the complete ordered paragraph
array against the unique current source candidate; audit the original scalar.
Otherwise competing `body`/`body_parts` are rejected by the contract, merger
and resource registry. Legacy single-form inputs preserve original bytes.
See `docs/source-routing-declaration-representation.md`.

## Compound local source-atom proposals

Local diagnostics can authorize a bounded primary proposal under
`validator_bound_compound_atom_proposal_v1`, not certify semantic equivalence.
Authenticate the complete current parent/error/source/invocation binding; copy
only named applicability/quote fields or append the missing side of a mixed
inventory without editing existing atoms. Preserve unrequested raw changes in
audit, reject reported conflicts, and require complete revalidation and fresh
independent source-first review. Do not guess applicability, invent human acts,
stamp provenance onto an unaccepted parent or extend the ordinary retry budget.
See `docs/compound-local-atom-proposals.md`.
