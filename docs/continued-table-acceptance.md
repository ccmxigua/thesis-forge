# Continued-table acceptance (read-only, per candidate/table/target app)

A literal continuation caption, a copied first row, an effective automatic
repeat flag, and observed pagination are different facts. Do not report one
as proof of another or add a second caption or a page break automatically.
Run this audit only when a source-bound requirement or an observed pagination
issue calls for the continued-table check. Preserve the candidate, original,
existing campaign and immutable receipts.

1. Identify the numbered logical table, its actual candidate SHA-256, and the
   **one** target application (WPS or Word). The locator is the one-based
   top-level OOXML table ordinal, not the printed table number or page number.
2. Choose the strategy from the real document and school evidence. Run:
   `python scripts/continued_table_acceptance.py CANDIDATE.docx --table 2 --target-app wps --strategy manual`.
   For automatic continuation use `--strategy automatic`. Its active
   `w:tblHeader` flags must form a contiguous run from row 1; absent, false,
   ambiguous and later noncontiguous flags do not create a header block.
   For manual continuation supply `--school-rules EXACT-REQUIREMENTS.docx
   --manual-permission REVIEW.json` with boolean `allow_manual: true`,
   `requirements_sha256`, `candidate_sha256`, one-based `table_ordinal`,
   an actual `clause_ref` and actual reviewer `review_ref`. A format spec
   saying “repeat the header”, a copied sample row, or an invented reference
   does not authorize a manual split. The helper verifies the byte/scope
   binding, not the authenticity or legal interpretation of the permission.
3. Observe both adjacent **physical** pages in the named native app for the
   same candidate/object/version at the reserved observation gate. Confirm
   the logical table identity, repeated meaningful header, non-placeholder
   cells and required caption on the correct continuation page. Freeze two
   independently captured PNG/JPEG screenshots. Re-run with
   `--evidence ACTUAL-NATIVE-OBSERVATION.json --page-capture FIRST.png
   --page-capture NEXT.png`. The observation must bind
   `app`, `candidate_sha256`, `table_ordinal`, `native_document_ref`,
   `native_document_version`, `review_ref`, four boolean checks
   (`same_logical_table`, `header_repeated`, `header_is_not_placeholder`,
   `caption_on_continuation_page`), and exactly two consecutive `pages`,
   each with physical page number, unique `capture_ref` and
   `capture_sha256`; first page asserts `original_segment_visible: true`,
   second `continuation_segment_visible: true`.
4. Record the scoped result only. `host_evidence_bound_unverified` means
   structurally bound observer claims and usable capture bytes only, not
   cryptographic host authentication, actual independently verified native
   observation, a global Word/WPS pass or release-ready. The host must
   actually view both pages, and enter the result through the existing
   authenticated Word render/human review or scoped WPS observation flow. Wrong application, stale bytes, invalid/rewritten
   images, missing/ambiguous evidence stay pending/HOLD. PDF/static XML,
   matching image data, or an unbound `table_continuation.verified` boolean
   cannot substitute.

The conservative resolver recognizes clear top-level `表N`, `续表N` and
`表N…（续）` caption sequences, with a same-number predecessor chain and
plain comparable first rows. Common placeholder and non-distinct multi-cell
first rows are HOLD, not confirmed headers. Merged/field-driven row headers,
unnumbered or complex/non-adjacent chains require manual investigation. This
does not replace the official campaign or native receipt path, and issues no
new candidate, render or native application operation. CLI exit 2 means
that artifact remains unverified through this helper, whether a structurally
bound packet was provided or not. Inspect the JSON status for the distinction.

Generation reports separate `table_continuation_geometry` as
`pending_native_geometry` with `blocking:false`; it does not alter the
existing release, visual or scope gates or certify a table. Only an explicit
table-level `repeat_header_row: true` executes the automatic serialized
header instruction in the existing approved table population. Its lone
false flag is set to true; duplicates and malformed values remain unchanged
and fail the structural audit. A nested continuation repeat rule expresses
the visible contract, not authority to add automatic row flags.

## Formal release stays on HOLD

The packaged direct and recomputed post-Word submission audit require a
separate, authenticated, exact-candidate table-scope native receipt before
this contract can formally pass. A supported table-specific native acceptance
protocol is not available in this version, so any nonempty continuation rule
gets `continued_table_native_scope_pending` at formal submission, even if the
generic Word render and global visual review pass. This does not prohibit
review-draft generation, mark unrelated formatting wrong, or invalidate a
generic scoped WPS receipt for its existing checks. It cannot be cleared by
adding a boolean or copying the diagnostic helper's JSON; keep the table
observation in the separate review materials until the supported receipt
path exists.
