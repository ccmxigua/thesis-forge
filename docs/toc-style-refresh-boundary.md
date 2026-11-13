# TOC style persistence and native refresh

`apply_format_spec.py` consumes level declarations only from an explicitly supplied
`--official-template`, via the source-bound comment contract. A thesis input or
cached field run formatting does not independently grant style-write authority.
The contract records source hash, comment identities, raw declaration hash and
unexecuted layout clauses. Unsupported or ambiguous declarations are preserved.

Only complete dynamic TOC field intervals authorize a cache target. Locked,
malformed, mixed cache/prose paragraphs, ambiguous level styles and styles shared
with non-TOC content are preserved. Existing dependent styles retain their old
inheritance through the contract's private snapshot mechanism.

`toc-style-application.json` records application and a serialized style-definition
audit before the output is committed. This is configuration evidence only:
`native_verified` remains false. It does not refresh fields, validate page numbers,
close manual requirements, prove installed fonts or certify visual pagination.
A separate native refresh and visual inspection remain mandatory. Do not replace
cached TOC results with inferred page numbers.

If the written specification already contains nonempty TOC role or requirement
properties, template-level extension is conservatively skipped, including when
one property matches. Existing written-rule execution is preserved; the receipt
records `toc_style_written_authority_present`. This does not assert that the
written rule itself survives native refresh; that remains to be independently
verified. No implicit property-priority merge is performed.

Explicit script faces replace competing theme attributes in the same selected
TOC style slots. For example, writing `eastAsia="SimSun"` while retaining
`eastAsiaTheme` does not establish an effective explicit CJK face: the theme
can take precedence. The shared script-slot helper removes only the theme tokens
for faces authorized by the bound contract; unspecified Latin/CJK slots remain
unchanged. Synthetic CLI tests exercise both replacement and preservation.

A separate conservative compatibility projection now compacts redundant direct
CJK heading faces only when a bound TOC/PAGEREF entry resolves to one uniquely
owned source heading and a unique, paired, ordered bookmark within that heading.
The cached title must agree with the source, and every effective heading run face
must already equal its resolved paragraph-style face. Only redundant `eastAsia`
attributes are removed; the heading's effective CJK face remains unchanged.
Mixed/intentional overrides, character-style CJK declarations, themes, ambiguous
ownership, protected or unresolved fields/styles/revisions are preserved.

`toc-heading-fonts.json` records each removed attribute, source identities and
before/after XML hashes, skipped-source reasons and a serialized effective-face
audit before commit. No TOC cached text, run formatting or page number is repaired
or invented. This is not a native TOC rebuild: `native_font_retention_verified`
remains false. A native application may still copy other heading properties.
A passing configuration audit must not be reported as native font retention or
full-document acceptance; fresh native refresh/visual evidence remains required.

The engineering CLI now invokes this contract and compatibility projection.
Source-role ownership is captured immediately after loading the input, before
header, cover and style mutations, solely for TOC use; existing body-role routing
is not replaced. The four receipts are `toc-source-role-bindings.json`,
`toc-style-application.json`, `toc-heading-fonts.json` and
`toc-refresh-request.json`. Only TOC-scoped dirty flags are requested, without
enabling document-wide automatic field updates. This CLI integration has
synthetic regression evidence, not native WPS acceptance.

Both TOC audits are rerun on the final review-draft serialization after manual
markers and the scorecard are added. Successful receipts bind that final DOCX
SHA-256. A failing final audit aborts the second commit. The first-stage DOCX
and its prior receipts may remain for diagnostics: an unsuccessful run is HOLD,
not a final deliverable, even if that intermediate receipt reports valid.
