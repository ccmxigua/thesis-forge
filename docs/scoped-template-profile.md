# Explicit template scopes (review candidates)

Use this optional path when a source template reuses styles across unrelated
content, or when only part of a paragraph is governed by a rule. The ordinary
formatter remains the default. This path does **not** select a school, infer an
author's intent, accept revisions, update printed pages, or establish compliance.

```sh
python scripts/apply_format_spec.py input.docx scopes.json candidate.docx \
  --out-dir audit --scoped-profile
```

Here the second positional file is a
`schema/scoped-template-profile.schema.json` profile, rather than a role-wide
format spec. Other legacy/semantic/release options cannot be mixed into this
mode. Invalid input, overlap, unrepresentable values or runtime errors fail
without falling back to the broad formatter. Guarded, unsupported individual
rules are preserved and listed as `unhandled_preserved`; a published draft may
therefore contain both applied and unresolved rules. Inspect the receipt, not
just process exit zero. `submission_ready` and native verification stay false.

## Authority and identity

The profile explicitly declares `authority: "caller_declared"`. Each rule names
its applicable role, property values and evidence references. The caller must
review that evidence and its conditions; a SHA proves bytes, not semantics.
The adapter does not silently promote declarations to a verified school rule.
School-specific numbers, fonts and prefixes belong in the profile, never code.

Bind `input_sha256` to the exact input DOCX. Select a unique top-level paragraph
by the SHA-256 of concatenated `w:t` UTF-8 text, optionally its source style and
two unique region boundary text hashes. A selector is a declaration of role,
not an automatic semantic classifier. Ambiguous/missing selectors are retained.
Paragraph numbers, file names, school names and page numbers are not selectors.
Up to four exact text hashes can express explicitly reviewed before/after
decorative-prefix or terminal forms. To apply to an edited candidate, explicitly
rebind its new input hash; stale source identity is rejected.

Optional `sources` carry profile-relative or absolute evidence file paths and
SHA-256. All are checked and protected before any rule is skipped. No content
is fetched. DOCX, profile and evidence files may not alias output/report,
including existing hardlinks and symlinks. Duplicate OPC names are rejected.

## Supported narrow actions

* `font` and `paragraph` reuse the existing format-spec property definitions.
  Formatting stays direct and local: source styles, basedOn chains and all
  unrequested properties remain. Latin writes only `ascii`/`hAnsi` and their
  competing themes, CJK only `eastAsia`/its theme; the CS slot is not replaced.
  Forced/uncertain script, hidden, revised or compatibility branches are retained.
* Explicit subtitle roles can use a decorative `display_prefix`. No title,
  translation or scientific content is generated. Repeated prefixes are flagged.
* `keyword` formats a literal leading label separately from its value, splitting
  only plain text runs. Fields/objects are retained; comment/empty runs are not
  duplicated. Without a value-font property, intentional value emphasis stays.
* `header` selects an existing side of the source heading's section, binds the
  old placeholder text hash, and optionally synchronizes plain heading text.
  Effective inherited/shared OPC parts, fields and protected scopes are retained.
  Side selection and all applicable school conditions must be declared by the
  caller. No global replacement or new sections/headers are created.
* `table` requires the explicit `three_line` contract and one adjacent declared
  caption (a narrow standalone unit note may intervene). It changes only border
  declarations, preserving pagination. Caption font/paragraph properties can be
  applied in the same atomic rule. Forms, controls, merged/nested grids,
  conditional cell edges and row-exception borders are unresolved. A signature
  algorithm word or thumbnail alone does not make a genuine data table a form.
  Widths use eighth-points; optional color is explicit.
* `terminal` is a one-character bibliography-entry terminal replacement from an
  explicit accepted set. URL/DOI references and protected text ranges remain
  manual. It does not complete or validate citations or claim full GB/T coverage.
* `spacer` only inserts missing empty paragraphs following a declared heading.
  It never deletes extra blanks. Blank-looking fields, ranges, page breaks and
  objects are not disposable. Absolute line spacing is explicit, not guessed.

Relative character indentation resolves actual run/character/paragraph/default
size, or uses the current explicitly authorized size. Mixed/unknown size remains
unresolved unless an explicit line-height suffices for line-relative spacing.
Half-point font sizes and twip dimensions must be exactly representable. Direct
paragraph-mark font sizes are not treated as the text font. Competing character
indent/spacing attributes are removed only for the authorized property; logical
directional indents are retained when interpretation is unresolved.

`repair_toc_navigation` uses the existing guarded full-field parser, transaction
and serialized audit. Declare source heading roles explicitly in the rules.
It does not move/delete original bookmarks or recompute cached pages. A target
touched by an unproved partial/cross-paragraph range is rejected in the default
strict policy. Body
cross-references are not repaired by guessing. Navigation evidence is bound to
the final candidate SHA; no static result is native click/save evidence.

`figure_checks` only reports potential XML-order issues for a literal reference
label and a bound caption/adjacent object. Literal text is not proof of narrative
meaning or first visual mention. No figure movement, scaling or prose insertion
occurs. Floating layout still requires a renderer and an author decision.

## Optional boundary and additive-target proofs

The original range guards remain the default. A rule with exactly one subtitle
prefix or bibliography terminal action can additionally declare
`"boundary_edit_policy": "navigation_only"`. This is a narrower proof path,
not permission to ignore a range. It reads the complete supplied package's
complex fields (including split instructions), simple fields and hyperlink
anchors. Missing, dynamic, cross-story, ambiguous or unsupported consumers
remain blocked. `REF`/`NOTEREF` text readers, `TOC \\b` selections and external
reference contexts do not establish this proof. Comment, permission and move
ranges cannot authorize a text change. A point comment reference can remain
untouched while an unrelated boundary is edited.

Only the first `w:t` gets the explicit decorative prefix, or the final full-stop
variant gets its configured replacement. Existing runs, attributes, markers,
comment references, reference data, URL/DOI tokens and all other text remain.
No font change or generated citation supplies authority to change content.
The ledger records exact before/after text and source/final locators. Its inverse
is checked against an independent source-based control with the same authorized
formatting and no boundary text edits, across **all Word XML parts**, before
publication and again on the serialized candidate. Every OPC member payload
must also equal the verified staged payload. A local `inverse_verified` entry
and the full `boundary_inverse` report are distinct scopes.

Unrelated footer MC alternatives may contain complete literal `PAGE` fields.
When delimiters and instruction are independently paired inside each logical
story/branch and no dynamic argument exists, every possible branch reads no
bookmark text. The inventory records that conservative consumer union, while
`display_selection_unresolved` remains true. It does not choose, flatten,
refresh or change the branch. A REF/IF/unknown/revised or cross-branch field
does not qualify.

With navigation repair enabled, the separate explicit
`"navigation_target_policy": "bound_heading_additive"` can bind a unique source
heading even when unrelated original cross-paragraph endpoints touch it. It
requires one consistent TOC HYPERLINK/PAGEREF pair, a complete consumer graph,
no additional/non-TOC reader of that pair's old target, and an unambiguous whole
heading. Point or partial local bookmarks cannot supply ownership. All original
ranges and their other consumers remain; only the selected TOC pair can be
retargeted to an existing complete heading range or a new complete managed pair.
No author bookmark is adopted just because its name starts with `TF_TOC`.

Permissions/revisions and wide comment ranges remain blocked. A complete inline
comment may stay enclosed by the new whole-heading range only when **both**
actual insertion positions are outside that comment; its text and all control
markers remain unchanged and are independently audited. The serialized audit
binds final bytes, without recomputing cached pages or asserting native clicks.
`toc_navigation.restore_additive_navigation` is an exact in-memory inverse for
the still-identical receipt-bound candidate; it refuses subsequent user edits.
Retargeting navigation is not reconstruction of an author's old range intent.

## Publication and evidence

All supported rules are staged before any live XML write. Conflicting actual
effect targets fail the batch, even if selected through different captions.
Guard rejection preserves the entire rule, including compound caption/table
actions. The output is non-in-place. Original math/OLE/drawing/field and range
identity/offset checks precede publication; navigation has its own final audit.
Unmodified OPC member payloads remain exact. With no changes the entire DOCX
is copied byte-for-byte, including ZIP metadata. Updated XML can be serialized
differently and is not promised byte-identical.

DOCX plus report are staged and replaced with exception rollback of existing
destinations. This is not a concurrent-reader, crash-consistent filesystem
transaction. Ordinary runtime/serialization failures preserve previous outputs;
if filesystem recovery itself fails, `ArtifactRollbackError` identifies the
retained original backups and requires explicit recovery, never success. The
report records final output SHA, applied /
already-satisfied / unhandled rules, source authority and pending evidence.

The eight native evidence categories inventory static fields, section properties,
table flags, font declarations and opaque-object counts. They do not prove TOC
pages, whitespace, printed footer position, rule appearance, repeated headers,
figure grouping, opaque equation display/editability, or actual font glyphs.
Use a supported separately authorized native/visual workflow for those checks;
do not bypass the UNO complex-object guard. Content length, real references,
identity, achievements, source authority and physical binding are not invented.

Generate the small synthetic two-profile example (no private paper):

```sh
python examples/scoped_template_profile_demo.py /absolute/new-demo-directory
```

Run the two printed commands and compare receipts. These profiles are fictional
regression configurations, not university requirements or native acceptance.
