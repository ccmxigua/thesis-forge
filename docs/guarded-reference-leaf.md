# Read-only diagnosis of a possible reference-only leaf

The normal generation, review, native acceptance and release routes do not use
this tool. It does not remove paragraphs, write a DOCX, issue or renew a review,
or authorize any action. It can raise a specific bounded visual-review hint:
a numbered reference list, one exact empty property-only paragraph before a true
section boundary and the appendix, plus a matching header-and-page-number-only
leaf in the bound PDF.

After a completed renderer operation and an already issued visual review:

```sh
python scripts/guarded_reference_leaf.py diagnose --work /absolute/current-run
```

The CLI checks the active visual artifact manifest, exact code/candidate
binding, completed operation and PDF/ordered page hashes. Those hashes do not
prove how the PDF was produced: a fabricated PDF paired with self-reported
records may be consistent. The tool always reports
`renderer_provenance_verified: false` and `repair_authorized: false`.
Do not use the diagnostic as renderer attestation, native acceptance, a fix,
or authorization to alter a document. Inspect the actual layout independently.
This diagnostic has no correction command; the independent optional tool is below.

Support is deliberately narrow and capped: 3–30 PDF pages, 10 MB DOCX,
20 MB PDF, one exact empty non-section paragraph with
`pageBreakBefore=false`, no parity exception, and no competing leaf.
Existing files and review state are only read. This is not a checker for
concurrent hostile modifications of the working directory.

## Optional causal correction

Read-only diagnosis stays the default. It never authorizes this command.
For an explicitly authorized text-only input and expected physical leaf and
terminal reference, use a new directory outside protected work:

```sh
python scripts/reference_leaf_correction.py \
  --input /absolute/review-input.docx --expect-input-sha256 EXACT_SHA256 \
  --expect-page 4 --expect-terminal-reference 21 \
  --output-dir /absolute/new-review-output
```

Only the supported, nonwritable managed LibreOffice tree identified by the
built-in complete inventory pin may render. The launcher is matched as
data; the exact probed executable is run directly, with a distinct private
profile and minimal environment on each side. There is no CLI switch to
supply a renderer, prior PDF, ledger, evidence or retrospective proof. If the
pinned runtime is unavailable or differs, stop correction and use the
read-only diagnostic instead. This pin is not proof of an OS, fonts, or
vendor signature; run only in a trusted stable host.

The tool rejects documents outside the explicitly allowed text-only OPC
relationships/types. It invokes both renders itself, on exact source and
on the otherwise unchanged document with one bound empty non-section
paragraph removed. It writes neither original file nor any formal run,
review, campaign or budget. It refuses if pages other than the removed leaf
change visually, apart from one tightly located numeric footer moving by
one. A receipt with matching byte hashes is required for these entirely new
local outputs; an incomplete directory after an interrupted process is not
a result. This is not Word/WPS acceptance or a release; review it anew
against the required native application.
