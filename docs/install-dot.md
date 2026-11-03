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
