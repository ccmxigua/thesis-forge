# Portable skill and installation

The runtime bundle is independent of the engineering checkout. It contains the
ordinary DOCX/TEX packet workflow and its scripts, schemas, filters and static
resources. It does not include user inputs, generated runs, credentials, test
fixtures or the multi-school benchmark. Development-only benchmark builders
remain in the source repository, not the runtime package.

## Prepare the runtime

This release is validated with Python 3.12 on Linux. Other Python/platform
combinations are not certified by this check. Create a project-local virtual
environment outside the installed skill directory and install the exact pins:

```sh
python3.12 -m venv /absolute/thesis-runtime-venv
/absolute/thesis-runtime-venv/bin/python -m pip install -r /absolute/skill/requirements-runtime.txt
/absolute/thesis-runtime-venv/bin/python /absolute/skill/scripts/skill_runtime.py --request docx
```

The probe runs only executable version queries, never a model, conversion or
render. Missing Pandoc blocks TEX but not ordinary DOCX packets. LibreOffice
and Poppler are optional native rendering capabilities. The package does not
install OS applications or supply fonts. WPS, image viewing and independent
internal tasks require actual tools exposed by the current host. A tool path
is not proof that a document has passed visual acceptance.

## Recover an existing startup checkpoint

Before reopening or re-running an existing project, run the read-only guard
from the complete selected skill using its pinned Python environment:

```sh
/absolute/thesis-runtime-venv/bin/python /absolute/skill/scripts/startup_readiness.py \
  --manifest-sha256 APPROVED_BUNDLE_MANIFEST_SHA256 \
  --campaign /absolute/existing-campaign --campaign-id EXISTING_CAMPAIGN_UUID \
  --ledger-sha256 CURRENT_AUTHORITATIVE_LEDGER_SHA256 --phase prepare
```

Use `--installation-platform chatgpt` only for the observed installation
normalization documented below. For `--phase resume`, also supply the retained
`--work-dir` and its `--run-id`; for `--phase generate`, include both and the
stable `--operation-id`. Obtain the expected identifiers and digests from the
approved deployment/checkpoint records, not from the files being checked.
Do not substitute a repaired draft, archived ledger, source checkout, or a
new campaign. Exit 2 and a named blocker identify what must be restored.

This check does not create or reserve anything and never authorizes execution.
Use the authoritative session `status` / `next` followed by the usual bound
candidate reservation only when permitted. Ledger incompatibility must be
resolved by recovering a reviewed compatible runtime, not by editing ledger
JSON, resetting limits or replacing already charged operations. This released
budget authority understands v1 only; a separately reviewed v2 or v3 campaign
requires its own compatible authority even if the bundle label matches.
The guard does not weaken independent review, WPS, or Word release gates.

## Build and verify

```sh
python scripts/skill_bundle.py build --output /absolute/thesis-skill.zip
python /absolute/unpacked-skill/scripts/skill_bundle.py verify
```

The explicit file list in `skill-package.json` is authoritative. Archives are
reproducible and include per-file SHA256. This detects drift, not publisher
authenticity: compare the archive hash with the approved source delivery.
Runtime outputs and the virtual environment must live outside the bundle.

## Load in dot

Use the host's supported personal-skill installation flow. On a host exposing
personal-skills management, save the complete verified skill in its managed
personal-skills checkout and verify synchronization through that flow. Do not
copy files to a guessed global skills path or treat a source push as a skill
installation. When the host does not expose an installation interface, report
installation as pending, while separately stating whether the current
conversation can explicitly read `SKILL.md` and execute the local scripts.

After a supported ChatGPT installation, the service may normalize
`agents/openai.yaml` and add its default `assets/icon.svg`. Use
`python /absolute/installed-skill/scripts/skill_bundle.py verify
--installation-platform chatgpt` only for this installation check. This explicit
adapter recognizes one exact observed pair: the original descriptor hash/size,
the complete normalized descriptor hash, and the complete default icon hash.
It records both accepted differences. Unknown UI fields, a changed icon,
additional files, missing resources and any code difference remain blocked.
The default `verify` mode remains byte-exact and will report these differences.

A successful adapted check means exact declared payload plus that explicitly
recorded UI pair; it does not mean every installed byte equals the archive.
Nor do recognized bytes authenticate platform origin or prove registration.
Verify the supported installation's actual registered entry separately, then
freeze the installed runtime fingerprint before starting a business run.
A changed platform normalization requires fresh inspection, not a broader
ignore rule or a silently regenerated baseline.

The assistant should use the current conversation for semantics and its
supported internal tasks for independent review. It must not start another
engineering conversation or model CLI as an implicit execution backend.

Record three separate outcomes: source delivery, installed/loaded skill, and
an actual current-conversation run. None implies the other two. WPS scoped
acceptance is distinct from the unchanged formal Word release gate.
