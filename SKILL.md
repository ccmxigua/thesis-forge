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

## Runtime preflight

Read `docs/install-dot.md` when installing or moving this skill. Keep caller
inputs and run outputs outside the skill directory. Verify a distributed
bundle with `scripts/skill_bundle.py verify`. After supported ChatGPT installation,
use the explicit `verify --installation-platform chatgpt` mode described in
`docs/install-dot.md` for the exact observed UI metadata pair. All code, other
resources, and unexpected files remain strictly checked. Then run the read-only capability
probe `scripts/skill_runtime.py --request docx` (use `--request tex` for TEX).
Use the pinned project runtime, not an unrelated Python environment. Missing
optional renderers or Word do not prevent DOCX packet preparation. Actual
internal reviewer and WPS access must be observed in this host, not inferred
from a successful dependency probe. Never call a directory copy “installed.”

## Ordinary conversation: start here

Use this skill directly in the invoking conversation. The current Agent does
the semantic reading with its current model; do not launch another agent CLI,
choose a model, set provider credentials, or edit global settings by default.
A host needs file read/write access and a Python execution tool with the project
dependencies. A chat without those tools cannot execute the local pipeline;
report the missing capability rather than claim universal execution.

1. Initialize **one** authorized validation campaign with
   `python scripts/validation_budget.py init --campaign /absolute/campaign`.
   Keep that campaign across candidates and resumes. Do not create a new
   campaign to evade its caps. It allows at most two candidates, two full
   renders per candidate, and one WPS save-close-reopen per candidate.
2. Run `python scripts/current_session.py run /absolute/requirements.docx
   /absolute/thesis.docx --work-dir /absolute/new-run --campaign
   /absolute/campaign --author-task-id <actual-current-task-id>`. This invokes
   the existing provider-free preparation workflow, not another model CLI.
3. Read **all** packets named by `review/requirements/host-agent-review-manifest.json`
   and `llm-request-chunks.json`. Write each declarative response to the exact
   `batch.response_filename`. Use the current conversation's model; do not
   invent model identity. Run the existing `merge_host_agent_review.py` against
   that review directory. Missing/stale chunks block continuation.
4. Use `current_session.py status`, `next`, or `resume --work-dir <run>` to
   inspect the existing authoritative artifacts. Resume is read-only and
   never repeats a pending or uncertain operation. When the next action is
   `generate_candidate`, use `current_session.py generate --work-dir <run>
   --operation-id <stable-unique-id>` to reserve the candidate before creation.
5. Issue `session_review.py issue --work-dir <run> --role source_format`.
   Give the immutable task snapshots to a fresh internal read-only reviewer,
   retaining the real host task identity. Do not have the author review itself.
   Import the exact response with `session_review.py import`. A reviewer JSON
   alone is `pending_host_verification`, never an independent pass.
6. The current host may call `record_host_observation` **only** using an actual
   observed `collaboration.list_agents` tool result, its real conversation
   reference, and the exact completed review text. Never use reviewer-supplied
   or invented tool JSON. The resulting `host_observed_scoped` is a scoped
   conversation observation, **not** offline cryptographic authentication.
   Unobservable host provenance stays pending. Test fixtures cannot be promoted.
   A HOLD requires correction and a fresh review task, not an edited PASS.
7. Before a full render or WPS acceptance, reserve that operation in the same
   campaign and check `can_execute`. Unknown consumes the reservation; only
   actual evidence of non-execution can release it. For visual review, save
   the exact candidate-bound render inventory and completed render operation
   in `render/render-binding.json`, then issue role `visual`. All page images
   must be observed; absent image-tool evidence remains pending.

For native acceptance, read `references/wps-native-acceptance.md` before any
upload or edit. Import real observations with `native_acceptance.py record`.
`pass_scoped` never changes Word/provider/submission flags. A missing export
is null, not the input hash. Native URL/version changes invalidate reuse.

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

## Detailed contracts

Before interpreting semantic packets or diagnosing source/merge failures, read
`references/engine-contract.md` using its reading map. It preserves the existing
source-reference, scoped repair, review-draft and formal release contracts.
Read the native-adapter sections only for explicit legacy opt-in; ordinary
current-conversation execution must not launch those CLIs. A previous benchmark
budget never grants a new campaign or a new current-session validation budget.

## Evidence handoff formats

An internal reviewer must return its complete structured report as the final
result: task_id, role, binding, artifact_manifest_sha256, reviewer_task_id,
verdict, findings, read_artifacts, schema_version, and source_kind. The host
copies that exact returned JSON text into result_text when importing. A free
text PASS cannot attest a candidate or be reused for a new task.

For visual review, render/render-binding.json must contain candidate_sha256,
operation_id, a pdf record (path and sha256), and pages ordered 1..N with each
page number, path and sha256. The completed full_render budget operation's
evidence must bind candidate_sha256, pdf_sha256, and page_inventory_sha256
computed by session_review.render_inventory_sha256 from the complete inventory. N is checked from the
actual PDF; omitted or duplicate pages are rejected. All PDF/source files and
page images are included in the immutable review snapshot.
