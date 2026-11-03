# Current-conversation skill validation

This report separates source readiness, actual skill installation, and execution
inside the invoking conversation. A source commit or ZIP copy does not prove the
other two outcomes. The runtime remains a review-draft workflow, not formal Word
or submission acceptance.

## Scope

The portable package includes the ordinary DOCX/TEX packet workflow, deterministic
scripts and schemas, source/candidate/task binding, resumable status projection,
atomic validation budgets, independent internal review records, and scoped WPS
observation import. Native model CLIs remain explicit legacy opt-in only.

The release is exercised on Python 3.12/Linux with pinned runtime dependencies.
TEX needs Bash and Pandoc. Native rendering, installed fonts, image-view tools and
WPS access are separate host capabilities and are not supplied by the package.

## Independent code review

The whole feature branch was independently reviewed. Findings repaired and
rechecked include completed-review replay binding, immutable terminal WPS
operations, relocated ZIP conversion, complete PDF/PNG inventory binding,
canonical image-path uniqueness, and nested timeout/process ownership.

The aggregate regression exposed a pre-existing intermittent table traversal
bug: storing integer IDs of short-lived lxml proxies could omit later cells after
address reuse. The minimal repair retains the XML elements during traversal.
Independent checks covered streaming 90-cell tables, nested/later tables,
merged-cell deduplication and repeated isolated style analysis.

## Frozen version and validation status

The installed runtime is version **1.0.2**, fixed at source commit
`0c7c8439805fdc1f27628d3802ea9faa1b3c437e`. The final runtime repair is
`251aa65666674bb813ad380e7c287ddb3b3e3d88`; the later commit changes only
the package version. Subsequent release-summary commits do not change this
installed runtime or its already-bound candidates.

The applicable aggregate for the repaired code completed with **2,480 passed,
1,996 subtests passed, 46 skipped, and 3 warnings**. Compilation and diff checks
passed. The version/package regression subsequently passed 39 tests. The same
installed skill passed exact payload verification with the narrowly recorded
UI metadata pair, official structural validation and a read-only DOCX capability
probe. Installation is distinct from successful native acceptance.

A fresh guarded aggregate on the cleaned publication tree also completed with
**2,480 passed, 1,996 subtests passed, 46 skipped and 3 warnings** in 550.38
seconds. Its runtime archive exactly reproduces the installed 1.0.2 package
hash. The clean release starts from the public base without importing private
development-artifact provenance; the original development history is retained
separately. No runtime code changed after this run.

The invoking conversation actually prepared fresh semantic packets, supplied
declarative responses, merged them and generated two synthetic candidates using
the local deterministic runtime. Both candidates received one complete local
render and one WPS save-close-reopen attempt. **Both WPS attempts failed.**
The candidate-generation budget and both candidates' WPS allowances are spent.
No unrestricted native suite, third candidate or extra WPS retry is implied.

The first candidate exposed a source-preservation bug: absent page-number rules
were interpreted as explicit removal, deleting an existing PAGE footer. Version
1.0.2 preserves unspecified page-number contracts and no longer creates empty
header/footer parts during read-only inventory. Explicit numbering changes with
insufficient format information fail closed rather than being silently ignored.

The second native attempt confirmed visible PAGE footers 1 through 6 and correct
TOC hyperlink destinations, heading-with-body behavior and the ordinary image
preview. Nevertheless, after a whole-TOC update, its labels remained **6/6/7**
while the corresponding visible PAGE footers and target pages were **5/5/6**.
This persisted after reopening. It is not an accepted logical-numbering offset;
the underlying native discrepancy remains unresolved. The local render has seven
pages while the native editor has six; equal pagination across applications is
not claimed. Fixing the source PAGE loss did not establish a WPS pass.

The synthetic input contains no OLE object or equation. Font observations are
limited samples, not exact-font or all-glyph certification. Formal independent
review, provider-model, Word-release and submission-ready claims remain false.
Raw candidates, native-object URLs, screenshots, conversation identifiers and
local execution records remain outside this repository. Only sanitized results
are included here and in `release-status.json`.

Project-branch publication and its exact remote status are verified separately
after this report is frozen. This report does not claim that publication, skipped
CI or successful installation turns the failed native acceptance into a pass.


The budget-safe aggregate runs all applicable pytest/unittest cases with an
execution-only guard against actual model/native-renderer launches. Forty known
native cases are explicitly withheld; other inherited environment skips remain
visible. This is not equivalent to the unrestricted `make check` native suite.
The existing Makefile and GitHub workflow are unchanged.

Packaging checks include exact resources, no user inputs/credentials/work dirs,
reproducible ZIP bytes, relocated paths with spaces, a genuinely read-only package
with writable external work dirs, ordinary DOCX preparation without optional
tools, and TEX preparation with current runtime Python rather than unrelated
ambient configuration. Preparation does not spend a business candidate.

## Acceptance boundaries

- Independent `host_observed_scoped` is based on actual host tool observations;
  it is not offline cryptographic identity or a formal provider attestation.
- WPS `pass_scoped` covers only the observed exact native object and checklist.
  It does not certify Word, OLE editability, mathematics, or all-font fidelity.
- Unknown upload/execution outcomes stay charged and are located before retry.
- Real thesis files and old evidence are preserved; synthetic inputs are used for
  the bounded current-conversation run.
- Formal independent/provider/submission flags remain false.
- A final new source commit may use `[skip ci]` to avoid unbudgeted native CI.
  A skipped or pending remote workflow is not a passing CI result.

## Observed installation metadata

The supported personal-skill service adds a default icon and normalizes the
agent UI descriptor. The explicit ChatGPT installation verifier accepts only
the observed original/normalized descriptor hashes and exact icon hash as one
pair. Default archive verification stays byte-exact. Adapted verification lists
the differences and rejects every other missing, changed or additional file.
Recognizing those bytes does not authenticate their origin or prove registration.
The installed entry must still be verified separately through the supported flow.
