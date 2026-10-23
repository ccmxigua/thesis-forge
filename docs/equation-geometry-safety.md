# Display equation geometry safety

`apply_format_spec.py` centers standalone display math in its current text
measure. Numeric labels have a separate right stop. Inline math and existing
structured math, including multiline relation alignment, are preserved. Flat
additive expressions have the narrowly defined continuation fallback below.
A native unnumbered `centerGroup` uses paragraph centering; adding a center tab
to that native layout would center it twice.

Tabs are relative to the container text origin. With declared paragraph
indents, the right stop is `container_width - right_indent` and the math center
is `(left_indent + right_stop) / 2`. Equal-width section columns use the section
column count and spacing. Fixed table cells use grid tracks, spans, and
inherited cell margins rather than a potentially stale `tcW`.

For nonempty math, acceptance additionally requires local LibreOffice and
PyMuPDF. `equation_geometry.py` renders a private, source-bound probe containing
wide math and label measurements and the proposed paragraph at its actual text
measure and page height. It checks complete extracted math glyphs, width,
height, center, label clearance, right alignment, and the rendered label line.
Context bounds use actual painted glyph/rule ink, with label paint separated
by a private color, rather than an exporter's logical font boxes. Different
exporters can give identical stretched outlines different text boxes; the
existing two-point tolerance is unchanged. A math rule crossing a number is
included in the math bounds and still fails the collision check.
Right tabs are checked against the label's text advance, retaining full-width
parenthesis bearings; clipping and collisions use painted ink. The isolated
paragraph starts at an artificial page top, so vertical checks use complete
ink height versus the source height budget and physical PDF edges, rather
than treating an ascender in the probe margin as source-document clipping.
Absolute source-page vertical placement still requires full-page review.
A label that automatically wraps is detected even without an explicit newline.
When equation line spacing is unspecified, automatic line height lets native
math limits and tall objects remain visible; inherited fixed body line heights
are not used. Explicit equation spacing is retained and checked for clipping.
The evidence records probe DOCX/PDF hashes and is written under
`<out-dir>/equation-geometry/<validation-key>/`.

This probe is a bounded geometry check. It is not whole-document visual
acceptance, native Word/WPS testing, or a submission-readiness attestation.
Review full rendered pages separately, including surrounding content and page
breaks. Word/WPS open/save/reopen remains a separate check.
An exporter that corrupts a source relation (for example a leading aligned
`=` painted as an inverted question mark in some LibreOffice exports) remains
blocked by glyph completeness. The source paragraph is restored exactly;
there is no character replacement or renderer-version exemption.

If the formula or label will not fit, the renderer is unavailable, glyph
extraction is incomplete, or the container is ambiguous, the tool returns a
nonzero result with `overall_status=manual_review_required` and
`failure_type=equation_layout_unsafe_or_unverifiable`. The report identifies the
failed equation and evidence. It does not replace the requested output; an
existing output is preserved. At the API level, an unsafe paragraph is restored
in place. No terms are removed, no font sizes are reduced to make an equation
fit. Structured expressions are not algebraically rewritten. Choose a suitable
layout manually when the supported continuation fallback cannot fit.

Conservative manual cases include unequal columns, automatic outer tables,
conflicting table preferred widths, conditional table font/margin rules,
positioned/RTL tables, shared exact-height rows, and nonzero explicit first-line
or character/directional indents. An empty OMML fixture has no measurable ink
and is marked `empty_placeholder`, never as rendered geometry verified.

Formatting of newly inserted tab runs is applied during their first pass.
Indent attribute order and consumed padding are stable on repeated runs. The
CLI checks the final staged package after document font processing and compares
all body/inline math before atomically publishing it: exact subtree hashes for
unchanged math and exact source-to-output replay for a permitted layout change.

## Native delimiter and PDF export compatibility

For a delimiter enclosing a multirow matrix/array, the formatter materializes
an absent `m:dPr/m:grow` as `m:grow m:val="1"`. This makes the standard delimiter
default explicit. It does not change an existing `grow=0`, shape, mathematical
font, matrix cell, array row, symbol, or alignment. The original complete XML
and exact replay are protected like a continuation proof; an inverse also
reconstructs the original XML exactly. A failed geometry probe rolls the edit
back. Single-row and inline math are outside this normalization.

The normative default for delimiters is true; the different n-ary default is
false ([Open XML GrowOperators remarks](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.math.growoperators?view=openxml-3.0.1)).
Consequently, an exporter that draws a one-line bracket around an otherwise
complete matrix has not been proved fixed by a configuration change or by a
LibreOffice pass. The source's `m:shp` is also preserved; its `centered` default
is not silently replaced by `match` ([Open XML Shape remarks](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.math.shape?view=openxml-3.0.1)).

Audit the actual exported PDF separately:

```sh
python scripts/audit_equation_export.py candidate.docx actual-native.pdf \
  --regions reviewed-regions.json --out native-export-audit.json
```

The region file binds to `source_docx_sha256` and `pdf_sha256`; `regions` is a
list of `{ "math_index": 0, "delimiter_index": 0, "page": 1,
"bbox": [80, 70, 140, 150] }`. Indices are zero-based in source document order;
page numbers are one-based and coordinates are PDF points. Measure a tight
rectangle containing the delimiter and its complete operand, excluding any
prefix, equation number, and neighboring text. Retain the reviewed region file
as evidence. Changing either input invalidates its binding.

The checker measures actual text/vector delimiter extents against operand ink,
checks ordered flat matrix/array row text, and rejects used PDF glyphs whose
Unicode mapping resolves to U+FFFD. Missing regions, absent/ambiguous ink,
nested/structured row operands, changed row text, or short brackets require
review and return a nonzero exit. It never repairs PDF text by replacement.
Observed fonts and the declared math font are recorded separately; a declaration
does not prove the font was used. PDF checks cover only the recorded scope;
they do not attest to editor glyph appearance, editing/save/reopen, full-page
layout, or equal page counts across renderers. Run those native checks again
after any document or export-engine change.

## Flat additive continuations

When a display cannot fit horizontally or leave space for its number, the
formatter may wrap a flat identifier/decimal addition chain at existing binary
`+` characters. An optional single identifier/decimal followed by `=` is preserved. Every
continuation row starts with the original `+`; no operator or term is repeated,
inserted, reordered, or removed, and source run font properties are cloned.
The output uses an OMML equation array as a layout container. This is a layout
change to the XML, not a claim that the entire source XML stayed identical.

This fallback rejects fractions, matrices, delimiters, nested expressions,
multiple relations, subtraction/other operators, literal prose, tracked run
formatting, and authored alignment or manual breaks. Their breaks require an
author's choice. `equations.wrap_simple_sums=false` disables the fallback.
No `same_line` requirement is relaxed and table widths are not changed.

Proposals use measured available width and a bounded number of partitions.
Each proposed array must pass all existing rendered width, height, glyph,
centering and number checks. A proof records the original complete math XML,
source/target hashes and original operator offsets. The staged output must
exactly equal a replay of that one allowed transformation; ignoring math
properties or comparing unordered text cannot pass this gate. All other math,
including inline objects, retains the original exact-hash requirement.

A number wider than its own cell needs a format decision (for example a wider
column or a separately permitted number position). A single equation array
taller than the page is not automatically split into new mathematical objects.
The author must specify permissible row boundaries, cross-page alignment and
numbering. Both remain unresolved rather than being called completed layout
repairs.

Run `make check` and `make pytest`. The CI installs LibreOffice Writer/Math,
fonts-liberation and poppler-utils so real equation and TOC rendering tests run.
Without LibreOffice, rendered test classes are skipped where declared, and
production nonempty equation formatting fails closed. Tests cover physical
centers with indents and columns, number collision/wrapping, overwide and
overheight math, exact row height, renderer absence, unchanged outputs, protected
math/bookmarks/inline content, and five byte-identical CLI passes.
