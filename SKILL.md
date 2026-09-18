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

The audit trail is written to
`<work-dir>/requirements/requirements-input-manifest.json`. It records the
original path/kind/size/SHA-256, converter command/tool/version, normalized
path/size/SHA-256, validation result, and conversion status. Missing converters,
converter failures, and invalid DOCX output fail closed with no semantic work
or document formatting performed.

Normalization is byte/file handling only. Every freshly extracted clause from
the normalized DOCX still goes to the current Host Agent for the complete
contract-2.1 review. The original `.doc` identity and SHA-256 remain the source
provenance anchor; `rule_only` and `known_template` are not fallback paths.

## One-command workflow

When the local OpenClaw Gateway is available, the normal user-facing command
can run the complete fresh path:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/host-auto-$(date -u +%Y%m%d-%H%M%S) \
  --auto-host-agent
```

This invokes the current Host Agent once per fresh request chunk through the
local isolated `openclaw agent exec --json` CLI, without `--deliver`. By
default packets contain 20 clauses and at most four independent chunk turns
run concurrently. At run start the bridge snapshots the exact parent
session's effective `provider/model` route, preferring a session override when
one is present. Every child receives that route explicitly and may retry only
on the same route; it cannot fall through to the Gateway's global fallback
chain. The bridge records and verifies each child winner route before saving
its response. If the parent route is unavailable, or any child reports a
different provider/model, the run fails closed immediately. Each chunk may be
retried once in a new isolated turn if its response is invalid. The bridge
writes only the current chunk responses, the existing offline merger validates
contract 2.1/provenance, and the pipeline then performs the full deterministic
DOCX, declaration-resource, and audit stages. A non-empty prior work directory
or existing output is rejected in this mode. Invalid, incomplete, stale, or
route-mismatched responses fail closed; there is no fallback to an older
response.

The parent session key can be supplied with
`--host-agent-parent-session-key`. If omitted, the bridge checks
`OPENCLAW_PARENT_SESSION_KEY` and `OPENCLAW_SESSION_KEY`, then uses the
freshest recent interactive session available from `openclaw sessions`.
Use `--host-agent-model provider/model` only when an explicit route is
intentionally desired. `--no-host-agent-model-inheritance` is an explicit
compatibility escape hatch and delegates to the ordinary OpenClaw default;
the normal multi-user workflow should leave inheritance enabled.

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
build/host-review/requirements/host-agent-review-manifest.json
```

Read the manifest and every request in
`build/host-review/requirements/llm-request-chunks.json`. Process **all**
chunks with the current host Agent model. For each chunk, write the exact JSON
contract to the filename in `batch.response_filename`, normally:

```text
build/host-review/requirements/llm-response-chunk-0001.json
```

Each response must satisfy the request's `response_schema` and must:

1. copy that chunk's `provenance` object unchanged;
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
  build/host-review/requirements \
  --response-out build/host-review/llm-response.json
```

The merge step validates each chunk's contract and provenance, checks complete
clause coverage, shifts local requirement indexes, and binds the merged
response to the full request. It performs no network call.

Then run the final formatting stage:

```bash
python3 scripts/thesis_format.py \
  requirements.doc input.tex output.docx \
  --work-dir build/final \
  --llm-response build/host-review/llm-response.json
```

The final stage re-extracts the requirements, verifies the response against
the new extraction, applies the format specification, and runs the configured
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
