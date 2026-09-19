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
  current Host Agent for contract-2.1 semantic review. Existing files, template
  profiles, or prior responses must never silently disable this path.
- Do not set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `THESIS_FORMAT_LLM_*`, or a
  provider/model override for this skill. The bridge may copy the current
  parent session's route; `--host-agent-model` is reserved for an explicit
  intentional override, not an independent provider client.
- Keep semantic output declarative JSON only. Never let a model emit OOXML
  edits, shell commands, or executable code.
- Treat the local scripts as the authority for extraction, provenance,
  contract validation, merging, formatting, and release gates.
- Fail closed when a clause is missing, ambiguous, unsupported, stale, or not
  backed by the supplied evidence. Never fill gaps with a plausible guess.

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
contract-2.1 review. The original `.doc` identity and SHA-256 remain the source
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
2. include `contract_version: "2.1"`;
3. include every supplied clause exactly once in `clause_reviews`;
4. use a non-empty `reason` on every clause review and requirement;
5. cite only supplied clause/evidence IDs and allowed roles/properties;
6. use zero-based `requirement_indexes`, with an empty list for non-executable
   classifications;
7. report uncertainty as `unresolved`, `requires_metadata`,
   `requires_source_content`, `unsupported_backend`, `unverifiable`, or another
   contract classification rather than inventing a requirement.

Do not combine chunks manually and do not copy a response from an earlier
run. The provenance hash binds the response to the exact fresh extraction.

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
