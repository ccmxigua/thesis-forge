---
name: thesis-latex2docx
description: Convert thesis LaTeX or DOCX files using a school's .doc/.docx formatting requirements, with evidence-bound semantic review performed by the current host Agent model and deterministic local DOCX validation. Use when a user asks to format, audit, or produce a thesis DOCX from an unseen university template.
---

# Thesis LaTeX/DOCX Formatting

This project is a host-runtime skill. The Agent invoking it supplies semantic
interpretation with the model it is currently using. The project never selects
an LLM, calls a provider, reads an API key, or assumes that the host model is
OpenAI, Grok, Claude, or any other specific vendor.

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

## Existing requirement reference integrity

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

## Explicit semantic issue acknowledgements

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
  --work-dir build/host-auto-$(date -u +%Y%m%d-%H%M%S) \
  --prepare-agent-review
```

Then the current host Agent must read the manifest and all chunk requests,
write one response per requested filename, and run the offline merge followed
by the final deterministic pipeline.  It must not copy a response from an
earlier run or silently hand the semantic work to another host.  When the
current host does not expose an automatic adapter, this packet workflow is
the supported path; the missing capability is reported rather than replaced
by OpenClaw, Claude, Codex, or another installed program.

The packet workflow remains valid for Codex, Claude, OpenClaw, and other
hosts.  The host identity and the model/provider identity are separate
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

No model is silently selected by this skill: omitting `--codex-model` preserves
the current native Codex CLI configuration.  Pin a model only when the
invocation explicitly requires reproducibility, for example
`--codex-model gpt-5.6-luna`.

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
  --llm-response build/review-draft/review/host-agent-response.json
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
as an inspectable artifact (`diagnostic_draft_generated`), but it is not an
accepted review draft unless deterministic validation is clean, `format_ready`
is true, and every expected property receipt is verified against the serialized
DOCX. Technical failure stops the current case and a fail-fast batch; only
genuinely unresolved human inputs/decisions may remain as red markers while a
technically valid review draft is accepted. This is not a formatting or release
pass. The ten-school batch runner defaults to this policy. Use
`--output-policy submission` only after the red items are resolved and the
submission-mode run has been started fresh; that mode retains the full
fail-closed capability, render, and final audit gates.

`--allow-offline-review` is only a non-release test escape hatch. It requires
`--output-policy review_draft` and cannot be combined with
`--require-submission-ready` or `--strict-release`; a supplied response without
current host receipts must never be presented as submission-ready.

### Visible review-draft placeholders

Every ledger item must have one visible `MR-xxxx` marker in the editable DOCX.
Markers use Chinese red text (`C00000`), pale-yellow shading (`FFF2CC`) and an
explicit East Asian font (`Noto Sans SC`); make that font available to the target
renderer and verify actual glyph rendering, not merely text extraction.
The renderer must not replace Chinese explanations with English-only labels.

Only code-owned role/property paths may anchor a marker beside source content.
Table-related notes go outside the table. Free-text questions, measurements
such as `3cm`, and missing official templates must never trigger a guessed cover
or abstract location. Unlocated items go at the front under
`待定位人工处理（审查草稿，不可提交）`, each with its MR ID, source excerpt, reason,
and editable handling prompt. Full original text stays in the bound ledger;
placement never changes unresolved status, requirements, or source paragraphs.

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
unverified.

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

Then run the final formatting stage:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/host-review \
  --llm-response build/host-review/review/host-agent-response.json
```

The final stage writes fresh deterministic artifacts under
`build/host-review/execution/requirements/`, re-extracts the requirements,
verifies the response and the review/merge receipts against the new extraction,
applies the format specification, and runs the configured
DOCX/official-template/release audits. A response from another document,
another run, or another chunk set must be rejected.

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

- Missing chunk response: stop and report the exact filename.
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
