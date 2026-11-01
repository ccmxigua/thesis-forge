# Explicit LibreOffice UNO TOC PDF backend

This optional post-format backend updates one existing TOC in an isolated,
in-memory LibreOffice model and exports a **new PDF**. It never saves a DOCX or
persists the original DOCX's field cache. The default `toc_materializer.py`
PDF-cache backend and Word release gate are unchanged. No model or complete
thesis-generation pipeline is invoked.

```sh
python scripts/toc_materializer.py source.docx review-baseline.docx \
  --backend libreoffice-uno --runtime-profile runtime.json \
  --source-sha256 <source-SHA256> --baseline-sha256 <baseline-SHA256> \
  --work-dir /absolute/new-directory --timeout 150
```

Do not pass the legacy third positional DOCX output. The output directory must
not exist, including dangling symlinks. `rendered.pdf` and
`uno-render-report.json` are published only after all checks. Failed runs retain
`failure.json` and `_diagnostics/`; PDFs/raw UNO receipts there are diagnostic,
not accepted output. Partial publication rolls back only its own new links and
never replaces existing files.

## Runtime profile

The installation must already exist; this entry point never installs or finds a
replacement. Strict JSON has exactly these fields (replace all placeholders):

```json
{
  "schema_version": "1.0",
  "backend": "libreoffice-uno",
  "version": "26.8.2.2",
  "build_id": "<40 lowercase hex characters from program/versionrc>",
  "office_root": "/absolute/isolated/libreoffice26.8",
  "java_executable": "/absolute/java",
  "java_sha256": "<64 lowercase hex characters>",
  "files": {
    "program/soffice": "<SHA256>",
    "program/soffice.bin": "<SHA256>",
    "program/versionrc": "<SHA256>",
    "program/libjpipe.so": "<SHA256>",
    "program/libjuh.so": "<SHA256>",
    "program/libmergedlo.so": "<SHA256>",
    "program/classes/libreoffice.jar": "<SHA256>",
    "program/classes/unoloader.jar": "<SHA256>"
  }
}
```

These are caller-declared fingerprints, **not signatures or official download
verification**. Preserve independent installation provenance. Components/build
ID are checked before launch, the connected renderer's exact version before
loading, and protected hashes again before publication. Java needs source-file
launching with a compiler (tested: Java 21). Missing dependencies block output;
they are not downloaded. This fingerprint covers listed executable/bridge
components, not every library, font, environment variable or OS input.

One fresh Office process/profile and Java client use a private local pipe, no
shared Office session or TCP listener. Macro execution and automatic document
updates are disabled at load. The client explicitly calls
`XRefreshable.refresh` (or `XDocumentIndex.update`) on one existing ContentIndex,
exports PDF, checks original bookmark names/range text, and disposes the model.
Only its own process group is cleaned up. Child-specific environment settings
do not modify the installation or global configuration.

## Supported scope and evidence

The first implementation intentionally accepts only a verifiable subset:

- Text-only DOCX, one complete `TOC \o "1-N" \h`, unique explicit outline
  headings including style inheritance. No guessed heading keywords.
- Unique complete bookmarks. Body REF/PAGEREF or internal markup links need one
  reference and unique literal context per paragraph, with a visible target
  range within one paragraph. Partial body targets need one attributable
  rendered token. Heading targets must cover the entire title; an outline point
  is not proof of a partial-heading range start. Ambiguous targets fail rather
  than being inferred by order.
- Explicit default PAGE footers (optional Arabic/Roman format), consistent
  bottom margins, no separate first/even-page footer scope.

Math/OLE/images/tables, external relationships, unhandled fields/switches,
revisions/MC, locks/protection and uncertain source/PDF scopes block this backend.
Their source bytes remain unchanged; objects are not converted or normalized.
Legacy and other project paths retain their existing behavior and guards.

The report binds final PDF, source/baseline SHA256, renderer/build/profile and
helper identities. It compares the explicit UNO result with source heading
order, actual PDF footer labels and outline destinations, then accounts for
every PDF link as a TOC or source-bound body reference. Physical pages and printed
labels are separate. PDF text/link inspection is not a mouse click, visual page
review or glyph/typeface check.

`submission_ready`, Word/WPS, specified typeface and repeatability verification
remain false. Existing repeatability/release requirements are not waived by one
successful export. LibreOffice may change in-memory layout/results while never
storing the source DOCX; the save state records that distinction. No whole-thesis
or native Word/WPS guarantee follows from the synthetic fixture.

Pure tests: `python -B -m unittest discover -s tests -p test_uno_toc_backend.py -v`.
They block Office subprocesses and replay the already verified PDF.
`tests/fixtures/uno-toc/ORIGIN.json` binds the immutable prior synthetic input/PDF;
`prior-native.json` is the earlier raw update log, not a new execution.
